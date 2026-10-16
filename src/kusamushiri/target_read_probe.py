"""Inspect one already displayed target through the GUI's owned live session.

No navigation, lifecycle, authentication-state API, action or settings writes.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from kusamushiri.actions import OWNED_TIMESTAMP_LINK_SELECTOR, _target_article
from kusamushiri.active_target_session import ActiveTargetSession, TargetReadStopError

READ_PATHS = ("actions_target_article", "core_owned_timestamp", "core_permalink", "browser_login", "browser_account")
REASONS = frozenset(
    {
        "matched",
        "invalid_request",
        "no_session",
        "wrong_thread",
        "cancelled_or_stale",
        "rate_limited",
        "network_blocked",
        "session_changed",
        "page_closed",
        "target_page_mismatch",
        "target_unmatched",
        "account_unmatched",
        "elapsed_limit",
        "clock_failed",
        "read_failed",
    }
)
STAGES = frozenset({"preflight", "article", "timestamp", "permalink", "login", "viewer", "complete"})


@dataclass(frozen=True, slots=True)
class TargetReadRequest:
    url: str
    expected_viewer: str

    def validate(self) -> None:
        if (
            type(self.url) is not str
            or not re.fullmatch(r"https://x\.com/[A-Za-z0-9_]{1,15}/status/[0-9]{1,25}", self.url)
            or type(self.expected_viewer) is not str
            or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", self.expected_viewer)
        ):
            raise ValueError("invalid_request")


@dataclass(frozen=True, slots=True)
class TargetReadResult:
    reason: str
    stage: str = "preflight"
    read_paths: tuple[str, ...] = ()
    target_match: bool = False
    account_match: bool = False
    matched: bool = False

    def safe_fields(self) -> dict:
        if (
            self.reason not in REASONS
            or self.stage not in STAGES
            or self.read_paths != READ_PATHS[: len(self.read_paths)]
            or len(self.read_paths) > len(READ_PATHS)
            or any(type(x) is not bool for x in (self.target_match, self.account_match, self.matched))
            or (self.reason == "matched") != self.matched
            or self.matched
            and (
                self.reason != "matched"
                or self.stage != "complete"
                or self.read_paths != READ_PATHS
                or not self.target_match
                or not self.account_match
            )
        ):
            raise ValueError("invalid_result")
        return dict(
            reason=self.reason,
            stage=self.stage,
            read_paths=list(self.read_paths),
            target_match=self.target_match,
            account_match=self.account_match,
            matched=self.matched,
            strict_adapter_validated=False,
            ready_for_live=False,
        )


def probe_current_target(
    core: Any,
    request: TargetReadRequest,
    *,
    worker_thread_id: int | None,
    is_current: Callable[[], bool] = lambda: True,
    monotonic: Callable[[], float] = time.monotonic,
    _session: ActiveTargetSession | None = None,
) -> TargetReadResult:
    stage, called = "preflight", []
    target_match = account_match = False
    page = None
    session: ActiveTargetSession | None = None

    def check(*, target_required: bool = True) -> None:
        assert session is not None
        session.check(target_required=target_required)

    try:
        if type(request) is not TargetReadRequest:
            return TargetReadResult("invalid_request")
        try:
            request.validate()
        except ValueError:
            return TargetReadResult("invalid_request")
        # No lazy _require_page: it can resume suspended networking.
        if _session is not None and (
            type(_session) is not ActiveTargetSession or _session.core is not core
            or _session.url != request.url or _session.worker_thread_id != worker_thread_id
        ):
            return TargetReadResult("session_changed")
        session = _session or ActiveTargetSession(core, request.url, worker_thread_id, is_current, monotonic)
        page = session.page
        check(target_required=False)
        # The sync worker blocks on its command queue while the user navigates.
        # Page.url/is_closed are cached until the driver dispatches pending events.
        # Use the same zero-time protocol yield as BrowserManager.start; no goto,
        # polling, auth API or network resume. Recheck every guard afterwards.
        try:
            page.wait_for_timeout(0)
        except Exception:
            check(target_required=False)
            raise
        check()
        stage = "article"
        article = _target_article(page, request.url)
        called.append(READ_PATHS[0])
        check()
        if article.count() != 1 or not article.is_visible():
            raise TargetReadStopError("target_unmatched")
        check()
        stage = "timestamp"
        timestamp = core._get_owned_timestamp_link(article)
        called.append(READ_PATHS[1])
        check()
        stage = "permalink"
        permalink = core._get_article_permalink(article)
        called.append(READ_PATHS[2])
        check()
        expected_path = urlsplit(request.url).path.casefold()

        def same_target(value: Any) -> bool:
            if type(value) is not str:
                return False
            parsed = urlsplit(value)
            return (
                parsed.scheme == "https"
                and parsed.netloc == "x.com"
                and parsed.path.casefold() == expected_path
                and not parsed.query
                and not parsed.fragment
            )

        target_match = timestamp is not None and same_target(timestamp[1]) and same_target(permalink)
        if not target_match:
            raise TargetReadStopError("target_unmatched")
        # Production helpers collapse links by post ID. Also require every owned
        # timestamp to agree on author/host/path; a conflicting same-ID author
        # must not be hidden by the first visible permalink. Reuse its selector.
        links = article.locator(OWNED_TIMESTAMP_LINK_SELECTOR)
        check()
        count = links.count()
        check()
        target_match = count > 0
        for index in range(count):
            check()
            href = links.nth(index).get_attribute("href")
            check()
            if type(href) is not str or not same_target(urljoin("https://x.com/", href)):
                target_match = False
                break
        if not target_match:
            raise TargetReadStopError("target_unmatched")
        stage = "login"
        logged_in = core.is_logged_in()
        called.append(READ_PATHS[3])
        check()
        stage = "viewer"
        viewer = core.get_logged_in_username()
        called.append(READ_PATHS[4])
        check()
        account_match = (
            logged_in is True and type(viewer) is str and viewer.casefold() == request.expected_viewer.casefold()
        )
        if not account_match:
            raise TargetReadStopError("account_unmatched")
        stage = "complete"
        check()
        return TargetReadResult("matched", stage, tuple(called), target_match, account_match, True)
    except TargetReadStopError as error:
        reason = str(error) if str(error) in REASONS else "read_failed"
    except Exception:
        reason = "read_failed"  # never echo an SDK exception/URL/form value
    return TargetReadResult(reason, stage, tuple(called), target_match, account_match)
