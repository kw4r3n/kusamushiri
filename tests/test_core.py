from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from kusamushiri.actions import DELETE_MENU_ITEM_SELECTORS
from kusamushiri.browser import BrowserManager
from kusamushiri.core import POST_ARTICLE_SELECTOR, XDeleterCore
from kusamushiri.models import CollectRequest, PostKind
from kusamushiri.parsing import (
    extract_post_id,
    extract_profile_username,
    parse_count_from_aria_label,
)


class EmptyArticleLocator:
    def all(self) -> list[object]:
        return []


class TextLocator:
    @property
    def first(self) -> "TextLocator":
        return self

    def count(self) -> int:
        return 1

    def inner_text(self) -> str:
        return "loading results"

    def wait_for(self, *, state: str, timeout: int) -> None:
        return None


class EmptySearchPage:
    def goto(self, url: str, *, timeout: int) -> None:
        return None

    def wait_for_timeout(self, timeout: int) -> None:
        return None

    def evaluate(self, script: str) -> int | None:
        if script == "document.body.scrollHeight":
            raise RuntimeError("execution context was destroyed")
        return None

    def locator(self, selector: str) -> EmptyArticleLocator | TextLocator:
        if selector == POST_ARTICLE_SELECTOR:
            return EmptyArticleLocator()
        return TextLocator()


class RecordingContext:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def close(self) -> None:
        self._events.append("context.close")


class RecordingPage:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def wait_for_timeout(self, timeout: int) -> None:
        self._events.append(f"page.wait:{timeout}")


class RecordingPlaywright:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def stop(self) -> None:
        self._events.append("playwright.stop")


class ClickableLocator:
    @property
    def first(self) -> "ClickableLocator":
        return self

    def click(self) -> None:
        return None

    def wait_for(self, *, state: str, timeout: int) -> None:
        return None


class ArticleLocator:
    @property
    def first(self) -> "ArticleLocator":
        return self

    def element_handle(self) -> "ArticleLocator":
        return self

    def wait_for(self, *, state: str, timeout: int) -> None:
        return None

    def locator(self, selector: str) -> ClickableLocator:
        return ClickableLocator()


class DeletePage:
    def wait_for_function(self, expression: str, *, arg: object, timeout: int) -> None:
        return None

    def goto(self, url: str, *, timeout: int) -> None:
        return None

    def wait_for_timeout(self, timeout: int) -> None:
        return None

    def wait_for_selector(self, selector: str, *, state: str, timeout: int) -> None:
        return None

    def locator(self, selector: str) -> ArticleLocator:
        return ArticleLocator()

    def on(self, event: str, handler: object) -> None:
        return None

    def remove_listener(self, event: str, handler: object) -> None:
        return None


def build_request(**overrides: object) -> CollectRequest:
    values: dict[str, object] = {
        "username": "user",
        "max_posts": 10,
        "media_filter": "all",
        "is_reply": False,
        "min_likes": 0,
        "min_replies": 0,
        "search_mode": "search",
        "post_kind_filter": "posts",
        "since_date": None,
        "until_date": None,
    }
    values.update(overrides)
    return CollectRequest(**values)


