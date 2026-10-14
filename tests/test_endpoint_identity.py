"""Offline public identity projection, secret rejection and unchanged safety gates."""

import hashlib
import json
import re
from dataclasses import replace

import pytest
from test_batch_improvements import scenario as batch_scenario
from test_network_diagnostics import Clock, Request, Response
from test_retry import target

from kusamushiri.endpoint_identity import GRAPHQL_TEMPLATE, public_identity
from kusamushiri.models import RetryPolicy
from kusamushiri.network_diagnostics import NetworkDiagnostics, diagnostic_excerpt, diagnostic_summary, sanitize_event
from kusamushiri.progress import ActionJournal
from kusamushiri.public_endpoint_catalog import FIXED_ROUTES, PUBLIC_OPERATIONS
from kusamushiri.rate_limits import classify_rate_limit
from kusamushiri.request_metadata import operation_digest
from kusamushiri.retry import run_post_actions

SECRET = "PRIVATE_DIAGNOSTIC_CANARY"
UNKNOWN_PUBLIC = "AiTrendByRestId"
GRAPH = "https://x.com/i/api/graphql/syntheticQueryHash/"
UNAVAILABLE = {"identity_status": "unavailable"}


@pytest.fixture
def scenario(monkeypatch):
    return batch_scenario.__wrapped__(monkeypatch)


@pytest.mark.parametrize("name", sorted(PUBLIC_OPERATIONS))
def test_catalog_symbols_emit_exact_identity_without_query_or_routing_hash(name):
    result = public_identity(GRAPH + name + "?auth=" + SECRET + "#" + SECRET, "GET")
    assert result == {"identity_status": "public_operation", "public_operation": name, "route_template": GRAPHQL_TEMPLATE}
    assert SECRET not in json.dumps(result) and "syntheticQueryHash" not in json.dumps(result)


@pytest.mark.parametrize("method,path", sorted(FIXED_ROUTES))
def test_fixed_public_paths_require_exact_authority_method_and_template(method, path):
    host = "api.x.com" if path.startswith("/1.1/") else "x.com"
    assert public_identity("https://" + host + path + "?user_id=1234567890&token=" + SECRET, method) == {
        "identity_status": "fixed_route", "route_template": path}
    assert public_identity("https://unknown.x.com" + path, method) == UNAVAILABLE
    assert public_identity("https://" + host + path + "/PrivateUsername", method) == UNAVAILABLE


@pytest.mark.parametrize("name", [SECRET, "ValidLookingUnknownOperation", "DeleteTweetSecret", "DeleteTweet" + "x" * 80,
                                 "DeleteTwee", "DeleteTweet_", "1234567890", "PrivateUsername", "deleteTweet",
                                 "DeleteTweet/" + SECRET, "%44eleteTweet", "DeleteTwеet", "DeleteTweet\ud800",
                                 "DeleteTweet\n", "DeleteTweet\x00", "DeleteTweet;" + SECRET, "DeleteTweet/1234567890"])
def test_sensitive_unknown_malformed_and_truncated_tokens_remain_unavailable(name):
    assert public_identity(GRAPH + name, "GET") == UNAVAILABLE


@pytest.mark.parametrize("url", [None, {}, [], 12, "", "https://x.com/" + "x" * 8200,
                                 "http://x.com/i/api/graphql/hash/DeleteTweet",
                                 "https://x.com:444/i/api/graphql/hash/DeleteTweet",
                                 "https://x.com:/i/api/graphql/hash/DeleteTweet",
                                 "https://" + SECRET + "@x.com/i/api/graphql/hash/DeleteTweet",
                                 "https://x.com.evil.invalid/i/api/graphql/hash/DeleteTweet",
                                 "https://api.x.com/i/api/graphql/hash/DeleteTweet",
                                 "https://x.com/i/api/graphql/" + "x" * 129 + "/DeleteTweet",
                                 "https://x.com/i/api/graphql/hash/DeleteTweet/",
                                 "https://x.com/i/api/graphql/hash/DeleteTweet\r\n?" + SECRET,
                                 "https://x.com/i/api/graphql/hash/DeleteTweet?x=\ud800",
                                 "https://x.com\\evil/i/api/graphql/hash/DeleteTweet",
                                 "https://x.com/i/api/1.1/users/PrivateUsername/settings.json",
                                 "https://x.com/i/api/1.1/statuses/destroy/1234567890.json",
                                 "https://x.com/i/api/1.1/account/%73ettings.json",
                                 "https://x.com/i/api/1.1/account/settings.json;" + SECRET,
                                 "https://api.x.com/i/api/1.1/account/settings.json",
                                 "https://x.com/1.1/account/settings.json"])
