"""Crash-safe action receipts. Uncertain submissions require manual verification."""

import contextlib
import json
import math
import os
import re
import socket
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import cast

from kusamushiri.i18n import tr
from kusamushiri.models import FailureKind, PostActionResult, PostActionTarget
from kusamushiri.parsing import extract_post_id


def _pid_namespace() -> str | None:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


STATES = {"inflight", "success", "retryable", "permanent", "auth", "ambiguous", "pending", "blocked"}


class ActionJournal:
    def __init__(self, path: Path, scope: str) -> None:
        self.path = path
        self.scope = scope
        self.entries: dict[str, str] = {}
        self.cooldown_until = 0.0
        self.cooldown_reason = ""
        self.completed_since_pause = 0
        self._lock_path = path.with_name(path.name + ".lock")
        self._retain_lock = False
        self._lock_owned = False
        self.stop_reason = ""
        self.viewer: str | None = None

    @staticmethod
    def key(target: PostActionTarget) -> str:
        post_id = extract_post_id(target.url)
        if not post_id.isascii() or not post_id.isdecimal() or target.kind not in {"post", "repost", "like"}:
            raise ValueError("Cannot record an action without a numeric post ID and known kind")
        return f"{target.kind}:{post_id}"

    @contextmanager
    def _recovery_guard(self) -> Iterator[None]:
        if os.name != "posix":
            yield
            return
        import fcntl

        # Keep this inode permanently. flock is released even by SIGKILL; unlinking
        # would allow two processes to lock different inodes under the same name.
        fd = os.open(self._lock_path.with_suffix(".recovering"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Journal recovery is in progress; no action was started.") from None
            yield
        finally:
            os.close(fd)

    def __enter__(self) -> "ActionJournal":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._recovery_guard():
            return self._acquire()

    def _acquire(self) -> "ActionJournal":
        try:
            fd = os.open(self._lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise RuntimeError(
                "Action journal is locked; another job may be using it. No action was started."
            ) from error
        os.close(fd)
        self._lock_owned = True
        try:
            self._write_lock("ready")
            self._load()
        except BaseException:
            self._lock_path.unlink()
            self._lock_owned = False
            raise
        return self

    def __exit__(self, typ: type[BaseException] | None, value: BaseException | None, tb: TracebackType | None) -> None:
        if not self._retain_lock:
            self._lock_path.unlink()
        self._lock_owned = False

    def _sync_directory(self) -> None:
        if os.name == "posix":
            fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def arm_isolation_storage(self) -> None:
        """Make the already-held lock durable before allowing scoped continuation."""
        try:
            fd = os.open(self._lock_path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self._sync_directory()
        except BaseException:
            self._retain_lock = True
            raise

    def persist_isolated_deadline(self, deadline: float) -> None:
        try:
            self.defer_until(deadline, tr("周辺通信の休止期限を保持"))
        except BaseException:
            # The durable pre-existing lock blocks restart even if a new deadline
            # cannot be saved. It requires manual review, never automatic unlink.
            self._retain_lock = True
            raise

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        if not isinstance(data, dict) or data.get("version") != 1 or data.get("scope") != self.scope:
            raise ValueError("Action journal version/profile mismatch; no action was started")
        entries = data.get("entries")
        deadline = data.get("cooldown_until", 0)
        if (
            not isinstance(entries, dict)
            or not all(isinstance(k, str) and re.fullmatch(r"(?:post|repost|like):[0-9]+", k) and isinstance(v, str) and v in STATES for k, v in entries.items())
            or isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
            or deadline < 0
        ):
            raise ValueError("Invalid action journal; no action was started")
        completed = data.get("completed_since_pause", 0)
        if isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
            raise ValueError("Invalid completion counter; no action was started")
        self.completed_since_pause = completed
        self.entries = entries
        reason = data.get("cooldown_reason", "")
        if not isinstance(reason, str) or len(reason) > 512:
            raise ValueError("Invalid cooldown reason; no action was started")
        self.cooldown_reason = reason
        self.cooldown_until = float(deadline)
        reason = data.get("stop_reason", "")
        viewer = data.get("viewer")
        if not isinstance(reason, str) or len(reason) > 512 or viewer is not None and (not isinstance(viewer, str) or re.fullmatch(r"[A-Za-z0-9_]{1,15}", viewer) is None):
            raise ValueError("Invalid journal stop reason/viewer; no action was started")
        self.stop_reason, self.viewer = reason, viewer

    def _write_lock(self, state: str) -> None:
        if not self._lock_owned:
            return
        # A truncated/interrupted metadata write is deliberately not recoverable.
        with self._lock_path.open("w") as stream:
            json.dump({"version": 1, "pid": os.getpid(), "host": socket.gethostname(),
                       "scope": self.scope, "recovery_state": state, "pid_namespace": _pid_namespace()}, stream)
            stream.flush()
            os.fsync(stream.fileno())

    def recover_lock(self) -> None:
        """Explicit offline recovery; never unlock a live, foreign or uncertain owner."""
        if os.name != "posix":
            raise RuntimeError("Safe owner liveness checks are only supported on POSIX; no lock was removed")
        with self._recovery_guard():
            data = json.loads(self._lock_path.read_text())
            if not isinstance(data, dict) or data.get("version") != 1 or data.get("host") != socket.gethostname() or data.get("scope") != self.scope or data.get("recovery_state") != "ready" or data.get("pid_namespace") != _pid_namespace():
                raise ValueError("Unknown/legacy lock or uncertain storage; automatic recovery is unsafe. No lock was removed.")
            pid = data.get("pid")
            if type(pid) is not int or pid <= 0:
                raise ValueError("Invalid lock owner; no lock was removed")
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError("Lock owner is still alive; no lock was removed")
            self._load()  # Validate the durable journal before making it available.
            self._lock_path.unlink()
            self._sync_directory()

    def bind_viewer(self, viewer: str) -> None:
        if self.viewer is not None and self.viewer.casefold() != viewer.casefold():
            raise ValueError("Journal account does not match the signed-in account")
        self.viewer = viewer
        self._write()

    def record_stop(self, reason: str) -> None:
        self.stop_reason = reason
        self._write()

    def status(self) -> dict[str, object]:
        self._load()
        return {"states": dict(Counter(self.entries.values())), "stop_reason": self.stop_reason,
                "not_before": self.cooldown_until, "locked": self._lock_path.exists(), "viewer": self.viewer,
                "held": {key: value for key, value in self.entries.items() if value in {"inflight", "ambiguous", "auth", "blocked", "permanent"}}}

    def wait_before_browser(self, cap: float, wait: Callable[[float], bool]) -> None:
        # Caller holds the journal lock: no browser/profile is opened during this wait.
        while (remaining := self.cooldown_until - time.time()) > 0:
            if remaining > cap:
                self.record_stop("single_wait_limit")
                raise ValueError("Saved server deadline exceeds the per-wait cap; deadline retained")
            if wait(remaining):
                self.record_stop("cancelled")
                raise InterruptedError("Resume cancelled; deadline retained")

    def previous_result(self, target: PostActionTarget) -> PostActionResult | None:
        from kusamushiri.actions import action_label_for

        state = self.entries.get(self.key(target))
        if state is None or state in {"retryable", "pending"}:
            return None
        if state == "success":
            return PostActionResult(target, action_label_for(target.kind), True, None, attempts=0)
        kind = "ambiguous" if state == "inflight" else state
        return PostActionResult(
            target,
            action_label_for(target.kind),
            False,
            tr("記録された {state} のため再実行しません。X と実行記録を確認してください。", state=kind),
            failure_kind=cast(FailureKind, kind),
            attempts=0,
        )

    def record(self, target: PostActionTarget, state: str, *, count_completion: bool = True) -> None:
        if state not in STATES:
            raise ValueError("Invalid action state")
        if count_completion and state == "success" and self.entries.get(self.key(target)) != "success":
            self.completed_since_pause += 1
        self.entries[self.key(target)] = state
        self._write()

    def reset_batch_count(self) -> None:
        self.completed_since_pause = 0
        self._write()

    def defer_until(self, deadline: float, reason: str = "") -> None:
        if reason:
            self.cooldown_reason = reason
        elif not deadline:
            self.cooldown_reason = ""
        self.cooldown_until = max(self.cooldown_until, deadline) if deadline else 0.0
        self._write()

    def _write(self) -> None:
        payload = {
            "version": 1,
            "stop_reason": self.stop_reason,
            "viewer": self.viewer,
            "scope": self.scope,
            "entries": self.entries,
            "cooldown_until": self.cooldown_until,
            "cooldown_reason": self.cooldown_reason,
            "completed_since_pause": self.completed_since_pause,
        }
        retained = self._retain_lock
        self._retain_lock = True
        self._write_lock("writing")
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(payload, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
            self._sync_directory()
            self._write_lock("ready")
            self._retain_lock = retained
        except BaseException:
            self._retain_lock = True
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise
