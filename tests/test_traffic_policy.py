"""Name-based post-action allowlist: pure rules, routing and fail-closed stops; no browser or network."""

import json
import tomllib
from unittest.mock import Mock

import pytest
from test_network_diagnostics import SECRET, Clock, attach_action, by_event
from test_retry import ActionLocator, ActionPage, target

from kusamushiri import cli
from kusamushiri.models import RetryPolicy
from kusamushiri.network_diagnostics import NetworkDiagnostics, diagnostic_summary, sanitize_event
from kusamushiri.recipe import RecipeError, format_recipe, parse_recipe
from kusamushiri.traffic_policy import TrafficPolicy, decide, host_class

GRAPH = "https://x.com/i/api/graphql/fixture/"
TARGET = "https://x.com/tester/status/1"


class FakeRequest:
    def __init__(self, url, method="GET", resource_type="xhr"):
        self._impl_obj = object()
        self.url, self.method, self.resource_type = url, method, resource_type


class Context:
    def __init__(self, *, fail_install=False):
        self.fail_install = fail_install
        self.pages = [Mock(), Mock()]
        self.handler = None

    def route(self, matcher, handler):
        if self.fail_install:
            raise RuntimeError("synthetic routing unavailable")
        assert matcher == "**/*"
        self.handler = handler


class Route:
    def __init__(self, request, *, fail_abort=False):
        self.request = request
        self.aborted = self.continued = False
        self.fail_abort = fail_abort

    def abort(self, reason):
        assert reason == "blockedbyclient"
        if self.fail_abort:
            raise RuntimeError("synthetic abort failure")
        self.aborted = True

    def fallback(self):
        self.continued = True


@pytest.mark.parametrize("url,method,resource,armed,expected", [
    (TARGET, "GET", "document", False, ("allow", "target_document")),
    (TARGET + "/photo/1", "GET", "document", False, ("block", "unlisted")),
    ("https://x.com/home", "GET", "document", False, ("block", "unlisted")),
    ("https://abs.twimg.com/responsive-web/client-web/main.js", "GET", "script", False, ("allow", "static_asset")),
    ("https://x.com/responsive-web/client-web/vendor.js", "GET", "script", False, ("allow", "static_asset")),
    ("https://abs.twimg.com/fonts/chirp.woff2", "GET", "font", False, ("allow", "static_asset")),
    ("https://pbs.twimg.com/profile_images/1/a.jpg", "GET", "image", False, ("block", "unlisted")),
    ("https://evil.example/main.js", "GET", "script", False, ("block", "unlisted")),
    (GRAPH + "TweetDetail?variables=%7B%7D", "GET", "xhr", False, ("allow", "target_read")),
    (GRAPH + "TweetResultByRestId", "GET", "fetch", False, ("allow", "target_read")),
    (GRAPH + "TweetDetail", "POST", "xhr", False, ("block", "unlisted")),
    (GRAPH + "TweetDetail", "GET", "script", False, ("block", "unlisted")),
    (GRAPH + "DeleteTweet", "POST", "xhr", True, ("allow", "armed_mutation")),
    (GRAPH + "DeleteTweet", "POST", "xhr", False, ("block", "unarmed_mutation")),
    (GRAPH + "DeleteRetweet", "POST", "xhr", True, ("allow", "armed_mutation")),
    (GRAPH + "CreateTweet", "POST", "xhr", True, ("block", "unlisted")),
    (GRAPH + "CreatorStudioTabBarItemQuery", "GET", "xhr", False, ("block", "unlisted")),
    (GRAPH + "HomeTimeline", "POST", "xhr", False, ("block", "unlisted")),
    ("https://api.x.com/1.1/account/settings.json", "GET", "xhr", False, ("block", "unlisted")),
    ("https://api.x.com/i/api/graphql/fixture/TweetDetail", "GET", "xhr", False, ("block", "unlisted")),
    ("https://x.com/i/api/1.1/jot/client_event.json", "POST", "xhr", False, ("block", "unlisted")),
    ("http://x.com/i/api/graphql/fixture/TweetDetail", "GET", "xhr", False, ("block", "unlisted")),
    ("https://user@x.com/i/api/graphql/fixture/TweetDetail", "GET", "xhr", False, ("block", "unlisted")),
    ("data:image/png;base64,AAAA", "GET", "image", False, ("allow", "local")),
    ("blob:https://x.com/0000", "GET", "media", False, ("allow", "local")),
    ("about:blank", "GET", "document", False, ("allow", "local")),
    ("data:text/html,<p>x</p>", "GET", "document", False, ("block", "unlisted")),
    ("blob:https://x.com/0000", "GET", "document", False, ("block", "unlisted")),
])
def test_only_listed_names_resources_and_hosts_are_admitted(url, method, resource, armed, expected):
    assert decide(url, method, resource, armed=armed)[1:] == expected


