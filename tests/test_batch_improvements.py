"""Privacy, finite stopping and reduced UI/navigation work with in-memory pages."""

import csv
import json
import logging
import tomllib
from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_ancillary_isolation import BADGES, Context
from test_network_diagnostics import Clock, Request, Response
from test_retry import ActionLocator, ActionPage, FakeCore, target

from kusamushiri import actions, cli
from kusamushiri.core import XDeleterCore
from kusamushiri.deleted_posts import DeletedPostStore
from kusamushiri.models import PostActionResult, RetryPolicy
from kusamushiri.network_diagnostics import NetworkDiagnostics, diagnostic_summary, sanitize_event
from kusamushiri.progress import ActionJournal
from kusamushiri.recipe import DELETE_KEYS, DeleteOptions, Recipe, format_recipe, parse_recipe
from kusamushiri.recipe_update import plan_recipe_update
from kusamushiri.retry import ActionBatchError, run_post_actions
from kusamushiri.view_cache import OVERLAYS

SECRET = "PRIVATE_BATCH_CANARY"


class ViewLocator(ActionLocator):
    def count(self):
        return int(self.page.overlay) if self.selector == OVERLAYS else self.page.target_count

    def is_visible(self):
        return self.page.target_visible

    def element_handle(self):
        return self.page.node


class ViewPage(ActionPage):
    def __init__(self):
        super().__init__()
        self.url = "about:blank"
        self.node = object()
        self.overlay = False
        self.target_count, self.target_visible = 1, True
        self.navigations = []
        self.on_navigate = lambda: None

    def locator(self, selector):
        return ViewLocator(self, selector)

    def goto(self, url, **kwargs):
        self.url = url
        self.node = object()
        self.navigations.append(url)
        self.on_navigate()

    def evaluate(self, script, refs):
        return refs["old"] is refs["current"] and self.target_visible


@pytest.fixture
def scenario(monkeypatch):
    clock, events = Clock(), []
    monkeypatch.setattr("kusamushiri.retry.time.time", clock.wall)
    core, page, context = XDeleterCore(), ViewPage(), Context()
    core.is_logged_in = lambda: True
    core.get_logged_in_username = lambda: "tester"
    core._action_viewer = "tester"
    trace = NetworkDiagnostics(sink=events.append, wall=clock.wall, monotonic=clock.monotonic)
    core._browser.diagnostics = trace
    core._browser.ancillary_isolation.diagnostics = trace
    core._browser.page, core._browser.context = page, context

    def request_event(request):
        if request.method == "POST":
            page.response.request = request
        core._browser._watch_request(request)

    page.on("request", request_event)
    page.on("response", core._browser._watch_response)

    def emit(url=BADGES, status=429):
        request = Request(clock, url)
        request.resource_type = "xhr"
        core._browser._watch_request(request)
        page.emit("response", Response(request, status=status, after="30"))

    return core, page, context, clock, events, emit


def test_known_unsent_retry_reuses_exact_idle_view_once(scenario, tmp_path):
    core, page, context, clock, events, emit = scenario
    page.on_navigate = lambda: emit() if len(page.navigations) == 1 else None
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock.tick(seconds)
        return False

    results = run_post_actions(core, [target()], 0, RetryPolicy(),
                               journal=ActionJournal(tmp_path / "receipt.json", "test"), wait_for_cancel=wait)
    assert results[0].success and len(page.navigations) == 1
    assert waits == [30.0]  # Per-wait mode uses the server deadline; fallback is only for missing headers.
    assert sum("confirmationSheetConfirm" in click for click in page.clicks) == 1
    assert sum(event["event"] == "view_reused" for event in events) == 1
    summary = core.last_action_batch_summary
    assert summary["deferrals"] == 1 and summary["cooldowns"] == 1 and summary["stop_reason"] == "completed"


