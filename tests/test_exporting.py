import csv
import json
from pathlib import Path

from kusamushiri.exporting import POST_CSV_FIELDS, write_post_list
from kusamushiri.models import PostRecord

POSTS = [
    PostRecord(
        id="1",
        url="https://x.com/alice/status/1",
        author_username="alice",
        text="おはよう 🌱\n二行目",
        date="2026-01-02T03:04:05.000Z",
        likes=3,
        replies=1,
        has_media=True,
        is_reply=False,
        is_repost=False,
    ),
    PostRecord(
        id="2",
        url="https://x.com/bob/status/2",
        author_username="bob",
        text="=HYPERLINK(\"x\")",
        date="2026-01-01",
        likes=0,
        replies=0,
        has_media=False,
        is_reply=True,
        is_repost=True,
    ),
]


def test_write_post_list_csv_is_excel_friendly(tmp_path: Path) -> None:
    path = tmp_path / "posts.csv"
    write_post_list(path, POSTS)

    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))
    assert rows == [
        list(POST_CSV_FIELDS),
        ["1", "https://x.com/alice/status/1", "2026-01-02T03:04:05.000Z", "post", "おはよう 🌱\n二行目", "3", "1", "true", "false"],
        ["2", "https://x.com/bob/status/2", "2026-01-01", "repost", "'=HYPERLINK(\"x\")", "0", "0", "false", "true"],
    ]


def test_write_post_list_json_keeps_raw_values(tmp_path: Path) -> None:
    path = tmp_path / "posts.json"
    write_post_list(path, POSTS)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[0] == {
        "id": "1",
        "url": "https://x.com/alice/status/1",
        "date": "2026-01-02T03:04:05.000Z",
        "kind": "post",
        "text": "おはよう 🌱\n二行目",
        "likes": 3,
        "replies": 1,
        "has_media": True,
        "is_reply": False,
    }
    assert data[1]["text"] == "=HYPERLINK(\"x\")"
    assert data[1]["kind"] == "repost"