@pytest.mark.parametrize("host,expected", [
    ("x.com", "x_web"), ("www.twitter.com", "x_web"), ("api.x.com", "x_api"), ("api.twitter.com", "x_api"),
    ("video.x.com", "x_subdomain"), ("abs.twimg.com", "twimg"), ("x.com.evil.example", "other"), (None, "other"),
])
def test_host_classes_are_closed_labels(host, expected):
    assert host_class(host) == expected


@pytest.fixture
def recorder():
    clock, events = Clock(), []
    return NetworkDiagnostics(sink=events.append, wall=clock.wall, monotonic=clock.monotonic), events


def armed_policy(trace, mode, *, context=None, sw=True):
    failures = []
    policy = TrafficPolicy(trace, lambda: failures.append(True))
    policy.mode = mode
    policy.install(context or Context(), service_workers_blocked=sw)
    policy.active = True
    return policy, failures


def test_report_records_every_host_without_routing_or_secrets(recorder):
    trace, events = recorder
    context = Context()
    policy, _ = armed_policy(trace, "report", context=context)
    assert policy.ready and context.handler is None and not policy.enforcing
    policy.observe(FakeRequest("https://abs.twimg.com/main.js?token=" + SECRET, resource_type="script"))
    policy.observe(FakeRequest(GRAPH + "TweetDetail?variables=" + SECRET))
    policy.observe(FakeRequest("https://api.x.com/1.1/account/settings.json?t=" + SECRET))
    records = by_event(events, "traffic_decision")
    assert [(r["host_class"], r["traffic_decision"], r["traffic_rule"]) for r in records] == [
        ("twimg", "allow", "static_asset"), ("x_web", "allow", "target_read"), ("x_api", "block", "unlisted")]
    assert all(r["traffic_enforced"] is False for r in records)
    assert records[1]["public_operation"] == "TweetDetail" and records[2]["route_template"] == "/1.1/account/settings.json"
    assert SECRET not in json.dumps(events)
    assert all(sanitize_event(r) == r for r in records)
    summary = diagnostic_summary(events)["traffic"]
    assert {(row["host_class"], row["traffic_decision"], row["requests"]) for row in summary} == {
        ("twimg", "allow", 1), ("x_web", "allow", 1), ("x_api", "block", 1)}


def test_inactive_or_off_policy_records_and_routes_nothing(recorder):
    trace, events = recorder
    context = Context()
    policy, _ = armed_policy(trace, "enforce", context=context)
    policy.active = False
    policy.observe(FakeRequest(GRAPH + "HomeTimeline", "POST"))
    route = Route(FakeRequest(GRAPH + "HomeTimeline", "POST"))
    context.handler(route)
    assert route.continued and not route.aborted and not by_event(events, "traffic_decision")
    off, _ = armed_policy(trace, "off")
    off.observe(FakeRequest(GRAPH + "HomeTimeline", "POST"))
    assert not off.ready and not by_event(events, "traffic_decision")


def test_enforce_routes_first_cleans_restored_pages_and_aborts_unlisted(recorder):
    trace, events = recorder
    context = Context()
    restored = context.pages[1]
    policy, _ = armed_policy(trace, "enforce", context=context)
    assert policy.enforcing and restored.close.called
    context.pages[0].goto.assert_called_once_with("about:blank")
    for url, method, resource, admitted in [
        (TARGET, "GET", "document", True), (GRAPH + "TweetDetail", "GET", "xhr", True),
        (GRAPH + "CreatorStudioTabBarItemQuery", "GET", "xhr", False),
        ("https://x.com/i/api/1.1/jot/client_event.json", "POST", "xhr", False),
    ]:
        request = FakeRequest(url, method, resource)
        policy.observe(request)
        route = Route(request)
        context.handler(route)
        assert (route.continued, route.aborted) == (admitted, not admitted)
    assert policy.blocked == 2
    assert all(r["traffic_enforced"] for r in by_event(events, "traffic_decision"))


