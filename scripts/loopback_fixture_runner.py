"""One real Playwright loopback runner, supervised in its own process.

No user browser/profile/auth state, external URLs, installs or sandbox bypass.
--inspect does not launch anything. Native execution is intentionally separate.
"""

from __future__ import annotations

import argparse
import ctypes
import faulthandler
import json
import os
import resource
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kusamushiri.readonly_startup import LoopbackReadOnlyPlan, ReadOnlyStartupAdapter  # noqa: E402

WALL_SECONDS = 30
CASES = ("baseline", "post", "unknown", "redirect", "wrong-target", "frame", "popup", "worker", "websocket")
LAUNCH = {"headless": True, "chromium_sandbox": True, "timeout": 5000,
          "args": ["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"]}


def loopback_audit(event, args):
    if event in {"socket.connect", "socket.bind"}:
        address = args[1]
        if not isinstance(address, tuple) or address[0] != "127.0.0.1":
            raise RuntimeError("External Python socket destination refused")


def make_plan(origin: str, case: str) -> LoopbackReadOnlyPlan:
    members = {(origin + "/target/123", "document"), (origin + "/bootstrap.js", "script")}
    if case == "baseline":
        members.add((origin + "/read?target=123", "fetch"))
    if case == "redirect":
        members.add((origin + "/redirect", "fetch"))
    return LoopbackReadOnlyPlan(origin + "/target/123", frozenset(members), True, "loopback-fixture", 0.75, 20)


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        cast(FixtureServer, self.server).hits.append(("GET", self.path))
        if self.path == "/target/123":
            body = b'<!doctype html><link rel="icon" href="data:,"><main id="fixture" data-read-ok="">synthetic</main><script src="/bootstrap.js"></script>'
            mime = "text/html"
        elif self.path == "/bootstrap.js":
            scripts = {
                "baseline": "fetch('/read?target=123').then(r=>r.json()).then(v=>document.querySelector('#fixture').dataset.readOk=v.target)",
                "post": "fetch('/write',{method:'POST',body:'synthetic fixture only'}).catch(()=>{})",
                "unknown": "fetch('/unexpected').catch(()=>{})",
                "redirect": "fetch('/redirect').catch(()=>{})",
                "wrong-target": "fetch('/read?target=456').catch(()=>{})",
                "frame": "const f=document.createElement('iframe');f.src='/unexpected';document.body.append(f)",
                "popup": "window.open('/unexpected','fixture')",
                "worker": "try {new Worker('/worker.js')} catch {document.querySelector('#fixture').dataset.readOk='worker-blocked'}",
                "websocket": "new WebSocket(location.origin.replace('http:','ws:')+'/socket')",
            }
            body = scripts[cast(FixtureServer, self.server).case].encode()
            mime = "application/javascript"
        elif self.path == "/read?target=123":
            body, mime = b'{"target":"123","ok":true}', "application/json"
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/redirect-target")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        cast(FixtureServer, self.server).hits.append(("POST", self.path))
        self.send_response(405)  # Every fixture write is independently refused.
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    case = "baseline"

    def __init__(self):
        self.hits: list[tuple[str, str]] = []
        super().__init__(("127.0.0.1", 0), FixtureHandler)


def save_progress(output: Path, record: dict) -> None:
    # Atomic checkpoint of controlled local-fixture data, no auth/exception text.
    try:
        pending = output / "worker-progress.pending"
        pending.write_text(json.dumps(record, indent=2) + "\n")
        pending.replace(output / "worker-progress.json")
    except Exception:
        record["progress_write_failed"] = True  # Must never bypass owned cleanup.


