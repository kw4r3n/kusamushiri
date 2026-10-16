"""Driver-free safety and GUI/worker wiring for the caret-only diagnostic."""

import json
import threading
from dataclasses import replace

import pytest
from test_target_read_probe import session  # noqa: F401
from test_verified_single_post import live_shape  # noqa: F401

from kusamushiri.menu_diagnostic import MenuDiagnosticResult
from kusamushiri.target_read_probe import TargetReadRequest

REQUEST = TargetReadRequest("https://x.com/author/status/123", "author")
SNAPSHOT = {"dropdown_total": 1, "dropdown_visible": 1, "menuitem_total": 0, "union_visible": 0, "dialog_visible": 0,
            "selector_counts": [0, 0, 0, 0], "truncated": False, "stable": True, "rows": []}


def test_diagnostic_uses_real_scope_and_only_caret_no_action_or_journal(live_shape, monkeypatch):  # noqa: F811
    core, manager, page = live_shape
    monkeypatch.setattr("kusamushiri.menu_diagnostic._snapshot", lambda scope: SNAPSHOT)
    def forbidden(*args, **kwargs):
        pytest.fail("action or journal path entered")
    monkeypatch.setattr(core, "execute_post_action", forbidden)
    monkeypatch.setattr("kusamushiri.app.run_post_actions", forbidden)
    monkeypatch.setattr("kusamushiri.progress.ActionJournal.__init__", forbidden)
    result = core.diagnose_post_menu(REQUEST, worker_thread_id=threading.get_ident())
    assert result.safe_fields()["snapshot"] == SNAPSHOT and result.caret_click_attempted
    assert len(page.clicks) == 1 and "caret" in page.clicks[0]
    assert page.submissions == 0 and page.gotos == []
    assert page.menu_open and core._verified_post_action is None and core._pending_view is None
    assert not manager.post_actions_active


@pytest.mark.parametrize("case", ["cancel", "rate", "hold", "stale", "viewer", "target", "wrong_thread"])
def test_diagnostic_preserves_preexisting_controls_and_never_clicks(live_shape, monkeypatch, case):  # noqa: F811
    core, manager, page = live_shape
    kwargs = {"worker_thread_id": threading.get_ident()}
    if case == "cancel":
        core.request_cancel()
    elif case == "rate":
        core.rate_limit_status = 429
    elif case == "hold":
        manager.action_control_failed = True
    elif case == "stale":
        kwargs["is_current"] = lambda: False
    elif case == "viewer":
        page.viewer = "other"
    elif case == "target":
        page.url = "https://x.com/author/status/999"
    else:
        kwargs["worker_thread_id"] = -1
    result = core.diagnose_post_menu(REQUEST, **kwargs)
    assert result.reason != "captured" and not result.caret_click_attempted
    assert not page.clicks and not page.gotos and not page.submissions
    if case == "rate":
        assert core.rate_limit_status == 429
    if case == "hold":
        assert manager.action_control_failed


@pytest.mark.parametrize("case", ["cancel_after_caret", "sdk_click_exception", "capture_exception"])
def test_one_click_latch_and_closed_errors_survive_failure(live_shape, monkeypatch, case):  # noqa: F811
    core, _, page = live_shape
    def clicked(_):
        if case == "cancel_after_caret":
            core.request_cancel()
        elif case == "sdk_click_exception":
            raise RuntimeError("SECRET_URL_COOKIE_BODY")
    page.click_hook = clicked
    if case == "capture_exception":
        monkeypatch.setattr("kusamushiri.menu_diagnostic._snapshot", lambda _: (_ for _ in ()).throw(RuntimeError("SECRET_URL_COOKIE_BODY")))
    result = core.diagnose_post_menu(REQUEST, worker_thread_id=threading.get_ident())
    assert result.reason != "captured" and result.caret_click_attempted
    assert "SECRET" not in json.dumps(result.safe_fields())
    assert len(page.clicks) == 1 and page.menu_open and not page.submissions and not page.gotos


