import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from playwright.sync_api import Browser, BrowserContext, Locator, Page, Playwright

from kusamushiri import actions
from kusamushiri.actions import OWNED_TIMESTAMP_LINK_SELECTOR, POST_ARTICLE_SELECTOR
from kusamushiri.actions import delete_post as _delete_post
from kusamushiri.actions import execute_post_action as _execute_post_action
from kusamushiri.actions import undo_repost as _undo_repost
from kusamushiri.actions import unfollow_account as _unfollow_account
from kusamushiri.actions import unlike_post as _unlike_post
from kusamushiri.browser import (
    BASE_X_URL,
    NAVIGATION_TIMEOUT_MS,
    BrowserManager,
    find_first_visible_locator,
)
from kusamushiri.follows import FollowCollectionResult, collect_following
from kusamushiri.i18n import tr
from kusamushiri.last_posts import LastPostResult, fetch_last_post
from kusamushiri.logger import logger
from kusamushiri.models import (
    CollectRequest,
    PostActionResult,
    PostActionTarget,
    PostKind,
    PostRecord,
    is_within_date_range,
    post_skip_reason,
)
from kusamushiri.parsing import (
    TEXT_PREVIEW_LENGTH,
    USERNAME_PATTERN,
    extract_post_author_username,
    extract_post_id,
    parse_count_from_aria_label,
    parse_post_date,
)

SEARCH_PATH = "search"
SCROLL_WAIT_MS = 2_000  # Time for new posts to load after scroll
MAX_SCROLL_ATTEMPTS = 50
MAX_COLLECTION_CYCLES = 100
EMPTY_STATE_SCROLL_Y = 1_000  # Smaller scroll when no articles visible (avoids overshoot)
COLLECTION_SCROLL_Y = 1_500  # Scroll distance during active collection (triggers incremental load)
MAX_STABLE_SCROLL_CYCLES = 3
VALID_MEDIA_FILTERS = {"all", "with_media", "without_media"}
VALID_SEARCH_MODES = {"profile", "search"}
VALID_POST_KIND_FILTERS = {"posts", "reposts", "all", "likes"}
LIKES_PATH = "likes"
SEARCH_EMPTY_STATE_MARKERS = (
    "No results for",
    "Try searching for something else",
    "No posts found",
    "検索結果はありません",
    "一致するポストはありません",
    "一致する結果はありません",
)
EMPTY_SEARCH_TIMEOUT_SECONDS = 15.0
MAX_ARTICLE_PARSE_ATTEMPTS = 3
POST_TIME_SELECTOR = "time"
OWNED_TEXT_SELECTOR = f"xpath=.//*[@data-testid='tweetText'][{actions.OWNED_NODE_XPATH}]"
SUPPORTED_POST_PATH_PATTERN = re.compile(
    r"/[A-Za-z0-9_]+/status/([0-9]+)(?:/[^?#]*)?"
)
# 返信先の表示は本文や引用ポストの外にある、ポスト自身のノードだけで判定する。
REPLY_CONTEXT_SELECTORS = tuple(
    f"xpath=.//*[text()[contains(., '{marker}')]]"
    f"[not(@data-testid='tweetText')][{actions.OWNED_NODE_XPATH}]"
    for marker in ("Replying to", "返信先:")
)
# X wraps its own photos in permalink anchors (/status/<id>/photo/N). Those
# anchors are not quote boundaries; any other containing link still is.
OWNED_MEDIA_NODE_XPATH = (
    "count(ancestor::article) = 1 and "
    "not(ancestor::blockquote or ancestor::*[@data-testid='quoteTweet' or @data-testid='tweetText'] or "
    "ancestor::*[@role='link'][not(self::a and (contains(@href, '/photo/') or contains(@href, '/video/')))])"
)
MEDIA_SELECTORS = (
    f"xpath=.//*[@data-testid='tweetPhoto' or @data-testid='videoPlayer'][{OWNED_MEDIA_NODE_XPATH}]",
)


@dataclass(slots=True)
class ArticleProcessingResult:
    collected_in_cycle: int
    new_posts_found: bool
    needs_retry: bool = False


@dataclass(slots=True)
class EmptySearchState:
    scroll_attempts: int
    previous_scroll_height: int
    should_stop: bool
    started_at: float | None = None