def worker(output: Path) -> int:
    from playwright.sync_api import sync_playwright
    sys.addaudithook(loopback_audit)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # Only this unauthenticated child.
    record = {"scope": "unauthenticated-loopback-only", "native_started": False, "server_started": False,
              "cleanup_complete": False, "phase": "driver_start", "cases": [], "checks": []}
    stack_file = (output / "worker-stack.txt").open("w")
    faulthandler.dump_traceback_later(5, repeat=True, file=stack_file)
    save_progress(output, record)
    browser = server = thread = None
    browser_closed = server_closed = False
    try:
        with sync_playwright() as pw:
            try:
                record["phase"] = "sandboxed_browser_launch"
                save_progress(output, record)
                browser = pw.chromium.launch(**LAUNCH)
                record["native_started"] = True
                server = FixtureServer()
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                record["server_started"] = True
                origin = f"http://127.0.0.1:{server.server_port}"
                for case in CASES:
                    record["phase"] = "fixture_" + case
                    save_progress(output, record)
                    server.case = case
                    server.hits.clear()
                    adapter = ReadOnlyStartupAdapter(make_plan(origin, case))
                    facts = {"read_body_verified": False, "worker_constructor_blocked": False}
                    def probe(page, facts=facts):
                        value = page.locator("#fixture").get_attribute("data-read-ok", timeout=100)
                        facts["read_body_verified"] |= value == "123"
                        facts["worker_constructor_blocked"] |= value == "worker-blocked"
                    def progress(stage, case=case):
                        record["phase"] = "fixture_" + case + ":" + stage
                        save_progress(output, record)
                    result = adapter.run(browser, probe=probe, progress=progress)
                    hits = list(server.hits)
                    passed = result.cleanup_complete and not browser.contexts and all(method == "GET" for method, path in hits)
                    if case == "baseline":
                        passed &= result.stop_reason == "time_limit" and facts["read_body_verified"] and ("GET", "/read?target=123") in hits
                    elif case == "post":
                        passed &= result.stop_reason == "mutation_or_non_get_blocked" and not any(path == "/write" for method, path in hits)
                    elif case == "redirect":
                        passed &= result.stop_reason == "http_or_redirect_or_time_block" and ("GET", "/redirect") in hits and not any(path == "/redirect-target" for method, path in hits)
                    elif case == "worker":
                        passed &= facts["worker_constructor_blocked"] and not any(path == "/worker.js" for method, path in hits)
                    elif case == "websocket":
                        passed &= result.stop_reason == "unsupported_transport" and not any(path == "/socket" for method, path in hits)
                    else:
                        passed &= result.stop_reason in {"unknown_or_unowned_request", "unexpected_page"} and not any(path in {"/unexpected", "/read?target=456"} for method, path in hits)
                    record["cases"].append({"case": case, "passed": bool(passed), "adapter": asdict(result), "local_server_hits": hits, "facts": facts})
                    save_progress(output, record)
                    if not passed:
                        raise RuntimeError("local fixture verification failed")
                record["checks"] = ["real_URL_and_GET_body_binding", "prepage_routes", "independent_POST_denial", "unknown_denial",
                                    "zero_redirect_and_retry", "actual_response_body_relay", "owned_context_cleanup", "frame_popup_worker_websocket"]
                record["phase"] = "verified"
            finally:
                if browser is not None:
                    record["phase"] = "browser_close"
                    save_progress(output, record)
                    browser.close()  # Entire process owns this browser; no borrowed UI.
                    browser_closed = True
                if server is not None:
                    record["phase"] = "server_close"
                    save_progress(output, record)
                    server.shutdown()
                    server.server_close()
                    if thread is not None:
                        thread.join(timeout=2)
                    server_closed = thread is None or not thread.is_alive()
    except Exception as error:
        # All data is synthetic, but still avoid dumping exception/request/auth text.
        record["failure_type"] = type(error).__name__
        text = str(error)
        record["sandbox_failure_marker"] = "SIGTRAP" in text or "sandbox_host_linux" in text or "Operation not permitted" in text
    finally:
        # No browser handle after launch failure is not absence/closure evidence.
        record["cleanup_complete"] = browser_closed and (server is None or server_closed)
        record["browser_closed_proven"] = browser_closed
        record["server_closed_proven"] = server is None or server_closed
        record["passed"] = bool(record["checks"]) and record["cleanup_complete"]
        save_progress(output, record)
        faulthandler.cancel_dump_traceback_later()
        stack_file.close()
        (output / "worker-result.json").write_text(json.dumps(record, indent=2) + "\n")
    return 0 if record["passed"] else 1


def proc_identity(pid: int):
    try:
        # Read only PPID/start-time, never commands, auth environment or user files.
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rfind(")") + 2:].split()
        return int(fields[1]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


# Stable Linux process handles via libc. Staging Python lacks os/signal pidfd
# wrappers; if libc/kernel denies them, FAIL CLOSED before launching the worker.
_LIBC = ctypes.CDLL(None, use_errno=True)
_OPEN = getattr(_LIBC, "pidfd_open", None)
_SEND = getattr(_LIBC, "pidfd_send_signal", None)
if _OPEN is not None:
    _OPEN.argtypes, _OPEN.restype = [ctypes.c_int, ctypes.c_uint], ctypes.c_int
if _SEND is not None:
    _SEND.argtypes, _SEND.restype = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint], ctypes.c_int


def pidfd_open(pid: int) -> int | None:
    if _OPEN is None or _SEND is None:
        return None
    fd = _OPEN(pid, 0)
    return fd if fd >= 0 else None


def pidfd_signal(fd: int, signum: int) -> bool:
    return _SEND is not None and _SEND(fd, signum, None, 0) == 0


def pidfd_exited(fd: int) -> bool | None:
    try:
        return bool(select.select([fd], [], [], 0)[0])
    except (OSError, ValueError):
        return None