@pytest.mark.parametrize("field,value", [("reason", "SECRET"), ("snapshot", {**SNAPSHOT, "body": "SECRET"}),
                                         ("snapshot", {**SNAPSHOT, "union_visible": True}),
                                         ("snapshot", {**SNAPSHOT, "selector_counts": [0, 0, 0, "SECRET"]})])
def test_closed_schema_rejects_canary_and_invalid_counts(field, value):
    result = replace(MenuDiagnosticResult("captured", "complete", True, SNAPSHOT), **{field: value})
    with pytest.raises(ValueError):
        result.safe_fields()


def test_worker_uses_dedicated_command_without_clearing_or_action(qtbot):
    from test_app import FakeCore

    from kusamushiri.app import XDeleterWorker

    core, results, calls = FakeCore(), [], []
    core.rate_limit_status, core.cancel_requested = 429, True
    def diagnose(request, **kwargs):
        assert core.rate_limit_status == 429 and core.cancel_requested
        calls.append((request, kwargs["worker_thread_id"], kwargs["is_current"]()))
        return MenuDiagnosticResult("rate_limited")
    core.diagnose_post_menu = diagnose
    worker = XDeleterWorker(core)
    worker.events.menu_diagnosed.connect(results.append)
    try:
        worker.enqueue_menu_diagnostic(REQUEST)
        qtbot.waitUntil(lambda: bool(results), timeout=2000)
        assert results == [MenuDiagnosticResult("rate_limited")]
        assert calls == [(REQUEST, worker._thread.ident, True)] and core.calls == []
        assert core.rate_limit_status == 429 and core.cancel_requested
    finally:
        worker.shutdown()


@pytest.mark.parametrize("mode,attempted", [("caret_only", True), ("existing_overlay_read_only", False)])
def test_worker_stale_result_retains_click_attempt_and_discards_snapshot(qtbot, mode, attempted):
    from test_app import FakeCore

    from kusamushiri.app import XDeleterWorker

    core, results = FakeCore(), []
    worker = XDeleterWorker(core)
    def diagnose(*args, **kwargs):
        worker.cancel_current_operation()
        return MenuDiagnosticResult("captured", "complete", attempted, SNAPSHOT, mode)
    core.diagnose_post_menu = diagnose
    worker.events.menu_diagnosed.connect(results.append)
    try:
        worker.enqueue_menu_diagnostic(REQUEST)
        qtbot.waitUntil(lambda: bool(results), timeout=2000)
        assert results == [MenuDiagnosticResult("cancelled_or_stale", "complete", attempted, inspection_mode=mode)]
        assert not core.calls
    finally:
        worker.shutdown()



def existing_menu(live_shape, monkeypatch):  # noqa: F811
    from kusamushiri.view_cache import OVERLAYS

    core, _, page = live_shape
    page.menu_open = True
    original = page.locator
    def locator(selector):
        if selector == OVERLAYS + ', [role="alertdialog"]:visible':
            return type("Count", (), {"count": lambda _: int(page.menu_open)})()
        return original(selector)
    page.locator = locator
    return core, page


def test_existing_menu_reads_without_action_scope_or_pending_view(live_shape, monkeypatch):  # noqa: F811
    core, page = existing_menu(live_shape, monkeypatch)
    marker = object()
    core._pending_view = marker
    def forbidden(*args, **kwargs):
        pytest.fail("passive inspection entered action state")
    monkeypatch.setattr(core, "verified_single_post_scope", forbidden)
    monkeypatch.setattr("kusamushiri.view_cache.PendingView.capture", forbidden)
    monkeypatch.setattr("kusamushiri.menu_diagnostic._snapshot", lambda scope: SNAPSHOT)
    result = core.diagnose_post_menu(REQUEST, worker_thread_id=threading.get_ident())
    assert result.reason == "captured" and result.inspection_mode == "existing_overlay_read_only"
    assert not result.caret_click_attempted and result.safe_fields()["snapshot"] == SNAPSHOT
    assert core._pending_view is marker and core._verified_post_action is None
    assert not page.clicks and not page.submissions and not page.gotos


