"""In-memory routed context and real action stages; no browser or network."""

import json
import tomllib
from datetime import UTC, datetime
from email.utils import format_datetime
from unittest.mock import Mock

import pytest
from test_network_diagnostics import Clock, Request, Response, attach_action, by_event
from test_retry import ActionPage, target

from kusamushiri import cli
from kusamushiri.ancillary_isolation import MAX_SUPPRESSED_REQUESTS, AncillaryIsolation, isolatable_endpoint
from kusamushiri.browser import BrowserManager
from kusamushiri.models import RetryPolicy
from kusamushiri.network_diagnostics import NetworkDiagnostics, diagnostic_excerpt
from kusamushiri.progress import ActionJournal
from kusamushiri.recipe import RecipeError, format_recipe, parse_recipe
from kusamushiri.retry import run_post_actions

BADGES = "https://x.com/i/api/graphql/fixture/ViewerBadgeCounts"
FLEETS = "https://x.com/i/api/fleets/v1/fleetline?only_spaces=true"
DELETE = "https://x.com/i/api/graphql/fixture/DeleteTweet"
CREATOR = "https://x.com/i/api/graphql/fixture/CreatorStudioTabBarItemQuery"
SETTINGS = "https://api.x.com/1.1/account/settings.json?include_mention_filter=true"


class Context:
    def __init__(self, *, fail_install=False):
        self.offline = []
        self.fail_install = fail_install

    def route(self, matcher, handler):
        if self.fail_install:
            raise RuntimeError("synthetic routing unavailable")
        self.matcher, self.handler = matcher, handler

    def set_offline(self, value):
        self.offline.append(value)


class Route:
    def __init__(self, url=FLEETS, method="GET", *, fail_abort=False):
        self.request = Mock(url=url, method=method)
        self.aborted, self.continued = False, False
        self.fail_abort = fail_abort

    def abort(self, reason):
        assert reason == "blockedbyclient"
        if self.fail_abort:
            raise RuntimeError("synthetic abort failure")
        self.aborted = True

    def fallback(self):
        self.continued = True


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    clock, events = Clock(), []
    monkeypatch.setattr("kusamushiri.ancillary_isolation.time.time", clock.wall)
    trace = NetworkDiagnostics(sink=events.append, wall=clock.wall, monotonic=clock.monotonic)
    page = ActionPage()
    core, trace, clock, events = attach_action(page, (trace, clock, events))
    core.prepare_post_action_policy(RetryPolicy(ancillary_rate_limit_mode="isolate-known"))
    context = Context()
    core._browser.context = context
    isolation = core._browser.ancillary_isolation
    isolation.diagnostics = trace
    isolation.install(context, service_workers_blocked=True)
    journal = ActionJournal(tmp_path / "fixture-progress.json", "synthetic")
    with journal:
        journal.arm_isolation_storage()
        isolation.persist_deadline = journal.persist_isolated_deadline
        yield core, page, context, clock, events


def emit_ancillary(isolated, url=FLEETS, *, status=429, after="120", request=None):
    core, page, context, clock, events = isolated
    request = request or Request(clock, url)
    core._browser._watch_request(request)
    response = Response(request, status=status, after=after)
    page.emit("response", response)
    return response


