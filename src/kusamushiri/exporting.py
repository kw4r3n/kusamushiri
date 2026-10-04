"""Write lists of records to CSV (UTF-8 with BOM, for Excel) or JSON."""

import csv
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from kusamushiri.models import PostRecord

EXPORT_SUFFIXES = frozenset({".csv", ".json"})
POST_CSV_FIELDS = ("id", "url", "date", "kind", "text", "likes", "replies", "has_media", "is_reply")
# Leading characters that make spreadsheet apps evaluate a cell as a formula.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def spreadsheet_safe(value: str) -> str:
    return f"'{value}" if value.startswith(FORMULA_PREFIXES) else value


def _csv_cell(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return spreadsheet_safe(value)
    return str(value)


def write_records(path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, object]]) -> None:
    """Write rows as CSV or JSON, chosen by the file extension.

    JSON keeps raw values; CSV neutralizes formula-like text and writes booleans as true/false.
    """
    if path.suffix.lower() == ".json":
        path.write_text(
            json.dumps([{field: row[field] for field in fields} for row in rows], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(fields)
        for row in rows:
            writer.writerow([_csv_cell(row[field]) for field in fields])


def write_post_list(path: Path, posts: Sequence[PostRecord]) -> None:
    write_records(
        path,
        POST_CSV_FIELDS,
        [
            {
                "id": post.id,
                "url": post.url,
                "date": post.date,
                "kind": "repost" if post.is_repost else "post",
                "text": post.text,
                "likes": post.likes,
                "replies": post.replies,
                "has_media": post.has_media,
                "is_reply": post.is_reply,
            }
            for post in posts
        ],
    )
