"""HTTP provenance, held results and diagnostic privacy; no browser/network."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_batch_improvements import scenario as _scenario  # noqa: F401 - reused fixture
from test_network_diagnostics import Clock, Request, Response
from test_retry import ActionPage, target

from kusamushiri import actions
from kusamushiri.http_failures import http_failure
from kusamushiri.models import RetryPolicy
from kusamushiri.network_diagnostics import NetworkDiagnostics, diagnostic_summary, sanitize_event
from kusamushiri.progress import ActionJournal
from kusamushiri.request_metadata import operation_digest
from kusamushiri.retry import run_post_actions

SECRET = "PRIVATE_ENDPOINT_BODY_COOKIE_CANARY"
GRAPH = "https://x.com/i/api/graphql/hash/"


class CompletedRequest(Request):
    @property
    def failure(self):
        return None


@pytest.fixture
def scenario(request):
    return request.getfixturevalue("_scenario")


def reply(url=GRAPH + "UnknownOperation", *, method="GET", status=403, body=None, headers=True):
    request = SimpleNamespace(url=url, method=method, resource_type="xhr", timing={"responseEnd": 1}, failure=None)
    payload = json.dumps(body).encode()
    return SimpleNamespace(url=url, status=status, request=request,
                           headers={"content-type": "application/json", "content-length": str(len(payload))} if headers else {},
                           body=Mock(return_value=payload))


@pytest.mark.parametrize("url,method,role,category", [
    (GRAPH + "UnknownOperation", "GET", "unknown", "unknown403"),
    (GRAPH + "UserByScreenName", "GET", "unknown", "unknown403"),
    (GRAPH + "TweetDetail", "GET", "target", "target403"),
    (GRAPH + "DeleteTweet", "POST", "mutation", "mutation403"),
    (GRAPH + "DeleteTweet", "GET", "unknown", "unknown403"),
    (GRAPH + "ViewerBadgeCounts", "GET", "ancillary", "ancillary403"),
    (GRAPH + "CreatorStudioTabBarItemQuery", "GET", "ancillary", "ancillary403"),
    ("https://x.com/other/DeleteTweet", "POST", "unknown", "unknown403"),
    ("https://foreign.example/i/api/graphql/hash/DeleteTweet", "POST", "unknown", "unknown403"),
])
def test_bare403_is_never_confirmed_auth(url, method, role, category):
    response = reply(url, method=method, headers=False)
    evidence = http_failure(response)
    assert evidence.status == 403 and evidence.role == role and evidence.category == category
    assert not evidence.auth_confirmed and evidence.codes == ()
    response.body.assert_not_called()


@pytest.mark.parametrize("code,category", [(32, "auth_code"), (89, "auth_code"), (326, "account_code")])
def test_only_fixed_bounded_auth_account_codes_support_auth(code, category):
    response = reply(body={"errors": [{"code": code, "message": SECRET}], "private": SECRET})
    evidence = http_failure(response)
    assert evidence.auth_confirmed and evidence.category == category and evidence.codes == (code,)
    assert evidence.provenance == "error_code" and SECRET not in json.dumps(evidence.fields())


@pytest.mark.parametrize("body", [None, {}, {"errors": []}, {"errors": "private"},
    {"errors": [{"code": [89]}]}, {"errors": [{"code": True}]}, {"errors": [{"code": 89.0}]},
    {"errors": [{"code": 89}, {"code": 123456789}]}, {"errors": [{"code": 88}]}])
def test_missing_malformed_or_other_error_codes_remain_blocked(body):
    evidence = http_failure(reply(body=body))
    assert not evidence.auth_confirmed and evidence.codes == ()
    assert "123456789" not in json.dumps(evidence.fields())


@pytest.mark.parametrize("condition", ["declared_large", "actual_large", "wrong_type", "body_error"])
def test_oversized_or_unreadable_envelopes_do_not_create_auth(condition):
    response = reply(body={"errors": [{"code": 89}]})
    if condition == "declared_large":
        response.headers["content-length"] = "8193"
    elif condition == "actual_large":
        response.body.return_value = b" " * 8193
    elif condition == "wrong_type":
        response.headers["content-type"] = "text/html"
    else:
        response.body.side_effect = RuntimeError(SECRET)
    evidence = http_failure(response)
    assert not evidence.auth_confirmed and SECRET not in json.dumps(evidence.fields())
    if condition in {"declared_large", "wrong_type"}:
        response.body.assert_not_called()


def test_http401_is_an_authentication_status_without_reading_body():
    response = reply(status=401)
    evidence = http_failure(response)
    assert evidence.auth_confirmed and evidence.category == "http401"
    response.body.assert_not_called()


@pytest.mark.parametrize("operation,role", [("UnknownOperation", "unknown"), ("TweetDetail", "target"),
                                           ("ViewerBadgeCounts", "ancillary")])
@pytest.mark.parametrize("mode", ["global", "isolate-known"])
def test_unsent403_stops_and_holds_blocked_without_mutation_or_retry(scenario, tmp_path, operation, role, mode):
    core, page, context, clock, events, emit = scenario
    page.on_navigate = lambda: emit(GRAPH + operation, 403)
    if mode == "isolate-known":
        core._browser.ancillary_isolation.install(context, service_workers_blocked=True)
    journal = ActionJournal(tmp_path / "journal.json", "test")
    waits = Mock(side_effect=AssertionError("403 must not wait/retry"))
    results = run_post_actions(core, [target(), target(2)], 0, RetryPolicy(ancillary_rate_limit_mode=mode),
                               journal=journal, wait_for_cancel=waits)
    assert len(results) == 1 and results[0].failure_kind == "blocked"
    assert results[0].http_failure.role == role and not results[0].http_failure.auth_confirmed
    assert not page.clicks and core.is_cancel_requested()
    assert core.last_action_batch_summary["stop_reason"] == "blocked"
    assert core.last_action_batch_summary["failed"] == core.last_action_batch_summary["unprocessed"] == 1
    assert not results[0].deferred
    waits.assert_not_called()
    with ActionJournal(journal.path, "test") as reopened:
        assert reopened.entries[reopened.key(target())] == "blocked"
        assert reopened.previous_result(target()).failure_kind == "blocked"
        assert reopened.previous_result(target()).attempts == 0
    summaries = diagnostic_summary(events)
    assert summaries["http_errors"][0]["status"] == 403
    assert summaries["http_errors"][0]["http_role"] == role
    assert summaries["http_errors"][0]["operation_sha256"]


def test_direct_mutation403_is_held_without_claiming_login_failed():
    page = ActionPage(status=403)
    result = actions.execute_post_action(page, target(), lambda owner, selectors: owner.locator(selectors[0]))
    assert result.failure_kind == "blocked" and result.http_failure.role == "mutation"
    assert sum("confirmationSheetConfirm" in click for click in page.clicks) == 1


def test_confirmed_account_code_on_direct_mutation_remains_auth():
    page = ActionPage(status=403)
    response = reply(page.response.url, method="POST", body={"errors": [{"code": 326, "message": SECRET}]})
    page.response.headers = response.headers
    page.response.body = response.body
    page.response.request.timing = {"responseEnd": 1}
    page.response.request.failure = None
    result = actions.execute_post_action(page, target(), lambda owner, selectors: owner.locator(selectors[0]))
    assert result.failure_kind == "auth" and result.http_failure.category == "account_code"
    assert SECRET not in str(result)


def test_existing_graphql200_auth_refusal_keeps_closed_code_evidence():
    page = ActionPage(body={"errors": [{"code": 89, "message": "Session invalid"}]})
    result = actions.execute_post_action(page, target(), lambda owner, selectors: owner.locator(selectors[0]))
    assert result.failure_kind == "auth"
    assert result.http_failure.status == 200 and result.http_failure.codes == (89,)
    events = []
    trace = NetworkDiagnostics(sink=events.append)
    with trace.operation(target()):
        trace.outcome(result, [], submitted=True)
    assert diagnostic_summary(events)["action_http_evidence"][0]["known_error_codes"] == [89]


def test_unknown403_never_turns_into_rate_deferral_when429_is_also_seen(scenario, tmp_path):
    core, page, context, clock, events, emit = scenario

    def load():
        emit(GRAPH + "UnknownOperation", 429)
        emit(GRAPH + "UnknownOperation", 403)

    page.on_navigate = load
    results = run_post_actions(core, [target()], 0, RetryPolicy(), journal=ActionJournal(tmp_path / "journal.json", "test"))
    assert results[0].failure_kind == "blocked" and not results[0].deferred
    assert not page.clicks


def test_operation_digests_exclude_hash_query_ids_and_private_path_data():
    expected = hashlib.sha256(b"graphql:UnknownOperation").hexdigest()
    assert operation_digest(GRAPH + "UnknownOperation?token=" + SECRET) == expected
    assert operation_digest("https://x.com/i/api/graphql/otherHash/UnknownOperation") == expected
    rest = hashlib.sha256(b"rest:client_event.json").hexdigest()
    assert operation_digest("https://x.com/i/api/1.1/" + SECRET + "/123456789/client_event.json?user=" + SECRET) == rest
    assert operation_digest("https://" + SECRET + "@x.com/i/api/graphql/hash/UnknownOperation") is None
    assert operation_digest("https://x.com/user/status/123456789") is None
    assert operation_digest("https://x.com/i/api/2/123456789.json") is None


def test_diagnostic_http_category_privacy_and_request_origin_are_preserved():
    clock, events = Clock(), []
    trace = NetworkDiagnostics(sink=events.append, wall=clock.wall, monotonic=clock.monotonic)
    request = CompletedRequest(clock, GRAPH + SECRET + "?cookie=" + SECRET)
    request.resource_type = "xhr"
    request.timing["responseEnd"] = 1
    with trace.operation(target()):
        trace.request(request)
    response = Response(request, status=403)
    response.headers = {"content-type": "application/json", "content-length": "80"}
    response.body = lambda: json.dumps({"errors": [{"code": 89, "message": SECRET}]}).encode()
    with trace.operation(target(2)):
        trace.response(response)
    event = events[-2]
    assert event["event"] == "response" and event["origin_operation"] == "op1" and event["operation"] == "op2"
    assert event["http_category"] == "auth_code" and event["known_error_codes"] == [89]
    assert event["api_label"] == "unrecognized" and event["operation_sha256"]
    assert SECRET not in json.dumps(events) and SECRET not in json.dumps(diagnostic_summary(events))
    poisoned = dict(event, http_category=SECRET, http_provenance=SECRET, operation_sha256=SECRET, known_error_codes=[SECRET, 123456789, 89])
    cleaned = sanitize_event(poisoned)
    assert SECRET not in json.dumps(cleaned) and "123456789" not in json.dumps(cleaned)
    assert cleaned["known_error_codes"] == [89]


def test_existing_auth_journal_receipt_is_retained_without_reclassification(tmp_path):
    path = tmp_path / "journal.json"
    with ActionJournal(path, "test") as journal:
        journal.record(target(), "auth")
    before = path.read_bytes()
    with ActionJournal(path, "test") as journal:
        assert journal.previous_result(target()).failure_kind == "auth"
    assert path.read_bytes() == before


def test_saved_auth_is_reported_as_historical_hold_not_a_new_login_observation(scenario, tmp_path):
    core, page, context, clock, events, emit = scenario
    path = tmp_path / "journal.json"
    with ActionJournal(path, "test") as journal:
        journal.bind_viewer("tester")
        journal.record(target(), "auth")
    before = path.read_bytes()
    result = run_post_actions(core, [target(), target(2)], 0, RetryPolicy(), journal=ActionJournal(path, "test"))
    assert result[0].failure_kind == "auth" and result[0].attempts == 0
    assert not page.navigations and not page.clicks
    assert core.last_action_batch_summary["stop_reason"] == "held_receipt"
    assert core.last_action_batch_summary["held_receipts"] == 1
    assert diagnostic_summary(events)["http_errors"] == []
    after = json.loads(path.read_bytes())
    previous = json.loads(before)
    assert after.pop("stop_reason") == "held_receipt"
    previous.pop("stop_reason")
    assert after == previous


def test_stop_precedes_bounded_body_read_and_evidence_is_cached(scenario):
    core, page, context, clock, events, emit = scenario
    request = CompletedRequest(clock, GRAPH + "UnknownOperation")
    request.resource_type = "xhr"
    request.timing["responseEnd"] = 1
    response = Response(request, status=403)
    response.headers = {"content-type": "application/json", "content-length": "100"}

    def read_body():
        assert core.is_cancel_requested(), "must stop before reading diagnostic body"
        return json.dumps({"errors": [{"code": 89, "message": SECRET}]}).encode()

    response.body = Mock(side_effect=read_body)
    with core.post_action_retry_scope():
        core._browser._watch_request(request)
        core._browser._watch_response(response)
        evidence = core._browser.diagnostics.http_failure_for(response)
        assert evidence.category == "auth_code"
        assert core._browser.diagnostics.http_failure_for(response) is evidence
    response.body.assert_called_once()
    assert SECRET not in json.dumps(events)


def test_unfinished403_body_never_blocks_the_response_observer():
    response = reply(body={"errors": [{"code": 89}]})
    response.request.timing["responseEnd"] = -1
    evidence = http_failure(response)
    assert evidence.category == "unknown403" and not evidence.auth_confirmed
    response.body.assert_not_called()


def test_failed_request_end_timing_is_not_mistaken_for_a_completed_body():
    response = reply(body={"errors": [{"code": 89}]})
    response.request.failure = SECRET
    evidence = http_failure(response)
    assert evidence.category == "unknown403" and not evidence.auth_confirmed
    response.body.assert_not_called()


def test_completed_body_enrichment_links_to_the_original_response_id():
    response = reply(body={"errors": [{"code": 89, "message": SECRET}]})
    response.request.timing["responseEnd"] = -1
    events = []
    trace = NetworkDiagnostics(sink=events.append)
    with trace.operation(target()):
        trace.request(response.request)
        trace.response(response)
        before = trace.http_failure_for(response)
        assert before.category == "unknown403"
        response.body.assert_not_called()
        response.request.timing["responseEnd"] = 1
        after = trace.http_failure_for(response)
        assert after.category == "auth_code" and after.response_id == before.response_id == "r1"
        assert trace.http_failure_for(response) is after
    response.body.assert_called_once()
    assert SECRET not in json.dumps(events)
