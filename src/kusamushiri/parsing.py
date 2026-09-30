import re
from datetime import date, datetime
from typing import Final
from urllib.parse import urlparse

from kusamushiri.logger import logger

TEXT_PREVIEW_LENGTH: Final = 100
USERNAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9_]{1,15}$")
POST_URL_PATTERN: Final = re.compile(r"^https?://(?:www\.)?x\.com/([^/]+)/status/\d+")


def parse_post_date(raw_date_text: str) -> date | None:
    if not raw_date_text or raw_date_text == "Unknown":
        return None

    try:
        parsed = datetime.fromisoformat(raw_date_text.replace("Z", "+00:00"))
    except ValueError:
        logger.debug("Failed to parse post date: %s", raw_date_text)
        return None
    # GUIの日付指定はローカル日付なので、UTCのタイムスタンプもローカル日付に揃える。
    # タイムゾーンなしの値はローカル時刻として扱われ、日付は変わらない。
    try:
        return parsed.astimezone().date()
    except (OverflowError, OSError):
        logger.debug("Failed to convert post date to local time: %s", raw_date_text)
        return parsed.date()


def extract_post_author_username(post_url: str) -> str | None:
    match = POST_URL_PATTERN.match(post_url)
    if match is None:
        return None
    return match.group(1)


def extract_post_id(post_url: str) -> str:
    path_parts = [part for part in urlparse(post_url).path.split("/") if part]
    if "status" in path_parts:
        status_index = path_parts.index("status")
        if status_index + 1 < len(path_parts):
            return path_parts[status_index + 1]
    return path_parts[-1] if path_parts else ""


def extract_profile_username(href: str | None) -> str | None:
    if not href:
        return None

    path = urlparse(href).path if href.startswith("http") else href
    username = path.strip("/").split("/", maxsplit=1)[0]
    if not username or not USERNAME_PATTERN.fullmatch(username):
        return None
    return username


def parse_count_from_aria_label(aria_label: str | None) -> int:
    if not aria_label:
        return 0
    count_match = re.search(r"(\d+)", aria_label.replace(",", ""))
    return int(count_match.group(1)) if count_match else 0
