"""Shared non-resuming ownership/control/soft-time checks for borrowed GUI pages."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any


class TargetReadStopError(RuntimeError):
    pass


class ActionNotSentError(RuntimeError):
    """A pre-send check failed, so the outcome is known: nothing was sent for the target."""


class TargetNotSentError(TargetReadStopError, ActionNotSentError):
    """Single-post verification stopped before sending; callers still see a TargetReadStopError."""


class ActiveTargetSession:
    def __init__(
        self,
        core: Any,
        url: str,
        worker_thread_id: int | None,
        is_current: Callable[[], bool],
        monotonic: Callable[[], float] = time.monotonic,
        *,
        _action_owner: object | None = None,
    ):
        self.core, self.url = core, url
        self.worker_thread_id, self.is_current, self.monotonic = worker_thread_id, is_current, monotonic
        self._action_owner = _action_owner
        self.manager = core._browser
        self.page, self.context = self.manager.page, self.manager.context
        self.browser, self.driver = self.manager.browser, self.manager.playwright
        self.start: float | None = None
        self.last: float | None = None

    def now(self) -> float:
        now = self.monotonic()
        if type(now) not in {int, float} or not math.isfinite(now) or self.last is not None and now < self.last:
            raise TargetReadStopError("clock_failed")
        self.start = now if self.start is None else self.start
        self.last = now
        if now - self.start >= 30:
            raise TargetReadStopError("elapsed_limit")
        return now

    def check(self, *, target_required: bool = True) -> float:
        if threading.get_ident() != self.worker_thread_id:
            raise TargetReadStopError("wrong_thread")
        now = self.now()
        if self.core.rate_limit_status is not None:
            raise TargetReadStopError("rate_limited")
        if not self.is_current() or self.core.is_cancel_requested():
            raise TargetReadStopError("cancelled_or_stale")
        manager, page, context, browser, driver = self.manager, self.page, self.context, self.browser, self.driver
        if manager is None or page is None or context is None or browser is None or driver is None:
            raise TargetReadStopError("no_session")
        if (
            manager.network_suspended
            or manager.action_control_failed
            or manager.network_resume_at > 0
            or manager.post_actions_active
            and not (
                self._action_owner is not None
                and getattr(self.core, "_verified_post_action", None) is self._action_owner
            )
        ):
            raise TargetReadStopError("network_blocked")
        if (
            self.core._browser is not manager
            or manager.page is not page
            or manager.context is not context
            or manager.browser is not browser
            or manager.playwright is not driver
            or page.context is not context
            or context.browser is not browser
            or not any(owned is page for owned in context.pages)
        ):
            raise TargetReadStopError("session_changed")
        if page.is_closed() or not browser.is_connected():
            raise TargetReadStopError("page_closed")
        if target_required and page.url != self.url:
            raise TargetReadStopError("target_page_mismatch")
        return now

    def hold(self) -> None:
        self.manager.action_control_failed = True
        self.core.request_cancel()
