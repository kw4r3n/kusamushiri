from dataclasses import replace
from datetime import date

import pytest

from kusamushiri.core import EmptySearchState, XDeleterCore
from kusamushiri.models import CollectRequest, PostRecord


def request() -> CollectRequest:
    return CollectRequest("user", 10, "all", False, 0, 0, "search")


def test_empty_search_waits_until_deadline_without_scrolling(monkeypatch):
    core = XDeleterCore()
    clock = [100.0]
    monkeypatch.setattr("kusamushiri.core.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(core, "_safe_get_page_text", lambda page: "loading results")
    monkeypatch.setattr(core._cancel_event, "wait", lambda timeout: None)
    state = EmptySearchState(0, 100, False)
    for _ in range(5):
        core._check_empty_search_early_exit(object(), request(), state)
    assert not state.should_stop
    clock[0] += 15
    core._check_empty_search_early_exit(object(), request(), state)
    assert state.should_stop
    assert core.collection_limit_reached


def test_explicit_empty_result_stops_immediately(monkeypatch):
    core = XDeleterCore()
    monkeypatch.setattr(core, "_safe_get_page_text", lambda page: "No results for from:user")
    state = EmptySearchState(0, 100, False)
    core._check_empty_search_early_exit(object(), request(), state)
    assert state.should_stop
    assert not core.collection_limit_reached


def test_transient_parse_failure_remains_retryable(monkeypatch):
    core = XDeleterCore()
    monkeypatch.setattr(core, "_get_article_permalink", lambda article: "https://x.com/user/status/1")
    monkeypatch.setattr(core, "_get_article_date_text", lambda article: "2026-01-01")
    calls = []

    def parse(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("detached during render")
        return PostRecord("1", "https://x.com/user/status/1", "user", "text", "2026-01-01", 0, 0, False, False, False)

    monkeypatch.setattr(core, "_process_known_article_url", parse)
    seen = set()
    posts = []
    result = core._process_articles([object()], request(), "user", posts, seen)
    assert result.needs_retry
    assert not seen
    result = core._process_articles([object()], request(), "user", posts, seen)
    assert not result.needs_retry
    assert len(posts) == 1
    core._process_articles([object()], request(), "user", posts, seen)
    assert len(calls) == 2


def test_collection_retries_before_scrolling_and_bounds_permanent_failures(monkeypatch):
    class Articles:
        def all(self):
            return [object()]

    class Page:
        def locator(self, selector):
            return Articles()

        def evaluate(self, script):
            if "scrollBy" in script:
                events.append("scroll")
            return 100

    core = XDeleterCore()
    core._browser.page = Page()
    events = []
    monkeypatch.setattr(core, "_navigate_to_collection_target", lambda *args: None)
    monkeypatch.setattr(core, "_wait_for_timeline_update", lambda *args: None)
    monkeypatch.setattr(core._cancel_event, "wait", lambda timeout: None)
    monkeypatch.setattr(core, "_get_article_permalink", lambda article: "https://x.com/user/status/1")
    monkeypatch.setattr(core, "_get_article_date_text", lambda article: "2026-01-01")

    def parse(**kwargs):
        events.append("parse")
        raise RuntimeError("permanently broken article")

    monkeypatch.setattr(core, "_process_known_article_url", parse)
    assert core.search_and_collect_posts(request()) == []
    assert events == ["parse", "parse", "parse", "scroll"] * 3


def test_cancel_interrupts_search_loading(monkeypatch):
    class Articles:
        def all(self):
            return []

    class Page:
        def locator(self, selector):
            return Articles()

        def evaluate(self, script):
            return 100

    core = XDeleterCore()
    core._browser.page = Page()
    monkeypatch.setattr(core, "_navigate_to_collection_target", lambda *args: None)
    monkeypatch.setattr(core, "_safe_get_page_text", lambda page: "loading")
    monkeypatch.setattr(core._cancel_event, "wait", lambda timeout: core.request_cancel())
    assert core.search_and_collect_posts(request()) == []
    assert core.is_cancel_requested()
    assert not core.collection_limit_reached


def test_filtered_post_is_marked_seen(monkeypatch):
    core = XDeleterCore()
    monkeypatch.setattr(core, "_get_article_permalink", lambda article: "https://x.com/user/status/1")
    monkeypatch.setattr(core, "_get_article_date_text", lambda article: "2026-01-01")
    monkeypatch.setattr(core, "_process_known_article_url", lambda **kwargs: None)
    seen = set()
    result = core._process_articles([object()], request(), "user", [], seen)
    assert seen == {"https://x.com/user/status/1"}
    assert not result.needs_retry


def test_profile_collection_continues_past_old_posts(monkeypatch):
    """Pinned posts and reposts do not establish a chronological lower bound."""
    posts = [
        PostRecord("1", "https://x.com/user/status/1", "user", "old pinned post", "2020-01-01", 0, 0, False, False, False),
        PostRecord("2", "https://x.com/other/status/2", "other", "old repost", "2021-01-01", 0, 0, False, False, True),
        PostRecord("3", "https://x.com/user/status/3", "user", "recent post", "2026-09-26", 0, 0, False, False, False),
    ]

    class Page:
        index = 0

        def locator(self, selector):
            return self

        def all(self):
            return [posts[self.index]]

        def evaluate(self, script):
            if "scrollBy" in script:
                self.index = min(self.index + 1, len(posts) - 1)
            return 100

    core = XDeleterCore()
    core._browser.page = Page()
    monkeypatch.setattr(core, "_navigate_to_collection_target", lambda *args: None)
    monkeypatch.setattr(core, "_wait_for_timeline_update", lambda *args: None)
    monkeypatch.setattr(core, "_get_article_permalink", lambda article: article.url)
    monkeypatch.setattr(core, "_get_article_date_text", lambda article: article.date)

    def parse(**kwargs):
        if core._is_post_within_date_range(kwargs["post_date"], kwargs["request"]):
            return kwargs["article"]
        return None

    monkeypatch.setattr(core, "_process_known_article_url", parse)
    filtered_request = replace(request(), search_mode="profile", since_date=date(2026, 9, 1), max_posts=1)
    assert core.search_and_collect_posts(filtered_request) == [posts[-1]]


@pytest.mark.parametrize("cancel_before_start", [False, True])
def test_article_processing_honors_cancellation_between_posts(monkeypatch, cancel_before_start):
    core = XDeleterCore()
    monkeypatch.setattr(core, "_get_article_permalink", lambda article: f"https://x.com/user/status/{article}")
    monkeypatch.setattr(core, "_get_article_date_text", lambda article: "2026-09-26")
    parsed = []

    def parse(**kwargs):
        parsed.append(kwargs["article"])
        core.request_cancel()
        return None

    monkeypatch.setattr(core, "_process_known_article_url", parse)
    if cancel_before_start:
        core.request_cancel()
    core._process_articles([1, 2, 3], request(), "user", [], set())
    assert parsed == ([] if cancel_before_start else [1])