@pytest.mark.parametrize("change", ["viewer", "target", "frame", "article", "cancel", "rate", "hold", "action_token", "active_action"])
def test_existing_menu_revalidates_identity_and_controls(live_shape, monkeypatch, change):  # noqa: F811
    from types import SimpleNamespace

    core, page = existing_menu(live_shape, monkeypatch)
    def snapshot(scope):
        if change == "viewer":
            page.viewer = "other"
        elif change == "target":
            page.url = "https://x.com/author/status/999"
        elif change == "frame":
            page.main_frame = SimpleNamespace(page=page)
        elif change == "article":
            page.element = object()
        elif change == "cancel":
            core.request_cancel()
        elif change == "rate":
            core.rate_limit_status = 429
        elif change == "hold":
            core._browser.action_control_failed = True
        elif change == "action_token":
            core._verified_post_action = object()
        else:
            core._browser.post_actions_active = True
        return SNAPSHOT
    monkeypatch.setattr("kusamushiri.menu_diagnostic._snapshot", snapshot)
    result = core.diagnose_post_menu(REQUEST, worker_thread_id=threading.get_ident())
    assert result.reason != "captured" and result.inspection_mode == "existing_overlay_read_only"
    assert not result.caret_click_attempted and not page.clicks and not page.submissions and not page.gotos
    if change == "action_token":
        assert core._verified_post_action is not None
    if change == "active_action":
        assert core._browser.post_actions_active


@pytest.mark.parametrize("appears", [False, True])
def test_overlay_race_never_falls_back_or_reclicks(live_shape, monkeypatch, appears):  # noqa: F811
    from kusamushiri.view_cache import OVERLAYS

    core, _, page = live_shape
    page.menu_open = not appears
    original = page.locator
    def count():
        prior = int(page.menu_open)
        page.menu_open = appears
        return prior
    def locator(selector):
        if selector == OVERLAYS + ', [role="alertdialog"]:visible':
            return type("Count", (), {"count": lambda _: count()})()
        return original(selector)
    page.locator = locator
    monkeypatch.setattr("kusamushiri.menu_diagnostic._snapshot", lambda _: {**SNAPSHOT, "dropdown_total": 0, "dropdown_visible": 0})
    result = core.diagnose_post_menu(REQUEST, worker_thread_id=threading.get_ident())
    assert result.inspection_mode == ("caret_only" if appears else "existing_overlay_read_only")
    assert result.reason == ("target_unmatched" if appears else "captured")
    assert not result.caret_click_attempted and not page.clicks and not page.submissions


def test_existing_menu_does_not_weaken_real_action_overlay_guard(live_shape, monkeypatch):  # noqa: F811
    from kusamushiri.models import PostActionTarget

    core, _ = existing_menu(live_shape, monkeypatch)
    monkeypatch.setattr("kusamushiri.menu_diagnostic._snapshot", lambda _: SNAPSHOT)
    assert core.diagnose_post_menu(REQUEST, worker_thread_id=threading.get_ident()).reason == "captured"
    with pytest.raises(RuntimeError, match="target_unmatched"), core.verified_single_post_scope(
            PostActionTarget(REQUEST.url, "post"), REQUEST.expected_viewer, current_view_only=True):
        pytest.fail("action unexpectedly accepted open overlay")


def test_existing_mode_cannot_claim_caret_attempt():
    with pytest.raises(ValueError):
        MenuDiagnosticResult("captured", "complete", True, SNAPSHOT, "existing_overlay_read_only").safe_fields()
    assert MenuDiagnosticResult("captured", "complete", False, SNAPSHOT, "existing_overlay_read_only").safe_fields()["inspection_mode"] == "existing_overlay_read_only"



def test_closed_menu_retains_original_outer_deadline(live_shape, monkeypatch):  # noqa: F811
    from kusamushiri.menu_diagnostic import _MenuReadScope

    core, _, page = live_shape
    original = _MenuReadScope.has_overlay
    def expire(scope):
        found = original(scope)
        scope.session.monotonic = lambda: scope.session.start + 30
        return found
    monkeypatch.setattr(_MenuReadScope, "has_overlay", expire)
    result = core.diagnose_post_menu(REQUEST, worker_thread_id=threading.get_ident())
    assert result.reason == "elapsed_limit" and not result.caret_click_attempted
    assert not page.clicks and not page.submissions