def test_post_matches_filters_respects_all_filters() -> None:
    core = XDeleterCore()
    cases = [
        (build_request(media_filter="with_media"), True, False, False, 0, 0, date(2026, 4, 15), True),
        (build_request(media_filter="with_media"), False, False, False, 0, 0, date(2026, 4, 15), False),
        (build_request(media_filter="without_media"), False, False, False, 0, 0, date(2026, 4, 15), True),
        (build_request(media_filter="without_media"), True, False, False, 0, 0, date(2026, 4, 15), False),
        (build_request(media_filter="all"), True, False, False, 0, 0, date(2026, 4, 15), True),
        (build_request(media_filter="all"), False, False, False, 0, 0, date(2026, 4, 15), True),
        (build_request(post_kind_filter="posts"), False, False, False, 0, 0, date(2026, 4, 15), True),
        (build_request(post_kind_filter="posts"), False, False, True, 0, 0, date(2026, 4, 15), False),
        (build_request(post_kind_filter="reposts"), False, False, True, 0, 0, date(2026, 4, 15), True),
        (build_request(post_kind_filter="reposts"), False, False, False, 0, 0, date(2026, 4, 15), False),
        (build_request(post_kind_filter="all"), False, False, True, 0, 0, date(2026, 4, 15), True),
        (build_request(post_kind_filter="all"), False, False, False, 0, 0, date(2026, 4, 15), True),
        (build_request(min_likes=5), False, False, False, 4, 0, date(2026, 4, 15), False),
        (build_request(min_likes=5), False, False, False, 5, 0, date(2026, 4, 15), True),
        (build_request(min_replies=3), False, False, False, 0, 2, date(2026, 4, 15), False),
        (build_request(min_replies=3), False, False, False, 0, 3, date(2026, 4, 15), True),
        (build_request(is_reply=True), False, True, False, 0, 0, date(2026, 4, 15), True),
        (build_request(is_reply=True), False, False, False, 0, 0, date(2026, 4, 15), False),
        (build_request(is_reply=False), False, True, False, 0, 0, date(2026, 4, 15), True),
        (build_request(is_reply=False), False, False, False, 0, 0, date(2026, 4, 15), True),
        (build_request(since_date=date(2026, 4, 10), until_date=date(2026, 4, 19)), False, False, False, 0, 0, date(2026, 4, 10), True),
        (build_request(since_date=date(2026, 4, 10), until_date=date(2026, 4, 19)), False, False, False, 0, 0, date(2026, 4, 19), True),
        (build_request(since_date=date(2026, 4, 10), until_date=date(2026, 4, 19)), False, False, False, 0, 0, date(2026, 4, 9), False),
        (build_request(since_date=date(2026, 4, 10), until_date=date(2026, 4, 19)), False, False, False, 0, 0, date(2026, 4, 20), False),
    ]

    for request, has_media, is_reply, is_repost, likes_count, replies_count, post_date, expected in cases:
        assert (
            core._post_matches_filters(
                request=request,
                has_media=has_media,
                is_reply=is_reply,
                kind="repost" if is_repost else "post",
                likes_count=likes_count,
                replies_count=replies_count,
                post_date=post_date,
                article_index=1,
            )
            is expected
        )


@pytest.mark.parametrize(
    ("include_keywords", "exclude_keywords", "text", "expected"),
    [
        ((), (), "何でも", True),
        (("懸賞",), (), "懸賞に応募しました", True),
        (("懸賞", "キャンペーン"), (), "ｷｬﾝﾍﾟｰﾝ中", True),
        (("懸賞",), (), "日常の話", False),
        (("懸賞",), (), "", False),
        ((), ("大事",), "これは大事なポスト", False),
        ((), ("大事",), "消してよいポスト", True),
        ((), ("大事",), "", True),
        (("懸賞",), ("当選",), "懸賞に当選しました", False),
        (("Giveaway",), (), "GIVEAWAY time", True),
    ],
)
def test_post_matches_filters_applies_keywords(
    include_keywords: tuple[str, ...],
    exclude_keywords: tuple[str, ...],
    text: str,
    expected: bool,
) -> None:
    core = XDeleterCore()
    request = build_request(include_keywords=include_keywords, exclude_keywords=exclude_keywords)

    assert (
        core._post_matches_filters(
            request=request,
            has_media=False,
            is_reply=False,
            kind="post",
            likes_count=0,
            replies_count=0,
            post_date=date(2026, 4, 15),
            article_index=1,
            text_content=text,
        )
        is expected
    )


def test_keywords_do_not_change_search_query() -> None:
    core = XDeleterCore()
    request = build_request(include_keywords=("懸賞",), exclude_keywords=("大事",))

    assert core._build_search_query("user", request) == "from:user"


