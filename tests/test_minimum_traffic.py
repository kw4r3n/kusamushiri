"""Synthetic only: this prototype has no browser, cookies, headers or transport."""

from dataclasses import replace

import pytest

from kusamushiri.minimum_traffic import (
    MinimumTrafficPrototype,
    OfflineDependencyCapsule,
    OfflineRoute,
    SyntheticReceipt,
    SyntheticRequest,
)

TARGET = "https://x.com/fixture/status/123"
ASSET = "https://abs.twimg.com/responsive-web/fixture/bootstrap.fixture.js"
DETAIL = "https://x.com/i/api/graphql/fixture/TweetDetail"
RESULT = "https://x.com/i/api/graphql/fixture/TweetResultByRestId"
DELETE = "https://x.com/i/api/graphql/fixture/DeleteTweet"


def fixture_capsule():
    return OfflineDependencyCapsule(
        assets=frozenset({(ASSET, "script")}), bootstrap_complete=True, scope="offline-fixture",
        api_requests=frozenset({("GET", DETAIL, "xhr"), ("GET", RESULT, "xhr"), ("POST", DELETE, "xhr")}),
    )


def prototype():
    p = MinimumTrafficPrototype(TARGET, fixture_capsule(), True, True, True)
    assert p.start()
    return p


def loading(p):
    assert p.route(OfflineRoute(SyntheticRequest(TARGET, "GET", "document")))
    assert p.route(OfflineRoute(SyntheticRequest(ASSET, "GET", "script")))
    assert p.route(OfflineRoute(SyntheticRequest(DETAIL, "GET", "xhr", "123")))
    assert p.route(OfflineRoute(SyntheticRequest(RESULT, "GET", "xhr", "123")))


def armed():
    p = prototype()
    loading(p)
    assert p.verify_view(logged_in=True, owned_target_article=True, correct_target_id=True, delete_menu=True, confirmation=True)
    assert p.arm_delete()
    return p


def mutation(p):
    request = SyntheticRequest(DELETE, "POST", "xhr", "123")
    assert p.route(OfflineRoute(request))
    return request


def test_one_view_not_one_request_per_concept_and_no_result_polling():
    p = armed()
    request = mutation(p)
    assert p.finish(SyntheticReceipt(request, 200)) == "success"
    assert p.confirmed and p.document_count == 1 and sum(p.read_counts.values()) == 2
    assert p.events.count("fixture-dispatch") == 5
    assert not any("auth" in event or "poll" in event for event in p.events)


@pytest.mark.parametrize("overrides,reason", [
    ({"experimental_opt_in": False}, "opt-in-required"),
    ({"service_workers_blocked": False}, "routing-guarantee-missing"),
    ({"routing_ready": False}, "routing-guarantee-missing"),
    ({"capsule": OfflineDependencyCapsule()}, "bootstrap-dependencies-unresolved"),
    ({"capsule": replace(fixture_capsule(), scope="live")}, "bootstrap-dependencies-unresolved"),
    ({"capsule": replace(fixture_capsule(), target_reads=frozenset({"HomeTimeline"}))}, "target-read-dependencies-unresolved"),
    ({"target_url": TARGET + "?redirect=other"}, "invalid-target"),
    ({"target_url": "https://x.com/home"}, "invalid-target"),
])
def test_unestablished_bootstrap_blocks_before_any_dispatch(overrides, reason):
    values = dict(target_url=TARGET, capsule=fixture_capsule(), experimental_opt_in=True, service_workers_blocked=True, routing_ready=True)
    values.update(overrides)
    p = MinimumTrafficPrototype(**values)
    assert not p.start() and p.stop_reason == reason
    route = OfflineRoute(SyntheticRequest(TARGET, "GET", "document"))
    assert not p.route(route) and route.aborted and not route.dispatched
    assert not p.sent


