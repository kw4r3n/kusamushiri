"""Request correlation and real action stages using in-memory doubles only."""

import json
from unittest.mock import Mock

import pytest
from test_retry import ActionPage, target

from kusamushiri import cli
from kusamushiri.core import XDeleterCore
from kusamushiri.models import PostActionResult, PostActionTarget
from kusamushiri.network_diagnostics import NetworkDiagnostics, diagnostic_excerpt, sanitize_event

SECRET = "PRIVATE_AUTH_COOKIE_POST_BODY"
PRIVATE_URL = "https://x.com/private_user/status/1234567890123456789?auth=" + SECRET


class Clock:
    def __init__(self):
        self.now = 1700000000.0

    def tick(self, seconds=0.01):
        self.now += seconds

    def wall(self):
        return self.now

    def monotonic(self):
        return self.now - 1700000000.0


class Request:
    def __init__(self, clock, url=None, method="GET", *, identity=None, start=None):
        self._impl_obj = identity if identity is not None else object()
        self.url = url or "https://x.com/i/api/fleets/v1/fleetline?only_spaces=true&auth=" + SECRET
        self.method = method
        self.timing = {"startTime": (clock.wall() if start is None else start) * 1000}

    @property
    def headers(self):
        raise AssertionError("request headers must not be read")

    @property
    def post_data(self):
        raise AssertionError("request body must not be read")

    @property
    def failure(self):
        raise AssertionError("raw transport error must not be read")


class Response:
    def __init__(self, request, *, status=429, after="2"):
        self._impl_obj = object()
        self.request = request
        self.url = request.url
        self.status = status
        self.headers = {"retry-after": after, "set-cookie": SECRET, "authorization": SECRET}
        self.json = Mock(side_effect=AssertionError("response body must not be read by diagnostics"))


@pytest.fixture
def recorder():
    clock, events = Clock(), []
    trace = NetworkDiagnostics(sink=events.append, wall=clock.wall, monotonic=clock.monotonic)
    return trace, clock, events


def by_event(events, event):
    return [record for record in events if record["event"] == event]


def test_response_keeps_original_attempt_and_navigation_after_retry(recorder):
    trace, clock, events = recorder
    with trace.operation(target()):
        trace.navigation()
        clock.tick()
        request = Request(clock)
        trace.request(request)
    clock.tick()
    with trace.operation(target()):
        trace.navigation()
        clock.tick()
        trace.response(Response(request))
    reply = by_event(events, "response")[0]
    assert reply["request"] == "q1" and reply["origin_operation"] == "op1" and reply["operation"] == "op2"
    assert reply["origin_attempt"] == 1 and reply["attempt"] == 2 and reply["item"] == "item1"
    assert reply["origin_navigation"] == "nav1" and reply["navigation"] == "nav2"
    assert reply["cross_operation"] and reply["cross_navigation"]
    assert reply["since_request_event_ms"] > 0 and reply["retry_after_seconds"] == 2
    assert SECRET not in json.dumps(events) and "private_user" not in json.dumps(events)


def test_delayed_start_event_does_not_claim_current_operation(recorder):
    trace, clock, events = recorder
    old_start = clock.wall()
    clock.tick()
    with trace.operation(target()):
        trace.navigation()
        request = Request(clock, start=old_start)
        trace.request(request)
        trace.response(Response(request))
    reply = by_event(events, "response")[0]
    assert reply["attribution"] == "start_predates_operation"
    assert reply["origin_operation"] is None and reply["origin_navigation"] is None
    assert reply["request_event_operation"] == "op1" and reply["operation"] == "op1"
    assert "cross_operation" not in reply and "cross_navigation" not in reply


def test_request_predating_navigation_retains_only_operation_association(recorder):
    trace, clock, events = recorder
    with trace.operation(target()):
        clock.tick()
        start = clock.wall()
        clock.tick()
        trace.navigation()
        trace.request(Request(clock, start=start))
    record = by_event(events, "request_start")[0]
    assert record["origin_operation"] == "op1" and record["origin_navigation"] is None
    assert record["attribution"] == "start_predates_navigation" and record["origin_phase"] == "unattributed"


def test_unmatched_and_evicted_responses_have_no_invented_owner():
    clock, events = Clock(), []
    trace = NetworkDiagnostics(sink=events.append, wall=clock.wall, monotonic=clock.monotonic, capacity=1)
    first, second = Request(clock), Request(clock)
    with trace.operation(target()):
        trace.request(first)
        trace.request(second)
        trace.response(Response(first))
        trace.request_failed(first)
    reply = by_event(events, "response")[0]
    assert reply["request"] is None and reply["origin_operation"] is None and reply["attribution"] == "unmatched"
    assert by_event(events, "request_failed")[0]["origin_operation"] is None


