"""X の「データのアーカイブをダウンロード」で得たアーカイブからポストを読み込む。

GUI に依存しない純粋なモジュールで、収集結果と同じ PostRecord を返す。
"""

import json
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Final

from kusamushiri.models import CollectRequest, PostRecord, text_contains_any_keyword
from kusamushiri.parsing import USERNAME_PATTERN, parse_post_date

FALLBACK_USERNAME: Final = "i"
TWEETS_FILE_PATTERN: Final = re.compile(r"^tweets?(?:-part(\d+))?\.js$")
ACCOUNT_FILE_NAME: Final = "account.js"
YTD_PREFIX_PATTERN: Final = re.compile(r"^\s*window\.YTD\.[A-Za-z0-9_]+\.part\d+\s*=\s*")
RETWEET_TEXT_PATTERN: Final = re.compile(r"^RT @([A-Za-z0-9_]{1,15})")
# created_at は "Wed Oct 10 20:19:24 +0000 2018" 形式。%b はロケール依存なので月名は自前で引く。
MONTHS: Final = {
    name: index
    for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1
    )
}
UNKNOWN_DATE: Final = "Unknown"


class ArchiveError(Exception):
    """アーカイブが見つからない、読めない、または形式が不正なときに送出する。"""


@dataclass(frozen=True, slots=True)
class _ArchiveFile:
    name: str
    content: str


def load_archive_posts(archive_path: str | Path, fallback_username: str | None = None) -> list[PostRecord]:
    """アーカイブ(.zip または展開済みフォルダ)からポストを読み込み、新しい順に返す。

    URL のユーザー名は data/account.js を優先し、無ければ fallback_username、
    それも無ければ "i" (https://x.com/i/status/<id>) を使う。
    """
    path = Path(archive_path)
    if path.is_dir():
        tweet_files, account_file = _read_folder(path)
    elif path.is_file():
        tweet_files, account_file = _read_zip(path)
    else:
        raise ArchiveError(f"Archive not found: {path}")

    if not tweet_files:
        raise ArchiveError(f"No tweets.js found in archive: {path}")

    username = _resolve_username(account_file, fallback_username)
    records: dict[str, tuple[datetime | None, PostRecord]] = {}
    for tweet_file in tweet_files:
        for entry in _parse_ytd_array(tweet_file):
            created_at, record = _build_record(entry, username, tweet_file.name)
            records[record.id] = (created_at, record)

    return [record for _, record in sorted(records.values(), key=_sort_key, reverse=True)]


def filter_archive_posts(posts: Iterable[PostRecord], request: CollectRequest) -> list[PostRecord]:
    """収集時と同じ条件(種別、メディア、返信、いいね/返信数、期間、キーワード)で絞り込む。

    username / search_mode / max_posts はブラウザ収集向けの項目なので使わない。
    """
    return [post for post in posts if _post_matches_request(post, request)]


def _post_matches_request(post: PostRecord, request: CollectRequest) -> bool:
    if request.media_filter == "with_media" and not post.has_media:
        return False
    if request.media_filter == "without_media" and post.has_media:
        return False
    if request.is_reply and not post.is_reply:
        return False
    if request.post_kind_filter == "posts" and post.is_repost:
        return False
    if request.post_kind_filter == "reposts" and not post.is_repost:
        return False
    if post.likes < request.min_likes or post.replies < request.min_replies:
        return False
    post_date = parse_post_date(post.date)
    if request.since_date is not None and (post_date is None or post_date < request.since_date):
        return False
    if request.until_date is not None and (post_date is None or post_date > request.until_date):
        return False
    if request.include_keywords and not text_contains_any_keyword(post.text, request.include_keywords):
        return False
    return not (request.exclude_keywords and text_contains_any_keyword(post.text, request.exclude_keywords))


def _is_archive_data_dir(parent: PurePosixPath) -> bool:
    # data/ 直下に加え、data フォルダそのものを指定された場合のアーカイブ直下も受け付ける。
    return parent.name == "data" or str(parent) in {"", "."}


def _tweet_file_order(name: str) -> int:
    match = TWEETS_FILE_PATTERN.match(PurePosixPath(name).name)
    return int(match.group(1)) if match and match.group(1) else 0


def _read_folder(folder: Path) -> tuple[list[_ArchiveFile], _ArchiveFile | None]:
    data_dir = folder / "data" if (folder / "data").is_dir() else folder
    tweet_paths = sorted(
        (child for child in data_dir.iterdir() if child.is_file() and TWEETS_FILE_PATTERN.match(child.name)),
        key=lambda child: _tweet_file_order(child.name),
    )
    account_path = data_dir / ACCOUNT_FILE_NAME
    try:
        tweet_files = [_ArchiveFile(child.name, child.read_text(encoding="utf-8-sig")) for child in tweet_paths]
        account_file = (
            _ArchiveFile(account_path.name, account_path.read_text(encoding="utf-8-sig"))
            if account_path.is_file()
            else None
        )
    except (OSError, UnicodeDecodeError) as error:
        raise ArchiveError(f"Could not read archive folder {folder}: {error}") from error
    return tweet_files, account_file


def _read_zip(zip_path: Path) -> tuple[list[_ArchiveFile], _ArchiveFile | None]:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = [
                name
                for name in archive.namelist()
                if not name.endswith("/") and _is_archive_data_dir(PurePosixPath(name).parent)
            ]
            tweet_names = sorted(
                (name for name in members if TWEETS_FILE_PATTERN.match(PurePosixPath(name).name)),
                key=_tweet_file_order,
            )
            account_name = next((name for name in members if PurePosixPath(name).name == ACCOUNT_FILE_NAME), None)
            tweet_files = [_ArchiveFile(name, archive.read(name).decode("utf-8-sig")) for name in tweet_names]
            account_file = (
                _ArchiveFile(account_name, archive.read(account_name).decode("utf-8-sig"))
                if account_name is not None
                else None
            )
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, RuntimeError) as error:
        raise ArchiveError(f"Could not read archive zip {zip_path}: {error}") from error
    return tweet_files, account_file


def _parse_ytd_array(archive_file: _ArchiveFile) -> list[dict[str, Any]]:
    """`window.YTD.<name>.partN = [...]` の接頭辞を外して JSON 配列として読む。"""
    prefix = YTD_PREFIX_PATTERN.match(archive_file.content)
    if prefix is None:
        raise ArchiveError(f"Unexpected format in {archive_file.name}: missing window.YTD prefix")
    try:
        data = json.loads(archive_file.content[prefix.end() :].strip().removesuffix(";"))
    except json.JSONDecodeError as error:
        raise ArchiveError(f"Malformed JSON in {archive_file.name}: {error}") from error
    if not isinstance(data, list) or not all(isinstance(entry, dict) for entry in data):
        raise ArchiveError(f"Unexpected format in {archive_file.name}: expected an array of objects")
    return data


def _resolve_username(account_file: _ArchiveFile | None, fallback_username: str | None) -> str:
    if account_file is not None:
        for entry in _parse_ytd_array(account_file):
            account = entry.get("account")
            username = account.get("username") if isinstance(account, dict) else None
            if isinstance(username, str) and USERNAME_PATTERN.fullmatch(username):
                return username
    fallback = (fallback_username or "").strip().removeprefix("@")
    return fallback if USERNAME_PATTERN.fullmatch(fallback) else FALLBACK_USERNAME


def _parse_created_at(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    parts = raw.split()
    if len(parts) != 6 or parts[1] not in MONTHS:
        return None
    _, month_name, day, clock, offset, year = parts
    try:
        hour, minute, second = (int(value) for value in clock.split(":"))
        sign = -1 if offset.startswith("-") else 1
        offset_delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5])) * sign
        return datetime(int(year), MONTHS[month_name], int(day), hour, minute, second, tzinfo=timezone(offset_delta))
    except ValueError:
        return None


def _to_int(value: object) -> int:
    try:
        return int(value) if isinstance(value, (int, str)) else 0
    except ValueError:
        return 0


def _has_media(tweet: dict[str, Any]) -> bool:
    for key in ("extended_entities", "entities"):
        entities = tweet.get(key)
        if isinstance(entities, dict) and entities.get("media"):
            return True
    return False


def _build_record(entry: dict[str, Any], username: str, source_name: str) -> tuple[datetime | None, PostRecord]:
    tweet = entry.get("tweet", entry)
    if not isinstance(tweet, dict):
        raise ArchiveError(f"Unexpected tweet entry in {source_name}")
    post_id = tweet.get("id_str") or tweet.get("id")
    if not isinstance(post_id, str) or not post_id.isdigit():
        raise ArchiveError(f"Tweet without a valid id_str in {source_name}")

    text = tweet.get("full_text") or tweet.get("text") or ""
    text = text if isinstance(text, str) else ""
    created_at = _parse_created_at(tweet.get("created_at"))
    retweet_match = RETWEET_TEXT_PATTERN.match(text)
    return created_at, PostRecord(
        id=post_id,
        url=f"https://x.com/{username}/status/{post_id}",
        # 収集結果と同様、リポストの author_username は元ポストの投稿者にする。
        author_username=retweet_match.group(1) if retweet_match else username,
        text=text,
        date=created_at.isoformat() if created_at is not None else UNKNOWN_DATE,
        likes=_to_int(tweet.get("favorite_count")),
        replies=_to_int(tweet.get("reply_count")),
        has_media=_has_media(tweet),
        is_reply=bool(tweet.get("in_reply_to_status_id_str")),
        is_repost=retweet_match is not None,
    )


def _sort_key(item: tuple[datetime | None, PostRecord]) -> tuple[float, int]:
    created_at, record = item
    return (created_at.timestamp() if created_at is not None else float("-inf"), int(record.id))