def test_mutation_is_admitted_once_and_only_while_armed(recorder):
    trace, _ = recorder
    context = Context()
    policy, _ = armed_policy(trace, "enforce", context=context)
    policy.begin_operation(("post", TARGET))

    def send():
        request = FakeRequest(GRAPH + "DeleteTweet", "POST")
        policy.observe(request)
        route = Route(request)
        context.handler(route)
        return route.continued

    assert not send()
    trace.phase("mutation_submit")
    assert send() and not send()
    policy.begin_operation(("post", TARGET + "2"))
    assert send() and policy.mutations == 1


def test_unobserved_route_is_decided_never_admitted_blindly(recorder):
    trace, events = recorder
    context = Context()
    armed_policy(trace, "enforce", context=context)
    allowed, blocked = Route(FakeRequest(TARGET, resource_type="document")), Route(FakeRequest(GRAPH + "HomeTimeline", "POST"))
    context.handler(allowed)
    context.handler(blocked)
    assert allowed.continued and blocked.aborted and len(by_event(events, "traffic_decision")) == 2


def test_abort_failure_fails_closed_and_stops(recorder):
    trace, _ = recorder
    context = Context()
    policy, failures = armed_policy(trace, "enforce", context=context)
    request = FakeRequest(GRAPH + "HomeTimeline", "POST")
    policy.observe(request)
    context.handler(Route(request, fail_abort=True))
    assert policy.failed and failures and not policy.enforcing
    later = Route(FakeRequest(TARGET, resource_type="document"))
    context.handler(later)
    assert later.aborted and not later.continued


@pytest.mark.parametrize("context,sw", [(Context(fail_install=True), True), (Context(), False)])
def test_enforce_without_routing_guarantee_is_not_ready(recorder, context, sw):
    trace, _ = recorder
    policy, _ = armed_policy(trace, "enforce", context=context, sw=sw)
    assert not policy.ready and not policy.enforcing


def enforced_core(*, ready=True):
    page = ActionPage()
    clock, events = Clock(), []
    trace = NetworkDiagnostics(sink=events.append, wall=clock.wall, monotonic=clock.monotonic)
    core, trace, clock, events = attach_action(page, (trace, clock, events))
    core.prepare_post_action_policy(RetryPolicy(traffic_policy="enforce"))
    assert core._browser.block_service_workers and core._browser.traffic_mode == "enforce"
    context = Context(fail_install=not ready)
    core._browser.context = context
    traffic = core._browser.traffic_policy
    traffic.diagnostics = trace
    traffic.mode = core._browser.traffic_mode
    traffic.install(context, service_workers_blocked=True)
    return core, page, traffic, events


def test_unsent_failure_under_enforce_is_held_blocked_without_click(monkeypatch):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    core, page, traffic, events = enforced_core()

    def unavailable(*args, **kwargs):
        traffic.observe(FakeRequest(GRAPH + "CreatorStudioTabBarItemQuery"))
        raise PlaywrightTimeoutError("synthetic target unavailable")

    monkeypatch.setattr(ActionLocator, "wait_for", unavailable)
    with core.post_action_retry_scope():
        assert traffic.enforcing
        result = core.execute_post_action(target())
    assert not result.success and result.failure_kind == "blocked" and not result.deferred
    assert "diagnostics --summary" in (result.error_message or "") and not page.clicks
    assert not traffic.active


def test_success_under_enforce_is_unchanged():
    core, page, traffic, _ = enforced_core()
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
    assert result.success and result.failure_kind != "blocked"


def test_requested_enforce_without_routes_refuses_to_start():
    core, page, traffic, _ = enforced_core(ready=False)
    with core.post_action_retry_scope():
        assert not traffic.active and core.is_cancel_requested()


