"""Read followed accounts' newest post dates and keep the results per app profile."""

import contextlib
import json
import os
import tempfile
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Locator, Page

from kusamushiri.actions import OWNED_TIMESTAMP_LINK_SELECTOR, POST_ARTICLE_SELECTOR
from kusamushiri.browser import BASE_X_URL, NAVIGATION_TIMEOUT_MS, find_first_visible_locator
from kusamushiri.follows import SCROLL_WAIT_MS, SCROLL_Y, FollowRecord, LastPostEntry
from kusamushiri.i18n import tr
from kusamushiri.logger import logger
from kusamushiri.parsing import USERNAME_PATTERN, extract_post_author_username
from kusamushiri.paths import get_account_profile_dir

# Stored inside the app profile's directory, so renaming or deleting the profile carries it along.
STORE_FILE_NAME = "last-posts.json"
STORE_VERSION = 1
DEFAULT_LAST_POST_INTERVAL_SECONDS = 5.0
TIMELINE_LOAD_TIMEOUT_SECONDS = 15.0
TIMELINE_POLL_SECONDS = 0.25
# Pinned posts and reposts can fill the first screen; look a little further, not through the whole history.
MAX_LAST_POST_SCROLLS = 2
# Protected, suspended, missing and empty profiles show this instead of a timeline.
EMPTY_STATE_SELECTOR = "[data-testid='primaryColumn'] [data-testid='emptyState']"
# "Pinned" (and "<name> reposted") labels sit above a top-level post, outside any quoted post.
SOCIAL_CONTEXT_SELECTOR = "xpath=.//*[@data-testid='socialContext'][count(ancestor::article) = 1]"


@dataclass(frozen=True, slots=True)
class LastPostResult:
    username: str
    checked_at: str
    last_post_at: str | None = None
    note: str | None = None  # why no date was found (protected, empty, ...)
    error_message: str | None = None

    @property
    def success(self) -> bool:
        return self.error_message is None