def test_sdk_wrappers_with_same_identity_keep_one_request_id(recorder):
    trace, clock, events = recorder
    identity = object()
    first, second = Request(clock, identity=identity), Request(clock, identity=identity)
    trace.request(first)
    trace.request(second)
    response = Response(second)
    trace.response(response)
    trace.response(response)  # global and local listeners see the same event
    assert len(by_event(events, "request_start")) == len(by_event(events, "response")) == 1
    assert by_event(events, "response")[0]["request"] == "q1"


def test_unknown_endpoint_and_hostile_fields_never_leak_credentials(recorder):
    trace, clock, events = recorder
    with trace.operation(PostActionTarget(PRIVATE_URL, "post")):
        trace.navigation()
        request = Request(clock, "https://x.com/i/api/" + SECRET, method=SECRET)
        trace.request(request)
        response = Response(request, after=SECRET)
        trace.response(response)
        trace.request_failed(request)
        trace.outcome(PostActionResult(target(), "delete", False, SECRET, "ambiguous"), [], submitted=True)
    rendered = json.dumps(events)
    assert SECRET not in rendered and PRIVATE_URL not in rendered and "1234567890123456789" not in rendered
    reply = by_event(events, "response")[0]
    assert reply["endpoint"] == "unclassified" and reply["method"] == "OTHER" and reply["retry_after_seconds"] is None
    response.json.assert_not_called()


def test_outcome_does_not_assign_previous_operation_mutation_to_current(recorder):
    trace, clock, events = recorder
    with trace.operation(target()):
        request = Request(clock, "https://x.com/i/api/graphql/fixture/DeleteTweet", "POST")
        trace.request(request)
    clock.tick()
    with trace.operation(target()):
        trace.outcome(PostActionResult(target(), "delete", False, SECRET, "ambiguous"), [request], submitted=True)
    outcome = by_event(events, "operation_outcome")[0]
    assert outcome["owned_mutation_requests"] == [] and outcome["other_mutation_requests"] == ["q1"]


def attach_action(page, recorder):
    trace, clock, events = recorder
    core = XDeleterCore()
    core.is_logged_in = lambda: True
    core.get_logged_in_username = lambda: "tester"
    core._action_viewer = "tester"
    core._browser.diagnostics = trace
    core._browser.page = page
    core._find_first_visible_locator = lambda owner, selectors: owner.locator(selectors[0])

    def bind_request(request):
        clock.tick()
        request.method = "POST"
        request.timing = {"startTime": clock.wall() * 1000}
        page.response.request = request
        core._browser._watch_request(request)

    page.on("request", bind_request)
    page.on("response", core._browser._watch_response)
    return core, trace, clock, events


@pytest.mark.parametrize(
    "status,body,missing,timeout,outcome,code88",
    [
        (200, {}, False, False, "success", False),
        (429, {}, False, False, "retryable", False),
        (200, {"errors": [{"code": 88, "message": SECRET}]}, False, False, "retryable", True),
        (403, {}, False, False, "blocked", False),
        (200, {"errors": [{"code": 326, "message": SECRET}]}, False, False, "auth", False),
        (200, {}, True, False, "ambiguous", False),
        (200, {}, False, True, "ambiguous", False),
    ],
)
def test_real_action_outcomes_and_receipts_are_sanitized(recorder, status, body, missing, timeout, outcome, code88):
    page = ActionPage(status=status, body=body, request_missing=missing, timeout=timeout)
    page.response.headers = {"retry-after": "2", "set-cookie": SECRET}
    core, trace, clock, events = attach_action(page, recorder)
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
    reported = by_event(events, "operation_outcome")[0]
    assert reported["outcome"] == outcome and reported["graphql_code88"] == code88
    assert reported["owned_mutation_requests"] == ["q1"] and reported["other_mutation_requests"] == []
    assert reported["unattributed_mutation_requests"] == 0
    assert SECRET not in json.dumps(events) and "tester" not in json.dumps(events)
    assert result.success == (outcome == "success")
    if status == 200 and not missing and not timeout and not body:
        assert page.response.json.call_count == 1  # diagnostics performs no extra body reads
    if status == 429:
        reply = by_event(events, "response")[0]
        assert reply["request"] == "q1" and reply["status"] == 429 and reply["retry_after_seconds"] == 2


@pytest.mark.parametrize("before_submission", [False, True])
def test_ancillary_429_before_and_after_submission_preserves_policy(recorder, before_submission):
    page = ActionPage()
    core, trace, clock, events = attach_action(page, recorder)

    def ancillary(*args, **kwargs):
        clock.tick()
        request = Request(clock)
        core._browser._watch_request(request)
        page.emit("response", Response(request))

    if before_submission:
        page.goto = ancillary
    else:
        page.wait_for_function = ancillary
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
    reported = by_event(events, "operation_outcome")[0]
    assert reported["outcome"] == ("deferred" if before_submission else "success")
    assert result.deferred is before_submission and result.success is (not before_submission)
    assert reported["submitted"] is (not before_submission)
    assert reported["blocking_responses"]
    assert core.rate_limit_status == 429  # success never clears the ancillary signal
    fleet = next(record for record in by_event(events, "response") if record["endpoint"] == "fleetline")
    assert fleet["origin_phase"] == ("target_load" if before_submission else "mutation_wait")