@pytest.mark.parametrize("url,resource", [
    ("https://evil.example/bootstrap.js", "script"),
    (ASSET + "?token=fixture", "script"),
    (ASSET, "xhr"), (ASSET.replace("https", "http"), "script"),
    (ASSET.replace("abs.twimg.com", "abs.twimg.com@evil.example"), "script"),
])
def test_static_capsule_is_exact_and_cannot_hide_api_requests(url, resource):
    p = MinimumTrafficPrototype(TARGET, replace(fixture_capsule(), assets=frozenset({(url, resource)})), True, True, True)
    assert not p.start() and p.stop_reason == "invalid-static-capsule"


@pytest.mark.parametrize("fixture_request", [
    SyntheticRequest("https://x.com/home", "GET", "document"),
    SyntheticRequest("https://x.com/i/api/graphql/fixture/HomeTimeline", "GET", "xhr", "123"),
    SyntheticRequest("https://x.com/i/api/graphql/fixture/CreatorStudioTabBarItemQuery", "GET", "xhr", "123"),
    SyntheticRequest("https://x.com/i/api/graphql/fixture/grokAccountJwtQuery", "GET", "xhr", "123"),
    SyntheticRequest("https://x.com/i/api/graphql/fixture/ViewerBadgeCounts", "GET", "xhr", "123"),
    SyntheticRequest("https://x.com/i/api/fleets/v1/fleetline?only_spaces=true", "GET", "xhr", "123"),
    SyntheticRequest("https://x.com/i/unknown-fixture", "GET", "xhr"),
    SyntheticRequest("https://x.com/i/api/graphql/fixture/DeleteRetweet", "POST", "xhr", "123"),
    SyntheticRequest("https://x.com/i/api/graphql/fixture/UnfavoriteTweet", "POST", "xhr", "123"),
    SyntheticRequest(DELETE, "POST", "xhr", "456"),
    SyntheticRequest(DETAIL, "POST", "xhr", "123"),
    SyntheticRequest(DELETE, "GET", "xhr", "123"),
    SyntheticRequest(DETAIL + "/extra", "GET", "xhr", "123"),
    SyntheticRequest(DETAIL.replace("TweetDetail", "%54weetDetail"), "GET", "xhr", "123"),
    SyntheticRequest(DETAIL.replace("x.com", "x.com:443"), "GET", "xhr", "123"),
    SyntheticRequest(DETAIL.replace("x.com", "x.com@evil.example"), "GET", "xhr", "123"),
    SyntheticRequest(DETAIL + "#fragment", "GET", "xhr", "123"),
    SyntheticRequest(DETAIL + "\n", "GET", "xhr", "123"),
    SyntheticRequest(DETAIL, "GET", "xhr", "123", owner="other-page"),
    SyntheticRequest(DETAIL, "GET", "xhr", "123", frame="subframe"),
    SyntheticRequest(DETAIL, "GET", "xhr", "123", frame="worker"),
    SyntheticRequest(DETAIL, "GET", "xhr", "123", redirected=True),
    SyntheticRequest(ASSET.replace("bootstrap", "unknown-chunk"), "GET", "script"),
    SyntheticRequest(ASSET, "POST", "script"),
])
def test_unestablished_request_aborts_and_stops_before_mutation(fixture_request):
    p = prototype()
    route = OfflineRoute(fixture_request)
    assert not p.route(route) and route.aborted and not route.dispatched
    assert p.stop_reason == "unestablished-or-excess-traffic" and not p.sent
    assert not p.arm_delete()


@pytest.mark.parametrize("predicate", ["logged_in", "owned_target_article", "correct_target_id", "delete_menu", "confirmation"])
def test_required_ui_proof_is_not_replaced_by_successful_loading(predicate):
    p = prototype()
    loading(p)
    values = dict(logged_in=True, owned_target_article=True, correct_target_id=True, delete_menu=True, confirmation=True)
    values[predicate] = False
    assert not p.verify_view(**values)
    assert not p.route(OfflineRoute(SyntheticRequest(DELETE, "POST", "xhr", "123")))
    assert not p.sent


def test_unarmed_mutation_and_second_mutation_never_dispatch():
    p = prototype()
    assert not p.route(OfflineRoute(SyntheticRequest(DELETE, "POST", "xhr", "123")))
    assert not p.sent
    p = armed()
    request = mutation(p)
    second = OfflineRoute(request)
    assert not p.route(second) and second.aborted and not second.dispatched
    assert p.finish(SyntheticReceipt(request, 200)) == "success"
    assert p.stop_reason and p.state == "stopped"