@pytest.mark.parametrize("url,method,expected", [
    (FLEETS, "GET", "fleetline"), (BADGES, "GET", "ViewerBadgeCounts"),
    (FLEETS.replace("x.com", "twitter.com"), "GET", "fleetline"),
    (BADGES, "POST", None), (BADGES, "HEAD", None), (DELETE, "GET", None),
    (BADGES.replace("https:", "http:"), "GET", None),
    (BADGES.replace("x.com", "unknown.x.com"), "GET", None),
    (BADGES.replace("x.com", "example.com"), "GET", None),
    ("https://x.com/i/api/fleetline", "GET", None),
    ("https://x.com/i/api/fleets/v1/fleetline.json", "GET", None),
    ("https://x.com/i/api/fleets/v1/fleetline", "GET", None),
    ("https://x.com/i/api/fleets/v1/fleetline?only_spaces=false", "GET", None),
    ("https://x.com/i/api/fleets/v1/fleetline?only_spaces=true&only_spaces=false", "GET", None),
    (BADGES + "/dependency", "GET", None),
    ("https://x.com/i/api/unknown/ViewerBadgeCounts", "GET", None),
    ("https://x.com/i/api/graphql/fixture/CreatorStudioTabBarItemQuery?variables=%7B%7D", "GET", "CreatorStudioTabBarItemQuery"),
    ("https://x.com/i/api/graphql/fixture/CreatorStudioTabBarItemQuery", "POST", None),
    ("https://api.x.com/1.1/account/settings.json?include_mention_filter=true", "GET", "account/settings.json"),
    ("https://api.twitter.com/1.1/account/settings.json", "GET", "account/settings.json"),
    ("https://api.x.com/1.1/account/settings.json", "POST", None),
    ("https://api.x.com/1.1/help/settings.json", "GET", None),
    ("https://api.x.com/i/api/graphql/fixture/ViewerBadgeCounts", "GET", None),
    ("https://x.com/1.1/account/settings.json", "GET", None),
])
def test_only_exact_nonessential_gets_qualify(url, method, expected):
    assert isolatable_endpoint(url, method) == expected


@pytest.mark.parametrize("url", [FLEETS, BADGES, CREATOR, SETTINGS])
@pytest.mark.parametrize("before", [True, False])
def test_isolated_ancillary_before_or_after_success_does_not_defer(isolated, url, before):
    core, page, context, clock, events = isolated
    def emit(*args, **kwargs):
        return emit_ancillary(isolated, url)
    if before:
        page.goto = emit
    else:
        page.wait_for_function = emit
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
        assert result.success and not result.deferred and core.rate_limit_status is None
        assert not core.is_cancel_requested()
        route = Route(url)
        assert context.matcher(url)
        context.handler(route)
        assert route.aborted and not route.continued
        mutation = Route(DELETE, "POST")
        assert not context.matcher(DELETE)
        context.handler(mutation)
        assert mutation.continued and not mutation.aborted
    assert len(by_event(events, "ancillary_quarantine")) == 1
    assert len(by_event(events, "request_suppressed")) == 1
    outcome = by_event(events, "operation_outcome")[0]
    assert outcome["owned_mutation_requests"] and not outcome["blocking_responses"]
    assert by_event(events, "ancillary_quarantine")[0]["response"]
    assert by_event(events, "ancillary_quarantine")[0]["endpoint"] == isolatable_endpoint(url, "GET")


@pytest.mark.parametrize("safe,route_failure", [(False, False), (True, True)])
def test_missing_routing_guarantee_falls_back_to_global(isolated, safe, route_failure):
    core, page, context, clock, events = isolated
    isolation = AncillaryIsolation(core._browser.diagnostics, core._browser._isolation_failed)
    isolation.install(Context(fail_install=route_failure), service_workers_blocked=safe)
    core._browser.ancillary_isolation = isolation
    page.goto = lambda *args, **kwargs: emit_ancillary(isolated)
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
    assert result.deferred and core.rate_limit_status == 429 and not isolation.deadlines
    assert by_event(events, "isolation_fallback")


def test_zero_cooldowns_keeps_explicit_stop_after_any_limit(isolated):
    core, page, context, clock, events = isolated
    core.prepare_post_action_policy(RetryPolicy(ancillary_rate_limit_mode="isolate-known", max_cooldowns=0))
    page.goto = lambda *args, **kwargs: emit_ancillary(isolated)
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
    assert result.deferred and core.rate_limit_status == 429
    assert not core.isolated_rate_limit_seen


@pytest.mark.parametrize("url,method", [(DELETE, "POST"), (BADGES, "POST"),
    ("https://x.com/i/api/graphql/fixture/TweetDetail", "GET"),
    ("https://x.com/i/api/graphql/fixture/UnknownDependency", "GET")])
