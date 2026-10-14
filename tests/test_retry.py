"""Simulated item-34 failures and fake-clock waits; never contacts X."""

import json
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import format_datetime
from unittest.mock import Mock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from kusamushiri import actions
from kusamushiri.core import XDeleterCore
from kusamushiri.models import PostActionResult, PostActionTarget, RetryPolicy
from kusamushiri.progress import ActionJournal
from kusamushiri.retry import ActionBatchError, parse_retry_after, run_post_actions


def target(number=1, kind="post"):
    return PostActionTarget(f"https://x.com/tester/status/{number}", kind)


class FakeCore:
    def __init__(self, monkeypatch):
        self.clock = 1000.0
        monkeypatch.setattr("kusamushiri.retry.time.time", lambda: self.clock)
        self.cancelled = False
        self.rate_limit_status = None
        self.rate_limit_retry_after = None
        self.calls = []
        self.waits = []
        self.behavior = lambda item: PostActionResult(item, "delete", True, None)
        self.cancel_on_wait = False

    @contextmanager
    def post_action_retry_scope(self):
        yield

    def execute_post_action(self, item):
        self.calls.append(item)
        return self.behavior(item)

    def is_cancel_requested(self):
        return self.cancelled

    def request_cancel(self):
        self.cancelled = True

    def clear_rate_limit(self):
        assert not self.cancelled  # A limit never clears a user cancellation.
        self.rate_limit_status = None
        self.rate_limit_retry_after = None

    def wait_for_cancel(self, seconds):
        self.waits.append((len(self.calls), seconds))
        if self.cancel_on_wait:
            self.clock += min(5, seconds)
            self.cancelled = True
            return True
        self.clock += seconds
        return self.cancelled


@pytest.mark.parametrize(
    "value, expected",
    [("120", 120), (" 0 ", 0), ("-1", None), ("nan", None), ("inf", None), ("1.5", None), ("bad", None), (None, None)],
)
def test_retry_after_seconds(value, expected):
    assert parse_retry_after(value, now=1000) == expected


def test_retry_after_http_date_and_past_date():
    value = format_datetime(datetime.fromtimestamp(1123, UTC), usegmt=True)
    assert parse_retry_after(value, now=1000) == 123
    assert parse_retry_after(value, now=1200) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_retries": -1},
        {"max_retries": 21},
        {"max_retries": True},
        {"initial_delay_seconds": 0},
        {"max_wait_seconds": float("inf")},
        {"max_delay_seconds": 1},
        {"batch_size": -1},
        {"batch_pause_seconds": float("nan")},
    ],
)
def test_invalid_policy(overrides):
    with pytest.raises(ValueError):
        replace(RetryPolicy(), **overrides).validate()


def test_proactive_pause_after_33_confirmed_successes(monkeypatch):
    core = FakeCore(monkeypatch)
    results = run_post_actions(
        core, [target(i) for i in range(1, 36)], 0, RetryPolicy(batch_size=33, batch_pause_seconds=60)
    )
    assert all(r.success for r in results)
    assert core.waits == [(33, 60)]
    assert len(core.calls) == 35


def test_item_34_failure_recovers_with_exponential_waits(monkeypatch):
    core = FakeCore(monkeypatch)
    tries = Counter()

    def fail_twice(item):
        tries[item.url] += 1
        if item == target(34) and tries[item.url] <= 2:
            core.rate_limit_status = 429
            return PostActionResult(item, "delete", False, "HTTP 429", "retryable")
        return PostActionResult(item, "delete", True, None)

    core.behavior = fail_twice
    results = run_post_actions(
        core, [target(i) for i in range(1, 36)], 0, RetryPolicy(batch_size=33, batch_pause_seconds=60)
    )
    assert len(results) == 35 and all(r.success for r in results)
    assert results[33].attempts == 3
    assert tries[target(34).url] == 3
    assert all(tries[target(i).url] == 1 for i in range(1, 34))
    assert core.waits == [(33, 60), (34, 120), (35, 240), (36, 5)]