@pytest.mark.parametrize("resource,url,target_id", [("document", TARGET, None), ("script", ASSET, None), ("xhr", DETAIL, "123")])
def test_duplicate_navigation_static_or_read_is_not_a_new_budget(resource, url, target_id):
    p = prototype()
    request = SyntheticRequest(url, "GET", resource, target_id)
    assert p.route(OfflineRoute(request))
    assert not p.route(OfflineRoute(request))
    assert not p.sent


def test_missing_control_never_falls_through():
    p = prototype()
    route = OfflineRoute(SyntheticRequest("https://evil.example/fixture", "GET", "xhr"), fail_abort=True)
    assert not p.route(route) and not route.dispatched and p.stop_reason == "abort-control-failed"
    assert not p.sent


@pytest.mark.parametrize("status", [401, 403, 420, 429])
@pytest.mark.parametrize("after_send", [False, True])
def test_permitted_traffic_auth_rate_limits_remain_blocking(status, after_send):
    p = armed()
    request = mutation(p) if after_send else None
    p.observe_http(status=status, retry_after_seconds=999)
    assert p.server_status == status and p.retry_after_seconds == 999
    if after_send:
        assert p.finish(SyntheticReceipt(request, status)) in {"blocked", "rate-refused"}
    else:
        route = OfflineRoute(SyntheticRequest(DELETE, "POST", "xhr", "123"))
        assert not p.route(route) and not p.sent
    assert not p.confirmed and not p.arm_delete()


@pytest.mark.parametrize("kind", ["missing", "wrong-request", "5xx", "unknown-body", "dom-only", "explicit-refusal", "late"])
def test_truthful_receipts_no_replay_under_uncertain_outcome(kind):
    p = armed()
    request = mutation(p)
    receipt = SyntheticReceipt(request, 200)
    if kind == "missing":
        receipt = None
    elif kind == "wrong-request":
        receipt = SyntheticReceipt(replace(request), 200)
    elif kind == "5xx":
        receipt = SyntheticReceipt(request, 503)
    elif kind == "unknown-body":
        receipt = SyntheticReceipt(request, 200, "unrecognized")
    elif kind == "dom-only":
        receipt = SyntheticReceipt(request, 200, dom_complete=False)
    elif kind == "explicit-refusal":
        receipt = SyntheticReceipt(request, 200, "explicit-refusal")
    else:
        p.stop("cancelled-after-send")
    assert p.finish(receipt) == ("refused" if kind == "explicit-refusal" else "success" if kind == "late" else "ambiguous")
    assert p.confirmed == (kind == "late")
    assert not p.route(OfflineRoute(request)) and not p.arm_delete()


def test_actual_python_action_stages_with_unknown_read_abort_no_submission():
    from test_retry import ActionPage, target

    from kusamushiri.actions import execute_post_action
    p = prototype()
    page = ActionPage()
    def goto(url, **kwargs):
        route = OfflineRoute(SyntheticRequest("https://x.com/i/unknown-fixture", "GET", "xhr"))
        assert not p.route(route)
    page.goto = goto
    result = execute_post_action(page, target(), lambda owner, selectors: owner.locator(selectors[0]), should_cancel=lambda: p.stop_reason is not None)
    assert not result.success and not any("confirmationSheetConfirm" in s for s in page.clicks)
    assert not p.sent


def test_no_browser_route_or_live_receipt_can_enter_prototype():
    p = prototype()
    with pytest.raises(TypeError):
        p.route(object())
    assert not p.sent


