"""X の「データのアーカイブをダウンロード」で得たアーカイブからポストを読み込む。

GUI に依存しない純粋なモジュールで、収集結果と同じ PostRecord を返す。
"""

import json
import re
import zipfile
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Final

from kusamushiri.models import CollectRequest, PostRecord, post_skip_reason
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


@dataclass(frozen=True, slots=True)
class _ArchiveSource:
    tweet_names: list[str]
    account_name: str | None
    read: Callable[[str], _ArchiveFile]


def load_archive_posts(
    archive_path: str | Path,
    fallback_username: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[PostRecord]:
    """アーカイブ(.zip または展開済みフォルダ)からポストを読み込み、新しい順に返す。

    archive_path には data/tweets.js などのファイル自体も渡せる。
    URL のユーザー名は data/account.js を優先し、無ければ fallback_username、
    それも無ければ "i" (https://x.com/i/status/<id>) を使う。
    on_progress は tweets.js の各パートを読み始める前に (読み終えたパート数, 全パート数) で呼ばれる。
    """
    path = Path(archive_path)
    # A picked tweets.js stands for the extracted archive folder around it.
    if path.is_file() and path.suffix.lower() == ".js":
        path = path.parent
    if not path.exists():
        raise ArchiveError(f"Archive not found: {path}")

    records: dict[str, tuple[datetime | None, PostRecord]] = {}
    try:
        with _open_archive(path) as source:
            if not source.tweet_names:
                raise ArchiveError(f"No tweets.js found in archive: {path}")
            account_file = source.read(source.account_name) if source.account_name is not None else None
            username = _resolve_username(account_file, fallback_username)
            for index, name in enumerate(source.tweet_names):
                if on_progress is not None:
                    on_progress(index, len(source.tweet_names))
                # Read one part at a time and drop each tweet dict as soon as its record is built,
                # so a large archive never holds every parsed tweet in memory at once.
                for entry in _iter_ytd_array(source.read(name)):
                    created_at, record = _build_record(entry, username, name)
                    records[record.id] = (created_at, record)
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, RuntimeError) as error:
        raise ArchiveError(f"Could not read archive {path}: {error}") from error

    return [record for _, record in sorted(records.values(), key=_sort_key, reverse=True)]


def filter_archive_posts(posts: Iterable[PostRecord], request: CollectRequest) -> list[PostRecord]:
    """収集時と同じ条件(種別、メディア、返信、いいね/返信数、期間、キーワード)で絞り込む。

    username / search_mode / max_posts はブラウザ収集向けの項目なので使わない。
    """
    return [post for post in posts if _post_matches_request(post, request)]


@dataclass(frozen=True, slots=True)
class ArchiveSelection:
    posts: list[PostRecord]
    total: int
    skipped_reposts: int
    skipped_deleted: int = 0


def select_archive_posts(
    posts: Sequence[PostRecord],
    request: CollectRequest,
    deleted_ids: Collection[str] = (),
    *,
    oldest_first: bool = False,
) -> ArchiveSelection:
    """アーカイブで扱えない条件を除いて絞り込み、新しい順 (oldest_first なら古い順) に max_posts 件までを返す。

    アーカイブのリポストは元ポストではなくリポスト自体の ID を持つため URL から解除できず、対象外にする。
    アーカイブには返信数が無いので、最低返信数の条件は使わない。
    deleted_ids (このアプリで削除済みのポスト ID) は件数上限より前に除き、空いた枠に古いポストが入るようにする。
    """
    originals = [post for post in posts if post.kind != "repost"]
    remaining = [post for post in originals if post.id not in deleted_ids]
    matched = filter_archive_posts(remaining, replace(request, min_replies=0))
    if oldest_first:
        matched.reverse()
    return ArchiveSelection(
        posts=matched[: request.max_posts],
        total=len(posts),
        skipped_reposts=len(posts) - len(originals),
        skipped_deleted=len(originals) - len(remaining),
    )


