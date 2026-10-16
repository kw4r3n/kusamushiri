"""Synthetic sibling-label regression; every browser request is locally handled.

The extra label is invented fixture text, not the unknown second live label.
"""

import threading

import pytest
from test_verified_menu_dom import URL, document, fixture_browser  # noqa: F401

from kusamushiri import actions, verified_post_action
from kusamushiri.core import XDeleterCore
from kusamushiri.models import PostActionTarget, RetryPolicy
from kusamushiri.progress import ActionJournal
from kusamushiri.retry import run_post_actions
from kusamushiri.target_read_probe import TargetReadRequest


def partial_document(label="削除", *, other_first=False):
    html = document("none", "single")
    exact = f'<div role="menuitem" onclick="menu.hidden=true; sheet.hidden=false"><div><svg></svg></div><span>{label}</span></div>'
    other = f'<div role="menuitem" onclick="window.otherClicks++"><span>synthetic {label} suffix</span></div>'
    fillers = '<div role="menuitem">Keep</div>' * 11
    old = '<div role="menuitem" data-testid="delete" onclick="menu.hidden=true; sheet.hidden=false">Delete</div>'
    html = html.replace(old, (other + exact if other_first else exact + other) + fillers)
    return html.replace('window.finalClicks=0;', 'window.finalClicks=0; window.otherClicks=0;')


def local_core(browser_fixture, html, *, strict=True):
    driver, browser = browser_fixture
    context = browser.new_context(service_workers="block", strict_selectors=strict)
    requests = []

    def local(route):
        request = route.request
        requests.append((request.method, request.url))
        if request.url == URL and request.method == "GET":
            route.fulfill(content_type="text/html; charset=utf-8", body=html)
        elif request.url == "https://x.com/i/api/graphql/synthetic/DeleteTweet" and request.method == "POST":
            route.fulfill(content_type="application/json", body='{"data":{"delete_tweet":{"tweet_results":{}}}}')
        else:
            route.abort()

    context.route("**/*", local)
    page = context.new_page()
    page.goto(URL)
    core = XDeleterCore()
    manager = core._browser
    manager.browser, manager.context, manager.page, manager.playwright = browser, context, page, driver
    return core, page, context, requests


def run(core, tmp_path):
    target = PostActionTarget(URL, "post")
    with core.verified_single_post_scope(target, "author", current_view_only=True):
        return run_post_actions(core, [target], 0, RetryPolicy(max_retries=0, max_cooldowns=0),
                                journal=ActionJournal(tmp_path / "synthetic.json", "synthetic"))[0]


@pytest.mark.parametrize("label", ["Delete", "削除", "삭제"])
@pytest.mark.parametrize("other_first", [False, True])
@pytest.mark.parametrize("legacy_predicate", [False, True])
def test_exact_label_causally_removes_only_synthetic_partial_sibling(
        fixture_browser, tmp_path, monkeypatch, label, other_first, legacy_predicate):  # noqa: F811
    core, page, context, requests = local_core(fixture_browser, partial_document(label, other_first=other_first))
    stops = []
    monkeypatch.setattr(core._browser.diagnostics, "verification_stop", lambda stage, reason: stops.append((stage, reason)))
    if legacy_predicate:
        # Reinsert only the prior full substring union; all identity/control gates
        # and this fixture remain identical for the causal comparison.
        def legacy(page):
            return page.locator(", ".join(actions.DELETE_MENU_ITEM_SELECTORS))
        monkeypatch.setattr(actions, "delete_menu_items", legacy)
        monkeypatch.setattr(verified_post_action, "delete_menu_items", legacy)
    try:
        result = run(core, tmp_path)
        assert result.success == (not legacy_predicate), result
        assert page.evaluate("window.otherClicks") == 0
        assert page.evaluate("window.finalClicks") == int(not legacy_predicate)
        assert requests == [("GET", URL)] + ([] if legacy_predicate else [
            ("POST", "https://x.com/i/api/graphql/synthetic/DeleteTweet")])
        if legacy_predicate:
            assert result.failure_kind == "blocked" and stops == [("delete_menu", "control_count")]
        else:
            assert stops == []
    finally:
        context.close()