def test_background_429_after_item_34_success_never_repeats_it(monkeypatch):
    core = FakeCore(monkeypatch)

    def success_with_limit(item):
        if item == target(34):
            core.rate_limit_status = 429
            core.rate_limit_retry_after = 1500
        return PostActionResult(item, "delete", True, None)

    core.behavior = success_with_limit
    results = run_post_actions(
        core, [target(i) for i in range(1, 36)], 0, RetryPolicy(batch_size=33, batch_pause_seconds=60)
    )
    assert all(r.success for r in results)
    assert core.calls.count(target(34)) == 1
    # Retry-After exceeds the 900s backoff cap and is still honored.
    assert core.waits == [(33, 60), (34, 1500)]


def test_success_counter_ignores_retries_and_duplicate_rows(monkeypatch):
    core = FakeCore(monkeypatch)

    def once(item):
        if len(core.calls) == 1:
            return PostActionResult(item, "delete", False, "pre-submit timeout", "retryable")
        return PostActionResult(item, "delete", True, None)

    core.behavior = once
    run_post_actions(core, [target(1), target(1), target(2), target(3)], 0, RetryPolicy(batch_size=2))
    assert core.waits == [(1, 60), (3, 120)]
    assert core.calls == [target(1), target(1), target(2), target(3)]


def test_exhausted_safe_retries_stop_before_next_item(monkeypatch):
    core = FakeCore(monkeypatch)
    core.behavior = lambda item: PostActionResult(item, "delete", False, "timeout", "retryable")
    results = run_post_actions(core, [target(), target(2)], 0, RetryPolicy())
    assert len(core.calls) == 4 and len(results) == 1
    assert core.waits == [(1, 60), (2, 120), (3, 240)]
    assert core.cancelled


def test_recurring_background_limits_have_batch_wide_budget(monkeypatch):
    core = FakeCore(monkeypatch)

    def limited_success(item):
        core.rate_limit_status = 429
        return PostActionResult(item, "delete", True, None)

    core.behavior = limited_success
    results = run_post_actions(core, [target(i) for i in range(1, 10)], 0, RetryPolicy(batch_size=0))
    assert len(core.calls) == len(results) == 4
    assert core.waits == [(1, 120), (2, 240), (3, 480)]


