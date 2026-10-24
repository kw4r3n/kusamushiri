"""Regression cases from the account-cleanup audit; no X traffic."""

from unittest.mock import Mock

import pytest
from test_core import build_request

from kusamushiri import actions
from kusamushiri.core import XDeleterCore


@pytest.mark.parametrize("text, collapsed, expected", [("", False, False), ("   ", False, False),
                                                       ("visible prefix", True, False), ("readable", False, True)])
def test_exclusions_require_readable_complete_text(monkeypatch, text, collapsed, expected):
    core = XDeleterCore()
    article = Mock()
    article.locator.return_value.count.return_value = int(collapsed)
    monkeypatch.setattr(core, "_get_article_text", lambda _: text)
    monkeypatch.setattr(core, "_article_has_any_selector", lambda *_: False)
    monkeypatch.setattr(core, "_get_article_metric_count", lambda *_: 0)
    result = core._process_known_article_url(article=article, request=build_request(exclude_keywords=("protected",)),
        normalized_username="user", article_index=0, post_url="https://x.com/user/status/1",
        date_text="2026-10-01", post_date=None)
    assert (result is not None) == expected


@pytest.mark.parametrize("mutation", ["DeleteTweet", "DeleteRetweet", "UnfavoriteTweet"])
def test_production_dom_completion_requires_an_observed_mutation(mutation):
    page = Mock(url="https://www.x.com/user/status/1")
    assert actions._wait_for_mutation_result([], mutation, lambda: None, page=page) is not None