def test_build_collection_url_uses_likes_timeline_for_likes() -> None:
    core = XDeleterCore()
    request = build_request(search_mode="profile", post_kind_filter="likes")

    assert core._build_collection_url("user", request) == "https://x.com/user/likes"


def test_likes_filter_accepts_only_like_kind() -> None:
    core = XDeleterCore()
    request = build_request(search_mode="profile", post_kind_filter="likes", min_likes=2)

    def matches(kind: PostKind, likes_count: int = 5) -> bool:
        return core._post_matches_filters(
            request=request,
            has_media=False,
            is_reply=False,
            kind=kind,
            likes_count=likes_count,
            replies_count=0,
            post_date=date(2026, 4, 15),
            article_index=1,
        )

    assert matches("like") is True
    assert matches("like", likes_count=1) is False
    # Posts and reposts filters never keep liked posts.
    assert core._post_matches_filters(
        request=build_request(post_kind_filter="posts"),
        has_media=False,
        is_reply=False,
        kind="like",
        likes_count=0,
        replies_count=0,
        post_date=date(2026, 4, 15),
        article_index=1,
    ) is False


def test_unlike_post_verifies_like_button_becomes_visible(monkeypatch) -> None:
    calls: list[object] = []

    class RecordingPage(DeletePage):
        def wait_for_function(self, expression: str, *, arg: object, timeout: int) -> None:
            calls.append(arg)

    core = XDeleterCore()
    core._browser.page = RecordingPage()
    monkeypatch.setattr(core, "_find_first_visible_locator", lambda root, selectors: ClickableLocator())

    assert core.unlike_post("https://x.com/someone/status/12345") == (True, None)
    assert len(calls) == 1
    assert isinstance(calls[0], dict)
    assert (calls[0]["undoId"], calls[0]["doId"]) == ("unlike", "like")


def test_unlike_post_fails_without_unlike_button(monkeypatch) -> None:
    core = XDeleterCore()
    core._browser.page = DeletePage()
    monkeypatch.setattr(core, "_find_first_visible_locator", lambda root, selectors: None)

    assert core.unlike_post("https://x.com/someone/status/12345") == (
        False,
        "いいね取り消しボタンが見つかりませんでした。",
    )


def test_build_collection_url_uses_profile_path_for_profile_mode() -> None:
    core = XDeleterCore()
    request = build_request(search_mode="profile")

    assert core._build_collection_url("user", request) == "https://x.com/user"


def test_build_collection_url_encodes_search_query() -> None:
    core = XDeleterCore()
    request = build_request(
        search_mode="search",
        post_kind_filter="all",
        media_filter="with_media",
    )

    url = core._build_collection_url("user", request)
    parsed_url = urlparse(url)
    query_values = parse_qs(parsed_url.query)

    assert parsed_url.path == "/search"
    assert query_values["f"] == ["live"]
    assert query_values["q"] == ["from:user include:nativeretweets filter:media"]


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/user/status/12345", ("https://x.com/user/status/12345", "12345")),
        ("https://www.x.com/user/status/12345/photo/1", ("https://www.x.com/user/status/12345/photo/1", "12345")),
        ("//x.com/user/status/12345?lang=ja", ("https://x.com/user/status/12345?lang=ja", "12345")),
    ],
)
def test_parse_supported_post_url_accepts_supported_x_post_links(href: str, expected: tuple[str, str]) -> None:
    assert XDeleterCore()._parse_supported_post_url(href) == expected


@pytest.mark.parametrize(
    "href",
    [
        None,
        "/user/12345",
        "/user/status/not-a-number",
        "https://example.com/user/status/12345",
        "https://x.com/user/extra/status/12345",
    ],
)
def test_parse_supported_post_url_rejects_unidentifiable_links(href: str | None) -> None:
    assert XDeleterCore()._parse_supported_post_url(href) is None


