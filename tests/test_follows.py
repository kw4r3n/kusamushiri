"""Following-list export: parsing, file output and a local Chromium scroll regression.

The DOM test fulfills every request from an in-memory document and never contacts X.
"""

import csv
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

from kusamushiri import follows
from kusamushiri.follows import (
    ExportFollowingRequest,
    FollowRecord,
    collect_following,
    parse_profile_href,
    write_follow_list,
)

RECORDS = [
    FollowRecord("alice", "Alice 🌱", "https://x.com/alice", True),
    FollowRecord("bob_2", "=HYPERLINK(\"x\")", "https://x.com/bob_2", False),
]


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/alice", "alice"),
        ("https://x.com/Bob_2", "Bob_2"),
        ("/alice/status/1", None),
        ("/i/lists", None),
        ("/home", None),
        ("/search?q=a", None),
        ("https://example.com/alice", None),
        ("/this_name_is_too_long", None),
        (None, None),
    ],
)
def test_parse_profile_href(href: str | None, expected: str | None) -> None:
    assert parse_profile_href(href) == expected


def test_write_follow_list_csv_is_excel_friendly(tmp_path: Path) -> None:
    path = tmp_path / "following.csv"
    write_follow_list(path, RECORDS)

    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))
    assert rows == [
        ["username", "display_name", "profile_url", "follows_you"],
        ["alice", "Alice 🌱", "https://x.com/alice", "true"],
        ["bob_2", "'=HYPERLINK(\"x\")", "https://x.com/bob_2", "false"],
    ]


def test_write_follow_list_json_keeps_raw_values(tmp_path: Path) -> None:
    path = tmp_path / "following.json"
    write_follow_list(path, RECORDS)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[0] == {
        "username": "alice",
        "display_name": "Alice 🌱",
        "profile_url": "https://x.com/alice",
        "follows_you": True,
    }
    assert data[1]["display_name"] == "=HYPERLINK(\"x\")"


@pytest.mark.parametrize(
    ("username", "filename"),
    [("", "a.csv"), ("bad name", "a.csv"), ("alice", "a.txt")],
)
def test_export_request_validation(username: str, filename: str) -> None:
    with pytest.raises(ValueError):
        ExportFollowingRequest(username=username, output_path=Path(filename)).validate()


TOTAL_ACCOUNTS = 45
RENDERED_WINDOW = 12

FOLLOWING_DOCUMENT = f"""
<!doctype html>
<html><head><meta charset="utf-8"></head><body style="margin:0">
  <main>
    <div data-testid="primaryColumn"><section id="list"></section></div>
    <div data-testid="sidebarColumn">
      <div data-testid="UserCell"><a href="/suggested">Suggested</a><a href="/suggested">@suggested</a></div>
    </div>
  </main>
  <script>
    const total = {TOTAL_ACCOUNTS};
    const windowSize = {RENDERED_WINDOW};
    let loaded = 5;
    const cell = i => `
      <div data-testid="UserCell" style="height:200px">
        <a href="/user_${{i}}"><img alt="avatar"></a>
        <a href="/user_${{i}}"><span>User ${{i}} </span><img alt="🌱"></a>
        <a href="/user_${{i}}">@user_${{i}}</a>
        ${{i % 3 === 0 ? '<span data-testid="userFollowIndicator">Follows you</span>' : ''}}
        <a href="/user_${{i}}/status/1">latest post</a>
      </div>`;
    const render = () => {{
      // Like X, keep only a window of cells in the DOM and pad the list above them.
      const first = Math.max(0, loaded - windowSize);
      const list = document.getElementById('list');
      list.style.paddingTop = `${{first * 200}}px`;
      list.innerHTML = Array.from({{length: loaded - first}}, (_, k) => cell(first + k)).join('');
    }};
    render();
    window.addEventListener('scroll', () => {{
      if (loaded < total && window.scrollY + window.innerHeight >= document.body.scrollHeight - 400) {{
        loaded = Math.min(total, loaded + 5);
        render();
      }}
    }});
  </script>
</body></html>
"""


@pytest.fixture
def following_page() -> Iterator[tuple[Page, list[str]]]:
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Playwright Chromium is not installed; run `uv run playwright install chromium`")
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_context(viewport={"width": 800, "height": 600}).new_page()
            navigations: list[str] = []

            def fulfill_locally(route) -> None:
                navigations.append(route.request.url)
                route.fulfill(content_type="text/html", body=FOLLOWING_DOCUMENT)

            page.route("**/*", fulfill_locally)
            yield page, navigations
        finally:
            browser.close()


def test_collect_following_scrolls_virtualized_list(
    following_page: tuple[Page, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    page, navigations = following_page
    monkeypatch.setattr(follows, "SCROLL_WAIT_MS", 300)
    monkeypatch.setattr(follows, "MAX_STABLE_CYCLES", 2)
    progress: list[int] = []

    result = collect_following(page, "target", threading.Event(), progress.append)

    assert navigations[0] == "https://x.com/target/following"
    assert [record.username for record in result.records] == [f"user_{i}" for i in range(TOTAL_ACCOUNTS)]
    assert result.records[0] == FollowRecord("user_0", "User 0 🌱", "https://x.com/user_0", True)
    assert result.records[1].follows_you is False
    assert not result.limit_reached
    assert progress[-1] == TOTAL_ACCOUNTS


def test_collect_following_stops_when_cancelled(following_page: tuple[Page, list[str]]) -> None:
    page, _navigations = following_page
    cancel_event = threading.Event()
    cancel_event.set()

    result = collect_following(page, "target", cancel_event)

    assert result.records == []
