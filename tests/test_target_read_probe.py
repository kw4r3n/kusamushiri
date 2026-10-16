"""Synthetic handles only: no Playwright driver, browser, auth store or network."""

import json
import threading
from types import SimpleNamespace

import pytest

from kusamushiri.actions import OWNED_TIMESTAMP_LINK_SELECTOR
from kusamushiri.browser import LOGIN_CHECK_SELECTORS, PROFILE_LINK_SELECTORS
from kusamushiri.core import XDeleterCore
from kusamushiri.target_read_probe import READ_PATHS, TargetReadRequest, TargetReadResult, probe_current_target

URL = "https://x.com/author/status/123"
REQUEST = TargetReadRequest(URL, "viewer")


class ReadHandle:
    def __getattr__(self, name):
        raise AssertionError(f"forbidden API: {name}")


class Links(ReadHandle):
    def __init__(self, hrefs):
        self.hrefs = hrefs

    def count(self):
        return len(self.hrefs)

    def nth(self, index):
        return Link(self.hrefs[index])

    @property
    def first(self):
        return self.nth(0)


class Link(ReadHandle):
    def __init__(self, href):
        self.href = href

    def is_visible(self):
        return True

    def get_attribute(self, name):
        assert name == "href"
        return self.href


class Article(ReadHandle):
    size = 1
    visible = True

    def __init__(self):
        self.links = Links(["/author/status/123"])

    def count(self):
        return self.size

    def is_visible(self):
        return self.visible

    def locator(self, selector):
        assert selector == OWNED_TIMESTAMP_LINK_SELECTOR
        return self.links


class Page(ReadHandle):
    url = URL
    closed = False

    def __init__(self, context):
        self.context = context
        self.article = Article()
        self.reads = []
        self.protocol_waits = []
        self.login = True
        self.viewer = "viewer"

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, timeout):
        assert timeout == 0  # Local driver event dispatch, never navigation.
        self.protocol_waits.append(timeout)

    def locator(self, selector):
        self.reads.append(selector)
        if selector.startswith("xpath=//article"):
            # Execute the production helper and assert its ownership boundary.
            # This fake does not claim to evaluate XPath/render a browser DOM.
            assert "not(ancestor::article)" in selector
            assert "ancestor::*[@data-testid='quoteTweet' or" in selector
            return self.article
        if selector == LOGIN_CHECK_SELECTORS[0]:
            return Links([f"/{self.viewer}"] if self.login else [])
        assert selector in PROFILE_LINK_SELECTORS
        return Links([f"/{self.viewer}"] if self.login else [])


@pytest.fixture
def session(monkeypatch):
    core = XDeleterCore()
    manager = core._browser
    browser = SimpleNamespace(is_connected=lambda: True)
    context = SimpleNamespace(browser=browser, pages=[])
    page = Page(context)
    context.pages.append(page)
    manager.page, manager.context, manager.browser, manager.playwright = page, context, browser, ReadHandle()

    # None of these may run, even on failure; fake handles forbid all other APIs.
    def forbidden(*args, **kwargs):
        pytest.fail("lifecycle/action/auth/resume API called")

    for name in ("start", "stop", "go_to_home", "resume_action_network"):
        monkeypatch.setattr(manager, name, forbidden)
    for name in ("clear_cancel", "execute_post_action", "search_and_collect_posts"):
        monkeypatch.setattr(core, name, forbidden)
    return core, manager, page


def run(core, **kwargs):
    return probe_current_target(core, REQUEST, worker_thread_id=threading.get_ident(), **kwargs)


def test_real_five_helpers_on_same_handles_with_independent_viewer(session):
    core, manager, page = session
    handles = (manager.page, manager.context, manager.browser, manager.playwright)
    result = core.probe_current_target(REQUEST, worker_thread_id=threading.get_ident())
    assert result == TargetReadResult("matched", "complete", READ_PATHS, True, True, True)
    assert (manager.page, manager.context, manager.browser, manager.playwright) == handles
    assert page.reads and result.safe_fields()["ready_for_live"] is False
    assert result.safe_fields()["strict_adapter_validated"] is False


def test_probe_refreshes_idle_navigation_before_url_check(session, monkeypatch):
    core, _, page = session
    page.url = "https://x.com/home"  # Python cache while worker queue was idle.

    def dispatch(timeout):
        assert timeout == 0
        page.protocol_waits.append(timeout)
        page.url = URL  # Pending frame-navigation event already sent by driver.

    monkeypatch.setattr(page, "wait_for_timeout", dispatch)
    assert run(core).matched
    assert page.protocol_waits == [0]


