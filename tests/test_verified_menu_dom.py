"""Owned ephemeral Chromium, locally fulfilled fixture only; no auth or X network."""

import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from kusamushiri.core import XDeleterCore
from kusamushiri.models import PostActionTarget, RetryPolicy
from kusamushiri.progress import ActionJournal
from kusamushiri.retry import run_post_actions

URL = "https://x.com/author/status/123"


@pytest.fixture(scope="module")
def fixture_browser():
    with sync_playwright() as driver:
        executable = os.environ.get("KUSAMUSHIRI_TEST_BROWSER_EXECUTABLE", driver.chromium.executable_path)
        if not Path(executable).is_file():
            pytest.skip("An installed Chromium executable is required; no browser is downloaded")
        browser = driver.chromium.launch(executable_path=executable, headless=True, chromium_sandbox=True, timeout=5000)
        try:
            yield driver, browser
        finally:
            browser.close()


def document(menu_wrapper, confirm_wrapper, *, unrelated=False):
    menu_open, menu_close = ("", "") if menu_wrapper == "none" else (f'<div role="{menu_wrapper}">', '</div>')
    confirm_open, confirm_close = ('<div role="dialog"><div role="alertdialog">', '</div></div>') if confirm_wrapper == "nested" else ('<div role="dialog">', '</div>')
    extra = '<div role="dialog" id="unrelated">Unrelated</div>' if unrelated else ''
    return f'''<!doctype html><html><body>
      <button data-testid="SideNav_NewTweet_Button">Post</button>
      <a data-testid="AppTabBar_Profile_Link" href="/author">Profile</a>
      <article data-testid="tweet"><a href="/author/status/123"><time>time</time></a>
        <button data-testid="caret" onclick="menu.hidden=false">More</button></article>
      <section id="menu" hidden>{menu_open}<div data-testid="Dropdown">
        <div role="menuitem" data-testid="delete" onclick="menu.hidden=true; sheet.hidden=false">Delete</div>
      </div>{menu_close}{extra}</section>
      <section id="sheet" hidden>{confirm_open}
        <button data-testid="confirmationSheetConfirm" onclick="finish()">Delete</button>
      {confirm_close}</section>
      <script>
        window.finalClicks=0;
        async function finish() {{
          window.finalClicks++;
          await fetch('/i/api/graphql/synthetic/DeleteTweet', {{method:'POST',body:'{{}}'}});
          document.querySelector('article').remove();
        }}
      </script></body></html>'''


@pytest.mark.parametrize("case,menu_wrapper,confirm_wrapper", [
    ("valid", "none", "single"), ("valid", "dialog", "single"), ("valid", "alertdialog", "nested"),
    ("unrelated_menu", "dialog", "single"), ("unrelated_confirm", "dialog", "nested"),
    ("duplicate_item", "dialog", "single"), ("duplicate_confirm", "dialog", "single"),
    ("replace_menu_dialog", "dialog", "single"), ("replace_confirm_dialog", "dialog", "nested"),
])
def test_portal_control_ownership(fixture_browser, tmp_path, monkeypatch, case, menu_wrapper, confirm_wrapper):
    driver, browser = fixture_browser
    context = browser.new_context(service_workers="block")
    page = context.new_page()
    requests = []
    html = document(menu_wrapper, confirm_wrapper, unrelated=case == "unrelated_menu")
    if case == "unrelated_confirm":
        html = html.replace('</section>\n      <script>', '<div role="dialog">Foreign</div></section>\n      <script>')
    if case == "duplicate_item":
        html = html.replace('</div>\n      </div>', '</div><div role="menuitem" data-testid="delete">Delete</div>\n      </div>')
    if case == "duplicate_confirm":
        html = html.replace('onclick="finish()">Delete</button>', 'onclick="finish()">Delete</button><button data-testid="confirmationSheetConfirm">Delete</button>')
    if case.startswith("replace_"):
        from kusamushiri.verified_post_action import VerifiedSinglePost
        original = VerifiedSinglePost.prepare_control

        def prepared(scope, locator, stage):
            click = original(scope, locator, stage)
            replace_stage = "delete_menu" if case == "replace_menu_dialog" else "confirm"
            if stage != replace_stage:
                return click

            def replace_then_click():
                # Replace only the wrapper, preserving the exact owned controls.
                page.evaluate("""() => {
                    const old = document.querySelector('#menu:not([hidden]) [role="dialog"], #sheet:not([hidden]) [role="dialog"]');
                    const replacement = old.cloneNode(false);
                    replacement.append(...old.childNodes); old.replaceWith(replacement);
                }""")
                click()
            return replace_then_click
        monkeypatch.setattr(VerifiedSinglePost, "prepare_control", prepared)

    def local(route):
        request = route.request
        requests.append((request.method, request.url))
        if request.url == URL and request.method == "GET":
            route.fulfill(content_type="text/html", body=html)
        elif request.url == "https://x.com/i/api/graphql/synthetic/DeleteTweet" and request.method == "POST":
            route.fulfill(content_type="application/json", body='{"data":{"delete_tweet":{"tweet_results":{}}}}')
        else:
            route.abort()

    context.route("**/*", local)
    try:
        page.goto(URL)
        core = XDeleterCore()
        manager = core._browser
        manager.browser, manager.context, manager.page, manager.playwright = browser, context, page, driver
        target = PostActionTarget(URL, "post")
        with core.verified_single_post_scope(target, "author", current_view_only=True):
            result = run_post_actions(core, [target], 0, RetryPolicy(max_retries=0, max_cooldowns=0),
                                      journal=ActionJournal(tmp_path / "synthetic-receipt.json", "synthetic"))[0]
        if case == "valid":
            assert result.success, result
            assert page.evaluate("window.finalClicks") == 1
            assert requests == [("GET", URL), ("POST", "https://x.com/i/api/graphql/synthetic/DeleteTweet")]
        else:
            assert not result.success, result
            assert result.failure_kind == ("ambiguous" if case == "replace_confirm_dialog" else "blocked")
            assert page.evaluate("window.finalClicks") == 0
            assert requests == [("GET", URL)]
            if not case.startswith("replace_"):
                assert "メニューまたは確認ボタン" in result.error_message
    finally:
        context.close()