@pytest.mark.parametrize("change", ["node", "url", "overlay", "hidden", "duplicate"])
def test_changed_view_stays_pending_without_reload_or_mutation(scenario, tmp_path, change):
    core, page, context, clock, events, emit = scenario
    page.on_navigate = emit

    def wait(seconds):
        clock.tick(seconds)
        if change == "node":
            page.node = object()
        elif change == "url":
            page.url = target(2).url
        elif change == "overlay":
            page.overlay = True
        elif change == "hidden":
            page.target_visible = False
        else:
            page.target_count = 2
        return False

    journal = ActionJournal(tmp_path / "receipt.json", "test")
    assert run_post_actions(core, [target()], 0, RetryPolicy(), journal=journal, wait_for_cancel=wait) == []
    assert len(page.navigations) == 1 and not page.clicks
    assert journal.entries[journal.key(target())] == "pending"
    assert core.last_action_batch_summary["stop_reason"] == "view_changed"


def test_unknown_429_keeps_finite_reload_limits_and_skips_reversible_menus(scenario, tmp_path):
    core, page, context, clock, events, emit = scenario
    page.on_navigate = lambda: emit("https://x.com/i/api/graphql/fixture/UserByScreenName")
    policy = RetryPolicy(cooldown_delay_seconds=30, max_delay_seconds=60, initial_delay_seconds=15, max_wait_seconds=300, wait_limit_mode="total")
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock.tick(seconds)
        return False

    assert run_post_actions(core, [target()], 0, policy, journal=ActionJournal(tmp_path / "receipt.json", "test"), wait_for_cancel=wait) == []
    assert waits == [30, 60, 60] and len(page.navigations) == 4 and not page.clicks
    assert not any(event["event"] == "view_reused" for event in events)
    assert core.last_action_batch_summary["stop_reason"] == "cooldown_limit"
    assert core.last_action_batch_summary["unprocessed"] == 1
    assert core.last_action_batch_summary["waited_seconds"] == 150
    info = diagnostic_summary(events)["rate_limits"][0]
    assert info["api_label"] == "UserByScreenName" and info["role"] == "unknown" and info["resource_type"] == "xhr"


def test_unknown_signal_invalidates_a_previously_cached_known_view(scenario):
    core, page, context, clock, events, emit = scenario
    page.on_navigate = emit
    with core.post_action_retry_scope():
        assert core.execute_post_action(target()).deferred and core._pending_view is not None
        emit("https://x.com/i/api/graphql/fixture/UnknownDependency")
        assert core._pending_view is None and core.rate_limit_scope == "unknown"


def test_global_mode_auth_invalidates_view_and_stops_before_new_mutation(scenario):
    core, page, context, clock, events, emit = scenario
    page.on_navigate = emit
    with core.post_action_retry_scope():
        assert core.execute_post_action(target()).deferred
        emit(status=403)
        assert core.is_cancel_requested() and core._pending_view is None
        result = core.execute_post_action(target())
        assert not result.success and not page.clicks


def test_nested_scopes_do_not_clear_outer_rate_limit_defer_flag(scenario):
    core, page, context, clock, events, emit = scenario
    with core.post_action_retry_scope():
        with core.post_action_retry_scope():
            assert core._defer_rate_limit_stop
        assert core._defer_rate_limit_stop and core._browser.post_actions_active
    assert not core._defer_rate_limit_stop and not core._browser.post_actions_active


def test_request_metadata_is_closed_and_groups_queries_without_leaking(scenario):
    core, page, context, clock, events, emit = scenario
    for query in ("one", "two"):
        request = Request(clock, f"https://x.com/i/api/graphql/hash/{SECRET}?auth={query}&body={SECRET}")
        request.resource_type = "xhr"
        core._browser.diagnostics.request(request)
        core._browser.diagnostics.response(Response(request))
    starts = [event for event in events if event["event"] == "request_start"]
    assert starts[0]["endpoint_group"] == starts[1]["endpoint_group"]
    assert starts[0]["api_label"] == "unrecognized" and starts[0]["request_family"] == "graphql"
    assert SECRET not in json.dumps(events) and SECRET not in json.dumps(diagnostic_summary(events))
    poisoned = dict(events[-1], resource_type=SECRET, request_family=SECRET, api_label=SECRET, endpoint_group=SECRET)
    assert SECRET not in json.dumps(sanitize_event(poisoned))
    assert SECRET not in json.dumps(diagnostic_summary([poisoned]))