def test_build_search_query_supports_filter_variants() -> None:
    core = XDeleterCore()
    cases = [
        (build_request(), "from:user"),
        (build_request(until_date=date(2026, 4, 19)), "from:user until:2026-04-21"),
        (build_request(since_date=date(2026, 4, 10)), "from:user since:2026-04-09"),
        (build_request(post_kind_filter="posts"), "from:user"),
        (build_request(post_kind_filter="reposts"), "from:user include:nativeretweets"),
        (build_request(media_filter="with_media"), "from:user filter:media"),
        (build_request(is_reply=True), "from:user filter:replies"),
        (build_request(min_likes=3), "from:user min_faves:3"),
        (build_request(min_replies=4), "from:user min_replies:4"),
    ]

    for request, expected in cases:
        assert core._build_search_query("user", request) == expected


def test_content_indicates_empty_search_results_for_known_markers() -> None:
    core = XDeleterCore()

    assert core._content_indicates_empty_search_results("<main>No results for from:user</main>") is True
    assert core._content_indicates_empty_search_results("<main>検索結果はありません</main>") is True
    assert core._content_indicates_empty_search_results("<article data-testid='tweet'>post</article>") is False


def test_should_stop_empty_search_collection_immediately_for_explicit_empty_state() -> None:
    core = XDeleterCore()
    request = build_request(search_mode="search")

    should_stop = core._should_stop_empty_search_collection(
        request=request,
        collected_posts_count=0,
        page_content="<main>No results for from:user</main>",
    )

    assert should_stop is True


def test_repeated_empty_cycles_do_not_prove_search_is_empty() -> None:
    core = XDeleterCore()
    request = build_request(search_mode="search")

    assert core._should_stop_empty_search_collection(
        request=request,
        collected_posts_count=0,
    ) is False
    assert core._should_stop_empty_search_collection(
        request=request,
        collected_posts_count=0,
    ) is False
    assert core._should_stop_empty_search_collection(
        request=request,
        collected_posts_count=1,
    ) is False


def test_search_and_collect_posts_continues_when_scroll_height_fails(monkeypatch) -> None:
    monkeypatch.setattr("kusamushiri.core.EMPTY_SEARCH_TIMEOUT_SECONDS", 0)
    core = XDeleterCore()
    core._browser.page = EmptySearchPage()
    request = build_request(max_posts=1, search_mode="search")

    posts = core.search_and_collect_posts(request)

    assert posts == []


def test_search_and_collect_posts_reports_progress_and_stops_at_absolute_limit(monkeypatch) -> None:
    monkeypatch.setattr("kusamushiri.core.MAX_COLLECTION_CYCLES", 2)
    core = XDeleterCore()
    core._browser.page = EmptySearchPage()
    progress: list[tuple[int, int, int]] = []
    request = build_request(max_posts=1, search_mode="profile")

    posts = core.search_and_collect_posts(request, on_progress=lambda *values: progress.append(values))

    assert posts == []
    assert progress == [(0, 1, 2), (0, 2, 2)]
    assert core.collection_limit_reached is True


def test_stop_browser_closes_context_without_accessing_closed_page() -> None:
    events: list[str] = []
    core = XDeleterCore()
    core._browser.context = RecordingContext(events)
    core._browser.page = RecordingPage(events)
    core._browser.playwright = RecordingPlaywright(events)

    core.stop_browser()

    assert events == ["context.close", "playwright.stop"]
    assert core.context is None
    assert core.page is None
    assert core.playwright is None


def test_browser_manager_restarts_when_profile_changes(monkeypatch, tmp_path) -> None:
    manager = BrowserManager()
    manager.context = object()
    manager.page = object()
    manager.playwright = object()
    manager.profile_dir = tmp_path / "old"
    stopped: list[bool] = []

    def stop() -> None:
        stopped.append(True)
        manager.context = None
        manager.page = None
        manager.playwright = None

    class Chromium:
        def launch_persistent_context(self, **kwargs):
            class Context:
                pages: list[object] = []
                browser = None

                def new_page(self):
                    return object()

            return Context()

    class Playwright:
        chromium = Chromium()

    class Starter:
        def start(self):
            return Playwright()

    monkeypatch.setattr(manager, "stop", stop)
    monkeypatch.setattr("kusamushiri.browser.sync_playwright", Starter)

    manager.start(tmp_path / "new")

    assert stopped == [True]
    assert manager.profile_dir == tmp_path / "new"