def test_single_read_ablation_is_fixture_evidence_only():
    capsule = replace(fixture_capsule(), target_reads=frozenset({"TweetDetail"}),
                      api_requests=frozenset({("GET", DETAIL, "xhr"), ("POST", DELETE, "xhr")}))
    p = MinimumTrafficPrototype(TARGET, capsule, True, True, True)
    assert p.start()
    assert p.route(OfflineRoute(SyntheticRequest(TARGET, "GET", "document")))
    assert p.route(OfflineRoute(SyntheticRequest(DETAIL, "GET", "xhr", "123")))
    assert p.verify_view(logged_in=True, owned_target_article=True, correct_target_id=True, delete_menu=True, confirmation=True)
    assert p.arm_delete()
    request = mutation(p)
    assert p.finish(SyntheticReceipt(request, 200)) == "success"
    assert p.document_count == 1 and sum(p.read_counts.values()) == 1


def test_no_production_command_imports_or_enables_minimum_traffic():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src/kusamushiri"
    for name in ("cli.py", "core.py", "browser.py", "app.py", "gui.py", "retry.py"):
        assert "minimum_traffic" not in (root / name).read_text()


def test_late_unknown_rate_limit_preserves_ack_and_stop():
    p = armed()
    request = mutation(p)
    p.observe_http(status=429, retry_after_seconds=999)
    assert p.finish(SyntheticReceipt(request, 200)) == "success"
    assert p.confirmed and p.server_status == 429 and p.retry_after_seconds == 999
    assert p.state == "stopped" and not p.arm_delete()


@pytest.mark.parametrize("suffix", ["?", "#", ";", "?unexpected=1", "?variables=%7B%7D", "#fragment"])
@pytest.mark.parametrize("kind", ["read", "mutation", "document", "static"])
def test_no_url_suffix_can_escape_exact_membership(suffix, kind):
    p = armed() if kind == "mutation" else prototype()
    url, method, resource, identity = {
        "read": (DETAIL, "GET", "xhr", "123"),
        "mutation": (DELETE, "POST", "xhr", "123"),
        "document": (TARGET, "GET", "document", None),
        "static": (ASSET, "GET", "script", None),
    }[kind]
    route = OfflineRoute(SyntheticRequest(url + suffix, method, resource, identity))
    assert not p.route(route) and route.aborted and not route.dispatched and not p.sent


@pytest.mark.parametrize("url", [DETAIL, RESULT, DELETE])
def test_persisted_query_hash_is_part_of_the_capsule_identity(url):
    p = armed() if url == DELETE else prototype()
    route = OfflineRoute(SyntheticRequest(url.replace("/fixture/", "/different-fixture-hash/"),
                                        "POST" if url == DELETE else "GET", "xhr", "123"))
    assert not p.route(route) and route.aborted and not p.sent


@pytest.mark.parametrize("method,url,resource", [
    ("GET", DETAIL + "?", "xhr"), ("GET", DETAIL + "#", "xhr"),
    ("POST", DELETE + ";", "xhr"), ("POST", DELETE + "?unexpected=1", "xhr"),
    ("POST", DETAIL, "xhr"), ("GET", DELETE, "xhr"),
    ("GET", DETAIL, "document"), ("POST", DELETE, "script"),
    ("GET", DETAIL.replace("TweetDetail", "HomeTimeline"), "xhr"),
])
def test_declaring_a_bad_api_member_does_not_make_it_valid(method, url, resource):
    capsule = replace(fixture_capsule(), api_requests=fixture_capsule().api_requests | {(method, url, resource)})
    p = MinimumTrafficPrototype(TARGET, capsule, True, True, True)
    assert not p.start() and p.stop_reason == "invalid-api-capsule"
    assert not p.sent and p.events == ["stopped"]


@pytest.mark.parametrize("entries", [
    frozenset(),
    frozenset({("GET", DETAIL, "xhr"), ("POST", DELETE, "xhr")}),
    frozenset({("GET", DETAIL, "xhr"), ("GET", RESULT, "xhr")}),
])
def test_incomplete_api_capsule_blocks_before_dispatch(entries):
    p = MinimumTrafficPrototype(TARGET, replace(fixture_capsule(), api_requests=entries), True, True, True)
    assert not p.start() and p.stop_reason == "api-identities-unresolved"
    assert not p.sent and not p.document_count