@pytest.mark.parametrize("case", ["duplicate", "nested", "foreign_menu", "partial_only", "label_changed", "delayed", "hidden", "dedup"])
def test_exact_label_keeps_ambiguity_and_pinned_control_checks(fixture_browser, tmp_path, monkeypatch, case):  # noqa: F811
    html = partial_document()
    exact = '<div role="menuitem" onclick="menu.hidden=true; sheet.hidden=false"><div><svg></svg></div><span>削除</span></div>'
    if case == "duplicate":
        html = html.replace(exact, exact + exact)
    elif case == "nested":
        html = html.replace(exact, '<div role="menuitem">' + exact + '</div>')
    elif case == "foreign_menu":
        html = html.replace(exact, exact + '</div><div data-testid="Dropdown">' + exact)
    elif case == "partial_only":
        html = html.replace(exact, '<div role="menuitem">削除<span> synthetic suffix</span></div>')
        monkeypatch.setattr(actions, "ACTION_STATE_TIMEOUT_MS", 100)
    elif case == "delayed":
        html = html.replace(exact, '')
        html = html.replace('onclick="menu.hidden=false"', 'onclick="menu.hidden=false; setTimeout(addExact,80)"')
        html = html.replace('window.finalClicks=0;', f'function addExact() {{ document.querySelector(\'[data-testid="Dropdown"]\').insertAdjacentHTML("beforeend", {exact!r}); }} window.finalClicks=0;')
    elif case == "hidden":
        html = html.replace(exact, exact + '<div role="menuitem" hidden>削除</div>')
    elif case == "dedup":
        html = html.replace(exact, exact.replace('role="menuitem"', 'role="menuitem" data-testid="delete"'))
    core, page, context, requests = local_core(fixture_browser, html)
    if case == "label_changed":
        original = verified_post_action.VerifiedSinglePost.prepare_control

        def prepare(scope, locator, stage):
            click = original(scope, locator, stage)
            if stage == "delete_menu":
                page.evaluate("""() => document.querySelector('[data-testid="Dropdown"] [role="menuitem"]').textContent = '削除 synthetic suffix'""")
            return click
        monkeypatch.setattr(verified_post_action.VerifiedSinglePost, "prepare_control", prepare)
    try:
        result = run(core, tmp_path)
        success = case in {"delayed", "hidden", "dedup"}
        assert result.success == success, result
        assert page.evaluate("window.otherClicks") == 0
        assert page.evaluate("window.finalClicks") == int(success)
        assert len(requests) == 1 + int(success)
        if not success:
            assert result.failure_kind == ("permanent" if case == "partial_only" else "blocked")
    finally:
        context.close()


def test_passive_diagnostic_uses_same_exact_union_without_claiming_menu_origin(fixture_browser, monkeypatch):  # noqa: F811
    html = partial_document(other_first=True).replace('<section id="menu" hidden>', '<section id="menu">')
    core, page, context, requests = local_core(fixture_browser, html)
    monkeypatch.setattr(core, "verified_single_post_scope", lambda *a, **k: pytest.fail("passive action scope"))
    try:
        result = core.diagnose_post_menu(TargetReadRequest(URL, "author"), worker_thread_id=threading.get_ident())
        fields = result.safe_fields()
        assert fields["inspection_mode"] == "existing_overlay_read_only" and not fields["caret_click_attempted"]
        assert fields["snapshot"]["menuitem_total"] == 13
        assert fields["snapshot"]["union_visible"] == 1
        assert fields["snapshot"]["selector_counts"] == [0, 0, 1, 0]
        assert page.evaluate("window.finalClicks") == page.evaluate("window.otherClicks") == 0
        assert requests == [("GET", URL)]
    finally:
        context.close()


def test_shared_nonverified_action_also_rejects_ambiguous_union(fixture_browser):  # noqa: F811
    html = partial_document().replace('<div role="menuitem">Keep</div>', '<div role="menuitem">削除</div>', 1)
    core, page, context, requests = local_core(fixture_browser, html)
    try:
        # Default ActionEvidence has no VerifiedSinglePost callback. It must not
        # make a two-candidate union eligible merely because evidence exists.
        result = actions.execute_post_action(page, PostActionTarget(URL, "post"))
        assert not result.success and result.failure_kind == "blocked"
        assert page.evaluate("window.finalClicks") == page.evaluate("window.otherClicks") == 0
        assert requests == [("GET", URL), ("GET", URL)]
    finally:
        context.close()