def test_delete_post_uses_only_explicit_delete_menu_selectors(monkeypatch) -> None:
    core = XDeleterCore()
    core._browser.page = DeletePage()
    selector_calls: list[tuple[str, ...]] = []

    def find_first_visible_locator(root: object, selectors: tuple[str, ...]) -> ClickableLocator:
        selector_calls.append(selectors)
        return ClickableLocator()

    monkeypatch.setattr(core, "_find_first_visible_locator", find_first_visible_locator)

    success, error_message = core.delete_post("https://x.com/user/status/12345")

    delete_selectors = selector_calls[1]
    assert success is True
    assert error_message is None
    assert '[data-testid="Dropdown"] [role="menuitem"]:has-text("삭제"):not(:has-text("리스트")):not(:has-text("목록")):visible' in delete_selectors
    assert '[data-testid="Dropdown"] [role="menuitem"][data-testid="delete"]:visible' in delete_selectors
    assert '[data-testid="Dropdown"] [role="menuitem"]' not in delete_selectors


def test_delete_post_fails_when_delete_menu_item_is_not_identified(monkeypatch) -> None:
    core = XDeleterCore()
    core._browser.page = DeletePage()
    call_count = 0

    def find_first_visible_locator(root: object, selectors: tuple[str, ...]) -> ClickableLocator | None:
        nonlocal call_count
        call_count += 1
        return ClickableLocator() if call_count == 1 else None

    monkeypatch.setattr(core, "_find_first_visible_locator", find_first_visible_locator)

    success, error_message = core.delete_post("https://x.com/user/status/12345")

    assert success is False
    assert error_message == "削除メニュー項目が見つかりませんでした。自分のポストではない可能性があります。"


def test_delete_post_waits_for_identified_menu_item_and_does_not_guess_on_timeout(monkeypatch) -> None:
    class SlowMenuPage(DeletePage):
        def __init__(self) -> None:
            self.waited_for: list[str] = []

        def wait_for_selector(self, selector: str, *, state: str, timeout: int) -> None:
            self.waited_for.append(selector)
            raise PlaywrightTimeoutError("menu item did not appear")

    core = XDeleterCore()
    page = SlowMenuPage()
    core._browser.page = page
    selector_calls: list[tuple[str, ...]] = []

    def find_first_visible_locator(root: object, selectors: tuple[str, ...]) -> ClickableLocator:
        selector_calls.append(selectors)
        return ClickableLocator()

    monkeypatch.setattr(core, "_find_first_visible_locator", find_first_visible_locator)

    success, error = core.delete_post("https://x.com/user/status/12345")

    assert success is False
    assert error == "削除メニュー項目が見つかりませんでした。自分のポストではない可能性があります。"
    assert page.waited_for == [", ".join(DELETE_MENU_ITEM_SELECTORS)]
    assert len(selector_calls) == 1  # Only the caret was selected, not an unknown menu item.


def test_delete_post_fails_when_deleted_article_does_not_disappear(monkeypatch) -> None:
    class AttachedPage(DeletePage):
        def wait_for_function(self, expression: str, *, arg: object, timeout: int) -> None:
            raise TimeoutError("article remained attached")

    core = XDeleterCore()
    core._browser.page = AttachedPage()
    monkeypatch.setattr(core, "_find_first_visible_locator", lambda root, selectors: ClickableLocator())

    success, error_message = core.delete_post("https://x.com/user/status/12345")

    assert success is False
    assert error_message is not None
    assert "article remained attached" in error_message
    assert "結果不明" in error_message
    assert "再試行前にXで確認" in error_message


