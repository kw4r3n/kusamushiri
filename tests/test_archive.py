import json
import zipfile
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from kusamushiri.archive import ArchiveError, filter_archive_posts, load_archive_posts, select_archive_posts
from kusamushiri.deleted_posts import DeletedPostStore
from kusamushiri.models import CollectRequest


def make_tweet(
    post_id: str,
    created_at: str,
    text: str = "hello",
    *,
    likes: int = 0,
    reply_to: str | None = None,
    media_key: str | None = None,
) -> dict[str, Any]:
    tweet: dict[str, Any] = {
        "id_str": post_id,
        "created_at": created_at,
        "full_text": text,
        "favorite_count": str(likes),
        "retweet_count": "0",
        "entities": {"hashtags": [], "user_mentions": []},
    }
    if reply_to is not None:
        tweet["in_reply_to_status_id_str"] = reply_to
    if media_key is not None:
        tweet[media_key] = {"media": [{"id_str": "9", "type": "photo"}]}
    return {"tweet": tweet}


def ytd(name: str, part: int, entries: list[dict[str, Any]]) -> str:
    return f"window.YTD.{name}.part{part} = {json.dumps(entries, indent=2)}"


ACCOUNT_JS = ytd("account", 0, [{"account": {"username": "alice", "accountId": "1"}}])
PART0 = [
    make_tweet("100", "Wed Oct 10 20:19:24 +0000 2018", "oldest post"),
    make_tweet("300", "Mon Jan 06 09:00:00 +0000 2020", "RT @bob: shared text"),
]
PART1 = [
    make_tweet("200", "Sun Jun 02 12:00:00 +0000 2019", "a reply", reply_to="150", likes=5),
    make_tweet("400", "Fri Mar 05 10:30:00 +0900 2021", "with photo", likes=12, media_key="extended_entities"),
]


def standard_files(*, with_account: bool = True) -> dict[str, str]:
    files = {"tweets.js": ytd("tweets", 0, PART0), "tweets-part1.js": ytd("tweets", 1, PART1)}
    if with_account:
        files["account.js"] = ACCOUNT_JS
    return files


def write_folder(root: Path, files: dict[str, str], prefix: str = "data") -> Path:
    data_dir = root / prefix if prefix else root
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (data_dir / name).write_text(content, encoding="utf-8")
    return root


def write_zip(path: Path, files: dict[str, str], prefix: str = "data/") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Your archive.html", "<html></html>")
        archive.writestr(f"{prefix}tweets_media/", "")
        for name, content in files.items():
            archive.writestr(f"{prefix}{name}", content)
    return path


def make_request(**overrides: Any) -> CollectRequest:
    base = CollectRequest(
        username="alice",
        max_posts=100,
        media_filter="all",
        is_reply=False,
        min_likes=0,
        min_replies=0,
        search_mode="profile",
        post_kind_filter="all",
    )
    return replace(base, **overrides)


def test_loads_multi_part_zip_newest_first(tmp_path: Path) -> None:
    posts = load_archive_posts(write_zip(tmp_path / "archive.zip", standard_files()))

    assert [post.id for post in posts] == ["400", "300", "200", "100"]
    assert posts[0].url == "https://x.com/alice/status/400"
    assert posts[0].date == "2021-03-05T10:30:00+09:00"
    assert posts[0].likes == 12
    assert posts[0].has_media is True
    assert posts[3].date == "2018-10-10T20:19:24+00:00"


def test_folder_matches_zip(tmp_path: Path) -> None:
    from_zip = load_archive_posts(write_zip(tmp_path / "archive.zip", standard_files()))
    from_folder = load_archive_posts(write_folder(tmp_path / "extracted", standard_files()))

    assert from_folder == from_zip


def test_folder_pointed_at_data_dir_and_zip_without_prefix(tmp_path: Path) -> None:
    data_dir = write_folder(tmp_path / "extracted", standard_files()) / "data"
    flat_zip = write_zip(tmp_path / "flat.zip", standard_files(), prefix="")

    assert [post.id for post in load_archive_posts(data_dir)] == ["400", "300", "200", "100"]
    assert [post.id for post in load_archive_posts(flat_zip)] == ["400", "300", "200", "100"]


def test_older_tweet_js_name(tmp_path: Path) -> None:
    files = {"tweet.js": ytd("tweet", 0, PART0), "account.js": ACCOUNT_JS}

    posts = load_archive_posts(write_folder(tmp_path, files))

    assert [post.id for post in posts] == ["300", "100"]


def test_retweet_reply_and_media_flags(tmp_path: Path) -> None:
    posts = {post.id: post for post in load_archive_posts(write_folder(tmp_path, standard_files()))}

    assert posts["300"].kind == "repost"
    assert posts["300"].author_username == "bob"
    assert posts["300"].url == "https://x.com/alice/status/300"
    assert posts["200"].is_reply is True
    assert posts["200"].kind == "post"
    assert posts["100"].is_reply is False
    assert posts["100"].has_media is False
    assert posts["100"].author_username == "alice"