@pytest.mark.parametrize(
    "change,reason",
    [
        ("url", "target_page_mismatch"),
        ("cancel", "cancelled_or_stale"),
        ("rate", "rate_limited"),
        ("suspended", "network_blocked"),
        ("closed", "page_closed"),
        ("page", "session_changed"),
    ],
)
def test_probe_rechecks_controls_after_idle_event_dispatch(session, monkeypatch, change, reason):
    core, manager, page = session

    def dispatch(timeout):
        assert timeout == 0
        page.protocol_waits.append(timeout)
        if change == "url":
            page.url = "https://x.com/home"
        elif change == "cancel":
            core.request_cancel()
        elif change == "rate":
            core.rate_limit_status = 429
        elif change == "suspended":
            manager.network_suspended = True
        elif change == "closed":
            page.closed = True
        else:
            manager.page = object()

    monkeypatch.setattr(page, "wait_for_timeout", dispatch)
    result = run(core)
    assert result.reason == reason and result.read_paths == () and not page.reads
    assert not result.account_match and not result.matched and page.protocol_waits == [0]


def test_preflight_url_mismatch_leaves_account_unevaluated(session, monkeypatch):
    core, _, page = session
    page.url = "https://x.com/home"

    def forbidden():
        pytest.fail("login/viewer must not be evaluated before target page matches")

    monkeypatch.setattr(core, "is_logged_in", forbidden)
    monkeypatch.setattr(core, "get_logged_in_username", forbidden)
    result = run(core)
    assert result.reason == "target_page_mismatch" and result.stage == "preflight"
    assert result.read_paths == () and not result.account_match and not page.reads


@pytest.mark.parametrize("closed,reason", [(False, "read_failed"), (True, "page_closed")])
def test_idle_dispatch_failure_is_sanitized_and_does_not_retry(session, monkeypatch, closed, reason):
    core, _, page = session

    def failed(timeout):
        page.protocol_waits.append(timeout)
        page.closed = closed
        raise RuntimeError("SECRET URL viewer cookie-token")

    monkeypatch.setattr(page, "wait_for_timeout", failed)
    result = run(core)
    assert result.reason == reason and not result.read_paths and not page.reads
    assert page.protocol_waits == [0] and "SECRET" not in json.dumps(result.safe_fields())


@pytest.mark.parametrize(
    "url,viewer",
    [
        ("http://x.com/author/status/123", "viewer"),
        (URL + "?x=y", "viewer"),
        (URL + "#fragment", "viewer"),
        (URL + "/photo/1", "viewer"),
        ("https://foreign.test/author/status/123", "viewer"),
        (URL, "@viewer"),
        (URL, ""),
    ],
)
def test_invalid_request_never_touches_session(url, viewer):
    result = probe_current_target(ReadHandle(), TargetReadRequest(url, viewer), worker_thread_id=threading.get_ident())
    assert result.reason == "invalid_request" and not result.read_paths


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("rate_limit_status", 429, "rate_limited"),
        ("cancel", True, "cancelled_or_stale"),
        ("network_suspended", True, "network_blocked"),
        ("action_control_failed", True, "network_blocked"),
        ("network_resume_at", 1, "network_blocked"),
        ("post_actions_active", True, "network_blocked"),
        ("page", None, "no_session"),
        ("playwright", None, "no_session"),
    ],
)
def test_existing_controls_are_preserved_without_reads(session, field, value, reason):
    core, manager, page = session
    if field == "rate_limit_status":
        core.rate_limit_status = value
    elif field == "cancel":
        core.request_cancel()
    else:
        setattr(manager, field, value)
    assert run(core).reason == reason
    assert page.reads == []
    assert page.protocol_waits == []
    if field == "cancel":
        assert core.is_cancel_requested()
    else:
        assert getattr(core if field == "rate_limit_status" else manager, field) == value


@pytest.mark.parametrize(
    "change,reason",
    [
        ("url", "target_page_mismatch"),
        ("closed", "page_closed"),
        ("disconnected", "page_closed"),
        ("context", "session_changed"),
        ("pages", "session_changed"),
        ("browser", "session_changed"),
    ],
)
def test_wrong_target_or_unowned_session_fails_preflight(session, change, reason):
    core, manager, page = session
    if change == "url":
        page.url = "https://x.com/author/status/999"
    elif change == "closed":
        page.closed = True
    elif change == "disconnected":
        manager.browser.is_connected = lambda: False
    elif change == "context":
        page.context = object()
    elif change == "pages":
        manager.context.pages.clear()
    else:
        manager.context.browser = object()
    assert run(core).reason == reason
    assert not page.reads
    assert page.protocol_waits == ([0] if change == "url" else [])