def stable_handles_available() -> bool:
    fd = pidfd_open(os.getpid())
    if fd is None:
        return False
    try:
        return pidfd_signal(fd, 0) and pidfd_exited(fd) is False
    finally:
        os.close(fd)


@dataclass(frozen=True)
class OwnedProcess:
    birth: int
    parent: int
    pidfd: int


def capture_owned(pid: int, *, expected_birth: int, parent: tuple[int, OwnedProcess] | None = None) -> OwnedProcess | None:
    before = proc_identity(pid)
    if before is None or before[1] != expected_birth:
        return None
    if parent is None:
        if before[0] != os.getpid():
            return None  # Only our newly created direct child can become a root.
    else:
        parent_pid, owner = parent
        parent_now = proc_identity(parent_pid)
        if (before[0] != parent_pid or parent_now is None or parent_now[1] != owner.birth
                or pidfd_exited(owner.pidfd) is not False):
            return None
    fd = pidfd_open(pid)
    if fd is None:
        return None
    # Verify around open: the stable handle must refer to the recorded generation.
    if proc_identity(pid) != before or (parent is not None and pidfd_exited(parent[1].pidfd) is not False):
        os.close(fd)
        return None
    return OwnedProcess(before[1], before[0], fd)


def discover_owned(root_pid: int, owned: dict[int, OwnedProcess]) -> None:
    if type(owned.get(root_pid)) is not OwnedProcess:
        return
    rows = {}
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            identity = proc_identity(int(path.name))
            if identity is not None:
                rows[int(path.name)] = identity
    changed = True
    while changed:
        changed = False
        for pid, (parent, born) in rows.items():
            if pid in owned or type(owned.get(parent)) is not OwnedProcess:
                continue
            owner = capture_owned(pid, expected_birth=born, parent=(parent, owned[parent]))
            if owner is not None:
                owned[pid] = owner  # Capture once, never replace an existing PID entry.
                changed = True


def owned_exited(owned: dict[int, OwnedProcess]) -> bool:
    return bool(owned) and all(type(owner) is OwnedProcess and pidfd_exited(owner.pidfd) is True for owner in owned.values())


def close_owned_handles(owned: dict[int, OwnedProcess]) -> None:
    for owner in owned.values():
        if type(owner) is OwnedProcess:
            with suppress(OSError):
                os.close(owner.pidfd)


def terminate_owned(process, owned: dict[int, OwnedProcess]) -> bool:
    # Never adopt/refresh a numeric root PID after poll()/wait() reaps it.
    if type(owned.get(process.pid)) is not OwnedProcess or any(type(item) is not OwnedProcess for item in owned.values()):
        return False
    discover_owned(process.pid, owned)
    for signum in (signal.SIGTERM, signal.SIGKILL):
        for pid, owner in list(owned.items()):
            ended = pidfd_exited(owner.pidfd)
            if ended is True:
                continue  # Original kernel process closed, even if its PID was reused.
            current = proc_identity(pid)
            if ended is not False or current is None or current[1] != owner.birth:
                continue  # Conflict/unestablished identity: no signal and no fallback.
            pidfd_signal(owner.pidfd, signum)  # Stable handle, never os.kill numeric PID.
        time.sleep(0.1)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return False
    return owned_exited(owned)