class XDeleterCore:
    def __init__(self, *, headless: bool = False) -> None:
        self._browser = BrowserManager(headless=headless)
        self._cancel_event = threading.Event()
        self.collection_limit_reached = False

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def clear_cancel(self) -> None:
        self._cancel_event.clear()

    def is_cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def wait_for_cancel(self, seconds: float) -> bool:
        """Sleep up to `seconds`; return True as soon as cancellation is requested."""
        return self._cancel_event.wait(seconds)

    @property
    def page(self) -> Page | None:
        return self._browser.page

    @property
    def context(self) -> BrowserContext | None:
        return self._browser.context

    @property
    def playwright(self) -> Playwright | None:
        return self._browser.playwright

    @property
    def browser(self) -> Browser | None:
        return self._browser.browser

    def _require_page(self) -> Page:
        return self._browser._require_page()

    def _normalize_username(self, username: str) -> str:
        normalized = username.strip().removeprefix("@")
        if not normalized or not USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError(tr("ユーザー名は1〜15文字の英数字またはアンダースコアで指定してください。"))
        return normalized

    def _get_scroll_height(self, page: Page) -> int:
        scroll_height = page.evaluate("document.body.scrollHeight")
        return int(scroll_height)

    def _safe_get_scroll_height(self, page: Page, fallback: int) -> int:
        try:
            return self._get_scroll_height(page)
        except Exception as error:
            logger.debug("Failed to read scroll height; reusing %s: %s", fallback, error)
            return fallback

    def _wait_for_timeline_update(self, page: Page, previous_scroll_height: int) -> None:
        try:
            page.wait_for_function(
                "previousHeight => document.body.scrollHeight > previousHeight",
                arg=previous_scroll_height,
                timeout=SCROLL_WAIT_MS,
            )
        except Exception as error:
            logger.debug("Timeline height did not change before timeout: %s", error)

    def _is_post_within_date_range(self, post_date: date | None, request: CollectRequest) -> bool:
        return is_within_date_range(post_date, request)

    def _find_first_visible_locator(
        self,
        root: Page | Locator,
        selectors: tuple[str, ...],
    ) -> Locator | None:
        return find_first_visible_locator(root, selectors)

    def _build_search_query(self, normalized_username: str, request: CollectRequest) -> str:
        query = f"from:{normalized_username}"
        if request.post_kind_filter in {"reposts", "all"}:
            query += " include:nativeretweets"
        # X's since:/until: dates are UTC days, while the requested range uses
        # local dates. Widen the query by one day on each side and let the
        # local-date post filter trim the extra results.
        if request.since_date is not None:
            query += f" since:{(request.since_date - timedelta(days=1)).isoformat()}"
        if request.media_filter == "with_media":
            query += " filter:media"
        if request.is_reply:
            query += " filter:replies"
        if request.min_likes > 0:
            query += f" min_faves:{request.min_likes}"
        if request.min_replies > 0:
            query += f" min_replies:{request.min_replies}"
        if request.until_date is not None:
            # X's `until:` filter is exclusive (does NOT include the specified date).
            # One day makes it inclusive; one more covers the UTC/local offset.
            query += f" until:{(request.until_date + timedelta(days=2)).isoformat()}"
        return query

    def _content_indicates_empty_search_results(self, page_content: str) -> bool:
        normalized_content = re.sub(r"\s+", " ", page_content)
        return any(marker in normalized_content for marker in SEARCH_EMPTY_STATE_MARKERS)

    def _should_stop_empty_search_collection(
        self,
        request: CollectRequest,
        collected_posts_count: int,
        page_content: str | None = None,
    ) -> bool:
        if request.search_mode != "search" or collected_posts_count > 0:
            return False
        return page_content is not None and self._content_indicates_empty_search_results(page_content)

    def _safe_get_page_text(self, page: Page) -> str | None:
        try:
            main_content = page.locator("main").first
            if main_content.count() > 0:
                return main_content.inner_text()
            return page.locator("body").first.inner_text()
        except Exception as error:
            logger.debug("Failed to inspect search page text: %s", error)
            return None

    def _validate_collect_filters(self, request: CollectRequest) -> None:
        request.validate()
        if request.media_filter not in VALID_MEDIA_FILTERS:
            raise ValueError(tr("media_filter は all / with_media / without_media のいずれかで指定してください。"))
        if request.search_mode not in VALID_SEARCH_MODES:
            raise ValueError(tr("search_mode は profile / search のいずれかで指定してください。"))
        if request.post_kind_filter not in VALID_POST_KIND_FILTERS:
            raise ValueError(tr("post_kind_filter は posts / reposts / all / likes のいずれかで指定してください。"))

    def _build_collection_url(self, normalized_username: str, request: CollectRequest) -> str:
        if request.post_kind_filter == "likes":
            # Liked posts are listed only on the account's own likes timeline.
            return urljoin(BASE_X_URL, f"{normalized_username}/{LIKES_PATH}")
        if request.search_mode == "search":
            query = self._build_search_query(normalized_username, request)
            encoded_query = quote(query)
            logger.debug("Search mode query: %s", query)
            return urljoin(BASE_X_URL, f"{SEARCH_PATH}?q={encoded_query}&f=live")
        return urljoin(BASE_X_URL, normalized_username)

    def _navigate_to_collection_target(
        self,
        page: Page,
        normalized_username: str,
        request: CollectRequest,
    ) -> None:
        url = self._build_collection_url(normalized_username, request)
        if request.search_mode == "search":
            logger.debug("Navigating to: %s", url)
        else:
            logger.debug("Profile mode navigating to: %s", url)
        page.goto(url, timeout=NAVIGATION_TIMEOUT_MS)
        page.locator("main").first.wait_for(state="visible", timeout=NAVIGATION_TIMEOUT_MS)

    def _get_article_metric_count(self, article: Locator, test_id: str) -> int:
        metric_test = "@data-testid='like' or @data-testid='unlike'" if test_id == "like" else f"@data-testid='{test_id}'"
        metrics = article.locator(f"xpath=.//*[{metric_test}][{actions.OWNED_NODE_XPATH}]")
        for index in range(metrics.count()):
            metric = metrics.nth(index)
            if metric.is_visible():
                return parse_count_from_aria_label(metric.get_attribute("aria-label"))
        return 0

    def _parse_supported_post_url(self, href: str | None) -> tuple[str, str] | None:
        if not href:
            return None

        post_url = urljoin(BASE_X_URL, href)
        try:
            parsed = urlparse(post_url)
        except ValueError:
            return None

        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {"x.com", "www.x.com"}:
            return None

        path_match = SUPPORTED_POST_PATH_PATTERN.fullmatch(parsed.path)
        if path_match is None:
            return None
        return post_url, path_match.group(1)

    def _get_owned_timestamp_link(self, article: Locator) -> tuple[Locator, str] | None:
        timestamp_links = article.locator(OWNED_TIMESTAMP_LINK_SELECTOR)
        if timestamp_links.count() == 0:
            return None

        post_ids: set[str] = set()
        visible_link: tuple[Locator, str] | None = None
        for index in range(timestamp_links.count()):
            link = timestamp_links.nth(index)
            parsed_url = self._parse_supported_post_url(link.get_attribute("href"))
            if parsed_url is None:
                return None

            post_url, post_id = parsed_url
            post_ids.add(post_id)
            if visible_link is None and link.is_visible():
                visible_link = (link, post_url)

        if len(post_ids) != 1:
            return None
        return visible_link

    def _get_article_permalink(self, article: Locator) -> str | None:
        timestamp_link = self._get_owned_timestamp_link(article)
        return timestamp_link[1] if timestamp_link is not None else None

    def _get_article_text(self, article: Locator) -> str:
        text_element = article.locator(OWNED_TEXT_SELECTOR)
        if text_element.count() > 0 and text_element.first.is_visible():
            return text_element.first.inner_text()
        return ""

    def _get_article_date_text(self, article: Locator) -> str:
        timestamp_link = self._get_owned_timestamp_link(article)
        if timestamp_link is None:
            return "Unknown"

        time_tag = timestamp_link[0].locator(POST_TIME_SELECTOR)
        if time_tag.count() > 0:
            return time_tag.first.get_attribute("datetime") or "Unknown"
        return "Unknown"

    def _article_has_any_selector(self, article: Locator, selectors: tuple[str, ...]) -> bool:
        return any(article.locator(selector).count() > 0 for selector in selectors)

    def _build_post_record(
        self,
        *,
        post_id: str,
        post_url: str,
        author_username: str,
        text_content: str,
        date_text: str,
        likes_count: int,
        replies_count: int,
        has_media: bool,
        is_reply: bool,
        kind: PostKind,
    ) -> PostRecord:
        return PostRecord(
            id=post_id,
            url=post_url,
            author_username=author_username,
            text=(
                text_content[:TEXT_PREVIEW_LENGTH] + "..."
                if len(text_content) > TEXT_PREVIEW_LENGTH
                else text_content
            ),
            date=date_text,
            likes=likes_count,
            replies=replies_count,
            has_media=has_media,
            is_reply=is_reply,
            kind=kind,
        )

    def _post_matches_filters(
        self,
        *,
        request: CollectRequest,
        has_media: bool,
        is_reply: bool,
        kind: PostKind,
        likes_count: int,
        replies_count: int,
        post_date: date | None,
        article_index: int,
        text_content: str = "",
    ) -> bool:
        reason = post_skip_reason(
            request,
            has_media=has_media,
            is_reply=is_reply,
            kind=kind,
            likes_count=likes_count,
            replies_count=replies_count,
            post_date=post_date,
            text=text_content,
        )
        if reason is not None:
            logger.debug("Article %s: Skipped (%s)", article_index, reason)
            return False
        return True

    def _check_empty_search_early_exit(
        self,
        page: Page,
        request: CollectRequest,
        state: EmptySearchState,
    ) -> EmptySearchState:
        logger.debug(
            "No tweets found in DOM. Scroll attempt %s/%s",
            state.scroll_attempts,
            MAX_SCROLL_ATTEMPTS,
        )
        if request.search_mode == "search":
            if state.started_at is None:
                state.started_at = time.monotonic()
            if self._should_stop_empty_search_collection(
                request, 0, self._safe_get_page_text(page),
            ):
                state.should_stop = True
            elif time.monotonic() - state.started_at >= EMPTY_SEARCH_TIMEOUT_SECONDS:
                logger.warning("Search results did not appear before the loading deadline.")
                self.collection_limit_reached = True
                state.should_stop = True
            else:
                # Do not scroll past results while the initial search is still loading.
                self._cancel_event.wait(0.25)
            return state
        state.scroll_attempts += 1
        page.evaluate(f"window.scrollBy(0, {EMPTY_STATE_SCROLL_Y});")
        self._wait_for_timeline_update(page, state.previous_scroll_height)
        return state

    def _process_articles(
        self,
        articles: list[Locator],
        request: CollectRequest,
        normalized_username: str,
        collected_posts: list[PostRecord],
        seen_urls: set[str],
    ) -> ArticleProcessingResult:
        collected_in_cycle = 0
        new_posts_found = False
        needs_retry = False

        for i, article in enumerate(articles):
            if self.is_cancel_requested() or len(collected_posts) >= request.max_posts:
                break

            try:
                post_url = self._get_article_permalink(article)
                if post_url is None:
                    logger.debug("Article %s: Time/URL element not visible or not found. Skipping.", i)
                    needs_retry = True
                    continue
                if post_url in seen_urls:
                    continue

                date_text = self._get_article_date_text(article)
                post_date = parse_post_date(date_text)

                post_info = self._process_known_article_url(
                    article=article,
                    request=request,
                    normalized_username=normalized_username,
                    article_index=i,
                    post_url=post_url,
                    date_text=date_text,
                    post_date=post_date,
                )
                seen_urls.add(post_url)
                new_posts_found = True
                if post_info is None:
                    continue
                collected_posts.append(post_info)
                collected_in_cycle += 1

            except Exception as error:
                needs_retry = True
                logger.warning("Error parsing post element: %s", error)
                continue

        return ArticleProcessingResult(
            collected_in_cycle=collected_in_cycle,
            new_posts_found=new_posts_found,
            needs_retry=needs_retry,
        )

    def _process_known_article_url(
        self,
        *,
        article: Locator,
        request: CollectRequest,
        normalized_username: str,
        article_index: int,
        post_url: str,
        date_text: str,
        post_date: date | None,
    ) -> PostRecord | None:
        post_id = extract_post_id(post_url)
        author_username = extract_post_author_username(post_url) or normalized_username
        kind: PostKind
        if request.post_kind_filter == "likes":
            # The likes timeline lists other accounts' posts; none of them is a repost.
            kind = "like"
        elif author_username.casefold() != normalized_username.casefold():
            kind = "repost"
        else:
            kind = "post"
        logger.debug("Article %s: Found new URL: %s", article_index, post_url)

        text_content = self._get_article_text(article)
        is_reply_post = self._article_has_any_selector(article, REPLY_CONTEXT_SELECTORS)
        has_media_post = self._article_has_any_selector(article, MEDIA_SELECTORS)

        likes_count = self._get_article_metric_count(article, "like")
        replies_count = self._get_article_metric_count(article, "reply")

        if not self._post_matches_filters(
            request=request,
            has_media=has_media_post,
            is_reply=is_reply_post,
            kind=kind,
            likes_count=likes_count,
            replies_count=replies_count,
            post_date=post_date,
            article_index=article_index,
            text_content=text_content,
        ):
            return None

        post_info = self._build_post_record(
            post_id=post_id,
            post_url=post_url,
            author_username=author_username,
            text_content=text_content,
            date_text=date_text,
            likes_count=likes_count,
            replies_count=replies_count,
            has_media=has_media_post,
            is_reply=is_reply_post,
            kind=kind,
        )
        logger.debug(
            "Collected post: %s - Kind: %s, Likes: %s, Replies: %s, Media: %s, Reply: %s",
            post_url,
            kind,
            likes_count,
            replies_count,
            has_media_post,
            is_reply_post,
        )
        return post_info

    def get_logged_in_username(self) -> str | None:
        return self._browser.get_logged_in_username()

    def start_browser(self, user_data_dir: str | Path | None = None, account_name: str | None = None) -> None:
        self._browser.start(user_data_dir, account_name)

    def go_to_home(self) -> None:
        self._browser.go_to_home()

    def is_logged_in(self) -> bool:
        return self._browser.is_logged_in()

    def stop_browser(self) -> None:
        self._browser.stop()

    def search_and_collect_posts(
        self,
        request: CollectRequest,
        on_progress: Callable[[int, int, int], None] | None = None,
    ) -> list[PostRecord]:
        """
        指定された条件に基づいてポストを収集する
        戻り値: [{'url': '...', 'text': '...', 'date': '...', 'id': '...'}, ...]
        """
        page = self._require_page()
        self.collection_limit_reached = False
        self._validate_collect_filters(request)
        normalized_username = self._normalize_username(request.username)

        logger.info(
            "Starting post collection: "
            f"username={normalized_username}, max={request.max_posts}, mode={request.search_mode}, "
            f"kind={request.post_kind_filter}, "
            f"media_filter={request.media_filter}, reply={request.is_reply}, "
            f"likes={request.min_likes}, replies={request.min_replies}, "
            f"since={request.since_date.isoformat() if request.since_date is not None else 'none'}, "
            f"until={request.until_date.isoformat() if request.until_date is not None else 'none'}"
        )
        collected_posts: list[PostRecord] = []
        seen_urls: set[str] = set()

        self._navigate_to_collection_target(page, normalized_username, request)

        empty_state = EmptySearchState(
            scroll_attempts=0,
            previous_scroll_height=self._safe_get_scroll_height(page, 0),
            should_stop=False,
        )
        stable_scroll_cycles = 0
        collection_cycles = 0
        logger.info("Starting scroll loop...")

        while (
            len(collected_posts) < request.max_posts
            and empty_state.scroll_attempts < MAX_SCROLL_ATTEMPTS
            and collection_cycles < MAX_COLLECTION_CYCLES
            and not self.is_cancel_requested()
        ):
            collection_cycles += 1
            if on_progress is not None:
                on_progress(len(seen_urls), collection_cycles, MAX_COLLECTION_CYCLES)
            articles = page.locator(POST_ARTICLE_SELECTOR).all()

            if not articles:
                if collected_posts:
                    empty_state.scroll_attempts += 1
                    previous_scroll_height = empty_state.previous_scroll_height
                    page.evaluate(f"window.scrollBy(0, {EMPTY_STATE_SCROLL_Y});")
                    self._wait_for_timeline_update(page, previous_scroll_height)
                    continue
                empty_state = self._check_empty_search_early_exit(page, request, empty_state)
                if empty_state.should_stop:
                    break
                continue

            empty_state.started_at = None
            logger.debug("Found %s tweet articles on screen.", len(articles))
            # Re-read the same viewport before scrolling; failed URLs remain eligible.
            aggregate = ArticleProcessingResult(collected_in_cycle=0, new_posts_found=False)
            for attempt in range(MAX_ARTICLE_PARSE_ATTEMPTS):
                if self.is_cancel_requested():
                    break
                article_result = self._process_articles(
                    articles=page.locator(POST_ARTICLE_SELECTOR).all(),
                    request=request,
                    normalized_username=normalized_username,
                    collected_posts=collected_posts,
                    seen_urls=seen_urls,
                )
                aggregate.collected_in_cycle += article_result.collected_in_cycle
                aggregate.new_posts_found |= article_result.new_posts_found
                if not article_result.needs_retry or len(collected_posts) >= request.max_posts:
                    break
                if attempt < MAX_ARTICLE_PARSE_ATTEMPTS - 1:
                    self._cancel_event.wait(0.25)
            if self.is_cancel_requested():
                break
            article_result = aggregate

            if not article_result.new_posts_found:
                empty_state.scroll_attempts += 1
                logger.debug(
                    "No new posts found during this cycle. Scroll attempt %s/%s",
                    empty_state.scroll_attempts,
                    MAX_SCROLL_ATTEMPTS,
                )
            else:
                empty_state.scroll_attempts = 0
                stable_scroll_cycles = 0

            # Pinned posts and reposts can precede newer posts. A viewport's
            # original post dates cannot establish the end of the requested range.
            logger.debug("Scrolling down... Collected so far: %s", len(collected_posts))
            previous_scroll_height = empty_state.previous_scroll_height
            page.evaluate(f"window.scrollBy(0, {COLLECTION_SCROLL_Y});")
            self._wait_for_timeline_update(page, previous_scroll_height)

            current_scroll_height = self._safe_get_scroll_height(page, empty_state.previous_scroll_height)
            if current_scroll_height <= empty_state.previous_scroll_height:
                stable_scroll_cycles += 1
                logger.debug(
                    "Scroll height did not increase. Stable cycles: %s/%s",
                    stable_scroll_cycles,
                    MAX_STABLE_SCROLL_CYCLES,
                )
                if stable_scroll_cycles >= MAX_STABLE_SCROLL_CYCLES and not article_result.new_posts_found:
                    logger.info("Stopping collection because scroll height remained unchanged and no new posts were found.")
                    break
            else:
                stable_scroll_cycles = 0
                empty_state.previous_scroll_height = current_scroll_height

        if collection_cycles >= MAX_COLLECTION_CYCLES and len(collected_posts) < request.max_posts:
            self.collection_limit_reached = True
            logger.warning(
                "Collection stopped at the absolute cycle limit (%s); scanned %s unique URLs.",
                MAX_COLLECTION_CYCLES,
                len(seen_urls),
            )
        if self.is_cancel_requested():
            logger.info("Post collection cancelled after gathering %s posts.", len(collected_posts))
        else:
            logger.info("Finished collection. Total posts gathered: %s", len(collected_posts))
        return collected_posts

    def collect_following(
        self,
        username: str,
        on_progress: Callable[[int], None] | None = None,
    ) -> FollowCollectionResult:
        page = self._require_page()
        return collect_following(page, self._normalize_username(username), self._cancel_event, on_progress)

    def unfollow_account(self, username: str) -> tuple[bool, str | None]:
        page = self._require_page()
        return _unfollow_account(page, username, find_locator=self._find_first_visible_locator)

    def fetch_last_post(self, username: str) -> LastPostResult:
        page = self._require_page()
        return fetch_last_post(page, username, self._cancel_event)

    def execute_post_action(self, target: PostActionTarget) -> PostActionResult:
        page = self._browser._require_page()
        return _execute_post_action(page, target, find_locator=self._find_first_visible_locator)

    def undo_repost(self, post_url: str) -> tuple[bool, str | None]:
        page = self._browser._require_page()
        return _undo_repost(page, post_url, find_locator=self._find_first_visible_locator)

    def unlike_post(self, post_url: str) -> tuple[bool, str | None]:
        page = self._browser._require_page()
        return _unlike_post(page, post_url, find_locator=self._find_first_visible_locator)

    def delete_post(self, post_url: str) -> tuple[bool, str | None]:
        page = self._browser._require_page()
        return _delete_post(page, post_url, find_locator=self._find_first_visible_locator)