def test_server_wait_over_budget_stops_without_shortening(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    core.rate_limit_status = 429
    core.rate_limit_retry_after = 2000
    journal = ActionJournal(tmp_path / "receipts.json", "test")
    results = run_post_actions(core, [target()], 0, RetryPolicy(), journal=journal)
    assert results == [] and core.calls == [] and core.waits == []
    # The refused wait must survive an immediate resume with changed settings.
    assert json.loads(journal.path.read_text())["cooldown_until"] >= 3000


@pytest.mark.parametrize("kind", ["auth", "permanent", "ambiguous"])
def test_unsafe_failures_never_retry_or_continue(monkeypatch, tmp_path, kind):
    core = FakeCore(monkeypatch)
    core.behavior = lambda item: PostActionResult(item, "delete", False, kind, kind)
    journal = ActionJournal(tmp_path / "receipts.json", "test")
    results = run_post_actions(core, [target(), target(2)], 0, RetryPolicy(), journal=journal)
    assert len(results) == len(core.calls) == 1 and core.waits == []
    core.cancelled = False
    run_post_actions(core, [target(), target(2)], 0, RetryPolicy(), journal=journal)
    assert len(core.calls) == 1  # Including a manual resume of the same item.


@pytest.mark.parametrize("proactive", [False, True])
def test_cancel_wait_and_resume_preserve_successes_and_deadline(monkeypatch, tmp_path, proactive):
    core = FakeCore(monkeypatch)
    journal = ActionJournal(tmp_path / "receipts.json", "test")
    core.cancel_on_wait = True
    if not proactive:

        def limited(item):
            if item == target(1):
                core.rate_limit_status = 429
                core.rate_limit_retry_after = 120
            return PostActionResult(item, "delete", True, None)

        core.behavior = limited
    policy = RetryPolicy(batch_size=1 if proactive else 0)
    results = run_post_actions(core, [target(1), target(2)], 0, policy, journal=journal)
    assert len(results) == 1 and core.calls == [target(1)]
    deadline = json.loads(journal.path.read_text())["cooldown_until"]
    assert deadline > core.clock
    core.cancelled = core.cancel_on_wait = False
    core.rate_limit_status = None
    core.behavior = lambda item: PostActionResult(item, "delete", True, None)
    run_post_actions(core, [target(1), target(2)], 0, policy, journal=journal)
    assert core.calls == [target(1), target(2)]
    assert core.waits[1][1] == deadline - 1005


def test_crash_inflight_is_ambiguous_on_resume(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    journal = ActionJournal(tmp_path / "receipts.json", "test")
    core.behavior = Mock(side_effect=RuntimeError("connection vanished after submission"))
    with pytest.raises(RuntimeError):
        run_post_actions(core, [target()], 0, RetryPolicy(), journal=journal)
    assert not journal._lock_path.exists()
    core.cancelled = False  # A new, explicitly resumed job clears cancellation.
    results = run_post_actions(core, [target()], 0, RetryPolicy(), journal=journal)
    assert results[0].failure_kind == "ambiguous"
    assert core.calls == [target()]


def test_journal_rejects_corruption_profile_mismatch_and_concurrent_job(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    path = tmp_path / "receipts.json"
    path.write_text("broken json")
    with pytest.raises(ActionBatchError) as error:
        run_post_actions(core, [target()], 0, RetryPolicy(), journal=ActionJournal(path, "test"))
    assert error.value.summary["stop_reason"] == "storage_error"
    path.write_text(json.dumps({"version": 1, "scope": "other", "entries": {}}))
    with pytest.raises(ActionBatchError):
        run_post_actions(core, [target()], 0, RetryPolicy(), journal=ActionJournal(path, "test"))
    path.unlink()
    with ActionJournal(path, "test"), pytest.raises(RuntimeError):
        run_post_actions(core, [target()], 0, RetryPolicy(), journal=ActionJournal(path, "test"))
    assert core.calls == []


def test_legacy_confirmed_deletions_seed_resume_without_new_action(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    journal = ActionJournal(tmp_path / "receipts.json", "test")
    results = run_post_actions(
        core, [target(1), target(2)], 0, RetryPolicy(), journal=journal, known_successes={"post:1"}
    )
    assert core.calls == [target(2)] and all(r.success for r in results)
    assert journal.completed_since_pause == 1


def test_rate_limit_scope_defers_stop_but_preserves_user_cancel():
    core = XDeleterCore()
    core.on_rate_limited = Mock()
    response = Mock(
        url="https://x.com/i/api/graphql/fixture/ViewerBadgeCounts", status=429, headers={"retry-after": "120"}
    )
    with core.post_action_retry_scope():
        core._browser._watch_response(response)
        assert not core.is_cancel_requested()
        assert core.rate_limit_status == 429 and core.rate_limit_retry_after == 120
        core.on_rate_limited.assert_not_called()
        core.request_cancel()
        core.clear_rate_limit()
        assert core.is_cancel_requested()
    core.clear_cancel()
    core._browser._watch_response(response)
    assert core.is_cancel_requested()


def test_wait_for_cancel_wakes_immediately_without_network():
    core = XDeleterCore()
    waiting = threading.Event()
    finished = threading.Event()

    def wait():
        waiting.set()
        assert core.wait_for_cancel(3600)
        finished.set()

    thread = threading.Thread(target=wait)
    thread.start()
    assert waiting.wait(1)
    core.request_cancel()
    assert finished.wait(1)
    thread.join(1)


class ActionPage:
    """Playwright-shaped local double; action functions execute all their real stages."""

    def __init__(self, *, status=200, body=None, navigation_status=None, request_missing=False, timeout=False):
        self.handlers = {"request": [], "response": []}
        self.response = Mock(
            url="https://x.com/i/api/graphql/fixture/DeleteTweet", status=status, headers={"retry-after": "120"}
        )
        self.response.request = Mock(url=self.response.url, method="POST")
        self.response.json.return_value = body or {}
        self.navigation_status = navigation_status
        self.request_missing = request_missing
        self.timeout = timeout
        self.clicks = []

    def is_closed(self):
        return False

    def wait_for_timeout(self, milliseconds):
        pass

    def on(self, event, handler):
        self.handlers[event].append(handler)

    def remove_listener(self, event, handler):
        self.handlers[event].remove(handler)

    def emit(self, event, value):
        for handler in list(self.handlers[event]):
            handler(value)

    def goto(self, url, *, timeout):
        if self.navigation_status is not None:
            response = Mock(url=url, status=self.navigation_status, headers={"retry-after": "120"})
            self.emit("response", response)
            return response
        return None

    def locator(self, selector):
        return ActionLocator(self, selector)

    def wait_for_selector(self, selector, **kwargs):
        pass

    def wait_for_function(self, expression, **kwargs):
        if self.timeout:
            raise PlaywrightTimeoutError("DOM never confirmed completion")


class ActionLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def locator(self, selector):
        return ActionLocator(self.page, selector)

    def element_handle(self):
        return self

    def wait_for(self, **kwargs):
        pass

    def click(self):
        self.page.clicks.append(self.selector)
        if (
            "confirmationSheetConfirm" in self.selector
            or "unlike" in self.selector
            or "unretweetConfirm" in self.selector
        ):
            request = Mock(url=self.page.response.url, method="POST", failure="response lost")
            request.response.return_value = None if self.page.request_missing else self.page.response
            self.page.emit("request", request)
            if not self.page.request_missing:
                self.page.emit("response", self.page.response)


@pytest.mark.parametrize(
    "status, body, missing, expected",
    [
        (429, {}, False, "retryable"),
        (420, {}, False, "retryable"),
        (401, {}, False, "auth"),
        (403, {}, False, "blocked"),
        (404, {}, False, "permanent"),
        (500, {}, False, "ambiguous"),
        (503, {}, False, "ambiguous"),
        (200, {}, False, "ambiguous"),
        (200, {}, True, "ambiguous"),
        (200, {"errors": [{"code": 88, "message": "Rate limit"}]}, False, "retryable"),
        (200, {"errors": [{"code": 89, "message": "Session invalid"}]}, False, "auth"),
        (200, {"errors": [{"code": 144, "message": "Not found"}]}, False, "permanent"),
    ],
)
def test_real_delete_stages_classify_refusal_vs_ambiguous_completion(status, body, missing, expected):
    page = ActionPage(status=status, body=body, request_missing=missing, timeout=True)
    result = actions.execute_post_action(page, target(), lambda root, selectors: root.locator(selectors[0]))
    assert not result.success and result.failure_kind == expected
    if expected == "retryable":
        assert result.retry_after_seconds == 120
    assert page.handlers == {"request": [], "response": []}


@pytest.mark.parametrize("status, expected", [(429, "retryable"), (401, "auth"), (403, "blocked"), (404, "permanent")])
def test_refusal_before_submission_never_clicks_confirm(status, expected):
    page = ActionPage(navigation_status=status)
    result = actions.execute_post_action(page, target(), lambda root, selectors: root.locator(selectors[0]))
    assert not result.success and result.failure_kind == expected
    assert not any("confirmationSheetConfirm" in s for s in page.clicks)


def test_cancel_before_submission_keeps_resume_safe():
    page = ActionPage()
    result = actions.execute_post_action(
        page, target(), lambda root, selectors: root.locator(selectors[0]), should_cancel=lambda: True
    )
    assert result.failure_kind == "retryable" and not result.success
    assert not any("confirmationSheetConfirm" in s for s in page.clicks)


def test_recipe_roundtrips_custom_retry_policy(tmp_path):
    import tomllib

    from kusamushiri.recipe import format_recipe, parse_recipe

    data = {
        "delete": {
            "max_retries": 4,
            "retry_delay": 75,
            "retry_max_delay": 600,
            "retry_max_wait": 2000,
            "batch_size": 25,
            "batch_pause": 90,
        }
    }
    recipe = parse_recipe(data, tmp_path / "recipe.toml")
    assert recipe.delete.retry_policy == RetryPolicy(
        max_retries=4,
        initial_delay_seconds=75,
        max_delay_seconds=600,
        max_wait_seconds=2000,
        batch_size=25,
        batch_pause_seconds=90,
    )
    assert parse_recipe(tomllib.loads(format_recipe(recipe)), recipe.path) == recipe


@pytest.mark.parametrize(
    "settings",
    [
        {"max_retries": True},
        {"retry_delay": float("nan")},
        {"retry_delay": 100, "retry_max_delay": 50},
        {"batch_size": -1},
    ],
)
def test_recipe_rejects_invalid_retry_configuration(tmp_path, settings):
    from kusamushiri.recipe import RecipeError, parse_recipe

    with pytest.raises(RecipeError):
        parse_recipe({"delete": settings}, tmp_path / "recipe.toml")


def test_gui_retry_settings_persist_per_profile(tmp_path):
    from PySide6.QtCore import QSettings

    from kusamushiri.settings import AccountSettings, AccountSettingsManager

    manager = AccountSettingsManager(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    settings = AccountSettings(
        action_max_retries=2,
        action_retry_delay_seconds=75,
        action_retry_max_delay_seconds=600,
        action_retry_max_wait_seconds=2000,
        action_batch_size=25,
        action_batch_pause_seconds=90,
    )
    manager.save_account_settings("test", settings)
    assert manager.load_account_settings("test") == settings
    assert manager.load_account_settings("other") == AccountSettings()


def test_dom_only_completion_without_observed_mutation_is_held(monkeypatch):
    page = ActionPage()
    real_click = ActionLocator.click

    def no_mutation(self):
        if "confirmationSheetConfirm" in self.selector:
            self.page.clicks.append(self.selector)
        else:
            real_click(self)

    monkeypatch.setattr(ActionLocator, "click", no_mutation)
    result = actions.execute_post_action(page, target(), lambda root, selectors: root.locator(selectors[0]))
    assert not result.success and result.failure_kind == "ambiguous"


@pytest.mark.parametrize(
    "path, role, label",
    [
        ("/i/api/graphql/hash/DeleteTweet", "mutation", "DeleteTweet"),
        ("/i/api/graphql/hash/DeleteRetweet", "mutation", "DeleteRetweet"),
        ("/i/api/graphql/hash/UnfavoriteTweet", "mutation", "UnfavoriteTweet"),
        ("/i/api/graphql/hash/TweetDetail", "target", "TweetDetail"),
        ("/tester/status/34", "target", "target-document"),
        ("/i/api/fleetline", "ancillary", "fleetline"),
        ("/i/api/graphql/hash/ViewerBadgeCounts", "ancillary", "ViewerBadgeCounts"),
        ("/new/api/secret-123456789", "unknown", "unclassified"),
    ],
)
def test_rate_labels_are_conservative_roles_not_quota_buckets(path, role, label):
    from kusamushiri.rate_limits import classify_rate_limit

    assert classify_rate_limit("https://x.com" + path + "?token=synthetic") == (role, label)


def test_new_defaults_are_consistent_and_more_conservative():
    from kusamushiri.models import DEFAULT_ACTION_INTERVAL_SECONDS
    from kusamushiri.settings import AccountSettings

    policy = RetryPolicy()
    settings = AccountSettings()
    assert DEFAULT_ACTION_INTERVAL_SECONDS == settings.action_interval_seconds == 3
    assert policy.batch_size == settings.action_batch_size == 25
    assert policy.batch_pause_seconds == settings.action_batch_pause_seconds == 120
    assert policy.max_cooldowns == settings.action_max_cooldowns == 3
    assert policy.cooldown_delay_seconds == settings.action_cooldown_delay_seconds == 120
    assert policy.rate_limit_interval_seconds == settings.action_rate_limit_interval_seconds == 5


def test_unsent_ancillary_deferral_does_not_consume_action_retry_or_failure(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    journal = ActionJournal(tmp_path / "receipts.json", "test")
    callbacks = []

    def defer_once(item):
        if len(core.calls) == 1:
            return PostActionResult(
                item,
                "delete",
                False,
                "unsubmitted",
                "retryable",
                deferred=True,
                rate_limit_scope="ancillary",
                rate_limit_endpoint="fleetline",
            )
        return PostActionResult(item, "delete", True, None)

    core.behavior = defer_once
    results = run_post_actions(
        core, [target()], 0, RetryPolicy(max_retries=0, max_cooldowns=1), journal=journal, on_result=callbacks.append
    )
    assert len(results) == len(callbacks) == 1 and results[0].success and results[0].attempts == 1
    assert core.waits == [(1, 120)]
    assert journal.entries == {"post:1": "success"}


def test_repeated_unsent_429_is_unprocessed_on_cooldown_exhaustion(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    journal = ActionJournal(tmp_path / "receipts.json", "test")
    callbacks = []
    core.behavior = lambda item: PostActionResult(
        item,
        "delete",
        False,
        "unsubmitted",
        "retryable",
        deferred=True,
        rate_limit_scope="ancillary",
        rate_limit_endpoint="fleetline",
    )
    results = run_post_actions(
        core,
        [target(), target(2)],
        0,
        RetryPolicy(max_retries=9, max_cooldowns=2),
        journal=journal,
        on_result=callbacks.append,
    )
    assert results == callbacks == []
    assert len(core.calls) == 3 and core.waits == [(1, 120), (2, 240)]
    assert core.cancelled and journal.entries == {"post:1": "pending"}
    assert journal.cooldown_until > core.clock and "ancillary" in journal.cooldown_reason


class QuietContext:
    def __init__(self, clock):
        self.offline = False
        self.events = []
        self.clock = clock

    def set_offline(self, offline):
        self.offline = offline
        self.events.append((self.clock.clock, offline))


def make_quiet_core(monkeypatch):
    clock = FakeCore(monkeypatch)
    core = XDeleterCore()
    context = QuietContext(clock)
    core._browser.context = context

    def wait(seconds):
        cancelled = clock.wait_for_cancel(seconds)
        if cancelled:
            core.request_cancel()
        return cancelled or core.is_cancel_requested()

    core.wait_for_action_cancel = wait
    core._find_first_visible_locator = lambda owner, selectors: owner.locator(selectors[0])
    return core, context, clock


@pytest.mark.parametrize(
    "endpoint, role",
    [
        ("fleetline", "ancillary"),
        ("ViewerBadgeCounts", "ancillary"),
        ("TweetDetail", "target"),
        ("future-unknown", "unknown"),
    ],
)
def test_real_action_stages_defer_then_resume_only_after_quiet_wait(monkeypatch, tmp_path, endpoint, role):
    core, context, clock = make_quiet_core(monkeypatch)
    journal = ActionJournal(tmp_path / "receipts.json", "test")
    pages = []
    execute = core.execute_post_action

    def action(item):
        assert not context.offline  # No target load while the network deadline is active.
        page = ActionPage()
        page.on("response", core._browser._watch_response)
        core._browser.page = page
        pages.append(page)
        if len(pages) == 1:

            def goto(url, *, timeout):
                page.emit(
                    "response", Mock(url=f"https://x.com/i/api/{endpoint}", status=429, headers={"retry-after": "180"})
                )

            page.goto = goto
        return execute(item)

    core.execute_post_action = action
    statuses = []
    results = run_post_actions(
        core, [target()], 0, RetryPolicy(max_retries=0, max_cooldowns=1), journal=journal, on_status=statuses.append
    )
    assert len(results) == 1 and results[0].success and results[0].attempts == 1
    assert not any("confirmationSheetConfirm" in click for click in pages[0].clicks)
    assert any("confirmationSheetConfirm" in click for click in pages[1].clicks)
    assert clock.waits == [(0, 180)]
    assert context.events == [(1000, True), (1180, False)]
    assert any(role in message and "期限" in message for message in statuses)


def test_reproduced_33_successes_ancillary_429_never_marks_34_failed(monkeypatch, tmp_path):
    core, context, clock = make_quiet_core(monkeypatch)
    journal = ActionJournal(tmp_path / "receipts.json", "test")
    pages = []
    execute = core.execute_post_action

    def action(item):
        page = ActionPage()
        page.on("response", core._browser._watch_response)
        core._browser.page = page
        pages.append(page)
        if item == target(34):
            page.goto = lambda url, **kwargs: page.emit(
                "response", Mock(url="https://x.com/i/api/fleetline", status=429, headers={})
            )
        return execute(item)

    core.execute_post_action = action
    policy = RetryPolicy(batch_size=33, batch_pause_seconds=60, max_cooldowns=3)
    results = run_post_actions(core, [target(i) for i in range(1, 36)], 0, policy, journal=journal)
    assert len(results) == 33 and all(result.success for result in results)
    assert len(pages) == 37  # Four safe target loads; no DeleteTweet for item34.
    assert sum("confirmationSheetConfirm" in click for page in pages for click in page.clicks) == 33
    assert journal.entries["post:34"] == "pending" and journal.cooldown_until > clock.clock
    assert context.offline
    assert [seconds for _, seconds in clock.waits] == [60, 120, 240, 480]


def test_actual_mutation_429_still_consumes_action_retry_and_respects_header(monkeypatch, tmp_path):
    core, context, clock = make_quiet_core(monkeypatch)
    journal = ActionJournal(tmp_path / "receipts.json", "test")
    pages = []
    execute = core.execute_post_action

    def action(item):
        page = ActionPage(status=429 if not pages else 200, timeout=not pages)
        page.on("response", core._browser._watch_response)
        core._browser.page = page
        pages.append(page)
        return execute(item)

    core.execute_post_action = action
    results = run_post_actions(core, [target()], 0, RetryPolicy(max_retries=1, max_cooldowns=1), journal=journal)
    assert len(results) == 1 and results[0].success and results[0].attempts == 2
    assert len(pages) == 2 and all(any("confirmationSheetConfirm" in click for click in page.clicks) for page in pages)
    assert clock.waits == [(0, 120)] and not context.offline


def test_queued_header_extends_wait_before_network_is_enabled(monkeypatch, tmp_path):
    core, context, clock = make_quiet_core(monkeypatch)
    page = ActionPage()
    core._browser.page = page
    page.on("response", core._browser._watch_response)
    with core.post_action_retry_scope():
        core._stop_on_rate_limit(Mock(url="https://x.com/i/api/fleetline", status=429, headers={"retry-after": "120"}))
    dispatched = []

    def drain(milliseconds):
        if context.offline and not dispatched:
            dispatched.append(True)
            page.emit("response", Mock(url="https://x.com/i/api/fleetline", status=429, headers={"retry-after": "600"}))

    page.wait_for_timeout = drain
    core.execute_post_action = lambda item: PostActionResult(item, "delete", True, None)
    journal = ActionJournal(tmp_path / "receipts.json", "test")
    results = run_post_actions(core, [target()], 0, RetryPolicy(), journal=journal)
    assert results[0].success and clock.waits == [(0, 600)]
    assert context.events == [(1000, True), (1600, False)]


def test_cancel_keeps_browser_offline_and_saved_deadline_blocks_restart(monkeypatch, tmp_path):
    from kusamushiri.browser import BrowserManager

    core, context, clock = make_quiet_core(monkeypatch)
    page = ActionPage()
    core._browser.page = page
    with core.post_action_retry_scope():
        core._stop_on_rate_limit(Mock(url="https://x.com/i/api/fleetline", status=429, headers={"retry-after": "300"}))
    clock.cancel_on_wait = True
    core.execute_post_action = Mock(side_effect=AssertionError("no mutation is allowed"))
    profile = tmp_path / "chromium-profile-test"
    journal = ActionJournal(profile / "action-progress.json", "test")
    assert run_post_actions(core, [target()], 0, RetryPolicy(), journal=journal) == []
    assert context.offline and journal.cooldown_until >= 1300
    with pytest.raises(RuntimeError):
        core._browser._require_page()
    launch = Mock(side_effect=AssertionError("no browser should start before the deadline"))
    monkeypatch.setattr("kusamushiri.browser.sync_playwright", launch)
    with pytest.raises(RuntimeError):
        BrowserManager().start(user_data_dir=profile)
    launch.assert_not_called()


def test_quiet_wait_pumps_events_and_cancels_within_a_tick(monkeypatch):
    core, _context, clock = make_quiet_core(monkeypatch)
    page = ActionPage()
    core._browser.page = page
    monkeypatch.setattr("kusamushiri.core.time.monotonic", lambda: clock.clock)

    def tick(milliseconds):
        clock.clock += milliseconds / 1000
        core.request_cancel()

    page.wait_for_timeout = tick
    assert XDeleterCore.wait_for_action_cancel(core, 120)
    assert clock.clock <= 1000.11


@pytest.mark.parametrize(
    "changes",
    [
        {"max_cooldowns": -1},
        {"max_cooldowns": 21},
        {"max_cooldowns": True},
        {"cooldown_delay_seconds": 0},
        {"rate_limit_interval_seconds": float("nan")},
        {"max_delay_seconds": 100},
    ],
)
def test_invalid_quiet_cooldown_settings(changes):
    with pytest.raises(ValueError):
        replace(RetryPolicy(), **changes).validate()


def test_real_refused_mutation_is_reported_when_cooldown_budget_is_zero(monkeypatch, tmp_path):
    core, context, _clock = make_quiet_core(monkeypatch)
    page = ActionPage(status=429, timeout=True)
    page.on("response", core._browser._watch_response)
    core._browser.page = page
    journal = ActionJournal(tmp_path / "receipts.json", "test")
    results = run_post_actions(core, [target(), target(2)], 0, RetryPolicy(max_cooldowns=0), journal=journal)
    assert len(results) == 1 and not results[0].success and not results[0].deferred
    assert journal.entries == {"post:1": "retryable"} and context.offline


def test_http200_graphql_code88_uses_rate_cooldown_not_generic_wait(monkeypatch, tmp_path):
    core, context, clock = make_quiet_core(monkeypatch)
    pages = []
    execute = core.execute_post_action

    def action(item):
        body = {"errors": [{"code": 88, "message": "Rate limit"}]} if not pages else {}
        page = ActionPage(body=body)
        page.response.headers = {}
        page.on("response", core._browser._watch_response)
        core._browser.page = page
        pages.append(page)
        return execute(item)

    core.execute_post_action = action
    journal = ActionJournal(tmp_path / "receipts.json", "test")
    results = run_post_actions(core, [target()], 0, RetryPolicy(max_retries=1, max_cooldowns=1), journal=journal)
    assert results[0].success and results[0].attempts == 2
    assert clock.waits == [(0, 120)] and not context.offline


def test_ancillary_http_date_and_past_date_waits_are_honored(monkeypatch, tmp_path):
    core, context, clock = make_quiet_core(monkeypatch)
    header = format_datetime(datetime.fromtimestamp(1420, UTC), usegmt=True)
    with core.post_action_retry_scope():
        core._stop_on_rate_limit(Mock(url="https://x.com/i/api/fleetline", status=429, headers={"retry-after": header}))
    core.execute_post_action = lambda item: PostActionResult(item, "delete", True, None)
    journal = ActionJournal(tmp_path / "receipts.json", "test")
    results = run_post_actions(core, [target()], 0, RetryPolicy(), journal=journal)
    assert results[0].success and clock.waits == [(0, 420)]
    assert context.events == [(1000, True), (1420, False)]


def test_after_rate_limit_item_spacing_has_configured_floor(monkeypatch):
    core = FakeCore(monkeypatch)

    def action(item):
        if len(core.calls) == 1:
            return PostActionResult(
                item,
                "delete",
                False,
                "pending",
                "retryable",
                deferred=True,
                rate_limit_scope="ancillary",
                rate_limit_endpoint="fleetline",
            )
        return PostActionResult(item, "delete", True, None)

    core.behavior = action
    results = run_post_actions(core, [target(), target(2)], 3, RetryPolicy(rate_limit_interval_seconds=7))
    assert len(results) == 2 and all(result.success for result in results)
    assert core.waits == [(1, 120), (2, 7)]