def test_media_in_entities(tmp_path: Path) -> None:
    files = {"tweets.js": ytd("tweets", 0, [make_tweet("1", "Wed Oct 10 20:19:24 +0000 2018", media_key="entities")])}

    assert load_archive_posts(write_folder(tmp_path, files))[0].has_media is True


def test_missing_account_uses_fallback_username(tmp_path: Path) -> None:
    root = write_folder(tmp_path, standard_files(with_account=False))

    assert load_archive_posts(root, fallback_username="@carol")[0].url == "https://x.com/carol/status/400"
    assert load_archive_posts(root)[0].url == "https://x.com/i/status/400"


def test_unparseable_date_sorts_last(tmp_path: Path) -> None:
    entries = [*PART0, make_tweet("999", "not a date")]
    posts = load_archive_posts(write_folder(tmp_path, {"tweets.js": ytd("tweets", 0, entries)}))

    assert posts[-1].id == "999"
    assert posts[-1].date == "Unknown"


def test_no_tweets_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError, match=r"No tweets\.js"):
        load_archive_posts(write_zip(tmp_path / "archive.zip", {"account.js": ACCOUNT_JS}))
    with pytest.raises(ArchiveError, match=r"No tweets\.js"):
        load_archive_posts(write_folder(tmp_path / "folder", {"account.js": ACCOUNT_JS}))


def test_missing_path_and_bad_zip_raise(tmp_path: Path) -> None:
    bad_zip = tmp_path / "broken.zip"
    bad_zip.write_bytes(b"not a zip file")

    with pytest.raises(ArchiveError, match="not found"):
        load_archive_posts(tmp_path / "missing.zip")
    with pytest.raises(ArchiveError, match="Could not read"):
        load_archive_posts(bad_zip)


@pytest.mark.parametrize(
    "content",
    [
        'window.YTD.tweets.part0 = [{"tweet": ',
        "[]",
        'window.YTD.tweets.part0 = {"tweet": {}}',
        'window.YTD.tweets.part0 = [{"tweet": {"full_text": "no id"}}]',
    ],
)
def test_malformed_tweets_raise(tmp_path: Path, content: str) -> None:
    with pytest.raises(ArchiveError):
        load_archive_posts(write_folder(tmp_path, {"tweets.js": content}))


def test_filter_applies_collect_request(tmp_path: Path) -> None:
    posts = load_archive_posts(write_folder(tmp_path, standard_files()))

    def ids(**overrides: Any) -> list[str]:
        return [post.id for post in filter_archive_posts(posts, make_request(**overrides))]

    assert ids() == ["400", "300", "200", "100"]
    assert ids(post_kind_filter="posts") == ["400", "200", "100"]
    assert ids(post_kind_filter="reposts") == ["300"]
    assert ids(media_filter="with_media") == ["400"]
    assert ids(media_filter="without_media") == ["300", "200", "100"]
    assert ids(is_reply=True) == ["200"]
    assert ids(min_likes=5) == ["400", "200"]
    assert ids(since_date=date(2019, 1, 1), until_date=date(2020, 12, 31)) == ["300", "200"]
    assert ids(include_keywords=("ＰＨＯＴＯ",)) == ["400"]
    assert ids(exclude_keywords=("Reply", "oldest")) == ["400", "300"]


def test_select_skips_deleted_ids_before_limit(tmp_path: Path) -> None:
    posts = load_archive_posts(write_folder(tmp_path, standard_files()))

    selection = select_archive_posts(posts, make_request(max_posts=2), deleted_ids={"400", "999"})

    # 300 is a repost; dropping deleted 400 lets older 100 fill the freed slot.
    assert [post.id for post in selection.posts] == ["200", "100"]
    assert selection.skipped_reposts == 1
    assert selection.skipped_deleted == 1


def test_deleted_post_store_round_trip(tmp_path: Path) -> None:
    store = DeletedPostStore(tmp_path / "profile" / "deleted-posts.json")
    assert store.load() == set()

    assert store.record(["12", "3", ""]) == {"12", "3"}
    assert store.record(["3", "40"]) == {"12", "3", "40"}
    assert DeletedPostStore(store.path).load() == {"12", "3", "40"}


@pytest.mark.parametrize("content", ["not json", "[]", '{"post_ids": "1"}', '{"post_ids": [1, "2"]}'])
def test_deleted_post_store_ignores_unreadable_file(tmp_path: Path, content: str) -> None:
    path = tmp_path / "deleted-posts.json"
    path.write_text(content, encoding="utf-8")

    assert DeletedPostStore(path).load() == ({"2"} if "[1" in content else set())