@pytest.mark.parametrize("url,method", [("https://foreign.example/i/api/graphql/fixture/DeleteTweet", "POST"),
    ("https://x.com/i/api/graphql/fixture/DeleteTweet", "GET"),
    ("https://x.com/other/DeleteTweet", "POST"), ("http://x.com/i/api/graphql/fixture/DeleteTweet", "POST")])
def test_foreign_get_or_noncanonical_request_cannot_supply_mutation_receipt(url, method):
    assert not actions._is_mutation_request(Mock(url=url, method=method), "DeleteTweet")


@pytest.mark.parametrize("errors", [None, False, "bad", {"code": 88}, ["bad"], [None]])
def test_malformed_error_envelope_is_ambiguous_and_never_retried(errors):
    page = ActionPage(body={"errors": errors})
    result = actions.execute_post_action(page, target(), lambda owner, selectors: owner.locator(selectors[0]))
    assert not result.success and result.failure_kind == "ambiguous"
    assert sum("confirmationSheetConfirm" in click for click in page.clicks) == 1


def test_multiple_mutation_requests_are_ambiguous(monkeypatch):
    page = ActionPage()
    click = ActionLocator.click

    def twice(locator):
        click(locator)
        if "confirmationSheetConfirm" in locator.selector:
            click(locator)

    monkeypatch.setattr(ActionLocator, "click", twice)
    result = actions.execute_post_action(page, target(), lambda owner, selectors: owner.locator(selectors[0]))
    assert not result.success and result.failure_kind == "ambiguous"


@pytest.mark.parametrize("status", [199, 301, 302, 307])
def test_non_success_response_cannot_prove_graphql_refusal(status):
    page = ActionPage(status=status, body={"errors": [{"code": 88}]})
    result = actions.execute_post_action(page, target(), lambda owner, selectors: owner.locator(selectors[0]))
    assert not result.success and result.failure_kind == "ambiguous"
    assert result.rate_limit_scope != "mutation"


@pytest.mark.parametrize("code", [[88], {"value": 88}])
def test_unhashable_error_code_is_ambiguous_without_raising(code):
    page = ActionPage(body={"errors": [{"code": code}]})
    result = actions.execute_post_action(page, target(), lambda owner, selectors: owner.locator(selectors[0]))
    assert not result.success and result.failure_kind == "ambiguous"


def test_noncanonical_response_request_cannot_prove_graphql_refusal():
    page = ActionPage(body={"errors": [{"code": 88}]})
    page.response.request = Mock(url="https://x.com/other/DeleteTweet", method="POST")
    result = actions.execute_post_action(page, target(), lambda owner, selectors: owner.locator(selectors[0]))
    assert not result.success and result.failure_kind == "ambiguous"


def test_limit_observed_while_waiting_for_menu_stops_before_opening_it(monkeypatch):
    page = ActionPage()
    pending = False

    def wait_for(locator, **kwargs):
        nonlocal pending
        if "caret" in locator.selector:
            pending = True

    monkeypatch.setattr(ActionLocator, "wait_for", wait_for)
    result = actions.execute_post_action(page, target(), lambda owner, selectors: owner.locator(selectors[0]),
                                         rate_limit_pending=lambda: pending)
    assert result.deferred and not page.clicks


def test_header_deadline_is_not_restarted_after_reporting_delay(monkeypatch):
    core = FakeCore(monkeypatch)

    def perform(item):
        if item == target():
            core.rate_limit_status, core.rate_limit_retry_after = 429, 120
        return PostActionResult(item, "delete", True, None)

    core.behavior = perform
    results = run_post_actions(core, [target(), target(2)], 0, RetryPolicy(cooldown_delay_seconds=30),
                               on_result=lambda result: setattr(core, "clock", core.clock + 20) if result.target == target() else None)
    assert all(result.success for result in results)
    assert core.waits == [(1, 100)]  # Original header expires at 1120, not 1140.


def test_external_stop_callback_sets_core_cancel_flag(monkeypatch):
    core = FakeCore(monkeypatch)
    results = run_post_actions(core, [target(), target(2)], 3, RetryPolicy(), wait_for_cancel=lambda seconds: True)
    assert len(results) == 1 and core.is_cancel_requested()


