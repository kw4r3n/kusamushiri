"""Real owned Python processes only. Never launch browser or signal numeric PID."""

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from test_loopback_runner import runner


@pytest.fixture
def child():
    assert runner.stable_handles_available()
    process = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(20)'], start_new_session=True)
    identity = runner.proc_identity(process.pid)
    owner = runner.capture_owned(process.pid, expected_birth=identity[1])
    assert owner is not None
    yield process, owner
    table = {process.pid: owner}
    assert runner.terminate_owned(process, table)
    runner.close_owned_handles(table)


def test_conflicting_record_never_refreshes_or_signals_actual_owned_child(child):
    process, owner = child
    conflict = replace(owner, birth=owner.birth + 1)
    table = {process.pid: conflict}
    assert not runner.terminate_owned(process, table)
    assert table[process.pid] is conflict and conflict.birth == owner.birth + 1
    assert process.poll() is None and runner.pidfd_exited(owner.pidfd) is False


def test_unestablished_legacy_birth_table_never_signals_or_adopts(child):
    process, owner = child
    table = {process.pid: owner.birth + 1}
    assert not runner.terminate_owned(process, table)
    assert table == {process.pid: owner.birth + 1} and process.poll() is None


def test_pid_identity_change_between_check_and_cleanup_is_refused(child, monkeypatch):
    process, owner = child
    original = runner.proc_identity
    monkeypatch.setattr(runner, 'proc_identity', lambda pid: (owner.parent, owner.birth + 1) if pid == process.pid else original(pid))
    table = {process.pid: owner}
    assert not runner.terminate_owned(process, table)
    assert table[process.pid] is owner and process.poll() is None
    monkeypatch.setattr(runner, 'proc_identity', original)


def test_numeric_pid_reuse_cannot_adopt_new_generation_after_original_exit(child, monkeypatch):
    process, owner = child
    table = {process.pid: owner}
    assert runner.terminate_owned(process, table)
    original = runner.proc_identity
    monkeypatch.setattr(runner, 'proc_identity', lambda pid: (owner.parent, owner.birth + 999) if pid == process.pid else original(pid))
    sent = []
    monkeypatch.setattr(runner, 'pidfd_signal', lambda *args: sent.append(args) or True)
    assert runner.terminate_owned(process, table)
    assert table[process.pid] is owner and not sent


def test_ownership_record_is_frozen_and_signal_uses_same_kernel_handle(child, monkeypatch):
    process, owner = child
    with pytest.raises(FrozenInstanceError):
        owner.birth = owner.birth + 1
    calls = []
    original = runner.pidfd_signal
    monkeypatch.setattr(runner, 'pidfd_signal', lambda fd, sig: calls.append((fd, sig)) or original(fd, sig))
    assert runner.terminate_owned(process, {process.pid: owner})
    assert calls and all(fd == owner.pidfd for fd, sig in calls)


@pytest.mark.parametrize('result_kind', ['missing', 'invalid-json', 'invalid-shape', 'early-crash', 'cleanup-success', 'misleading-success', 'invalid-case-items'])
def test_every_worker_exit_cleans_verified_owned_descendants(tmp_path, monkeypatch, result_kind):
    # Replace only the worker entry SCRIPT with an actual Python crash tree. No
    # simulated Browser/Context and no native/HTTP server are used.
    helper = tmp_path / 'owned_worker.py'
    emit = {
        'missing': '',
        'early-crash': '',
        'invalid-json': "(out/'worker-result.json').write_text('{broken')",
        'invalid-shape': "(out/'worker-result.json').write_text('null')",
        'cleanup-success': "(out/'worker-result.json').write_text(json.dumps({'scope':'unauthenticated-loopback-only','cleanup_complete':True,'passed':False,'native_started':False,'cases':[],'checks':[]}))",
        'invalid-case-items': "(out/'worker-result.json').write_text(json.dumps({'scope':'unauthenticated-loopback-only','cleanup_complete':True,'passed':True,'native_started':True,'cases':[{'case':[],'passed':True}]*9,'checks':[]}))",
        'misleading-success': "(out/'worker-result.json').write_text(json.dumps({'scope':'unauthenticated-loopback-only','cleanup_complete':True,'passed':True,'native_started':True,'cases':None,'checks':[]}))",
    }[result_kind]
    helper.write_text("import sys,subprocess,time,os,json\nfrom pathlib import Path\nout=Path(sys.argv[sys.argv.index('--output')+1])\nassert sys.stdin.readline(16)=='start\\n'\np=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(20)'])\n(out/'fixture-child.pid').write_text(str(p.pid))\ntime.sleep(.3)\n" + emit + "\nos._exit(1)\n")
    monkeypatch.setattr(runner, '__file__', str(helper))
    output = tmp_path / 'result'
    assert runner.supervise(output) == 1
    record = json.loads((output/'result.json').read_text())
    assert record['observed_owned_processes'] >= 2
    assert record['observed_processes_terminated'] is True and record['tracked_owned_processes_closed'] is True
    assert record['native_verified'] is False
    assert record['cleanup_complete'] == (result_kind == 'cleanup-success')
    if result_kind != 'cleanup-success':
        assert Path(record['preserved_owned_scratch']).is_dir()
    assert record.get('worker_result_status') in {'missing','unreadable_or_invalid_json','invalid_shape',None}


def test_missing_stable_handle_support_fails_before_any_worker_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'stable_handles_available', lambda: False)
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **kw: pytest.fail('No worker without stable process handles'))
    output = tmp_path/'result'
    assert runner.supervise(output) == 1
    record = json.loads((output/'result.json').read_text())
    assert record['reason'] == 'stable_process_handles_unavailable_no_worker'
    assert record['cleanup_complete'] is True and record['native_verified'] is False


def test_no_numeric_pid_signal_fallback_exists():
    source = Path(runner.__file__).read_text()
    assert 'os.kill(' not in source and 'owned[process.pid] = root[1]' not in source