def test_undo_repost_verifies_retweet_button_becomes_visible(monkeypatch) -> None:
    core = XDeleterCore()
    core._browser.page = DeletePage()
    monkeypatch.setattr(core, "_find_first_visible_locator", lambda root, selectors: ClickableLocator())

    success, error_message = core.undo_repost("https://x.com/user/status/12345")

    assert success is True
    assert error_message is None


def test_undo_repost_fails_when_button_state_does_not_change(monkeypatch) -> None:
    class UnchangedPage(DeletePage):
        def wait_for_function(self, expression: str, *, arg: object, timeout: int) -> None:
            raise TimeoutError("retweet button did not become visible")

    core = XDeleterCore()
    core._browser.page = UnchangedPage()
    monkeypatch.setattr(core, "_find_first_visible_locator", lambda root, selectors: ClickableLocator())

    success, error_message = core.undo_repost("https://x.com/user/status/12345")

    assert success is False
    assert error_message is not None
    assert "retweet button did not become visible" in error_message


@pytest.mark.parametrize("action_name", ["delete_post", "undo_repost", "unlike_post"])
@pytest.mark.parametrize("url", [
    "https://x.com/user/12345",
    "https://x.com/user/status/not-a-number",
    "https://x.com/user/status/１２３４５",
    "https://example.com/user/status/12345",
    "https://x.com.evil/user/status/12345",
    "https://x.com/user/extra/status/12345",
])
def test_post_actions_reject_unidentifiable_urls_before_navigation(action_name, url) -> None:
    navigations: list[str] = []

    class NavigationRecordingPage(DeletePage):
        def goto(self, url: str, *, timeout: int) -> None:
            navigations.append(url)

    core = XDeleterCore()
    core._browser.page = NavigationRecordingPage()

    success, error_message = getattr(core, action_name)(url)

    assert success is False
    assert error_message is not None
    assert "ポストID" in error_message
    assert navigations == []


def test_extract_profile_username_supports_relative_and_absolute_urls() -> None:
    assert extract_profile_username("/example_user") == "example_user"
    assert extract_profile_username("https://x.com/example_user") == "example_user"
    assert extract_profile_username("/home") == "home"


def test_extract_post_id_ignores_query_string() -> None:
    assert extract_post_id("https://x.com/example_user/status/12345?lang=ja") == "12345"


def test_extract_post_id_uses_status_segment_before_detail_suffix() -> None:
    assert extract_post_id("https://x.com/example_user/status/12345/photo/1") == "12345"


def test_parse_count_from_aria_label_reads_first_integer() -> None:
    assert parse_count_from_aria_label("1,234 Likes") == 1234
    assert parse_count_from_aria_label("返信 56件") == 56
    assert parse_count_from_aria_label(None) == 0
    assert parse_count_from_aria_label("No likes") == 0


def test_build_post_record_truncates_long_text_preview() -> None:
    core = XDeleterCore()
    long_text = "x" * 101

    record = core._build_post_record(
        post_id="123",
        post_url="https://x.com/user/status/123",
        author_username="user",
        text_content=long_text,
        date_text="2026-04-19T00:00:00.000Z",
        likes_count=1,
        replies_count=2,
        has_media=True,
        is_reply=False,
        kind="post",
    )

    assert record.text == ("x" * 100) + "..."


def test_is_post_within_date_range_is_inclusive() -> None:
    core = XDeleterCore()
    request = build_request(
        since_date=date(2026, 4, 10),
        until_date=date(2026, 4, 19),
    )

    assert core._is_post_within_date_range(date(2026, 4, 10), request) is True
    assert core._is_post_within_date_range(date(2026, 4, 19), request) is True
    assert core._is_post_within_date_range(date(2026, 4, 9), request) is False
    assert core._is_post_within_date_range(date(2026, 4, 20), request) is False