@pytest.mark.parametrize("size,visible", [(0, True), (2, True), (1, False)])
def test_missing_quoted_only_duplicate_or_hidden_target_rejected(session, size, visible):
    core, _, page = session
    page.article.size, page.article.visible = size, visible
    assert run(core).reason == "target_unmatched"


@pytest.mark.parametrize(
    "hrefs",
    [
        [],
        ["/other/status/123"],
        ["/author/status/999"],
        ["https://foreign.test/author/status/123"],
        ["/author/status/123", "/author/status/999"],
        ["/author/status/123", "/other/status/123"],
        ["/author/status/123", "https://x.com/other/status/123"],
        ["/author/status/123", "/author/status/123?canary=private"],
    ],
)
def test_owned_timestamp_and_permalink_identity_required(session, hrefs):
    core, _, page = session
    page.article.links = Links(hrefs)
    result = run(core)
    assert result.reason == "target_unmatched" and not result.account_match


@pytest.mark.parametrize("logged_in,viewer", [(False, "viewer"), (True, "other")])
def test_login_and_viewer_both_required(session, logged_in, viewer):
    core, _, page = session
    page.login, page.viewer = logged_in, viewer
    result = run(core)
    assert result.reason == "account_unmatched" and result.target_match and result.read_paths == READ_PATHS


@pytest.mark.parametrize(
    "change,reason",
    [
        ("cancel", "cancelled_or_stale"),
        ("page", "session_changed"),
        ("driver", "session_changed"),
        ("context", "session_changed"),
        ("browser", "session_changed"),
        ("manager", "session_changed"),
        ("url", "target_page_mismatch"),
        ("rate", "rate_limited"),
        ("suspended", "network_blocked"),
        ("generation", "cancelled_or_stale"),
    ],
)
def test_change_during_helper_stops_without_cleanup(session, monkeypatch, change, reason):
    core, manager, page = session
    actual = core._get_owned_timestamp_link
    current = True

    def changed(article):
        nonlocal current
        result = actual(article)
        if change == "cancel":
            core.request_cancel()
        elif change == "manager":
            core._browser = object()
        elif change == "driver":
            manager.playwright = object()
        elif change in {"page", "context", "browser"}:
            setattr(manager, change, object())
        elif change == "url":
            page.url = URL + "?changed=1"
        elif change == "rate":
            core.rate_limit_status = 403
        elif change == "suspended":
            manager.network_suspended = True
        else:
            current = False
        return result

    monkeypatch.setattr(core, "_get_owned_timestamp_link", changed)
    result = run(core, is_current=lambda: current)
    assert result.reason == reason and result.read_paths == READ_PATHS[:2]


def test_wrong_thread_and_stale_have_no_reads(session):
    core, _, page = session
    assert probe_current_target(core, REQUEST, worker_thread_id=-1).reason == "wrong_thread"
    assert run(core, is_current=lambda: False).reason == "cancelled_or_stale"
    assert not page.reads


@pytest.mark.parametrize(
    "times,reason",
    [
        ([0, 30], "elapsed_limit"),
        ([10, 9], "clock_failed"),
        ([float("nan")], "clock_failed"),
        ([float("inf")], "clock_failed"),
    ],
)
def test_elapsed_clock_is_checked_between_reads(session, times, reason):
    core, _, _ = session
    clock = iter(times)
    assert run(core, monotonic=lambda: next(clock)).reason == reason


def test_exception_output_is_closed_and_sanitized(session, monkeypatch):
    core, _, page = session

    def fail(*args):
        raise RuntimeError("SECRET-canary https://x.com/private/status/999 cookie-token")

    monkeypatch.setattr(page, "locator", fail)
    result = run(core)
    encoded = json.dumps(result.safe_fields())
    assert result.reason == "read_failed" and not result.matched
    assert all(value not in encoded for value in ("SECRET", "private", "cookie-token", URL, "viewer"))


@pytest.mark.parametrize(
    "result",
    [
        TargetReadResult("raw-secret"),
        TargetReadResult("matched", matched=True),
        TargetReadResult("matched"),
        TargetReadResult("read_failed", read_paths=("raw-secret",)),
        TargetReadResult("read_failed", target_match=1),
    ],
)
def test_untrusted_result_fields_rejected(result):
    with pytest.raises(ValueError, match="invalid_result"):
        result.safe_fields()
