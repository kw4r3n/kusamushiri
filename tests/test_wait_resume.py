"""Offline wait/stop/recovery contracts; no browser or X requests."""

import json
import os
import signal
import socket
import subprocess
import sys
from unittest.mock import Mock

import pytest
from test_retry import ActionPage, FakeCore, target

from kusamushiri import actions, cli
from kusamushiri.core import XDeleterCore
from kusamushiri.models import PostActionResult, RetryPolicy
from kusamushiri.progress import ActionJournal, _pid_namespace
from kusamushiri.retry import run_post_actions


def test_repeated_server_waits_do_not_add_up_or_exhaust_fallback_count(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)

    def limited_success(item):
        core.rate_limit_status = 429
        core.rate_limit_retry_after = 180
        return PostActionResult(item, "delete", True, None)

    core.behavior = limited_success
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    results = run_post_actions(core, [target(i) for i in range(1, 8)], 0,
                               RetryPolicy(batch_size=0, max_wait_seconds=180, max_cooldowns=1), journal=journal)
    assert len(results) == 7 and all(result.success for result in results)
    assert [seconds for _, seconds in core.waits] == [180] * 6
    assert len(set(core.calls)) == 7
    assert journal.cooldown_until == core.clock + 180


@pytest.mark.parametrize("seconds, expected", [(179.999, True), (180, True), (180.001, False)])
def test_single_wait_cap_boundary(monkeypatch, tmp_path, seconds, expected):
    core = FakeCore(monkeypatch)
    core.rate_limit_status = 429
    core.rate_limit_retry_after = seconds
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    results = run_post_actions(core, [target()], 0, RetryPolicy(max_wait_seconds=180), journal=journal)
    assert bool(results) is expected
    assert journal.cooldown_until >= 1000 + seconds if not expected else True
    assert journal.stop_reason == ("completed" if expected else "single_wait_limit")


def test_server_wait_is_not_inflated_to_local_fallback(monkeypatch):
    core = FakeCore(monkeypatch)
    core.rate_limit_status = 429
    core.rate_limit_retry_after = 30
    assert run_post_actions(core, [target()], 0, RetryPolicy(max_wait_seconds=30))[0].success
    assert core.waits == [(0, 30)]


def test_legacy_total_mode_remains_explicit(monkeypatch):
    core = FakeCore(monkeypatch)
    core.behavior = lambda item: PostActionResult(item, "delete", False, "retry", "retryable")
    results = run_post_actions(core, [target()], 0, RetryPolicy(max_wait_seconds=180, wait_limit_mode="total"))
    assert not results[0].success
    assert core.waits == [(1, 60), (2, 120)]