def test_unexpected_fault_retains_success_and_uncertain_current_result_without_secret(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    core.behavior = lambda item: PostActionResult(item, "delete", True, None) if item == target() else (_ for _ in ()).throw(RuntimeError(SECRET))
    journal = ActionJournal(tmp_path / "receipt.json", "test")
    with pytest.raises(ActionBatchError) as error:
        run_post_actions(core, [target(), target(2), target(3)], 0, RetryPolicy(), journal=journal)
    assert error.value.results[0].success and error.value.results[1].failure_kind == "ambiguous"
    assert error.value.summary["successes"] == 1 and error.value.summary["unprocessed"] == 1
    assert SECRET not in str(error.value) and core.is_cancel_requested()
    assert journal.previous_result(target()).success and journal.previous_result(target(2)).failure_kind == "ambiguous"


@pytest.mark.parametrize("first_deferred", [False, True])
def test_second_execution_crash_must_replace_previous_safe_result_with_ambiguous(monkeypatch, tmp_path, first_deferred):
    """Independent task-21 regression: a prior refusal/deferral is attempt-local."""
    core = FakeCore(monkeypatch)
    current = target(2)
    attempt = 0

    def execute(item):
        nonlocal attempt
        if item == target():
            return PostActionResult(item, "delete", True, None)
        attempt += 1
        if attempt == 1:
            return PostActionResult(item, "delete", False, "proven unsent/refused", "retryable", deferred=first_deferred)
        raise RuntimeError("PRIVATE_CANARY_after_possible_submission")

    core.behavior = execute
    journal = ActionJournal(tmp_path / "journal.json", "review")
    seen = []
    with pytest.raises(ActionBatchError) as caught:
        run_post_actions(core, [target(), current, target(3)], 0, RetryPolicy(), journal=journal, on_result=seen.append)
    error = caught.value
    assert core.cancelled and core.calls == [target(), current, current]
    assert journal.entries[journal.key(current)] == "inflight"
    assert journal.previous_result(current).failure_kind == "ambiguous"
    assert error.results[0].success
    assert len(error.results) == 2, "uncertain retry must appear as a failed current result, not as unsent/unprocessed"
    assert error.results[1].failure_kind == "ambiguous", "previous safe refusal cannot describe the crashed retry"
    assert seen == error.results and len(seen) == 2
    assert error.results[1].attempts == (1 if first_deferred else 2)
    assert error.summary["successes"] == 1 and error.summary["failed"] == 1 and error.summary["unprocessed"] == 1
    assert error.summary["retries"] == (0 if first_deferred else 1)
    assert "PRIVATE_CANARY" not in str(error)


@pytest.mark.parametrize("first_deferred", [False, True])
def test_real_core_receipt_transport_fault_after_retry_preserves_unknown_current(scenario, tmp_path, first_deferred):
    """Independent task-21 regression: failure happens after the confirm click."""
    core, page, context, clock, events, emit = scenario
    mutations = 0
    if first_deferred:
        page.on_navigate = lambda: emit() if len(page.navigations) == 1 else None
    else:
        page.response.json.return_value = {"errors": [{"code": 88, "message": "known refusal"}]}

    def receipt_fault(request):
        nonlocal mutations
        if request.method != "POST":
            return
        mutations += 1
        if first_deferred or mutations == 2:
            request.response.side_effect = RuntimeError("transport disconnected after irreversible click")

    page.on("request", receipt_fault)
    journal = ActionJournal(tmp_path / "journal.json", "review")

    def wait(seconds):
        clock.tick(seconds)
        return False

    seen = []
    with pytest.raises(ActionBatchError) as caught:
        run_post_actions(core, [target(), target(2)], 0, RetryPolicy(), journal=journal,
                         wait_for_cancel=wait, on_result=seen.append)
    assert core.is_cancel_requested()
    assert mutations == (1 if first_deferred else 2)
    assert journal.entries[journal.key(target())] == "inflight"
    assert journal.previous_result(target()).failure_kind == "ambiguous"
    assert len(caught.value.results) == 1, "the irreversible click with lost receipt must be reported as unknown"
    assert caught.value.results[0].failure_kind == "ambiguous"
    assert seen == caught.value.results and len(seen) == 1
    assert caught.value.summary["failed"] == 1 and caught.value.summary["unprocessed"] == 1
    assert caught.value.summary["retries"] == (0 if first_deferred else 1)
    assert core.last_action_batch_summary == caught.value.summary


@pytest.mark.parametrize("first_deferred", [False, True])
def test_cli_later_attempt_fault_reports_unknown_and_preserves_remaining_csv(monkeypatch, tmp_path, first_deferred):
    from contextlib import contextmanager

    core = FakeCore(monkeypatch)
    rows = [{"url": target(index).url, "kind": "post", "note": f"row-{index}"} for index in (1, 2, 3)]
    source = tmp_path / "posts.json"
    source.write_text(json.dumps(rows))
    output = tmp_path / "remaining.csv"
    attempt = 0

    def execute(item):
        nonlocal attempt
        if item == target():
            return PostActionResult(item, "delete", True, None)
        attempt += 1
        if attempt == 1:
            return PostActionResult(item, "delete", False, "proven unsent/refused", "retryable", deferred=first_deferred)
        raise RuntimeError(SECRET)

    core.behavior = execute
    monkeypatch.setattr(core, "record_action_batch_summary", lambda report: setattr(core, "last_action_batch_summary", report), raising=False)

    @contextmanager
    def session(*args, **kwargs):
        yield core

    profile = tmp_path / "profile"
    deleted = DeletedPostStore(profile / "deleted-posts.json")
    monkeypatch.setattr(cli, "browser_session", session)
    monkeypatch.setattr(cli, "get_account_profile_dir", lambda *args: profile)
    monkeypatch.setattr(DeletedPostStore, "for_account", lambda *args: deleted)
    monkeypatch.setattr(cli, "_confirm", lambda *args: None)
    monkeypatch.setattr(cli, "migrate_legacy_app_data", lambda: False)
    monkeypatch.setattr(cli, "configure_frozen_browser_path", lambda: None)
    assert cli.main(["delete", str(source), "--interval", "0", "--profile", "review", "--failed-output", str(output)]) == cli.EXIT_FAILURE
    with output.open(encoding="utf-8-sig", newline="") as stream:
        assert list(csv.DictReader(stream)) == rows[1:]
    assert deleted.load() == {"1"}
    with ActionJournal(profile / "action-progress.json", "review") as journal:
        assert journal.previous_result(target()).success
        assert journal.entries[journal.key(target(2))] == "inflight"
        assert journal.previous_result(target(2)).failure_kind == "ambiguous"
        assert journal.previous_result(target(3)) is None
    assert core.calls == [target(), target(2), target(2)] and core.cancelled
    assert core.last_action_batch_summary["successes"] == 1
    assert core.last_action_batch_summary["failed"] == 1
    assert core.last_action_batch_summary["unprocessed"] == 1


def test_minimal_template_keeps_every_explicit_legacy_value(tmp_path):
    base = Recipe(tmp_path / "recipe.toml", delete=DeleteOptions())
    assert set(tomllib.loads(format_recipe(base))["delete"]) == {"interval", "confirm"}
    legacy = {"delete": {"interval": 0.5, "confirm": False, "max_retries": 3, "batch_size": 25,
                         "cooldown_delay": 30, "retry_delay": 15, "retry_max_delay": 60, "ancillary_rate_limit_mode": "isolate-known"}}
    parsed = parse_recipe(legacy, base.path)
    exported = tomllib.loads(format_recipe(parsed))
    assert exported["delete"] == legacy["delete"]
    assert parse_recipe(exported, base.path) == parsed
    base.path.write_text(format_recipe(parsed))
    update = plan_recipe_update(base.path)
    # Updates add only the advanced keys the compact file omitted, never the explicit legacy ones.
    added = {key.removeprefix("delete.") for key in update.added_keys if key.startswith("delete.")}
    assert added == DELETE_KEYS - set(exported["delete"])
    assert not update.unknown_keys
    assert set(exported["delete"]) <= DELETE_KEYS


def test_nondefault_policy_survives_compact_serialization(tmp_path):
    policy = replace(RetryPolicy(), max_retries=0, batch_size=0, max_cooldowns=0, max_wait_seconds=300)
    recipe = Recipe(tmp_path / "recipe.toml", delete=DeleteOptions(retry_policy=policy))
    assert parse_recipe(tomllib.loads(format_recipe(recipe)), recipe.path) == recipe


def test_hidden_compatibility_flags_keep_exact_precedence(tmp_path, capsys):
    path = tmp_path / "recipe.toml"
    path.write_text('[delete]\ninterval=0.5\nmax_cooldowns=4\nbatch_pause=30\n')
    args = cli.build_parser().parse_args(["delete", "list.csv", "--recipe", str(path), "--max-cooldowns", "2", "--dry-run"])
    policy = cli._delete_tuning_from_args(args).retry_policy
    assert policy.max_cooldowns == 2 and policy.batch_pause_seconds == 30
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["delete", "--help"])
    text = capsys.readouterr().out
    assert "--max-cooldowns" in text and "--retry-max-wait" in text


