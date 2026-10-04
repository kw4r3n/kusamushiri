"""Collect an account's following list from X and export it to CSV or JSON."""

import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page

from kusamushiri.browser import BASE_X_URL, NAVIGATION_TIMEOUT_MS
from kusamushiri.exporting import write_records
from kusamushiri.i18n import tr
from kusamushiri.logger import logger
from kusamushiri.parsing import USERNAME_PATTERN

FOLLOWING_PATH = "following"
# The "Who to follow" sidebar also renders UserCells; only read the timeline column.
USER_CELL_SELECTOR = "[data-testid='primaryColumn'] [data-testid='UserCell']"
SCROLL_Y = 1_500
SCROLL_WAIT_MS = 2_000
# X caps following at a few thousand accounts; ~20 cells load per scroll.
MAX_FOLLOW_COLLECTION_CYCLES = 500
MAX_STABLE_CYCLES = 5
INITIAL_LOAD_TIMEOUT_SECONDS = 15.0
RESERVED_PATHS = frozenset({"home", "explore", "i", "messages", "notifications", "search", "settings"})
CSV_FIELDS = ("username", "display_name", "profile_url", "follows_you")

# Emoji in names render as <img alt="…">; innerText would drop them.
_READ_CELLS_JS = """
cells => cells.map(cell => {
  const textWithAlt = node => [...node.childNodes].map(child => {
    if (child.nodeType === Node.TEXT_NODE) return child.textContent;
    if (child.nodeName === 'IMG') return child.getAttribute('alt') || '';
    return child.nodeType === Node.ELEMENT_NODE ? textWithAlt(child) : '';
  }).join('');
  const links = [...cell.querySelectorAll('a[href]')];
  // Skip image-only links (the avatar) and the @handle link.
  const names = links
    .filter(a => a.innerText.trim() && !a.innerText.trim().startsWith('@'))
    .map(a => textWithAlt(a).trim());
  return {
    hrefs: links.map(a => a.getAttribute('href')),
    displayName: names[0] || '',
    followsYou: cell.querySelector('[data-testid="userFollowIndicator"]') !== null,
  };
})
"""


@dataclass(frozen=True, slots=True)
class FollowRecord:
    username: str
    display_name: str
    profile_url: str
    follows_you: bool


@dataclass(frozen=True, slots=True)
class ExportFollowingRequest:
    username: str
    output_path: Path

    def validate(self) -> None:
        if not USERNAME_PATTERN.fullmatch(self.username.strip().removeprefix("@")):
            raise ValueError(tr("ユーザー名は1〜15文字の英数字またはアンダースコアで指定してください。"))
        if self.output_path.suffix.lower() not in {".csv", ".json"}:
            raise ValueError(tr("保存先の拡張子は .csv または .json を指定してください。"))


@dataclass(slots=True)
class FollowCollectionResult:
    records: list[FollowRecord]
    limit_reached: bool = False


def parse_profile_href(href: str | None) -> str | None:
    """Return the username for a bare profile link such as ``/name``."""
    if not href:
        return None
    parsed = urlparse(urljoin(BASE_X_URL, href))
    if parsed.netloc.lower() not in {"x.com", "www.x.com"}:
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 1 or segments[0].lower() in RESERVED_PATHS:
        return None
    username = segments[0]
    return username if USERNAME_PATTERN.fullmatch(username) else None


def _record_from_cell(cell: dict[str, object]) -> FollowRecord | None:
    hrefs = cell.get("hrefs")
    if not isinstance(hrefs, list):
        return None
    username = next((name for href in hrefs if (name := parse_profile_href(href)) is not None), None)
    if username is None:
        return None
    return FollowRecord(
        username=username,
        display_name=str(cell.get("displayName") or ""),
        profile_url=urljoin(BASE_X_URL, username),
        follows_you=bool(cell.get("followsYou")),
    )


def collect_following(
    page: Page,
    username: str,
    cancel_event: threading.Event,
    on_progress: Callable[[int], None] | None = None,
) -> FollowCollectionResult:
    """Scroll ``/<username>/following`` and return each account once, in page order."""
    url = urljoin(BASE_X_URL, f"{username}/{FOLLOWING_PATH}")
    logger.info("Collecting following list: %s", url)
    page.goto(url, timeout=NAVIGATION_TIMEOUT_MS)
    page.locator("main").first.wait_for(state="visible", timeout=NAVIGATION_TIMEOUT_MS)

    records: dict[str, FollowRecord] = {}
    previous_height = int(page.evaluate("document.body.scrollHeight"))
    stable_cycles = 0
    empty_waited = 0.0
    result = FollowCollectionResult(records=[])

    for _cycle in range(MAX_FOLLOW_COLLECTION_CYCLES):
        if cancel_event.is_set():
            break
        # X virtualizes the list, so read the rendered cells before every scroll.
        cells = page.locator(USER_CELL_SELECTOR).evaluate_all(_READ_CELLS_JS)
        if not cells and not records:
            if empty_waited >= INITIAL_LOAD_TIMEOUT_SECONDS:
                logger.info("No accounts appeared on the following page.")
                break
            cancel_event.wait(0.5)
            empty_waited += 0.5
            continue

        found_new = False
        for cell in cells:
            record = _record_from_cell(cell)
            if record is None or record.username.casefold() in records:
                continue
            records[record.username.casefold()] = record
            found_new = True
        if on_progress is not None:
            on_progress(len(records))

        page.evaluate(f"window.scrollBy(0, {SCROLL_Y});")
        try:
            page.wait_for_function(
                "previousHeight => document.body.scrollHeight > previousHeight",
                arg=previous_height,
                timeout=SCROLL_WAIT_MS,
            )
        except Exception as error:
            logger.debug("Following list height did not change before timeout: %s", error)
        current_height = int(page.evaluate("document.body.scrollHeight"))
        if current_height > previous_height or found_new:
            stable_cycles = 0
            previous_height = max(previous_height, current_height)
        else:
            stable_cycles += 1
            if stable_cycles >= MAX_STABLE_CYCLES:
                break
    else:
        result.limit_reached = True
        logger.warning("Following collection stopped at the cycle limit (%s).", MAX_FOLLOW_COLLECTION_CYCLES)

    result.records = list(records.values())
    logger.info("Collected %s followed accounts.", len(result.records))
    return result


def write_follow_list(path: Path, records: list[FollowRecord]) -> None:
    """Write CSV (UTF-8 with BOM, for Excel) or JSON, chosen by the file extension."""
    write_records(path, CSV_FIELDS, [asdict(record) for record in records])