def test_observation_failures_do_not_change_action_result(recorder):
    trace, clock, events = recorder

    def broken_sink(event):
        raise OSError(SECRET)

    trace._sink = broken_sink
    page = ActionPage()
    core, trace, clock, events = attach_action(page, recorder)
    assert core.execute_post_action(target()).success
    assert not core.is_cancel_requested()


@pytest.mark.parametrize("endpoint,method", [("fleetline", "GET"), ("DeleteTweet", "POST")])
def test_prior_attempt_response_is_logged_as_prior_but_policy_still_defers(recorder, endpoint, method):
    trace, clock, events = recorder
    with trace.operation(target()):
        trace.navigation()
        clock.tick()
        old = Request(clock, f"https://x.com/i/api/graphql/fixture/{endpoint}", method)
        trace.request(old)
    clock.tick()
    page = ActionPage()
    core, trace, clock, events = attach_action(page, recorder)
    page.goto = lambda *args, **kwargs: page.emit("response", Response(old))
    with core.post_action_retry_scope():
        result = core.execute_post_action(target())
    assert result.deferred
    reply = by_event(events, "response")[0]
    assert reply["origin_operation"] == "op1" and reply["operation"] == "op2" and reply["request"] == "q1"
    outcome = by_event(events, "operation_outcome")[0]
    assert outcome["operation"] == "op2" and outcome["owned_mutation_requests"] == []


def test_faulted_operation_and_unknown_transport_have_no_false_outcome(recorder):
    trace, clock, events = recorder
    with pytest.raises(RuntimeError), trace.operation(target()):
        raise RuntimeError(SECRET)
    ended = by_event(events, "operation_end")[0]
    assert ended["faulted"] and not ended["outcome_recorded"]
    assert SECRET not in json.dumps(events)


def log_line(event):
    return "2026-10-13 21:00:00,000 - kusamushiri - INFO - [MainThread] KS_DIAG " + json.dumps(event) + "\n"


def test_export_reads_rotations_and_projects_closed_schema(tmp_path, recorder):
    trace, clock, events = recorder
    trace.phase("home_load")
    previous = dict(events[0], trace="abcdef123456", authorization=SECRET, url=PRIVATE_URL)
    path = tmp_path / "kusamushiri.log"
    path.with_name(path.name + ".1").write_text(log_line(previous))
    poisoned = dict(events[-1], method=SECRET, endpoint=PRIVATE_URL, message=SECRET, post_body=SECRET)
    path.write_text("legacy private log " + PRIVATE_URL + "\n" + log_line(poisoned))
    excerpt = diagnostic_excerpt(path)
    assert len(excerpt) == 1 and excerpt[0]["trace"] == trace.trace
    assert SECRET not in json.dumps(excerpt) and PRIVATE_URL not in json.dumps(excerpt)
    assert len(diagnostic_excerpt(path, trace="abcdef123456")) == 1
    assert "authorization" not in diagnostic_excerpt(path, trace="abcdef123456")[0]


def test_export_bad_json_unknown_schema_and_invalid_values_are_ignored(tmp_path):
    path = tmp_path / "log"
    path.write_text(
        log_line({"schema": 1, "trace": "abcdef123456", "event": [SECRET]})
        + log_line({"schema": True, "trace": "abcdef123456", "event": "response"})
        + "2026-10-13 21:00:00,000 - kusamushiri - INFO - [MainThread] KS_DIAG {broken}\n"
    )
    assert diagnostic_excerpt(path) == []
    assert sanitize_event({"schema": 1, "trace": "abcdef123456", "event": "phase", "elapsed_ms": 10**1000}) == {
        "schema": 1,
        "trace": "abcdef123456",
        "event": "phase",
    }


def test_diagnostics_cli_has_no_account_or_browser_side_effects(tmp_path, recorder, monkeypatch, capsys):
    trace, clock, events = recorder
    path = tmp_path / "log"
    path.write_text(log_line(events[0]))
    for name in ("migrate_legacy_app_data", "configure_frozen_browser_path", "XDeleterCore"):
        monkeypatch.setattr(cli, name, Mock(side_effect=AssertionError("must not invoke " + name)))
    assert cli.main(["diagnostics", "--log", str(path)]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["trace"] == trace.trace and output.err == ""


@pytest.mark.parametrize("args", [["--max-events", "0"], ["--max-events", "1001"], ["--trace", SECRET]])
def test_diagnostics_cli_rejects_invalid_export_options(args, capsys):
    assert cli.main(["diagnostics", *args]) == cli.EXIT_USAGE
    assert SECRET not in capsys.readouterr().err