def test_real_mutation_target_unknown_or_non_get_remains_blocking(isolated, url, method):
    core, page, context, clock, events = isolated
    request = Request(clock, url, method)
    page.goto = lambda *args, **kwargs: emit_ancillary(isolated, request=request)
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
    assert result.deferred and core.rate_limit_status == 429
    assert not any("confirmationSheetConfirm" in selector for selector in page.clicks)


@pytest.mark.parametrize("endpoint,method,expected", [(FLEETS, "GET", "success"), (DELETE, "POST", "deferred")])
def test_delayed_response_keeps_origin_and_does_not_weaken_mutation_gate(isolated, endpoint, method, expected):
    core, page, context, clock, events = isolated
    trace = core._browser.diagnostics
    with trace.operation(target()):
        trace.navigation()
        old = Request(clock, endpoint, method)
        trace.request(old)
    clock.tick()
    page.goto = lambda *args, **kwargs: emit_ancillary(isolated, request=old)
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
    reply = next(event for event in by_event(events, "response") if event["status"] == 429)
    assert reply["origin_operation"] == "op1" and reply["operation"] == "op2" and reply["cross_operation"]
    assert by_event(events, "operation_outcome")[0]["outcome"] == expected
    assert result.success == (expected == "success")
    if expected == "success":
        quarantined = by_event(events, "ancillary_quarantine")[0]
        assert quarantined["origin_operation"] == "op1" and quarantined["operation"] == "op2"


@pytest.mark.parametrize("status,body,kind", [(429, {}, "retryable"),
    (200, {"errors": [{"code": 88}]}, "retryable"),
    (403, {}, "blocked"), (200, {"errors": [{"code": 326}]}, "auth")])
def test_simultaneous_actual_mutation_refusal_wins_over_isolated_ancillary(isolated, status, body, kind):
    core, page, context, clock, events = isolated
    page.response.status = status
    page.response.json.return_value = body
    page.response.headers = {"retry-after": "999"}
    page.goto = lambda *args, **kwargs: emit_ancillary(isolated)
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
    assert not result.success and result.failure_kind == kind and not result.deferred
    if kind == "retryable":
        assert result.rate_limit_scope == "mutation" and result.retry_after_seconds == 999
    if status == 429:
        assert core.rate_limit_scope == "mutation" and core.rate_limit_retry_until == pytest.approx(clock.wall() + 999)


@pytest.mark.parametrize("before", [True, False])
@pytest.mark.parametrize("status", [401, 403])
def test_auxiliary_auth_stops_and_keeps_confirmed_success(isolated, before, status):
    core, page, context, clock, events = isolated
    def emit(*args, **kwargs):
        return emit_ancillary(isolated, status=status)
    if before:
        page.goto = emit
    else:
        page.wait_for_function = emit
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
    assert core.is_cancel_requested()
    if before:
        assert not result.success and result.failure_kind == ("auth" if status == 401 else "blocked")
    else:
        assert result.success


def test_missing_mutation_ack_is_ambiguous_despite_isolated_auxiliary(isolated):
    core, page, context, clock, events = isolated
    page.request_missing = True
    page.goto = lambda *args, **kwargs: emit_ancillary(isolated)
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
    assert not result.success and result.failure_kind == "ambiguous" and not result.deferred
    assert sum("confirmationSheetConfirm" in s for s in page.clicks) == 1


def test_missing_target_dependency_stops_without_mutation_or_automatic_retry(isolated, monkeypatch):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from test_retry import ActionLocator

    core, page, context, clock, events = isolated
    page.goto = lambda *args, **kwargs: emit_ancillary(isolated)

    def unavailable(*args, **kwargs):
        raise PlaywrightTimeoutError("synthetic target unavailable")

    monkeypatch.setattr(ActionLocator, "wait_for", unavailable)
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
    assert not result.success and result.deferred and core.is_cancel_requested()
    assert not page.clicks