def test_saved_deadline_wait_cancel_and_resume_without_browser(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    with journal:
        journal.defer_until(1180, "server")
        with pytest.raises(InterruptedError):
            journal.wait_before_browser(180, lambda _: True)
    assert journal.cooldown_until == 1180
    with journal:
        journal.wait_before_browser(180, core.wait_for_cancel)
    assert core.clock == 1180 and core.calls == []


def test_safe_stop_keeps_unsent_item_retryable_and_success_skipped(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    core.cancel_on_wait = True
    results = run_post_actions(core, [target(1), target(2)], 3, RetryPolicy(), journal=journal)
    assert len(results) == 1 and journal.stop_reason == "cancelled"
    core.cancel_on_wait = core.cancelled = False
    results = run_post_actions(core, [target(1), target(2)], 0, RetryPolicy(), journal=journal)
    assert core.calls == [target(1), target(2)] and results[0].attempts == 0


@pytest.mark.parametrize("stage", ["target_load", "menu", "confirmation", "mutation_submit", "mutation_wait"])
def test_stop_at_each_delete_stage_never_repeats_mutation(monkeypatch, tmp_path, stage):
    core = FakeCore(monkeypatch)
    page = ActionPage()
    diagnostics = Mock()
    diagnostics.http_failure_for.return_value = None
    diagnostics.navigation.side_effect = lambda: core.request_cancel() if stage == "target_load" else None
    diagnostics.phase.side_effect = lambda phase: core.request_cancel() if phase == stage else None
    core.execute_post_action = lambda item: actions.execute_post_action(
        page, item, find_locator=lambda owner, selectors: owner.locator(selectors[0]),
        should_cancel=core.is_cancel_requested, diagnostics=diagnostics)
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    results = run_post_actions(core, [target()], 0, RetryPolicy(), journal=journal)
    confirmations = sum("confirmationSheetConfirm" in click for click in page.clicks)
    if stage in {"mutation_submit", "mutation_wait"}:
        assert confirmations <= 1
    else:
        assert confirmations == 0
    if confirmations:
        assert results[0].success or results[0].failure_kind == "ambiguous"


def test_abrupt_interrupt_retains_inflight_and_never_resends(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    core.behavior = Mock(side_effect=KeyboardInterrupt)
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    with pytest.raises(KeyboardInterrupt):
        run_post_actions(core, [target()], 0, RetryPolicy(), journal=journal)
    assert journal.entries[ActionJournal.key(target())] == "inflight"
    core.cancelled = False
    result = run_post_actions(core, [target()], 0, RetryPolicy(), journal=journal)[0]
    assert result.failure_kind == "ambiguous" and len(core.calls) == 1


def test_dead_lock_recovery_keeps_receipts_and_deadline(monkeypatch, tmp_path):
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    with journal:
        journal.record(target(), "inflight")
        journal.defer_until(9999999999, "server")
        metadata = journal._lock_path.read_text()
    journal._lock_path.write_text(metadata)
    monkeypatch.setattr(os, "kill", Mock(side_effect=ProcessLookupError))
    journal.recover_lock()
    assert not journal._lock_path.exists()
    assert journal.cooldown_until == 9999999999
    assert journal.previous_result(target()).failure_kind == "ambiguous"


@pytest.mark.parametrize("state", ["alive", "legacy", "foreign", "writing"])
def test_recovery_never_unlocks_unproven_owner(monkeypatch, tmp_path, state):
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    data = {"version": 1, "scope": "fixture", "pid": 42, "host": socket.gethostname(), "recovery_state": "ready", "pid_namespace": _pid_namespace()}
    if state == "foreign":
        data["host"] = "different-host"
    if state == "writing":
        data["recovery_state"] = "writing"
    journal._lock_path.write_text("" if state == "legacy" else json.dumps(data))
    monkeypatch.setattr(os, "kill", Mock(return_value=None))
    with pytest.raises((ValueError, RuntimeError)):
        journal.recover_lock()
    assert journal._lock_path.exists()


def test_resume_account_checks_before_each_submission(monkeypatch):
    core = XDeleterCore()
    page = Mock()
    page.is_closed.return_value = False
    core._browser.page = page
    core.resume_viewer = "fixture"
    core.is_logged_in = lambda: True
    core.get_logged_in_username = lambda: "fixture"
    assert core._resume_identity(target()) is False  # tester is a different URL owner
    assert core._resume_identity(None)
    core.get_logged_in_username = lambda: "other"
    assert not core._resume_identity(None)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_sigterm_and_sigint_share_safe_stop_and_restore(monkeypatch, sig):
    core = Mock()
    core.rate_limit_status = None
    core.is_cancel_requested.return_value = False
    monkeypatch.setattr(cli, "XDeleterCore", lambda **_: core)
    monkeypatch.setattr(cli, "_wait_for_login", lambda *_: True)
    originals = {signal.SIGINT: object(), signal.SIGTERM: object()}
    handlers = dict(originals)

    def install(signum, handler):
        previous = handlers[signum]
        handlers[signum] = handler
        return previous

    monkeypatch.setattr(signal, "signal", install)
    with cli.browser_session("fixture", headless=True):
        handlers[sig](sig, None)
        core.request_cancel.assert_called_once()
        core.is_cancel_requested.return_value = True
        with pytest.raises(KeyboardInterrupt):
            handlers[sig](sig, None)
    assert handlers == originals
    core.stop_browser.assert_called_once()


def test_unfinished_real_request_never_blocks_on_response():
    request = Mock(spec=actions.Request)
    request.timing = {"responseEnd": -1}
    assert actions._completed_response(request) is None
    request.response.assert_not_called()


def test_real_process_exit_recovery_never_clears_uncertain_receipt(tmp_path):
    if os.name != "posix":
        pytest.skip("Dead-owner recovery is POSIX-only")
    path = tmp_path / "progress.json"
    source = f"""
import os,time
from pathlib import Path
from kusamushiri.progress import ActionJournal
from kusamushiri.models import PostActionTarget
with ActionJournal(Path({str(path)!r}), 'fixture') as journal:
    journal.record(PostActionTarget('https://x.com/tester/status/1','post'), 'success')
    journal.record(PostActionTarget('https://x.com/tester/status/2','post'), 'inflight')
    journal.defer_until(time.time()+60, 'server')
    os._exit(0)
"""
    subprocess.run([sys.executable, "-c", source], check=True, timeout=10)
    journal = ActionJournal(path, "fixture")
    journal.recover_lock()
    assert journal.previous_result(target(1)).success
    assert journal.previous_result(target(2)).failure_kind == "ambiguous"
    assert journal.cooldown_until > 0


def test_progress_and_resolve_are_offline_and_keep_deadline(monkeypatch, tmp_path, capsys):
    profile = tmp_path / "profile"
    monkeypatch.setattr(cli, "account_profile_path", lambda _: profile)
    monkeypatch.setattr(cli, "XDeleterCore", Mock(side_effect=AssertionError("must stay offline")))
    assert cli.main(["progress", "--profile", "fixture"]) == 0
    assert not profile.exists()
    journal = ActionJournal(profile / "action-progress.json", "fixture")
    with journal:
        journal.record(target(), "inflight")
        journal.defer_until(9999999999, "server")
    assert cli.main(["resolve", "--profile", "fixture", "--post-id", "1", "--verified-applied", "--yes"]) == 0
    journal._load()
    assert journal.entries["post:1"] == "success" and journal.cooldown_until == 9999999999
    assert cli.main(["resolve", "--profile", "fixture", "--post-id", "1", "--verified-not-applied", "--yes"]) != 0
    journal._load()
    assert journal.entries["post:1"] == "success"


def test_resume_refuses_unresolved_state_before_browser(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    monkeypatch.setattr(cli, "account_profile_path", lambda _: profile)
    browser = Mock(side_effect=AssertionError("must not start"))
    monkeypatch.setattr(cli, "XDeleterCore", browser)
    journal = ActionJournal(profile / "action-progress.json", "fixture")
    with journal:
        journal.record(target(), "inflight")
    path = tmp_path / "reviewed.json"
    path.write_text(json.dumps([{"url": target().url, "kind": "post"}]))
    assert cli.main(["resume", str(path), "--profile", "fixture", "--expected-viewer", "tester", "--yes"]) != 0
    browser.assert_not_called()


def test_resume_preserves_expected_viewer_for_batch(monkeypatch, tmp_path):
    from test_cli import FakeCore as CliCore

    profile = tmp_path / "profile"
    monkeypatch.setattr(cli, "account_profile_path", lambda _: profile)
    monkeypatch.setattr(cli, "get_account_profile_dir", lambda _: profile)
    monkeypatch.setattr(cli, "XDeleterCore", CliCore)
    monkeypatch.setattr(cli.DeletedPostStore, "for_account", lambda _: cli.DeletedPostStore(tmp_path / "deleted.json"))
    journal = ActionJournal(profile / "action-progress.json", "fixture")
    with journal:
        journal.record(target(1), "success")
    path = tmp_path / "reviewed.json"
    path.write_text(json.dumps([{"url": target(i).url, "kind": "post"} for i in [1, 2]]))
    assert cli.main(["resume", str(path), "--profile", "fixture", "--expected-viewer", "tester", "--interval", "0", "--yes"]) == 0
    assert CliCore.instances[-1].actions == [target(2)]
    assert CliCore.instances[-1].resume_viewer == "tester"


@pytest.mark.parametrize("extension, completes", [(180, True), (180.001, False)])
def test_new_server_deadline_is_rechecked_without_adding_elapsed_time(monkeypatch, tmp_path, extension, completes):
    core = FakeCore(monkeypatch)
    core.rate_limit_status = 429
    core.rate_limit_retry_until = 1090
    core.rate_limit_generation = 1
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    waits = []

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 1:
            core.clock += 45
            core.rate_limit_retry_until = core.clock + extension
            core.rate_limit_generation += 1
        else:
            core.clock += seconds
        return False

    results = run_post_actions(core, [target()], 0, RetryPolicy(max_wait_seconds=180), journal=journal, wait_for_cancel=wait)
    assert bool(results) is completes
    if completes:
        assert waits == [90, 180] and core.clock == 1225
    else:
        assert waits == [90] and journal.cooldown_until >= 1045 + extension
        assert journal.stop_reason == "single_wait_limit"


def test_late_server_response_persists_deadline_outside_batch_scope(monkeypatch, tmp_path):
    clock = FakeCore(monkeypatch)
    core = XDeleterCore()
    profile = tmp_path / "chromium-profile-fixture"
    profile.mkdir()
    core._browser.profile_dir = profile
    response = Mock(url="https://x.com/i/api/graphql/fixture/Unknown", status=429,
                    headers={"retry-after": "300", "x-rate-limit-reset": "1400"})
    core._stop_on_rate_limit(response)
    journal = ActionJournal(profile / "action-progress.json", "fixture")
    journal._load()
    assert journal.cooldown_until == clock.clock + 400
    assert core.is_cancel_requested()


def test_predicted_reset_over_cap_stops_without_next_navigation(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    core.rate_limit_pace_until = 1500
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    assert run_post_actions(core, [target()], 0, RetryPolicy(max_wait_seconds=180), journal=journal) == []
    assert core.calls == [] and core.waits == []
    assert journal.cooldown_until == 1500 and journal.stop_reason == "single_wait_limit"


def test_resume_binds_account_and_unwinds_scope_on_mismatch(monkeypatch, tmp_path):
    core = XDeleterCore()
    page = Mock()
    page.is_closed.return_value = False
    core._browser.page = page
    core.resume_viewer = "tester"
    core.is_logged_in = lambda: True
    core.get_logged_in_username = lambda: "tester"
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    with journal, core.post_action_journal_scope(journal):
        assert journal.viewer == "tester" and core._resume_identity(target())
        core.get_logged_in_username = lambda: "other"
        assert not core._resume_identity(target())
    assert core._action_journal is None and core._resume_page is None
    core.resume_viewer = "other"
    with journal, pytest.raises(ValueError), core.post_action_journal_scope(journal):
        pytest.fail("journal account mismatch must fail before actions")
    assert core._action_journal is None and core._resume_page is None


@pytest.mark.parametrize("source", ["predicted", "persisted", "both"])
@pytest.mark.parametrize("delivery", ["dispatch", "resume"])
@pytest.mark.parametrize("cap, completes", [(300, True), (299, False)])
def test_predicted_or_persisted_extension_after_dispatch_is_not_erased(monkeypatch, tmp_path, source, delivery, cap, completes):
    core = FakeCore(monkeypatch)
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    with journal:
        journal.defer_until(1090, "initial reset")
    core.rate_limit_pace_until = 1090
    dispatched = False
    action_times = []

    def pump():
        nonlocal dispatched
        if core.clock == 1090 and not dispatched:
            dispatched = True
            if source in {"predicted", "both"}:
                core.rate_limit_pace_until = 1390
            if source in {"persisted", "both"}:
                journal.defer_until(1390, "extended reset")

    def action(item):
        action_times.append(core.clock)
        return PostActionResult(item, "delete", True, None)

    if delivery == "dispatch":
        core.poll_action_events = pump
    else:
        core.resume_action_network = pump
    core.behavior = action
    results = run_post_actions(core, [target()], 0, RetryPolicy(max_wait_seconds=cap), journal=journal)
    assert bool(results) is completes
    if completes:
        assert action_times == [1390] and core.waits == [(0, 90), (0, 300)]
    else:
        assert action_times == [] and core.clock == 1090
        saved = json.loads(journal.path.read_text())
        assert saved["cooldown_until"] == 1390 and saved["stop_reason"] == "single_wait_limit"


def authenticated_core(viewer="tester"):
    core = XDeleterCore()
    core._browser.page = ActionPage()
    core._find_first_visible_locator = lambda owner, selectors: owner.locator(selectors[0])
    core.is_logged_in = lambda: True
    core.get_logged_in_username = lambda: viewer
    return core


@pytest.mark.parametrize("resume", [False, True])
def test_bound_account_rejects_another_signed_in_viewer_before_action(monkeypatch, tmp_path, resume):
    FakeCore(monkeypatch)
    core = authenticated_core("wrong_account")
    core.resume_viewer = "tester" if resume else None
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    with journal:
        journal.bind_viewer("tester")
    with pytest.raises(cli.ActionBatchError):
        run_post_actions(core, [target(), target(2)], 0, RetryPolicy(), journal=journal)
    assert core._browser.page.clicks == []
    journal._load()
    assert journal.viewer == "tester" and not journal.entries


def test_fresh_ordinary_batch_binds_account_and_checks_each_mutation(monkeypatch, tmp_path):
    FakeCore(monkeypatch)
    core = authenticated_core()
    page = core._browser.page
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    original_phase = core._browser.diagnostics.phase

    def phase(value):
        original_phase(value)
        if value == "confirmation":
            core.get_logged_in_username = lambda: "wrong_account"

    core._browser.diagnostics.phase = phase
    results = run_post_actions(core, [target(), target(2)], 0, RetryPolicy(), journal=journal)
    assert len(results) == 1 and results[0].failure_kind == "blocked"
    assert not any("confirmationSheetConfirm" in click for click in page.clicks)
    assert journal.viewer == "tester" and journal.entries["post:1"] == "blocked"


def test_legacy_receipts_need_explicit_verified_account(monkeypatch, tmp_path):
    FakeCore(monkeypatch)
    core = authenticated_core()
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    with journal:
        journal.record(target(), "success")
    with pytest.raises(cli.ActionBatchError):
        run_post_actions(core, [target(), target(2)], 0, RetryPolicy(), journal=journal)
    assert not core._browser.page.clicks
    core.clear_cancel()
    core.resume_viewer = "tester"
    results = run_post_actions(core, [target(), target(2)], 0, RetryPolicy(), journal=journal)
    assert results[0].attempts == 0 and results[1].success
    assert journal.viewer == "tester"


@pytest.mark.parametrize("method, kind", [("delete_post", "post"), ("undo_repost", "repost"), ("unlike_post", "like")])
def test_legacy_core_entrypoints_cannot_bypass_bound_owner(method, kind, tmp_path):
    core = authenticated_core()
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    with journal, core.post_action_journal_scope(journal):
        core.get_logged_in_username = lambda: "wrong_account"
        success, _ = getattr(core, method)(target(kind=kind).url)
    assert not success
    assert not any("confirmationSheetConfirm" in click or "@data-testid='unlike'" in click for click in core._browser.page.clicks)


@pytest.mark.parametrize("kind", ["post", "repost", "like"])
def test_cancel_at_submit_phase_is_positive_unsent_and_resumable(monkeypatch, tmp_path, kind):
    core = FakeCore(monkeypatch)
    page = ActionPage()
    diagnostics = Mock()
    diagnostics.http_failure_for.return_value = None
    diagnostics.phase.side_effect = lambda value: core.request_cancel() if value == "mutation_submit" else None
    item = target(kind=kind)
    core.execute_post_action = lambda target: actions.execute_post_action(
        page, target, find_locator=lambda owner, selectors: owner.locator(selectors[0]),
        diagnostics=diagnostics, should_cancel=core.is_cancel_requested)
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    results = run_post_actions(core, [item], 0, RetryPolicy(), journal=journal)
    assert not results[0].success and results[0].failure_kind == "retryable"
    assert journal.entries[journal.key(item)] == "retryable"
    assert not any("confirmationSheetConfirm" in click or "unlike" in click for click in page.clicks)
    core.cancelled = False
    core.execute_post_action = lambda target: PostActionResult(target, "delete", True, None)
    assert run_post_actions(core, [item], 0, RetryPolicy(), journal=journal)[0].success


def test_click_invocation_error_is_still_ambiguous(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    page = ActionPage()

    def prepare(locator, stage):
        if stage == "confirm":
            return Mock(side_effect=RuntimeError("invocation may have sent a mutation"))
        return locator.click

    core.execute_post_action = lambda item: actions.execute_post_action(
        page, item, find_locator=lambda owner, selectors: owner.locator(selectors[0]), prepare_control=prepare)
    journal = ActionJournal(tmp_path / "progress.json", "fixture")
    result = run_post_actions(core, [target()], 0, RetryPolicy(), journal=journal)[0]
    assert result.failure_kind == "ambiguous" and journal.entries["post:1"] == "ambiguous"


def test_crash_while_holding_recovery_guard_does_not_strand_journal(tmp_path):
    if os.name != "posix":
        pytest.skip("Advisory recovery guard is POSIX-only")
    path = tmp_path / "progress.json"
    source = f"""
import os
from pathlib import Path
from kusamushiri.progress import ActionJournal
from kusamushiri.models import PostActionTarget
with ActionJournal(Path({str(path)!r}), 'fixture') as journal:
    journal.record(PostActionTarget('https://x.com/tester/status/1','post'), 'inflight')
    journal.defer_until(9999999999, 'server')
    with journal._recovery_guard():
        os._exit(0)
"""
    subprocess.run([sys.executable, "-c", source], check=True, timeout=10)
    journal = ActionJournal(path, "fixture")
    guard = journal._lock_path.with_suffix(".recovering")
    inode = guard.stat().st_ino
    journal.recover_lock()
    with journal:
        assert journal.entries["post:1"] == "inflight" and journal.cooldown_until == 9999999999
    assert guard.stat().st_ino == inode  # same inode must remain the shared exclusion point


def test_existing_empty_recovery_guard_is_not_a_permanent_lock(tmp_path):
    path = tmp_path / "progress.json"
    journal = ActionJournal(path, "fixture")
    journal._lock_path.with_suffix(".recovering").touch()
    with journal:
        journal.record(target(), "pending")
    assert not journal._lock_path.exists()


def test_live_advisory_recovery_guard_blocks_both_recovery_and_start(tmp_path):
    if os.name != "posix":
        pytest.skip("Advisory recovery guard is POSIX-only")
    path = tmp_path / "progress.json"
    source = f"""
import sys
from pathlib import Path
from kusamushiri.progress import ActionJournal
journal = ActionJournal(Path({str(path)!r}), 'fixture')
with journal._recovery_guard():
    print('ready', flush=True)
    sys.stdin.readline()
"""
    with subprocess.Popen([sys.executable, "-c", source], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as process:
        try:
            assert process.stdout.readline().strip() == "ready"
            journal = ActionJournal(path, "fixture")
            with pytest.raises(RuntimeError, match="recovery is in progress"), journal:
                pytest.fail("must not enter journal during recovery")
            with pytest.raises(RuntimeError, match="recovery is in progress"):
                journal.recover_lock()
        finally:
            process.communicate("done\n", timeout=10)
    with ActionJournal(path, "fixture"):
        pass