def test_host_path_ids_usernames_malformed_urls_and_unicode_are_never_echoed(url):
    assert public_identity(url, "GET") == UNAVAILABLE


@pytest.mark.parametrize("method", [None, [], {}, SECRET, "get", "PUT", "DELETE", "OTHER"])
def test_unsupported_methods_do_not_acquire_public_identity(method):
    assert public_identity(GRAPH + "DeleteTweet", method) == UNAVAILABLE


@pytest.mark.parametrize("fields", [
    {"identity_status": SECRET, "public_operation": SECRET, "route_template": SECRET},
    {"identity_status": "public_operation", "public_operation": SECRET, "route_template": GRAPHQL_TEMPLATE},
    {"identity_status": "public_operation", "public_operation": "DeleteTweet", "route_template": SECRET},
    {"identity_status": "fixed_route", "route_template": "/i/api/1.1/statuses/destroy/1234567890.json"},
    {"identity_status": "fixed_route", "route_template": "/i/api/1.1/account/settings.json", "public_operation": SECRET},
    {"identity_status": "public_operation", "public_operation": [], "route_template": {}},
    {"identity_status": [], "public_operation": {}, "route_template": []},
    {"identity_status": "fixed_route", "route_template": "/i/api/1.1/account/settings.json", "method": []},
    {"identity_status": "public_operation", "public_operation": "DeleteTweet", "route_template": GRAPHQL_TEMPLATE, "method": SECRET},
    {"identity_status": "unavailable", "public_operation": "DeleteTweet", "route_template": GRAPHQL_TEMPLATE},
])
def test_export_independently_rejects_poisoned_identity_fields(fields):
    event = {"schema": 1, "trace": "abcdef123456", "event": "response", "method": "GET", "status": 429,
             "url": SECRET, "headers": SECRET, "body": SECRET, **fields}
    clean = sanitize_event(event)
    assert clean["identity_status"] == "unavailable"
    assert "public_operation" not in clean and "route_template" not in clean and SECRET not in json.dumps(clean)


def test_old_trace_and_unknown_digest_remain_readable_and_unavailable(tmp_path):
    digest = operation_digest(GRAPH + SECRET)
    assert digest == hashlib.sha256(("graphql:" + SECRET).encode()).hexdigest()
    event = {"schema": 1, "trace": "abcdef123456", "event": "response", "status": 429,
             "method": "GET", "operation_sha256": digest, "role": "unknown", "endpoint_group": "g1"}
    path = tmp_path / "log"
    path.write_text('2026-10-14 03:00:00,000 - kusamushiri - INFO - [test] KS_DIAG ' + json.dumps(event) + '\n')
    summary = diagnostic_summary(diagnostic_excerpt(path))
    info = summary["rate_limits"][0]
    assert info["identity_status"] == "unavailable" and info["operation_sha256"] == digest
    assert info["status"] == 429 and info["method"] == "GET" and info["role"] == "unknown"
    assert SECRET not in json.dumps(summary)