@pytest.mark.parametrize("after", ["999", "bad", None, "0", "http-date"])
def test_deadline_never_shortens_and_endpoint_never_autoretries(isolated, after):
    core, page, context, clock, events = isolated
    start = clock.wall()
    if after == "http-date":
        after = format_datetime(datetime.fromtimestamp(start + 999, UTC), usegmt=True)
    with core.post_action_retry_scope():
        response = emit_ancillary(isolated, after=after)
        isolation = core._browser.ancillary_isolation
        expected = 999 if after not in (None, "bad", "0") else 120
        assert isolation.deadline == pytest.approx(start + expected)
        clock.tick(1001)
        core._browser._watch_response(response)  # duplicate never extends deadline
        assert isolation.deadline == pytest.approx(start + expected)
        route = Route()
        context.handler(route)
        assert route.aborted  # expiry does not generate another server attempt


@pytest.mark.parametrize("fail_abort", [True, False])
def test_route_failure_or_local_retry_storm_stops_with_deadline(isolated, fail_abort):
    core, page, context, clock, events = isolated
    with core.post_action_retry_scope():
        emit_ancillary(isolated, after="999")
        for _ in range(1 if fail_abort else MAX_SUPPRESSED_REQUESTS):
            context.handler(Route(fail_abort=fail_abort))
        assert core._browser.ancillary_isolation.failed and core.is_cancel_requested()
        assert core.rate_limit_status == 429
        assert core.rate_limit_retry_until >= clock.wall() + 999
        assert context.offline[-1] is True
    assert by_event(events, "isolation_fallback")


@pytest.mark.parametrize("cancel", [False, True])
def test_cancel_and_completion_keep_deadline_and_success_receipts_without_wait(isolated, tmp_path, cancel):
    core, page, context, clock, events = isolated
    waits = []
    journal = ActionJournal(tmp_path / "progress.json", "synthetic")

    def inject(*args, **kwargs):
        emit_ancillary(isolated, after="999")
        if cancel:
            core.request_cancel()

    page.wait_for_function = inject  # cancellation follows an acknowledged mutation
    results = run_post_actions(core, [target(), target(2)], 0,
                               RetryPolicy(ancillary_rate_limit_mode="isolate-known", max_wait_seconds=1),
                               journal=journal, wait_for_cancel=lambda seconds: waits.append(seconds) or False)
    assert results and all(result.success for result in results)
    assert len(results) == (1 if cancel else 2)
    assert waits == ([] if cancel else [5.0])  # ordinary spacing, never a long wait
    assert journal.cooldown_until >= clock.wall() + 999
    assert core._browser.network_suspended and context.offline[-1]
    assert journal.previous_result(target()).success
    receipts = json.loads((tmp_path / "progress.json").read_text())
    assert "synthetic" in str(receipts)


def test_global_unknown_budget_is_still_finite(isolated, tmp_path):
    core, page, context, clock, events = isolated
    page.goto = lambda *args, **kwargs: emit_ancillary(isolated, "https://x.com/i/api/graphql/fixture/UnknownDependency", after="999")
    waits = []
    results = run_post_actions(core, [target()], 0, RetryPolicy(ancillary_rate_limit_mode="isolate-known", max_wait_seconds=1),
                               journal=ActionJournal(tmp_path / "progress.json", "synthetic"),
                               wait_for_cancel=lambda seconds: waits.append(seconds) or False)
    assert results == [] and waits == [] and core.is_cancel_requested()
    assert not any("confirmationSheetConfirm" in s for s in page.clicks)


def test_unexpected_error_keeps_quarantine_deadline_and_inflight_receipt(isolated, tmp_path):
    core, page, context, clock, events = isolated
    journal = ActionJournal(tmp_path / "progress.json", "synthetic")

    def fault(item):
        emit_ancillary(isolated, after="999")
        raise RuntimeError("synthetic operation fault")

    core.execute_post_action = fault
    with pytest.raises(RuntimeError, match="Action batch stopped"):
        run_post_actions(core, [target()], 0, RetryPolicy(ancillary_rate_limit_mode="isolate-known"), journal=journal)
    assert journal.cooldown_until >= clock.wall() + 999 and context.offline[-1]
    assert journal.previous_result(target()).failure_kind == "ambiguous"