@pytest.mark.parametrize("stage,replaced,failure_kind", [
    ("delete_menu", "control", "blocked"), ("delete_menu", "root", "blocked"), ("confirm", "control", "ambiguous"),
])
def test_pinned_control_or_root_replaced_before_click_stops_without_submission(
        fixture_browser, tmp_path, monkeypatch, stage, replaced, failure_kind):
    from kusamushiri.verified_post_action import VerifiedSinglePost

    driver, browser = fixture_browser
    context = browser.new_context(service_workers="block")
    page = context.new_page()
    requests = []
    html = document("none", "single")
    original = VerifiedSinglePost.prepare_control

    def prepared(scope, locator, current_stage):
        click = original(scope, locator, current_stage)
        if current_stage != stage:
            return click

        def replace_then_click():
            # Swap the node that was pinned (or only its Dropdown root) for an
            # identical-looking one; the closure must refuse to click it.
            page.evaluate("""([stage, replaced]) => {
                if (replaced === 'root') {
                    const old = document.querySelector('#menu [data-testid="Dropdown"]');
                    const root = old.cloneNode(false);
                    root.append(...old.childNodes); old.replaceWith(root);
                    return;
                }
                const old = document.querySelector(stage === 'confirm'
                    ? '#sheet [data-testid="confirmationSheetConfirm"]' : '#menu [data-testid="delete"]');
                old.replaceWith(old.cloneNode(true));
            }""", [stage, replaced])
            click()
        return replace_then_click

    monkeypatch.setattr(VerifiedSinglePost, "prepare_control", prepared)

    def local(route):
        request = route.request
        requests.append((request.method, request.url))
        if request.url == URL and request.method == "GET":
            route.fulfill(content_type="text/html", body=html)
        else:
            route.abort()

    context.route("**/*", local)
    try:
        page.goto(URL)
        core = XDeleterCore()
        manager = core._browser
        manager.browser, manager.context, manager.page, manager.playwright = browser, context, page, driver
        target = PostActionTarget(URL, "post")
        with core.verified_single_post_scope(target, "author", current_view_only=True):
            result = run_post_actions(core, [target], 0, RetryPolicy(max_retries=0, max_cooldowns=0),
                                      journal=ActionJournal(tmp_path / "synthetic-receipt.json", "synthetic"))[0]
        assert not result.success, result
        assert result.failure_kind == failure_kind
        assert page.evaluate("window.finalClicks") == 0
        assert requests == [("GET", URL)]
    finally:
        context.close()
