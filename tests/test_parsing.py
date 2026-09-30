import time
from datetime import date

import pytest

from kusamushiri.parsing import (
    extract_post_author_username,
    extract_post_id,
    extract_profile_username,
    parse_count_from_aria_label,
    parse_post_date,
)


def test_extract_post_author_username_returns_status_url_author() -> None:
    assert extract_post_author_username("https://x.com/example_user/status/12345") == "example_user"
    assert extract_post_author_username("https://twitter.com/example_user/status/12345") is None


def test_extract_post_id_returns_status_segment_when_present() -> None:
    assert extract_post_id("https://x.com/example_user/status/12345/photo/1") == "12345"
    assert extract_post_id("https://x.com/example_user") == "example_user"
    assert extract_post_id("") == ""


def test_extract_profile_username_rejects_missing_or_invalid_usernames() -> None:
    assert extract_profile_username(None) is None
    assert extract_profile_username("/") is None
    assert extract_profile_username("/too-long-username-value") is None
    assert extract_profile_username("/valid_user/following") == "valid_user"


@pytest.fixture
def local_timezone(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is unavailable")

    def set_timezone(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield set_timezone
    monkeypatch.undo()
    time.tzset()


def test_parse_post_date_returns_date_for_iso_datetime(local_timezone) -> None:
    local_timezone("UTC")
    assert parse_post_date("2026-04-19T03:04:05.000Z") == date(2026, 4, 19)
    assert parse_post_date("Unknown") is None
    assert parse_post_date("not a date") is None


def test_parse_count_from_aria_label_reads_first_integer() -> None:
    assert parse_count_from_aria_label("1,234 Likes") == 1234
    assert parse_count_from_aria_label("返信 56件") == 56
    assert parse_count_from_aria_label(None) == 0
    assert parse_count_from_aria_label("No likes") == 0


def test_parse_post_date_uses_local_date(local_timezone) -> None:
    local_timezone("Asia/Tokyo")
    assert parse_post_date("2026-04-19T20:00:00.000Z") == date(2026, 4, 20)
    local_timezone("America/Los_Angeles")
    assert parse_post_date("2026-04-19T03:00:00.000Z") == date(2026, 4, 18)
    # タイムゾーンなしの値はローカル日付のまま扱う。
    assert parse_post_date("2026-04-19") == date(2026, 4, 19)