def test_cli_restores_global_console_handler_after_return(monkeypatch, tmp_path):
    handlers = [(handler, handler.stream, handler.level) for handler in cli.logger.handlers if type(handler) is logging.StreamHandler]
    path = tmp_path / "empty.log"
    path.write_text("")
    assert cli.main(["diagnostics", "--log", str(path)]) == 0
    assert all(handler.stream is stream and handler.level == level for handler, stream, level in handlers)


def test_summary_cli_is_read_only_sanitized_and_covers_all_available_records(scenario, monkeypatch, tmp_path, capsys):
    core, page, context, clock, events, emit = scenario
    with core.post_action_retry_scope():
        emit("https://x.com/i/api/graphql/fixture/UserByScreenName?auth=" + SECRET)
    path = tmp_path / "record.log"
    path.write_text("".join("2026-10-13 21:00:00,000 - kusamushiri - INFO - [MainThread] KS_DIAG " + json.dumps(event) + "\n" for event in events))
    for name in ("XDeleterCore", "migrate_legacy_app_data", "configure_frozen_browser_path"):
        monkeypatch.setattr(cli, name, Mock(side_effect=AssertionError("read-only summary")))
    capsys.readouterr()
    assert cli.main(["diagnostics", "--log", str(path), "--summary", "--max-events", "1"]) == 0
    text = capsys.readouterr().out
    data = json.loads(text)
    assert data["available_events"] == len(events) and data["complete_trace_start_available"]
    assert data["rate_limits"][0]["api_label"] == "UserByScreenName"
    assert data["package_source_sha256"] and SECRET not in text