def test_opt_in_launch_blocks_service_workers_without_launching_browser():
    manager = BrowserManager()
    manager.block_service_workers = True
    manager._launch_context = Mock(return_value=object())
    manager._launch_action_context(Mock(), Mock())
    assert manager._launch_context.call_args.kwargs["service_workers"] == "block"


def test_cli_prepares_policy_before_startup_without_account_actions(monkeypatch):
    core = Mock(rate_limit_status=None)
    core.is_cancel_requested.return_value = False
    monkeypatch.setattr(cli, "XDeleterCore", lambda **kwargs: core)
    monkeypatch.setattr(cli, "_wait_for_login", lambda *args: True)
    monkeypatch.setattr(cli.signal, "signal", Mock())
    policy = RetryPolicy(ancillary_rate_limit_mode="isolate-known")
    with cli.browser_session("synthetic", headless=False, retry_policy=policy) as active:
        assert active is core
    calls = [call[0] for call in core.mock_calls]
    assert calls.index("prepare_post_action_policy") < calls.index("start_browser")
    core.prepare_post_action_policy.assert_called_once_with(policy)


def test_config_defaults_roundtrip_precedence_and_invalid_mode(tmp_path):
    path = tmp_path / "recipe.toml"
    old = parse_recipe({"delete": {}}, path)
    assert old.delete.retry_policy.ancillary_rate_limit_mode == "global"
    new = parse_recipe({"delete": {"ancillary_rate_limit_mode": "isolate-known"}}, path)
    assert parse_recipe(tomllib.loads(format_recipe(new)), path) == new
    path.write_text(format_recipe(new))
    args = cli.build_parser().parse_args(["delete", "placeholder.csv", "--recipe", str(path), "--dry-run"])
    assert cli._delete_tuning_from_args(args).retry_policy.ancillary_rate_limit_mode == "isolate-known"
    args = cli.build_parser().parse_args(["delete", "placeholder.csv", "--recipe", str(path), "--ancillary-rate-limit-mode", "global", "--dry-run"])
    assert cli._delete_tuning_from_args(args).retry_policy.ancillary_rate_limit_mode == "global"
    for invalid in (True, 42, "ignore-all"):
        with pytest.raises(RecipeError):
            parse_recipe({"delete": {"ancillary_rate_limit_mode": invalid}}, path)


def test_quarantine_export_preserves_sanitized_endpoint_deadline(isolated, tmp_path):
    core, page, context, clock, events = isolated
    with core.post_action_retry_scope():
        emit_ancillary(isolated, BADGES + "?secret=PRIVATE_CANARY", after="999")
        context.handler(Route(BADGES + "?secret=PRIVATE_CANARY"))
    path = tmp_path / "events.log"
    path.write_text("".join("2026-10-13 21:00:00,000 - kusamushiri - INFO - [MainThread] KS_DIAG " + json.dumps(event) + "\n" for event in events))
    exported = diagnostic_excerpt(path, max_events=1000)
    assert "PRIVATE_CANARY" not in json.dumps(exported)
    quarantine = by_event(exported, "ancillary_quarantine")[0]
    assert quarantine["endpoint"] == "ViewerBadgeCounts" and quarantine["retry_after_seconds"] == 999
    assert quarantine["retry_until_utc"]
    assert by_event(exported, "request_suppressed")


def test_suppression_cap_counts_per_item_not_per_batch(isolated):
    core, page, context, clock, events = isolated
    page.goto = lambda *args, **kwargs: None
    with core.post_action_retry_scope():
        emit_ancillary(isolated, after="999")
        for item in range(3):
            core.execute_post_action(target(item + 1))
            for _ in range(MAX_SUPPRESSED_REQUESTS - 1):
                route = Route()
                context.handler(route)
                assert route.aborted
        isolation = core._browser.ancillary_isolation
        assert not isolation.failed and not core.is_cancel_requested()
        core.execute_post_action(target(3))  # A retry of the same item keeps its count.
        context.handler(Route())  # The current item reaches the cap and still stops.
        assert isolation.failed and core.is_cancel_requested()
