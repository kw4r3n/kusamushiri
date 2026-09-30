from datetime import date

import pytest

from kusamushiri.models import CollectRequest, ExecuteActionsRequest, PostActionTarget


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
        targets=[PostActionTarget(url="https://x.com/example/status/1", is_repost=False)],
        interval_seconds=-0.5,
    )

    with pytest.raises(ValueError, match="削除/解除間隔は0秒以上で指定してください。"):
        request.validate()