def test_cli_partial_error_still_writes_complete_remaining_rows(scenario, monkeypatch, tmp_path):
    core, page, context, clock, events, emit = scenario
    rows = [{"url": target(index).url, "kind": "post"} for index in (1, 2, 3)]
    source = tmp_path / "posts.json"
    source.write_text(json.dumps(rows))
    output = tmp_path / "remaining.json"
    summary = {"stop_reason": "unexpected_error"}
    partial = [PostActionResult(target(), "delete", True, None), PostActionResult(target(2), "delete", False, "uncertain", "ambiguous")]

    def fault(*args, **kwargs):
        core.record_action_batch_summary(summary)
        core.request_cancel()
        raise ActionBatchError(partial, summary)

    from contextlib import contextmanager

    @contextmanager
    def session(*args, **kwargs):
        yield core

    monkeypatch.setattr(cli, "browser_session", session)
    monkeypatch.setattr(cli, "run_post_actions", fault)
    monkeypatch.setattr(cli, "_confirm", lambda *args: None)
    monkeypatch.setattr(cli, "migrate_legacy_app_data", lambda: False)
    monkeypatch.setattr(cli, "configure_frozen_browser_path", lambda: None)
    assert cli.main(["delete", str(source), "--failed-output", str(output)]) == cli.EXIT_FAILURE
    assert json.loads(output.read_text()) == rows[1:]
