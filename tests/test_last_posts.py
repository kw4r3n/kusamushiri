"""Last post dates: the profile reader on local Chromium pages, and the per-profile store.

Every request is fulfilled from an in-memory document; these tests never contact X.
"""

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

from kusamushiri import last_posts
from kusamushiri import paths as x_deleter_paths
from kusamushiri.follows import FollowRecord
from kusamushiri.last_posts import (
    STORE_FILE_NAME,
    LastPostEntry,
    LastPostResult,
    LastPostStore,
    apply_last_post_entries,
    fetch_last_post,
)


def _article(author: str, post_id: int, timestamp: str, *, context: str = "", extra: str = "") -> str:
    social = f'<span data-testid="socialContext">{context}</span>' if context else ""
    return f"""
      <article data-testid="tweet">
        {social}
        <a href="/{author}/status/{post_id}"><time datetime="{timestamp}">date</time></a>
        <div data-testid="tweetText">post {post_id}</div>
        {extra}
      </article>
    """


QUOTED_NEWER_POST = """
  <div role="link"><a href="/target/status/99"><time datetime="2026-10-01T00:00:00.000Z">quoted</time></a></div>
"""
TIMELINE = "".join(
    [
        _article("target", 1, "2026-09-25T00:00:00.000Z", context="Pinned"),
        _article("someone", 2, "2026-09-20T12:00:00.000Z", context="Target reposted"),
        _article("target", 3, "2026-08-15T10:00:00.000Z", extra=QUOTED_NEWER_POST),
        _article("target", 4, "2026-08-01T09:00:00.000Z"),
    ]
)


def _profile(body: str) -> str:
    return f"""
        <!doctype html>
        <html><head><meta charset="utf-8"></head><body>
          <main><div data-testid="primaryColumn">{body}</div></main>
        </body></html>
    """


@pytest.fixture
def profile_page(monkeypatch: pytest.MonkeyPatch) -> Iterator[Page]:
    monkeypatch.setattr(last_posts, "NAVIGATION_TIMEOUT_MS", 5_000)
    monkeypatch.setattr(last_posts, "TIMELINE_LOAD_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(last_posts, "SCROLL_WAIT_MS", 100)
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Playwright Chromium is not installed; run `uv run playwright install chromium`")
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser.new_context().new_page()
        finally:
            browser.close()


def _serve(page: Page, html: str) -> list[str]:
    navigations: list[str] = []

    def fulfill_locally(route) -> None:
        navigations.append(route.request.url)
        route.fulfill(content_type="text/html", body=html)

    page.route("**/*", fulfill_locally)
    return navigations


def test_reads_newest_own_post_skipping_pinned_reposts_and_quotes(profile_page: Page) -> None:
    navigations = _serve(profile_page, _profile(TIMELINE))

    result = fetch_last_post(profile_page, "Target", threading.Event())

    assert navigations[0] == "https://x.com/Target"
    assert result.success and result.note is None
    assert result.last_post_at == "2026-08-15T10:00:00+00:00"
    assert result.checked_at.endswith("+00:00")


def test_protected_profile_records_unknown_with_reason(profile_page: Page) -> None:
    _serve(profile_page, _profile('<div data-testid="emptyState"><span>These posts are protected</span></div>'))

    result = fetch_last_post(profile_page, "target", threading.Event())

    assert result.success
    assert result.last_post_at is None
    assert result.note == "These posts are protected"


def test_profile_with_only_pinned_post_and_reposts_is_unknown(profile_page: Page) -> None:
    body = _article("target", 1, "2026-09-25T00:00:00.000Z", context="Pinned") + _article(
        "someone", 2, "2026-09-20T12:00:00.000Z", context="Target reposted"
    )
    _serve(profile_page, _profile(body))

    result = fetch_last_post(profile_page, "target", threading.Event())

    assert result.success and result.last_post_at is None
    assert result.note == "固定ポストとリポスト以外のポストが見つかりません。"


def test_timeline_that_never_loads_is_an_error(profile_page: Page) -> None:
    _serve(profile_page, _profile("<div>loading</div>"))

    result = fetch_last_post(profile_page, "target", threading.Event())

    assert not result.success
    assert result.error_message == "タイムラインを読み込めませんでした。"


def test_invalid_username_is_rejected_without_navigating(profile_page: Page) -> None:
    navigations = _serve(profile_page, _profile(TIMELINE))

    result = fetch_last_post(profile_page, "../settings", threading.Event())

    assert not result.success
    assert navigations == []


@pytest.fixture
def app_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(x_deleter_paths, "_app_data_path_override", data_dir)
    return data_dir


def test_store_round_trips_per_profile_and_keeps_earlier_results_on_failure(app_data_dir: Path) -> None:
    store = LastPostStore.for_account("work")
    assert store.path == app_data_dir / "chromium-profile-work" / STORE_FILE_NAME
    assert store.load() == {}

    store.record([
        LastPostResult("Alice", "2026-10-01T09:30:00+00:00", last_post_at="2026-09-30T08:00:00+00:00"),
        LastPostResult("locked", "2026-10-01T09:31:00+00:00", note="These posts are protected"),
    ])
    entries = store.record([LastPostResult("alice", "2026-10-02T00:00:00+00:00", error_message="timeout")])

    expected = {
        "alice": LastPostEntry("2026-10-01T09:30:00+00:00", "2026-09-30T08:00:00+00:00"),
        "locked": LastPostEntry("2026-10-01T09:31:00+00:00", None, "These posts are protected"),
    }
    assert entries == expected
    assert LastPostStore.for_account("work").load() == expected
    assert LastPostStore.for_account("other").load() == {}
    assert sorted(path.name for path in store.path.parent.iterdir()) == [STORE_FILE_NAME]
    assert json.loads(store.path.read_text(encoding="utf-8"))["version"] == 1


def test_store_ignores_unreadable_file(app_data_dir: Path) -> None:
    store = LastPostStore.for_account("work")
    store.path.write_text("{not json", encoding="utf-8")

    assert store.load() == {}


def test_apply_entries_matches_usernames_case_insensitively() -> None:
    records = [
        FollowRecord("Alice", "Alice", "https://x.com/Alice", False),
        FollowRecord("bob", "Bob", "https://x.com/bob", False),
    ]
    entries = {"alice": LastPostEntry("2026-10-01T09:30:00+00:00", None, "These posts are protected")}

    applied = apply_last_post_entries(records, entries)

    assert applied[0].checked_at == "2026-10-01T09:30:00+00:00"
    assert applied[0].last_post_at is None and applied[0].last_post_note == "These posts are protected"
    assert applied[1] == records[1]
