"""Real Core/action/journal path with local DOM/wire doubles; no driver or network."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_target_read_probe import URL, Links, ReadHandle, session  # noqa: F401

from kusamushiri.core import XDeleterCore
from kusamushiri.models import PostActionTarget, RetryPolicy
from kusamushiri.progress import ActionJournal
from kusamushiri.retry import run_post_actions
from kusamushiri.view_cache import OVERLAYS

TARGET = PostActionTarget(URL, "post")
POLICY = RetryPolicy(max_retries=0, max_cooldowns=0)


class Control:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        # Production find_first_visible_locator returns .first: its count hides peers.
        return SimpleNamespace(count=lambda: 1, is_visible=self.is_visible,
                               element_handle=self.element_handle, click=self.click, wait_for=self.wait_for)

    def filter(self, **kwargs):
        return self

    def or_(self, other):
        return self

    def count(self):
        if "confirmationSheetConfirm" in self.selector:
            return self.page.confirm_count
        if "Dropdown" in self.selector and "menuitem" in self.selector:
            return self.page.delete_count
        return 1

    def element_handle(self):
        return self

    def is_visible(self):
        return True

    def wait_for(self, **kwargs):
        pass

    def click(self):
        self.page.clicks.append(self.selector)
        if "caret" in self.selector:
            self.page.menu_open = True
        elif "confirmationSheetConfirm" not in self.selector:
            self.page.menu_open = False
        self.page.click_hook(self.selector)
        if "confirmationSheetConfirm" in self.selector:
            self.page.submissions += 1
            request = SimpleNamespace(url="https://x.com/i/api/graphql/synthetic/DeleteTweet", method="POST",
                                      resource_type="fetch", failure=None)
            response = SimpleNamespace(url=request.url, request=request, status=200, headers={}, json=lambda: {})
            request.response = lambda: None if self.page.missing_response else response
            self.page.emit("request", request)
            if not self.page.missing_response:
                self.page.emit("response", response)


@pytest.fixture
def live_shape(session, monkeypatch):  # noqa: F811
    core, manager, page = session
    monkeypatch.setattr(core, "execute_post_action", XDeleterCore.execute_post_action.__get__(core))
    page.viewer = "author"
    page.main_frame = SimpleNamespace(page=page, url=URL)
    page.element = object()
    page.clicks, page.gotos = [], []
    page.submissions = 0
    page.menu_open, page.foreign_control = False, False
    page.confirm_count, page.menu_count, page.delete_count = 1, 1, 1
    page.controls = {}

    def control(selector):
        key = selector.replace(":visible", "")
        if "Dropdown" in selector and "menuitem" in selector:
            key = '[data-testid="Dropdown"] [role="menuitem"][data-testid="delete"]'
        if key not in page.controls:
            page.controls[key] = Control(page, key)
        return page.controls[key]
    page.missing_response = False
    page.click_hook = lambda _: None
    page.handlers = {name: [] for name in ("request", "response")}
    page.on = lambda name, callback: page.handlers[name].append(callback)
    page.remove_listener = lambda name, callback: page.handlers[name].remove(callback)
    page.emit = lambda name, value: [callback(value) for callback in tuple(page.handlers[name])]
    def evaluate(script, refs):
        if "refs.old.length" in script:
            return refs["old"] == refs["current"]
        if "refs.control" in script:
            return refs["control"] is refs["current"] and not page.foreign_control
        return refs["old"] is refs["current"]

    page.evaluate = evaluate
    page.wait_for_selector = lambda *args, **kwargs: None
    page.wait_for_function = lambda *args, **kwargs: None
    page.article.element_handle = lambda: page.element
    page.article.wait_for = lambda **kwargs: None
    old_article_locator = page.article.locator
    page.article.locator = lambda selector: (
        old_article_locator(selector) if "a[.//time]" in selector else control(selector)
    )
    old_locator = page.locator
    def locator(selector):
        if selector == OVERLAYS:
            return Links(["menu"] if page.menu_open else [])
        if 'role="dialog"' in selector:
            return SimpleNamespace(count=lambda: 0, element_handles=lambda: [])
        if selector == '[data-testid="Dropdown"]:visible':
            if not page.menu_open:
                return Links([])
            selected = control(selector)
            selected.count = lambda: page.menu_count
            return selected
        if "confirmationSheetConfirm" in selector or "Dropdown" in selector:
            return control(selector)
        return old_locator(selector)

    page.locator = locator

    def goto(url, **kwargs):
        page.gotos.append(url)
        page.url, page.main_frame.url = url, url
        page.element = object()
        return None

    page.goto = goto
    return core, manager, page


def execute(handles, path, *, current=True):
    core, _, _ = handles
    with core.verified_single_post_scope(TARGET, "author", current_view_only=current):
        return run_post_actions(core, [TARGET], 0, POLICY, journal=ActionJournal(path, "synthetic"))


def test_existing_view_real_helpers_menu_submit_receipt_and_no_goto(live_shape, tmp_path):
    core, manager, page = live_shape
    results = execute(live_shape, tmp_path / "receipt.json")
    assert results[0].success and page.submissions == 1 and page.gotos == []
    assert len(page.clicks) == 3 and page.handlers == {"request": [], "response": []}
    assert core._verified_post_action is None and core._pending_view is None
    assert manager.post_actions_active is False


@pytest.mark.parametrize("change", ["viewer", "target", "element", "frame", "page", "cancel", "hold", "stale"])
def test_change_after_menu_prevents_confirmation_and_is_held(live_shape, tmp_path, change):
    core, manager, page = live_shape
    current = [True]

    def changed(_):
        if change == "viewer":
            page.viewer = "other"
        elif change == "target":
            page.url = "https://x.com/author/status/999"
        elif change == "element":
            page.element = object()
        elif change == "frame":
            page.main_frame = SimpleNamespace(page=page, url=URL)
        elif change == "page":
            manager.page = ReadHandle()
        elif change == "cancel":
            core.request_cancel()
        elif change == "hold":
            manager.action_control_failed = True
        else:
            current[0] = False

    page.click_hook = changed
    path = tmp_path / "receipt.json"
    with core.verified_single_post_scope(TARGET, "author", current_view_only=True, is_current=lambda: current[0]):
        results = run_post_actions(core, [TARGET], 0, POLICY, journal=ActionJournal(path, "synthetic"))
    expected = "retryable" if change == "cancel" else "blocked"
    assert not results[0].success and results[0].failure_kind == expected
    assert page.submissions == 0 and len(page.clicks) == 1 and not page.gotos
    with ActionJournal(path, "synthetic") as journal:
        previous = journal.previous_result(TARGET)
        assert previous is None if change == "cancel" else previous.failure_kind == "blocked"


def test_cli_style_one_initial_goto_then_same_view_checks(live_shape, tmp_path):
    _, _, page = live_shape
    page.url = "https://x.com/home"
    results = execute(live_shape, tmp_path / "receipt.json", current=False)
    assert results[0].success and page.gotos == [URL] and page.submissions == 1


def test_ambiguous_result_is_not_replayed_even_after_new_scope(live_shape, tmp_path):
    core, _, page = live_shape
    page.missing_response = True
    path = tmp_path / "receipt.json"
    results = execute(live_shape, path)
    assert results[0].failure_kind == "ambiguous" and page.submissions == 1
    core._cancel_event.clear()  # Synthetic new command; the durable receipt must still stop it.
    page.missing_response = False
    held = execute(live_shape, path)
    assert held[0].failure_kind == "ambiguous" and held[0].attempts == 0
    assert page.submissions == 1 and not page.gotos


def test_inflight_receipt_and_duplicate_attempt_never_submit(live_shape, tmp_path):
    core, _, page = live_shape
    path = tmp_path / "receipt.json"
    with ActionJournal(path, "synthetic") as journal:
        journal.bind_viewer("author")
        journal.record(TARGET, "inflight")
    result = execute(live_shape, path)[0]
    assert result.failure_kind == "ambiguous" and result.attempts == 0 and page.submissions == 0
    core._cancel_event.clear()
    with core.verified_single_post_scope(TARGET, "author", current_view_only=True):
        first = core.execute_post_action(TARGET)
        second = core.execute_post_action(TARGET)
    assert first.success and second.failure_kind == "blocked" and page.submissions == 1


@pytest.mark.parametrize("case", ["viewer", "wrong_author", "wrong_target", "held", "active", "thread"])
def test_preflight_failure_is_unsent_without_navigation(live_shape, tmp_path, case):
    core, manager, page = live_shape
    viewer, target, kwargs = "author", TARGET, {}
    if case == "viewer":
        page.viewer = "other"
    elif case == "wrong_author":
        viewer = "other"
    elif case == "wrong_target":
        target = replace(TARGET, url="https://x.com/author/status/999")
    elif case == "held":
        manager.action_control_failed = True
    elif case == "active":
        manager.post_actions_active = True
    else:
        kwargs["worker_thread_id"] = -1
    with pytest.raises(RuntimeError), core.verified_single_post_scope(target, viewer, current_view_only=True, **kwargs):
        pytest.fail("unsafe preflight passed")
    assert not page.gotos and not page.clicks and core._verified_post_action is None


@pytest.mark.parametrize("case", ["foreign_menu", "duplicate_menu", "duplicate_item", "duplicate_confirm", "replacement"])
def test_same_article_cannot_authorize_changed_controls(live_shape, tmp_path, case):
    core, _, page = live_shape

    def change(selector):
        if "caret" in selector and case == "foreign_menu":
            page.foreign_control = True
        elif "caret" in selector and case == "duplicate_menu":
            page.menu_count = 2
        elif "caret" in selector and case == "duplicate_item":
            page.delete_count = 2
        elif "caret" not in selector and case == "duplicate_confirm":
            page.confirm_count = 2

    page.click_hook = change
    if case == "replacement":
        evaluate = page.evaluate
        replaced = False

        def replace_confirm(script, refs):
            nonlocal replaced
            valid = evaluate(script, refs)
            if refs.get("stage") == "confirm" and not replaced:
                replaced = True
                replacement = Control(page, '[data-testid="confirmationSheetConfirm"]')
                page.controls['[data-testid="confirmationSheetConfirm"]'] = replacement
                refs["control"].element_handle = lambda: replacement
            return valid

        page.evaluate = replace_confirm
    path = tmp_path / "receipt.json"
    result = execute(live_shape, path)[0]
    assert not result.success and result.failure_kind == ("ambiguous" if case == "replacement" else "blocked")
    assert page.submissions == 0 and page.gotos == []
    with ActionJournal(path, "synthetic") as journal:
        assert journal.previous_result(TARGET).failure_kind == result.failure_kind
    assert core._verified_post_action is None


@pytest.mark.parametrize("held", [False, True])
def test_guarded_worker_real_path_preserves_controls(live_shape, tmp_path, monkeypatch, qtbot, held):
    from kusamushiri.app import XDeleterWorker
    from kusamushiri.models import ExecuteActionsRequest

    core, manager, page = live_shape
    manager.profile_dir = tmp_path / "chromium-profile-author"
    manager.profile_dir.mkdir()
    monkeypatch.setattr(core, "stop_browser", lambda: None)
    # The existing session fixture forbids clear_cancel; any accidental call fails the test.
    core.rate_limit_status = 429 if held else None
    worker = XDeleterWorker(core=core)
    results, errors = [], []
    worker.events.delete_completed.connect(results.append)
    worker.events.error_occurred.connect(errors.append)
    try:
        worker.enqueue_delete_posts(ExecuteActionsRequest([TARGET], 0, expected_viewer="author", current_view_only=True))
        qtbot.waitUntil(lambda: bool(results or errors), timeout=2000)
        if held:
            assert errors and not page.clicks and core.rate_limit_status == 429
        else:
            assert not errors and results[0][0].success and page.submissions == 1
            assert core._post_action_policy.max_retries == core._post_action_policy.max_cooldowns == 0
        assert page.gotos == []
    finally:
        worker.shutdown()


def test_control_stop_diagnostic_is_closed_and_retains_specific_message(live_shape, tmp_path):
    from kusamushiri.network_diagnostics import sanitize_event

    core, manager, page = live_shape
    records = []
    manager.diagnostics._sink = records.append
    page.click_hook = lambda _: setattr(page, "delete_count", 2)
    result = execute(live_shape, tmp_path / "receipt.json")[0]
    assert result.failure_kind == "blocked" and not page.submissions
    assert "メニューまたは確認ボタン" in result.error_message
    stopped = [event for event in records if event["event"] == "verification_stop"]
    assert len(stopped) == 1
    assert stopped[0]["verification_stage"] == "delete_menu"
    assert stopped[0]["verification_reason"] == "control_count"
    clean = sanitize_event({"schema": 1, "trace": "012345abcdef", "event": "verification_stop",
                            "verification_stage": "secret-canary", "verification_reason": "secret-canary",
                            "url": "secret-canary", "body": "secret-canary", "exception": "secret-canary"})
    assert clean == {"schema": 1, "trace": "012345abcdef", "event": "verification_stop"}