@dataclass(frozen=True, slots=True)
class FetchLastPostsRequest:
    targets: list[FollowRecord]
    interval_seconds: float = DEFAULT_LAST_POST_INTERVAL_SECONDS

    def validate(self) -> None:
        if not self.targets:
            raise ValueError(tr("最終ポスト日を取得するアカウントが選択されていません。"))
        if self.interval_seconds < 0:
            raise ValueError(tr("取得の間隔は0秒以上で指定してください。"))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _utc_iso(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.debug("Failed to parse post timestamp: %s", raw)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    # A single UTC format keeps the stored strings in chronological order.
    return parsed.astimezone(UTC).isoformat()


def _own_post_timestamp(article: Locator, username: str) -> str | None:
    """Return the article's timestamp when it is the account's own, unpinned post."""
    link = article.locator(OWNED_TIMESTAMP_LINK_SELECTOR).first
    if link.count() == 0:
        return None
    author = extract_post_author_username(urljoin(BASE_X_URL, link.get_attribute("href") or ""))
    if author is None or author.casefold() != username.casefold():
        return None  # a repost: the permalink belongs to the original author
    if article.locator(SOCIAL_CONTEXT_SELECTOR).count() > 0:
        return None  # pinned: shown first whatever its date
    return _utc_iso(link.locator("time").first.get_attribute("datetime"))


def _newest_loaded_post(page: Page, username: str) -> str | None:
    timestamps = [
        timestamp
        for article in page.locator(POST_ARTICLE_SELECTOR).all()
        if (timestamp := _own_post_timestamp(article, username)) is not None
    ]
    return max(timestamps, default=None)


def _wait_for_timeline(page: Page, cancel_event: threading.Event) -> str | None:
    """Return "posts", "empty", or None when neither appeared in time."""
    waited = 0.0
    while not cancel_event.is_set():
        if page.locator(POST_ARTICLE_SELECTOR).count() > 0:
            return "posts"
        if find_first_visible_locator(page, (EMPTY_STATE_SELECTOR,)) is not None:
            return "empty"
        if waited >= TIMELINE_LOAD_TIMEOUT_SECONDS:
            return None
        cancel_event.wait(TIMELINE_POLL_SECONDS)
        waited += TIMELINE_POLL_SECONDS
    return None


def _scroll_timeline(page: Page) -> None:
    previous_height = int(page.evaluate("document.body.scrollHeight"))
    page.evaluate(f"window.scrollBy(0, {SCROLL_Y});")
    try:
        page.wait_for_function(
            "previousHeight => document.body.scrollHeight > previousHeight",
            arg=previous_height,
            timeout=SCROLL_WAIT_MS,
        )
    except Exception as error:
        logger.debug("Profile timeline height did not change before timeout: %s", error)


def fetch_last_post(page: Page, username: str, cancel_event: threading.Event) -> LastPostResult:
    """Open ``x.com/<username>`` and read the date of its newest own post, skipping pinned posts and reposts."""
    checked_at = _now_iso()
    if not USERNAME_PATTERN.fullmatch(username):
        return LastPostResult(
            username,
            checked_at,
            error_message=tr("ユーザー名は1〜15文字の英数字またはアンダースコアで指定してください。"),
        )
    try:
        page.goto(urljoin(BASE_X_URL, username), timeout=NAVIGATION_TIMEOUT_MS)
        page.locator("main").first.wait_for(state="visible", timeout=NAVIGATION_TIMEOUT_MS)
        state = _wait_for_timeline(page, cancel_event)
        if cancel_event.is_set():
            return LastPostResult(username, checked_at, error_message=tr("取得を中断しました。"))
        if state is None:
            return LastPostResult(username, checked_at, error_message=tr("タイムラインを読み込めませんでした。"))
        if state == "empty":
            empty_state = find_first_visible_locator(page, (EMPTY_STATE_SELECTOR,))
            message = empty_state.inner_text().strip().splitlines() if empty_state is not None else []
            note = message[0] if message else tr("ポストが見つかりません。")
            logger.info("No posts shown for @%s: %s", username, note)
            return LastPostResult(username, checked_at, note=note)
        for scroll in range(MAX_LAST_POST_SCROLLS + 1):
            newest = _newest_loaded_post(page, username)
            if newest is not None:
                logger.info("Newest post of @%s: %s", username, newest)
                return LastPostResult(username, checked_at, last_post_at=newest)
            if scroll == MAX_LAST_POST_SCROLLS or cancel_event.is_set():
                break
            _scroll_timeline(page)
        if cancel_event.is_set():
            return LastPostResult(username, checked_at, error_message=tr("取得を中断しました。"))
        return LastPostResult(username, checked_at, note=tr("固定ポストとリポスト以外のポストが見つかりません。"))
    except Exception as error:
        logger.exception("Failed to read the newest post of @%s.", username)
        return LastPostResult(username, checked_at, error_message=str(error) or type(error).__name__)


class LastPostStore:
    """Fetched results keyed by lowercase username, in a small JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def for_account(cls, account_name: str | None) -> "LastPostStore":
        return cls(get_account_profile_dir(account_name) / STORE_FILE_NAME)

    def load(self) -> dict[str, LastPostEntry]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            logger.warning("Ignoring unreadable last-post file %s: %s", self.path, error)
            return {}
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(accounts, dict):
            return {}
        entries: dict[str, LastPostEntry] = {}
        for username, value in accounts.items():
            if not isinstance(value, dict) or not isinstance(value.get("checked_at"), str):
                continue
            last_post_at = value.get("last_post_at")
            note = value.get("note")
            entries[str(username).casefold()] = LastPostEntry(
                checked_at=value["checked_at"],
                last_post_at=last_post_at if isinstance(last_post_at, str) else None,
                note=note if isinstance(note, str) else None,
            )
        return entries

    def record(self, results: Iterable[LastPostResult]) -> dict[str, LastPostEntry]:
        """Merge successful results into the file and return every stored entry.

        Failed fetches keep the earlier result, if any.
        """
        entries = self.load()
        for result in results:
            if result.success:
                entries[result.username.casefold()] = LastPostEntry(
                    checked_at=result.checked_at, last_post_at=result.last_post_at, note=result.note
                )
        self._write(entries)
        return entries

    def _write(self, entries: Mapping[str, LastPostEntry]) -> None:
        payload = {
            "version": STORE_VERSION,
            "accounts": {
                username: {"last_post_at": entry.last_post_at, "checked_at": entry.checked_at, "note": entry.note}
                for username, entry in sorted(entries.items())
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write a sibling file and swap it in, so a crash never leaves a truncated store.
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise


def apply_last_post_entries(
    records: Iterable[FollowRecord], entries: Mapping[str, LastPostEntry]
) -> list[FollowRecord]:
    applied: list[FollowRecord] = []
    for record in records:
        entry = entries.get(record.username.casefold())
        if entry is not None:
            record = replace(
                record, last_post_at=entry.last_post_at, checked_at=entry.checked_at, last_post_note=entry.note
            )
        applied.append(record)
    return applied