def test_resource_variant_requires_an_explicit_api_capsule_entry():
    p = prototype()
    denied = OfflineRoute(SyntheticRequest(DETAIL, "GET", "fetch", "123"))
    assert not p.route(denied) and denied.aborted
    capsule = replace(fixture_capsule(), api_requests=fixture_capsule().api_requests | {("GET", DETAIL, "fetch")})
    p = MinimumTrafficPrototype(TARGET, capsule, True, True, True)
    assert p.start() and p.route(OfflineRoute(SyntheticRequest(DETAIL, "GET", "fetch", "123")))
    assert not p.route(OfflineRoute(SyntheticRequest(DETAIL, "GET", "xhr", "123")))
    assert not p.sent  # Switching listed resource identities cannot reset read budget.


@pytest.mark.parametrize("status", [500, 501, 502, 503, 504, 507, 599])
@pytest.mark.parametrize("semantics", ["explicit-refusal", "accepted", "malformed"])
def test_submitted_5xx_has_ambiguous_effect_regardless_of_semantic_label(status, semantics):
    p = armed()
    request = mutation(p)
    assert p.finish(SyntheticReceipt(request, status, semantics)) == "ambiguous"
    assert p.stop_reason == "uncertain-outcome-no-replay" and not p.confirmed
    assert not p.route(OfflineRoute(request)) and not p.arm_delete()


@pytest.mark.parametrize("status", [0, 100, 199, 300, 400, 404, 499, 600, True, 200.0, "200", None])
def test_unestablished_status_cannot_turn_a_label_into_definite_refusal(status):
    p = armed()
    request = mutation(p)
    assert p.finish(SyntheticReceipt(request, status, "explicit-refusal")) == "ambiguous"
    assert not p.confirmed and not p.arm_delete()


def test_established_2xx_error_semantics_remain_explicit_refusal():
    p = armed()
    request = mutation(p)
    assert p.finish(SyntheticReceipt(request, 200, "explicit-refusal")) == "refused"
    assert not p.confirmed and p.stop_reason == "explicit-refusal-no-prototype-retry"


@pytest.mark.parametrize("late", ["none", "clone", "5xx", "malformed", "dom-incomplete", "contradictory-refusal"])
def test_success_is_monotonic_across_incomplete_or_contradictory_observations(late):
    p = armed()
    request = mutation(p)
    assert p.finish(SyntheticReceipt(request, 200)) == "success"
    observations = {
        "none": None,
        "clone": SyntheticReceipt(replace(request), 200),
        "5xx": SyntheticReceipt(request, 503, "explicit-refusal"),
        "malformed": SyntheticReceipt(request, "200"),
        "dom-incomplete": SyntheticReceipt(request, 200, dom_complete=False),
        "contradictory-refusal": SyntheticReceipt(request, 200, "explicit-refusal"),
    }
    for _ in range(2):
        assert p.finish(observations[late]) == "success" and p.confirmed
        assert p.state == "stopped" and p.stop_reason == "post-completion-observation-fault"
    assert not p.arm_delete() and not p.route(OfflineRoute(request))
    assert p.finish(SyntheticReceipt(request, 200)) == "success" and p.state == "stopped"


@pytest.mark.parametrize("status", [401, 403, 420, 429])
def test_latched_success_never_clears_later_http_stop_or_deadline(status):
    p = armed()
    request = mutation(p)
    assert p.finish(SyntheticReceipt(request, 200)) == "success"
    p.observe_http(status=status, retry_after_seconds=999)
    assert p.finish(SyntheticReceipt(request, status)) == "success"
    assert p.confirmed and p.state == "stopped" and p.server_status == status
    assert p.retry_after_seconds == 999 and p.stop_reason is not None
    assert p.finish(None) == "success" and p.retry_after_seconds == 999
    assert not p.arm_delete() and not p.route(OfflineRoute(request))


def test_duplicate_correct_completion_is_idempotent_and_does_not_stop():
    p = armed()
    request = mutation(p)
    receipt = SyntheticReceipt(request, 200)
    assert p.finish(receipt) == p.finish(receipt) == "success"
    assert p.confirmed and p.state == "complete" and p.stop_reason is None
    assert not p.arm_delete()  # Completion does not re-arm its already sent mutation.
    assert p.finish(receipt) == "success" and p.state == "stopped"
