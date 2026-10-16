"""Real ephemeral Chromium and locally fulfilled HTML: never a live X call."""

import json
import threading

import pytest
from test_verified_menu_dom import URL, document, fixture_browser  # noqa: F401

from kusamushiri.core import XDeleterCore
from kusamushiri.target_read_probe import TargetReadRequest


@pytest.mark.parametrize("case,count", [("single", 1), ("nested", 2), ("sibling", 2), ("hidden", 1),
                                       ("zero", 0), ("dialog", 1), ("foreign_root", 2), ("truncated", 1), ("unstable", 1)])
@pytest.mark.parametrize("preopened", [False, True])
def test_actual_playwright_counts_structure_and_caret_only(fixture_browser, monkeypatch, case, count, preopened):  # noqa: F811
    driver, browser = fixture_browser
    context = browser.new_context(service_workers="block", strict_selectors=True)
    page = context.new_page()
    html = document("dialog" if case == "dialog" else "none", "single")
    html = html.replace('menu.hidden=true; sheet.hidden=false', 'window.deleteMenuClicks++; menu.hidden=true; sheet.hidden=false')
    html = html.replace('window.finalClicks=0;', 'window.finalClicks=0; window.deleteMenuClicks=0; window.caretClicks=0;')
    html = html.replace('onclick="menu.hidden=false"', 'onclick="window.caretClicks++; menu.hidden=false"')
    html = html.replace('<time>time</time>', '<time>time</time><span>PRIVATE_POST_BODY_CANARY</span>')
    original = '<div role="menuitem" data-testid="delete" onclick="window.deleteMenuClicks++; menu.hidden=true; sheet.hidden=false">Delete</div>'
    if case == "nested":
        html = html.replace(original, '<div role="menuitem">'+original+'</div>')
    elif case in {"sibling", "hidden"}:
        html = html.replace(original, original+'<div role="menuitem" '+('style="display:none" ' if case == "hidden" else '')+'>Delete</div>')
    elif case == "zero":
        html = html.replace(original, '<div role="menuitem">Keep</div>')
    elif case == "foreign_root":
        html = html.replace(original, original+'</div><div data-testid="Dropdown"><div role="menuitem">Delete</div>')
    if case == "truncated":
        html = html.replace(original, original + '<div role="menuitem">Keep</div>'*80)
    if preopened:
        html = html.replace('<section id="menu" hidden>', '<section id="menu">')
    html = html.replace('</body>', '<div data-testid="PRIVATE_TOKEN_CANARY">PRIVATE_ACCOUNT_CANARY</div></body>')
    requests = []
    def local(route):
        requests.append((route.request.method, route.request.url))
        if route.request.url == URL and route.request.method == "GET":
            route.fulfill(content_type="text/html", body=html)
        else:
            route.abort()
    context.route("**/*", local)
    try:
        page.goto(URL)
        core = XDeleterCore()
        manager = core._browser
        manager.browser, manager.context, manager.page, manager.playwright = browser, context, page, driver
        def forbidden(*args, **kwargs):
            pytest.fail("post-action or journal path entered")
        monkeypatch.setattr(core, "execute_post_action", forbidden)
        if preopened:
            monkeypatch.setattr(core, "verified_single_post_scope", forbidden)
            monkeypatch.setattr("kusamushiri.view_cache.PendingView.capture", forbidden)
            pending = object()
            core._pending_view = pending
        monkeypatch.setattr("kusamushiri.progress.ActionJournal.__init__", forbidden)
        if case == "unstable":
            original_evaluate = page.evaluate
            def evaluate(expression, *args, **kwargs):
                value = original_evaluate(expression, *args, **kwargs)
                if "const unique" in expression:
                    original_evaluate("""() => {
                        const extra = document.createElement('div');
                        extra.setAttribute('role', 'menuitem'); extra.textContent = 'Delete';
                        document.querySelector('[data-testid="Dropdown"]').append(extra);
                    }""")
                return value
            monkeypatch.setattr(page, "evaluate", evaluate)
        result = core.diagnose_post_menu(TargetReadRequest(URL, "author"), worker_thread_id=threading.get_ident())
        fields = result.safe_fields()
        assert result.reason == "captured", fields
        snapshot = fields["snapshot"]
        assert snapshot["union_visible"] == count
        assert snapshot["stable"] == (case != "unstable")
        assert snapshot["truncated"] == (case == "truncated")
        assert len(snapshot["rows"]) <= 48
        if case == "truncated":
            assert snapshot["menuitem_total"] == 81
        assert fields["caret_click_attempted"] == (not preopened)
        assert fields["inspection_mode"] == ("existing_overlay_read_only" if preopened else "caret_only")
        assert not fields["delete_requested"] and not fields["journal_used"]
        assert page.evaluate("window.caretClicks") == int(not preopened)
        if preopened:
            assert core._pending_view is pending and core._verified_post_action is None
        assert page.evaluate("window.deleteMenuClicks") == 0 and page.evaluate("window.finalClicks") == 0
        assert page.evaluate("document.getElementById('sheet').hidden")
        assert requests == [("GET", URL)] and 'PRIVATE' not in json.dumps(fields) and 'https:' not in json.dumps(fields)
        if case == "nested":
            assert snapshot["selector_counts"] == [1, 2, 0, 0]
            assert any(row["contains_candidates"] for row in snapshot["rows"])
        elif case == "hidden":
            hidden = [row for row in snapshot["rows"] if not row["visible"]]
            assert len(hidden) == 1 and hidden[0]["selector_matches"] == [False]*4
        elif case == "foreign_root":
            assert snapshot["dropdown_visible"] == 2
            assert {row["dropdown_index"] for row in snapshot["rows"]} == {0, 1}
        elif case == "dialog":
            assert any(row["dialog_ancestors"] == 1 for row in snapshot["rows"])
    finally:
        context.close()