def supervise(output: Path) -> int:
    output.mkdir(parents=True, exist_ok=False)
    # Do not pass unrelated auth tokens/proxy credentials to the fixture child.
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ", "HOME") if key in os.environ}
    env.update(PYTHONDONTWRITEBYTECODE="1", PLAYWRIGHT_BROWSERS_PATH="/home/kw4/.cache/ms-playwright")
    env["NO_PROXY"] = "127.0.0.1"
    record = {"scope": "unauthenticated-loopback-only", "hard_wall_seconds": WALL_SECONDS, "native_verified": False,
              "cleanup_complete": False, "security_bypass": False, "auth_state_used": False}
    owned: dict[int, OwnedProcess] = {}
    if not stable_handles_available():
        record.update(reason="stable_process_handles_unavailable_no_worker", cleanup_complete=True, timed_out=False)
        (output / "result.json").write_text(json.dumps(record, indent=2) + "\n")
        return 1
    scratch = Path(tempfile.mkdtemp(prefix="owned-local-", dir=output))
    for kind in ("CONFIG", "DATA", "STATE", "CACHE"):
        env[f"XDG_{kind}_HOME"] = str(scratch / kind.lower())
    env["TMPDIR"] = str(scratch)
    env["PYTHONPATH"] = str(ROOT / "src")
    with (output / "worker.stdout").open("w") as stdout, (output / "worker.stderr").open("w") as stderr:
        try:
            process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--worker", "--output", str(output)],
                                       cwd=scratch, env=env, stdout=stdout, stderr=stderr, stdin=subprocess.PIPE, text=True, start_new_session=True)
        except Exception as error:
            record.update(reason="worker_start_unestablished", failure_type=type(error).__name__, preserved_owned_scratch=str(scratch))
            (output / "result.json").write_text(json.dumps(record, indent=2) + "\n")
            return 1
        root_identity = proc_identity(process.pid)
        if root_identity is not None:
            owner = capture_owned(process.pid, expected_birth=root_identity[1])
            if owner is not None:
                owned[process.pid] = owner
        # The real worker starts no driver/browser until root ownership is pinned.
        try:
            if process.stdin is not None:
                if process.pid in owned:
                    process.stdin.write("start\n")
                    process.stdin.flush()
                process.stdin.close()
        except (BrokenPipeError, OSError):
            record["worker_start_grant_failed"] = True
        deadline = time.monotonic() + WALL_SECONDS
        while process.poll() is None and time.monotonic() < deadline:
            discover_owned(process.pid, owned)
            time.sleep(0.05)
        timed_out = process.poll() is None
        record["timed_out"] = timed_out
        record["observed_owned_processes"] = len(owned)
        if timed_out:
            record["observed_processes_terminated"] = terminate_owned(process, owned)
            record["reason"] = "hard_timeout_cleanup_unestablished"
        else:
            record["exit_code"] = process.returncode
        # ALWAYS close verified owned descendants, including early crash and
        # missing/malformed result. File parsing can never gate resource cleanup.
        remaining = not owned_exited(owned)
        if remaining:
            record["observed_processes_terminated"] = terminate_owned(process, owned)
        tracked_closed = owned_exited(owned)
        record["tracked_owned_processes_closed"] = tracked_closed
        result = output / "worker-result.json"
        data = None
        if result.is_file():
            try:
                parsed = json.loads(result.read_text())
                if (isinstance(parsed, dict) and parsed.get("scope") == "unauthenticated-loopback-only"
                        and type(parsed.get("cleanup_complete")) is bool and type(parsed.get("passed")) is bool
                        and type(parsed.get("native_started")) is bool and isinstance(parsed.get("cases"), list)
                        and isinstance(parsed.get("checks"), list)
                        and all(isinstance(item, dict) and isinstance(item.get("case"), str)
                                and item["case"] in CASES and type(item.get("passed")) is bool for item in parsed["cases"])):
                    data = parsed
                else:
                    record["worker_result_status"] = "invalid_shape"
            except (OSError, ValueError):
                record["worker_result_status"] = "unreadable_or_invalid_json"
        else:
            record["worker_result_status"] = "missing"
        if data is not None:
            record["worker_result"] = data
            record["cleanup_complete"] = not timed_out and tracked_closed and data.get("cleanup_complete") is True
            cases_valid = (len(data["cases"]) == len(CASES)
                           and all(isinstance(item, dict) and isinstance(item.get("case"), str)
                                   and item["case"] in CASES and item.get("passed") is True for item in data["cases"]))
            cases_valid = cases_valid and {item["case"] for item in data["cases"]} == set(CASES)
            record["native_verified"] = (record["cleanup_complete"] and process.returncode == 0 and data.get("passed") is True
                                         and data.get("native_started") is True and cases_valid)
        close_owned_handles(owned)

    if record["cleanup_complete"]:
        shutil.rmtree(scratch)  # Only our proven-closed temporary child resources.
    else:
        record["preserved_owned_scratch"] = str(scratch)
    (output / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"output": str(output), "native_verified": record["native_verified"], "cleanup_complete": record["cleanup_complete"],
                      "timed_out": record["timed_out"]}, indent=2))
    return 0 if record["native_verified"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inspect", action="store_true", help="print fixed policy; launch no process/server")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inspect:
        print(json.dumps({"launch": LAUNCH, "cases": CASES, "wall_seconds": WALL_SECONDS,
                          "destinations": "exact 127.0.0.1 ephemeral origin only", "auth_state": False,
                          "sandbox_bypass": False, "stable_process_handles": stable_handles_available(),
                          "runner": "actual installed Playwright; no simulation backend"}, indent=2))
        return 0
    if args.output is None or not args.output.is_absolute() or not args.output.resolve().is_relative_to(Path("/tmp")):
        parser.error("--output requires a fresh absolute result directory under /tmp")
    if sys.platform != "linux":
        parser.error("this owned-process supervisor requires Linux /proc")
    if args.worker:
        # Supervisor pins the direct child pidfd before allowing native startup.
        if sys.stdin.readline(16) != "start\n":
            return 1
        return worker(args.output)
    return supervise(args.output)


if __name__ == "__main__":
    raise SystemExit(main())