def _post_matches_request(post: PostRecord, request: CollectRequest) -> bool:
    return (
        post_skip_reason(
            request,
            has_media=post.has_media,
            is_reply=post.is_reply,
            kind=post.kind,
            likes_count=post.likes,
            replies_count=post.replies,
            post_date=parse_post_date(post.date) if request.since_date or request.until_date else None,
            text=post.text,
        )
        is None
    )


def _is_archive_data_dir(parent: PurePosixPath) -> bool:
    # data/ 直下に加え、data フォルダそのものを指定された場合のアーカイブ直下も受け付ける。
    return parent.name == "data" or str(parent) in {"", "."}


def _tweet_file_order(name: str) -> int:
    match = TWEETS_FILE_PATTERN.match(PurePosixPath(name).name)
    return int(match.group(1)) if match and match.group(1) else 0


@contextmanager
def _open_archive(path: Path) -> Iterator[_ArchiveSource]:
    if path.is_dir():
        data_dir = path / "data" if (path / "data").is_dir() else path
        tweet_names = sorted(
            (child.name for child in data_dir.iterdir() if child.is_file() and TWEETS_FILE_PATTERN.match(child.name)),
            key=_tweet_file_order,
        )
        account_name = ACCOUNT_FILE_NAME if (data_dir / ACCOUNT_FILE_NAME).is_file() else None
        yield _ArchiveSource(
            tweet_names,
            account_name,
            lambda name: _ArchiveFile(name, (data_dir / name).read_text(encoding="utf-8-sig")),
        )
        return
    with zipfile.ZipFile(path) as archive:
        members = [
            name
            for name in archive.namelist()
            if not name.endswith("/") and _is_archive_data_dir(PurePosixPath(name).parent)
        ]
        yield _ArchiveSource(
            sorted((name for name in members if TWEETS_FILE_PATTERN.match(PurePosixPath(name).name)), key=_tweet_file_order),
            next((name for name in members if PurePosixPath(name).name == ACCOUNT_FILE_NAME), None),
            lambda name: _ArchiveFile(name, archive.read(name).decode("utf-8-sig")),
        )


def _iter_ytd_array(archive_file: _ArchiveFile) -> Iterator[dict[str, Any]]:
    """`window.YTD.<name>.partN = [...]` の接頭辞を外し、配列の要素を 1 件ずつ返す。"""
    text = archive_file.content
    prefix = YTD_PREFIX_PATTERN.match(text)
    if prefix is None:
        raise ArchiveError(f"Unexpected format in {archive_file.name}: missing window.YTD prefix")
    decoder = json.JSONDecoder()
    index = _skip_whitespace(text, prefix.end())
    if not text.startswith("[", index):
        raise ArchiveError(f"Unexpected format in {archive_file.name}: expected an array of objects")
    index = _skip_whitespace(text, index + 1)
    if text.startswith("]", index):
        return
    while True:
        try:
            # raw_decode parses in place, so the (possibly large) text is not copied.
            entry, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError as error:
            raise ArchiveError(f"Malformed JSON in {archive_file.name}: {error}") from error
        if not isinstance(entry, dict):
            raise ArchiveError(f"Unexpected format in {archive_file.name}: expected an array of objects")
        yield entry
        index = _skip_whitespace(text, index)
        if text.startswith("]", index):
            return
        if not text.startswith(",", index):
            raise ArchiveError(f"Malformed JSON in {archive_file.name}: expected ',' or ']' at char {index}")
        index = _skip_whitespace(text, index + 1)


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _resolve_username(account_file: _ArchiveFile | None, fallback_username: str | None) -> str:
    if account_file is not None:
        for entry in _iter_ytd_array(account_file):
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
        kind="repost" if retweet_match is not None else "post",
    )


def _sort_key(item: tuple[datetime | None, PostRecord]) -> tuple[float, int]:
    created_at, record = item
    return (created_at.timestamp() if created_at is not None else float("-inf"), int(record.id))
