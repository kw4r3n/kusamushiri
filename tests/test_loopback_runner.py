"""Pure policy plus actual owned-Python-process checks; native browser NOT run."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from kusamushiri.readonly_startup import LoopbackReadOnlyPlan, _canonical_loopback, _read_binding

RUNNER = Path(__file__).resolve().parents[1] / "scripts/loopback_fixture_runner.py"
spec = importlib.util.spec_from_file_location("owned_loopback_runner", RUNNER)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)
ORIGIN = "http://127.0.0.1:8765"


@pytest.mark.parametrize("url", [
    "https://x.com/fixture/status/123", "http://localhost:8765/target/123", "http://127.0.0.2:8765/target/123",
    "http://127.0.0.1/target/123", "http://127.0.0.1:0/target/123", "http://user@127.0.0.1:8765/target/123",
    "http://127.0.0.1:8765/target/123?", "http://127.0.0.1:8765/target/123#", "http://127.0.0.1:8765/target/123;",
])
def test_no_external_auth_alias_or_noncanonical_loopback_identity(url):
    assert _canonical_loopback(url) is None


@pytest.mark.parametrize("query", ["target=456", "target=123&target=123", "target=%31%32%33", "target=123&token=AUTH_CANARY", "target", ""])
def test_target_is_bound_from_canonical_actual_url_not_an_invented_attribute(query):
    parsed = _canonical_loopback(ORIGIN + "/read?" + query)
    assert parsed is None or not _read_binding(parsed, "123")


@pytest.mark.parametrize("case", runner.CASES)
def test_real_runner_case_plan_is_get_only_single_loopback_origin(case):
    p = runner.make_plan(ORIGIN, case)
    assert p.valid() and p.scope == "loopback-fixture" and p.seconds <= 1
    assert all(url.startswith(ORIGIN + "/") and "/write" not in url for url, resource in p.gets)


def test_normal_shell_inspection_starts_no_driver_browser_or_server():
    p = subprocess.run([sys.executable, "-B", str(RUNNER), "--inspect"], text=True, capture_output=True, timeout=5)
    assert p.returncode == 0 and '"chromium_sandbox": true' in p.stdout
    assert '"auth_state": false' in p.stdout and '"sandbox_bypass": false' in p.stdout
    assert '--no-sandbox' not in p.stdout and 'localhost' not in p.stdout


def test_owned_process_termination_uses_actual_pid_birth_identity_without_browser():
    process = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(10)"], start_new_session=True)
    try:
        identity = runner.proc_identity(process.pid)
        assert identity is not None
        owner = runner.capture_owned(process.pid, expected_birth=identity[1])
        assert owner is not None
        owned = {process.pid: owner}
        assert runner.terminate_owned(process, owned)
        runner.close_owned_handles(owned)
        assert process.poll() is not None and runner.proc_identity(process.pid) is None
    finally:
        if process.poll() is None:
            identity = runner.proc_identity(process.pid)
            owner = runner.capture_owned(process.pid, expected_birth=identity[1])
            assert owner is not None
            cleanup = {process.pid: owner}
            assert runner.terminate_owned(process, cleanup)
            runner.close_owned_handles(cleanup)


def test_pid_reuse_or_unowned_identity_never_enters_discovered_children():
    process = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(10)"], start_new_session=True)
    try:
        identity = runner.proc_identity(process.pid)
        assert identity is not None
        owned = {process.pid: identity[1] + 1}  # deliberately wrong birth identity
        runner.discover_owned(process.pid, owned)
        assert owned == {process.pid: identity[1] + 1}
        assert process.poll() is None
    finally:
        identity = runner.proc_identity(process.pid)
        owner = runner.capture_owned(process.pid, expected_birth=identity[1])
        assert owner is not None
        cleanup = {process.pid: owner}
        assert runner.terminate_owned(process, cleanup)
        runner.close_owned_handles(cleanup)


def test_no_non_loopback_server_or_security_weakening_configuration():
    assert runner.LAUNCH['chromium_sandbox'] is True
    assert '--no-sandbox' not in runner.LAUNCH.get('args', [])
    p = LoopbackReadOnlyPlan(ORIGIN + '/target/123', frozenset({(ORIGIN + '/target/123', 'document'), ('http://127.0.0.1:9999/read?target=123', 'fetch')}), True, 'loopback-fixture')
    assert not p.valid()


def test_progress_checkpoint_is_atomic_and_retains_latest_case_stage(tmp_path):
    record = {'scope':'unauthenticated-loopback-only', 'phase':'fixture_popup:navigation', 'cases':[]}
    runner.save_progress(tmp_path, record)
    assert not (tmp_path/'worker-progress.pending').exists()
    assert __import__('json').loads((tmp_path/'worker-progress.json').read_text()) == record
    record['phase'] = 'fixture_popup:context_close'
    runner.save_progress(tmp_path, record)
    assert __import__('json').loads((tmp_path/'worker-progress.json').read_text())['phase'] == 'fixture_popup:context_close'


def test_checkpoint_write_failure_is_not_an_exception_that_bypasses_cleanup(tmp_path):
    bad = tmp_path/'not-a-directory'
    bad.write_text('fixture')
    record = {'phase':'browser_close'}
    runner.save_progress(bad, record)
    assert record['progress_write_failed'] is True


def test_watchdog_stack_preserves_python_stall_function_without_local_values(tmp_path):
    # Real short-lived owned Python process, no driver/browser/server or new native run.
    stack = tmp_path/'stack.txt'
    source = "import faulthandler,threading\nf=open("+repr(str(stack))+",'w')\nfaulthandler.dump_traceback_later(.2,file=f)\ndef local_fixture_stall():\n secret_canary='AUTH_STACK_CANARY'\n threading.Event().wait(.4)\nlocal_fixture_stall()\nfaulthandler.cancel_dump_traceback_later()\nf.close()\n"
    result = subprocess.run([sys.executable,'-B','-c',source], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    text = stack.read_text()
    assert 'local_fixture_stall' in text and 'AUTH_STACK_CANARY' not in text