@pytest.mark.parametrize("status", [200, 401, 403, 420, 429, 500])
def test_late_response_keeps_public_identity_origin_without_reading_429_body(status):
    clock, events = Clock(), []
    trace = NetworkDiagnostics(sink=events.append, wall=clock.wall, monotonic=clock.monotonic)
    request = Request(clock, GRAPH + UNKNOWN_PUBLIC + "?auth=" + SECRET)
    request.resource_type = "xhr"
    with trace.operation(target()):
        trace.request(request)
    # A response wrapper can be mutated/reused: the request's original metadata is authoritative.
    request.url = GRAPH + SECRET
    with trace.operation(target(2)):
        response = Response(request, status=status)
        trace.response(response)
    reply = next(event for event in events if event["event"] == "response")
    assert reply["public_operation"] == UNKNOWN_PUBLIC and reply["api_label"] == "unrecognized"
    assert reply["origin_operation"] == "op1" and reply["operation"] == "op2" and reply["cross_operation"]
    assert reply["role"] == "unknown" and SECRET not in json.dumps(events)
    response.json.assert_not_called()
    summary = diagnostic_summary(events)
    if status in (420, 429):
        assert summary["rate_limits"][0]["public_operation"] == UNKNOWN_PUBLIC
    if status in (401, 403):
        assert summary["http_errors"][0]["public_operation"] == UNKNOWN_PUBLIC


def test_unmatched_response_still_records_method_and_validated_fixed_route():
    clock, events = Clock(), []
    trace = NetworkDiagnostics(sink=events.append, wall=clock.wall, monotonic=clock.monotonic)
    request = Request(clock, "https://api.x.com/1.1/account/settings.json?screen_name=" + SECRET)
    trace.response(Response(request))
    reply = next(event for event in events if event["event"] == "response")
    assert reply["method"] == "GET" and reply["identity_status"] == "fixed_route"
    assert reply["origin_operation"] is None and reply["role"] == "unknown"
    assert SECRET not in json.dumps(events)


@pytest.mark.parametrize("url", [GRAPH + UNKNOWN_PUBLIC, GRAPH + SECRET,
                                 "https://api.x.com/1.1/account/settings.json?token=" + SECRET])
@pytest.mark.parametrize("mode", ["global", "isolate-known"])
def test_known_public_or_unavailable_unknown429_preserves_exact_finite_cooldown_stop(scenario, tmp_path, url, mode):
    core, page, context, clock, events, emit = scenario
    isolation = core._browser.ancillary_isolation
    isolation.install(context, service_workers_blocked=True)
    assert isolation.ready
    active_states = []

    def navigate():
        active_states.append(isolation.active)
        emit(url)

    page.on_navigate = navigate
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock.tick(seconds)
        return False

    policy = replace(RetryPolicy(), cooldown_delay_seconds=30, max_delay_seconds=60,
                     initial_delay_seconds=15, max_wait_seconds=300, ancillary_rate_limit_mode=mode)
    journal = ActionJournal(tmp_path / "receipts.json", "synthetic")
    assert run_post_actions(core, [target()], 0, policy, journal=journal, wait_for_cancel=wait) == []
    assert active_states == [mode == "isolate-known"] * 4
    assert waits == [30, 60, 60] and len(page.navigations) == 4 and not page.clicks
    assert journal.entries[journal.key(target())] == "pending"
    assert core.last_action_batch_summary["stop_reason"] == "cooldown_limit"
    assert core.last_action_batch_summary["deferrals"] == 4 and core.last_action_batch_summary["retries"] == 0
    assert not any(e["event"] == "ancillary_quarantine" for e in events)
    assert all(e["role"] == "unknown" for e in events if e["event"] == "response")
    assert SECRET not in json.dumps(diagnostic_summary(events))


def test_catalog_is_bounded_literal_data_and_never_a_role_allowlist():
    assert len(PUBLIC_OPERATIONS) <= 512 and len(FIXED_ROUTES) <= 256
    assert all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name) for name in PUBLIC_OPERATIONS)
    assert all(method in {"GET", "POST"} and re.fullmatch(r"/[a-z0-9_/.]{1,150}", path) for method, path in FIXED_ROUTES)
    assert UNKNOWN_PUBLIC in PUBLIC_OPERATIONS and classify_rate_limit(GRAPH + UNKNOWN_PUBLIC) == ("unknown", "unclassified")
