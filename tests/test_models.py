from datetime import date

import pytest

from kusamushiri.models import (
    CollectRequest,
    ExecuteActionsRequest,
    PostActionTarget,
    parse_keywords,
    text_contains_any_keyword,
)


def build_request(**overrides: object) -> CollectRequest:
    values: dict[str, object] = {
        "username": "user",
        "max_posts": 10,
        "media_filter": "all",
        "is_reply": False,
        "min_likes": 0,
        "min_replies": 0,
        "search_mode": "search",
        "post_kind_filter": "posts",
        "since_date": None,
        "until_date": None,
    }
    values.update(overrides)
    return CollectRequest(**values)


def test_collect_request_accepts_valid_date_range() -> None:
    request = build_request(
        since_date=date(2026, 4, 10),
        until_date=date(2026, 4, 19),
    )

    request.validate()


def test_collect_request_rejects_reversed_date_range() -> None:
    request = build_request(
        since_date=date(2026, 4, 20),
        until_date=date(2026, 4, 19),
    )

    with pytest.raises(ValueError, match="開始日は終了日以前で指定してください。"):
        request.validate()


def test_execute_actions_request_rejects_negative_interval() -> None:
    request = ExecuteActionsRequest(
        targets=[PostActionTarget(url="https://x.com/example/status/1", kind="post")],
        interval_seconds=-0.5,
    )

    with pytest.raises(ValueError, match="削除/解除間隔は0秒以上で指定してください。"):
        request.validate()


def test_parse_keywords_splits_on_ascii_and_japanese_commas_and_drops_blanks() -> None:
    assert parse_keywords(" 懸賞, キャンペーン、応募，,\n懸賞 ") == ("懸賞", "キャンペーン", "応募")
    assert parse_keywords("   ") == ()


def test_text_contains_any_keyword_ignores_case_and_width() -> None:
    assert text_contains_any_keyword("New ＰＯＳＴ here", ("post",))
    assert text_contains_any_keyword("ｷｬﾝﾍﾟｰﾝ実施中", ("キャンペーン",))
    assert not text_contains_any_keyword("日常の話", ("懸賞", "キャンペーン"))
    assert not text_contains_any_keyword("", ("懸賞",))


def test_collect_request_rejects_blank_keyword() -> None:
    with pytest.raises(ValueError, match="空のキーワードは指定できません。"):
        build_request(exclude_keywords=(" ",)).validate()


def test_collect_request_accepts_likes_in_profile_mode() -> None:
    build_request(search_mode="profile", post_kind_filter="likes").validate()


def test_collect_request_rejects_likes_in_search_mode() -> None:
    with pytest.raises(ValueError, match="いいねはプロフィールのいいね欄からのみ収集できます。"):
        build_request(search_mode="search", post_kind_filter="likes").validate()
