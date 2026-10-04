"""Local Chromium tests for unfollowing from a profile page.

Every request is fulfilled from an in-memory document; these tests never contact X.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

from kusamushiri import actions
from kusamushiri.actions import unfollow_account

DESTROY_URL = "https://x.com/i/api/1.1/friendships/destroy.json"
HEADER_BUTTON = '<button data-testid="42-unfollow" onclick="openConfirm()">Following</button>'
SUGGESTION = """
  <div data-testid="UserCell">
    <a href="/someone_else">Someone else</a>
    <button data-testid="7-unfollow" onclick="window.actionLog.push('suggestion')">Following</button>
  </div>
"""


def _profile(header: str = HEADER_BUTTON, *, extra: str = "", send_request: bool = True) -> str:
    send = f"await fetch('{DESTROY_URL}', {{method: 'POST'}}).catch(() => null);" if send_request else ""
    return f"""
        <!doctype html>
        <html><head><meta charset="utf-8"></head><body>
          <div data-testid="primaryColumn">
            <div id="header">{header}</div>
            {extra}
          </div>
          <aside data-testid="sidebarColumn">
            <button data-testid="9-unfollow" onclick="window.actionLog.push('sidebar')">Following</button>
          </aside>
          <button data-testid="confirmationSheetConfirm" hidden onclick="confirmUnfollow()">Unfollow</button>
          <script>
            window.actionLog = [];
            window.openConfirm = () => {{
              window.actionLog.push('header');
              document.querySelector('[data-testid="confirmationSheetConfirm"]').hidden = false;
            }};
            window.confirmUnfollow = async () => {{
              window.actionLog.push('confirm');
              document.querySelector('[data-testid="confirmationSheetConfirm"]').hidden = true;
              {send}
              document.getElementById('header').innerHTML =
                '<button data-testid="42-follow">Follow</button>';
            }};
          </script>
        </body></html>
    """


@pytest.fixture
def profile_page(monkeypatch: pytest.MonkeyPatch) -> Iterator[Page]:
    monkeypatch.setattr(actions, "ACTION_STATE_TIMEOUT_MS", 2_000)
    monkeypatch.setattr(actions, "NAVIGATION_TIMEOUT_MS", 5_000)
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Playwright Chromium is not installed; run `uv run playwright install chromium`")
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser.new_context().new_page()
        finally:
            browser.close()


def _serve(page: Page, html: str, destroy_status: int = 200, destroy_body: str = "{}") -> list[str]:
    navigations: list[str] = []

    def fulfill_locally(route) -> None:
        navigations.append(route.request.url)
        route.fulfill(content_type="text/html", body=html)

    page.route("**/*", fulfill_locally)
    page.route(
        "**/friendships/destroy.json",
        lambda route: route.fulfill(status=destroy_status, content_type="application/json", body=destroy_body),
    )
    return navigations


def _log(page: Page) -> list[str]:
    return page.evaluate("window.actionLog")


def test_unfollow_clicks_profile_button_and_confirms(profile_page: Page) -> None:
    navigations = _serve(profile_page, _profile(extra=SUGGESTION))

    assert unfollow_account(profile_page, "target") == (True, None)
    assert navigations[0] == "https://x.com/target"
    assert _log(profile_page) == ["header", "confirm"]


def test_unfollow_without_request_falls_back_to_dom_state(profile_page: Page) -> None:
    _serve(profile_page, _profile(send_request=False))

    assert unfollow_account(profile_page, "target") == (True, None)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, '{"errors": [{"code": 161, "message": "Rate limit"}]}', "Rate limit"),
        (429, "{}", "429"),
    ],
)
def test_unfollow_reports_failed_destroy_response(profile_page: Page, status: int, body: str, expected: str) -> None:
    _serve(profile_page, _profile(), destroy_status=status, destroy_body=body)

    success, error = unfollow_account(profile_page, "target")

    assert success is False
    assert error is not None and "friendships/destroy.json" in error and expected in error


def test_unfollow_reports_account_not_followed(profile_page: Page) -> None:
    _serve(profile_page, _profile('<button data-testid="42-follow">Follow</button>', extra=SUGGESTION))

    assert unfollow_account(profile_page, "target") == (False, "このアカウントをフォローしていません。")
    assert _log(profile_page) == []


def test_unfollow_never_clicks_suggestions_or_sidebar(profile_page: Page) -> None:
    _serve(profile_page, _profile("", extra=SUGGESTION))

    assert unfollow_account(profile_page, "target") == (False, "フォロー解除ボタンが見つかりませんでした。")
    assert _log(profile_page) == []


def test_unfollow_refuses_ambiguous_header(profile_page: Page) -> None:
    _serve(profile_page, _profile(HEADER_BUTTON + HEADER_BUTTON.replace("42-", "43-")))

    success, _error = unfollow_account(profile_page, "target")

    assert success is False
    assert _log(profile_page) == []


def test_unfollow_rejects_invalid_username_without_navigating(profile_page: Page) -> None:
    navigations = _serve(profile_page, _profile())

    success, _error = unfollow_account(profile_page, "../settings")

    assert success is False
    assert navigations == []