def test_config_roundtrip_precedence_and_invalid_mode(tmp_path):
    path = tmp_path / "recipe.toml"
    assert parse_recipe({"delete": {}}, path).delete.retry_policy.traffic_policy == "off"
    new = parse_recipe({"delete": {"traffic_policy": "report"}}, path)
    assert parse_recipe(tomllib.loads(format_recipe(new)), path) == new
    path.write_text(format_recipe(new))
    args = cli.build_parser().parse_args(["delete", "placeholder.csv", "--recipe", str(path), "--dry-run"])
    assert cli._delete_tuning_from_args(args).retry_policy.traffic_policy == "report"
    args = cli.build_parser().parse_args(["delete", "placeholder.csv", "--recipe", str(path), "--traffic-policy", "enforce", "--dry-run"])
    policy = cli._delete_tuning_from_args(args).retry_policy
    assert policy.traffic_policy == "enforce" and cli._needs_action_context(policy)
    assert not cli._needs_action_context(RetryPolicy())
    for invalid in (True, 42, "allow-all"):
        with pytest.raises(RecipeError):
            parse_recipe({"delete": {"traffic_policy": invalid}}, path)


def test_blocked_dependency_stops_batch_and_keeps_items_pending(monkeypatch, tmp_path):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    from kusamushiri.progress import ActionJournal
    from kusamushiri.retry import run_post_actions

    core, page, traffic, _ = enforced_core()

    def unavailable(*args, **kwargs):
        raise PlaywrightTimeoutError("synthetic target unavailable")

    monkeypatch.setattr(ActionLocator, "wait_for", unavailable)
    first, second = target(), target(2)
    journal = ActionJournal(tmp_path / "receipts.json", "synthetic")
    results = run_post_actions(core, [first, second], 0, RetryPolicy(traffic_policy="enforce"), journal=journal,
                               wait_for_cancel=lambda seconds: False)
    assert [r.failure_kind for r in results] == ["blocked"] and not page.clicks
    assert core.last_action_batch_summary["stop_reason"] == "blocked"
    assert journal.entries.get(journal.key(second)) in (None, "pending")


def test_recycled_identity_slot_is_a_miss_not_a_stale_allow(recorder):
    trace, events = recorder
    context = Context()
    policy, _ = armed_policy(trace, "enforce", context=context)
    blocked = FakeRequest(GRAPH + "HomeTimeline", "POST")
    policy._decisions[id(blocked._impl_obj)] = (object(), "allow")
    route = Route(blocked)
    context.handler(route)
    assert route.aborted and not route.continued
    assert [r["traffic_rule"] for r in by_event(events, "traffic_decision")] == ["unlisted"]


@pytest.mark.parametrize("route_first", [False, True])
def test_each_request_is_decided_once_in_either_order(recorder, route_first):
    trace, events = recorder
    context = Context()
    policy, _ = armed_policy(trace, "enforce", context=context)
    trace.phase("mutation_submit")
    for url, method in ((GRAPH + "DeleteTweet", "POST"), (GRAPH + "HomeTimeline", "POST")):
        request = FakeRequest(url, method)
        route = Route(request)
        if route_first:
            context.handler(route)
            policy.observe(request)
        else:
            policy.observe(request)
            context.handler(route)
        assert route.continued == (url.endswith("DeleteTweet"))
    records = by_event(events, "traffic_decision")
    assert [r["traffic_rule"] for r in records] == ["armed_mutation", "unlisted"]
    assert policy.mutations == 1 and policy.blocked == 1


def test_run_command_accepts_traffic_policy():
    args = cli.build_parser().parse_args(["run", "recipe.toml", "--traffic-policy", "report"])
    assert args.traffic_policy == "report"


def test_sanitizer_drops_contradictory_rule_and_decision():
    base = {"schema": 1, "trace": "0123456789ab", "event": "traffic_decision", "host_class": "other"}
    assert "traffic_decision" not in sanitize_event({**base, "traffic_decision": "allow", "traffic_rule": "unlisted"})
    kept = sanitize_event({**base, "traffic_decision": "block", "traffic_rule": "unlisted"})
    assert kept["traffic_decision"] == "block" and kept["traffic_rule"] == "unlisted"
