"""Regression cases from the account-cleanup audit; no X traffic."""

import json
from unittest.mock import Mock

import pytest
from test_core import build_request
from test_retry import FakeCore, target

from kusamushiri import actions
from kusamushiri.core import XDeleterCore
from kusamushiri.models import PostActionResult, RetryPolicy
from kusamushiri.progress import ActionJournal
from kusamushiri.retry import run_post_actions


@pytest.mark.parametrize("kind", ["like", "repost"])
def test_view_change_recovery_completes_and_forgets_undo_success(monkeypatch, tmp_path, kind):
    core = FakeCore(monkeypatch)
    item = target(1, kind)
    statuses = []
    core.behavior = lambda item: (
        PostActionResult(item, "undo", False, "changed", "retryable", deferred=True, view_reuse_invalid=True)
        if len(core.calls) == 1 else PostActionResult(item, "undo", True, None)
    )
    journal = ActionJournal(tmp_path / "progress.json", "test")
    results = run_post_actions(core, [item, target(2)], 0, RetryPolicy(), journal=journal, on_status=statuses.append)
    assert all(result.success for result in results)
    assert journal.stop_reason == "completed"
    assert journal.previous_result(item) is None
    assert "理由: 完了" in statuses[-1]
    assert not any("X の利用制限" in status for status in statuses)


@pytest.mark.parametrize("resume, expected_calls", [(True, []), (False, [target(1, "like")])])
def test_new_batch_and_resume_treat_old_undo_success_differently(monkeypatch, tmp_path, resume, expected_calls):
    core = FakeCore(monkeypatch)
    journal = ActionJournal(tmp_path / "progress.json", "test")
    with journal:
        journal.record(target(1, "like"), "success")
        journal.record(target(2), "success")
        journal.record(target(3, "repost"), "ambiguous")
    run_post_actions(core, [target(1, "like"), target(2)], 0, RetryPolicy(), journal=journal, resume=resume)
    assert core.calls == expected_calls
    assert journal.entries == {"post:2": "success", "repost:3": "ambiguous"}


def test_known_deletions_skip_writes_and_compact_redundant_receipts(monkeypatch, tmp_path):
    core = FakeCore(monkeypatch)
    journal = ActionJournal(tmp_path / "progress.json", "test")
    with journal:
        journal.record(target(1), "success")
        journal.record(target(999), "ambiguous")
    journal.record = Mock(wraps=journal.record)
    results = run_post_actions(core, [target(i) for i in range(1, 101)], 0, RetryPolicy(), journal=journal,
                               known_successes={f"post:{i}" for i in range(1, 101)})
    assert len(results) == 100 and all(result.success and result.attempts == 0 for result in results)
    assert core.calls == []
    journal.record.assert_not_called()
    assert json.loads(journal.path.read_text())["entries"] == {"post:999": "ambiguous"}


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
