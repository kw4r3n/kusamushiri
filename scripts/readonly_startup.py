"""Unauthenticated synthetic loopback startup only; no X/auth or mutation arm."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import ParseResult, parse_qsl, urlencode, urlparse

from playwright.sync_api import Browser, BrowserContext, Page

from kusamushiri.endpoint_identity import public_identity


def _canonical_loopback(url: object) -> ParseResult | None:
    if not isinstance(url, str) or not 1 <= len(url) <= 8192 or not url.isascii():
        return None
    if "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlparse(url)
        if (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port is not None
                and 1 <= parsed.port <= 65535 and parsed.netloc == f"127.0.0.1:{parsed.port}"
                and not parsed.params and not parsed.fragment and parsed.geturl() == url):
            return parsed
    except (ValueError, TypeError):
        pass
    return None


def _read_binding(parsed: ParseResult, target_id: str) -> bool:
    try:
        return (parsed.path == "/read" and parsed.query == urlencode({"target": target_id})
                and parse_qsl(parsed.query, strict_parsing=True) == [("target", target_id)])
    except ValueError:
        return False


# Runs before page scripts in every new frame. No worker/RTC transport is allowed.
TRANSPORT_GUARD = """(() => {
  for (const key of ['WebTransport','RTCPeerConnection','webkitRTCPeerConnection',
                     'Worker','SharedWorker','EventSource']) {
    Object.defineProperty(globalThis, key, {value: undefined, writable: false, configurable: false});
  }
})()"""


class PageLike(Protocol):
    main_frame: Any
    def goto(self, url: str, *, timeout: int, wait_until: str) -> Any: ...
    def wait_for_timeout(self, milliseconds: int) -> None: ...
    def close(self) -> None: ...


class ContextLike(Protocol):
    pages: list[PageLike]
    def route(self, matcher: str, handler: Callable[..., None]) -> None: ...
    def route_web_socket(self, matcher: str, handler: Callable[..., None]) -> None: ...
    def add_init_script(self, script: str) -> None: ...
    def on(self, event: str, handler: Callable[..., None]) -> None: ...
    def new_page(self) -> PageLike: ...
    def close(self) -> None: ...
    def set_offline(self, value: bool) -> None: ...


class ContextFactoryLike(Protocol):
    def new_context(self, **kwargs: Any) -> ContextLike: ...


@dataclass(frozen=True)
class LoopbackReadOnlyPlan:
    target_url: str
    # All members are GET on one exact loopback origin. Bind reads from URL APIs.
    gets: frozenset[tuple[str, str]]
    experimental_opt_in: bool = False
    scope: str = "unresolved"
    seconds: float = 10.0
    max_requests: int = 100

    def valid(self) -> bool:
        target = _canonical_loopback(self.target_url)
        if (self.experimental_opt_in is not True or self.scope != "loopback-fixture" or target is None
                or re.fullmatch(r"/target/[0-9]+", target.path) is None or target.query
                or type(self.seconds) not in {int, float} or not math.isfinite(self.seconds) or not 0 < self.seconds <= 10
                or type(self.gets) is not frozenset or not 1 <= len(self.gets) <= 100
                or type(self.max_requests) is not int or not 1 <= self.max_requests <= 100
                or (self.target_url, "document") not in self.gets):
            return False
        target_id = target.path.rsplit("/", 1)[-1]
        for member in self.gets:
            if type(member) is not tuple or len(member) != 2 or not all(isinstance(item, str) for item in member):
                return False
            url, resource = member
            parsed = _canonical_loopback(url)
            if parsed is None or parsed.netloc != target.netloc:
                return False
            if url == self.target_url and resource == "document":
                continue
            if (not parsed.query and (parsed.path, resource) in {
                ("/bootstrap.js", "script"), ("/fixture.css", "stylesheet"), ("/fixture.woff2", "font"),
            }):
                continue
            if resource in {"xhr", "fetch"} and _read_binding(parsed, target_id):
                continue
            if parsed.path == "/redirect" and not parsed.query and resource == "fetch":
                continue
            return False
        return True


@dataclass(frozen=True)
class ReadOnlyResult:
    stop_reason: str
    attempts: int
    fetched: int
    cleanup_complete: bool
    events: tuple[Mapping[str, object], ...]


class ReadOnlyStartupAdapter:
    def __init__(self, plan: LoopbackReadOnlyPlan, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.plan = plan
        self.clock = monotonic
        self.context: BrowserContext | ContextLike | None = None
        self.page: Page | PageLike | None = None
        self.creating_page = False
        self.navigating = False
        self.stop_reason: str | None = None
        self.attempts = 0
        self.fetched = 0
        self.deadline = 0.0
        self.seen: set[tuple[str, str]] = set()
        self.events: list[dict[str, object]] = []
        self.used = False

    def _record(self, event: str, *, request: Any = None, status: int | None = None) -> None:
        if len(self.events) >= 220:
            return
        fields: dict[str, object] = {"event": event}
        if request is not None:
            method, resource = request.method, request.resource_type
            fields["method"] = method if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "OTHER"
            fields["resource"] = resource if resource in {"document", "script", "stylesheet", "font", "xhr", "fetch"} else "other"
            fields.update(public_identity(request.url, method))
        if type(status) is int and 100 <= status <= 599:
            fields["status"] = status
        self.events.append(fields)

    def _stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
            self._record("stopped")

    def _deny(self, route: Any, reason: str) -> None:
        self._stop(reason)
        try:
            route.abort("blockedbyclient")
        except Exception:
            self._record("abort_failed")
            # Retain all routing blocks while quarantining the owned context.
            try:
                if self.context is not None:
                    self.context.set_offline(True)
            except Exception:
                self._record("offline_control_failed")

    def _mutation_block(self, route: Any) -> None:
        try:
            request = route.request
            if self.stop_reason is not None:
                self._deny(route, "stopped")
                return
            self.attempts += 1
            if self.clock() >= self.deadline or self.attempts > self.plan.max_requests:
                self._deny(route, "time_or_request_limit")
                return
            self._record("request_attempt", request=request)
            parsed = _canonical_loopback(request.url)
            if request.method != "GET" or (parsed is not None and parsed.path == "/write"):
                self._deny(route, "mutation_or_non_get_blocked")
                return
            route.fallback()  # Never override method, headers, URL, or body.
        except Exception:
            self._deny(route, "mutation_control_failed")

    def _get_policy(self, route: Any) -> None:
        response = None
        try:
            request = route.request
            key = (request.url, request.resource_type)
            # A popup navigation can precede frame creation. Reject disallowed
            # request identities before accessing the optional lifecycle metadata.
            if (self.stop_reason is not None or self.clock() >= self.deadline or request.method != "GET"
                    or _canonical_loopback(request.url) is None or key not in self.plan.gets or key in self.seen
                    or self.page is None or request.redirected_from is not None):
                self._deny(route, "unknown_or_unowned_request")
                return
            frame = request.frame
            if frame.page is not self.page or frame is not self.page.main_frame:
                self._deny(route, "unknown_or_unowned_request")
                return
            if request.resource_type == "document":
                if not self.navigating or request.url != self.plan.target_url:
                    self._deny(route, "unplanned_navigation")
                    return
            elif frame.url != self.plan.target_url or (self.plan.target_url, "document") not in self.seen:
                self._deny(route, "non_target_document")
                return
            parsed = _canonical_loopback(request.url)
            if request.post_data is not None or (parsed is not None and parsed.path == "/read"
                    and not _read_binding(parsed, self.plan.target_url.rsplit("/", 1)[-1])):
                self._deny(route, "target_binding_unestablished")
                return
            self.seen.add(key)
            remaining = max(1, int((self.deadline - self.clock()) * 1000))
            # continue_ can follow redirects without routing the next hop. Fetch
            # only this exact GET with zero redirects/retries, then relay its actual
            # response without changing status/body or fabricating acknowledgement.
            response = route.fetch(url=request.url, method="GET", max_redirects=0, max_retries=0, timeout=remaining)
            self.fetched += 1
            status = response.status
            self._record("get_response", request=request, status=status)
            if (self.stop_reason is not None or self.clock() >= self.deadline or response.url != request.url
                    or type(status) is not int or not 200 <= status < 300):
                self._deny(route, "http_or_redirect_or_time_block")
                return
            route.fulfill(response=response)
        except Exception:
            self._deny(route, "get_control_failed")
        finally:
            if response is not None:
                try:
                    response.dispose()
                except Exception:
                    self._stop("response_disposal_failed")

    def _websocket(self, route: Any) -> None:
        self._stop("unsupported_transport")
        try:
            route.close(code=1008, reason="read-only observation")  # Never connect_to_server.
        except Exception:
            self._record("websocket_control_failed")

    def _page_opened(self, page: Page | PageLike) -> None:
        if self.creating_page and self.page is None:
            self.page = page
            return
        # Lifecycle callbacks can run reentrantly while route/navigation RPCs are
        # pending. Latch refusal and return; close every owned page via context
        # cleanup after callbacks unwind, never wait on page.close() here.
        self._stop("unexpected_page")

    def _worker(self, worker: Any) -> None:
        self._stop("unexpected_worker")

    def run(self, browser: Browser | ContextFactoryLike, *, probe: Callable[[Any], None] | None = None,
            progress: Callable[[str], None] | None = None) -> ReadOnlyResult:
        if self.used:
            raise RuntimeError("Read-only observation cannot restart or retry.")
        self.used = True
        if type(self.plan) is not LoopbackReadOnlyPlan or not self.plan.valid():
            self._stop("unresolved_or_invalid_read_only_plan")
            return ReadOnlyResult(self.stop_reason or "invalid", 0, 0, True, tuple(self.events))
        def checkpoint(stage: str) -> None:
            if progress is not None:
                try:
                    progress(stage)
                except Exception:
                    self._record("progress_write_failed")

        cleanup = False
        creation_attempted = False
        self.deadline = self.clock() + self.plan.seconds
        try:
            creation_attempted = True
            checkpoint("context_create")
            context = browser.new_context(service_workers="block", accept_downloads=False, java_script_enabled=True)
            self.context = context
            if context.pages:
                self._stop("nonempty_context")
            else:
                # Playwright routes are LIFO. The independent blocker is installed
                # last so it runs first; GET policy also independently refuses POST.
                checkpoint("routing_setup")
                context.route("**/*", self._get_policy)
                context.route("**/*", self._mutation_block)
                context.route_web_socket("**/*", self._websocket)
                context.add_init_script(TRANSPORT_GUARD)
                context.on("serviceworker", self._worker)
                context.on("page", self._page_opened)
                self.creating_page = True
                checkpoint("page_create")
                page = context.new_page()
                self.creating_page = False
                if self.page is not None and self.page is not page:
                    self._stop("unexpected_page")
                self.page = page
                if self.stop_reason is None and self.clock() < self.deadline:
                    self.navigating = True
                    checkpoint("navigation")
                    page.goto(self.plan.target_url, timeout=max(1, int((self.deadline - self.clock()) * 1000)), wait_until="commit")
                    self.navigating = False
                checkpoint("observe")
                while self.stop_reason is None and self.clock() < self.deadline:
                    page.wait_for_timeout(max(1, min(100, int((self.deadline - self.clock()) * 1000))))
                    if probe is not None:
                        probe(page)  # Runner's fixed read-only local DOM checks only.
                self._stop("time_limit")
        except Exception:
            self._stop("startup_or_observation_failed")
        finally:
            self._stop("observation_closed")
            self.creating_page = self.navigating = False
            if self.context is not None:
                try:
                    # Do not unroute first: that creates a window of unrestricted
                    # dispatch. Closing only our new context releases its resources.
                    checkpoint("context_close")
                    self.context.close()
                    cleanup = True
                except Exception:
                    self._record("context_close_failed")
                    try:
                        self.context.set_offline(True)
                    except Exception:
                        self._record("offline_control_failed")
                    try:
                        if self.page is not None:
                            self.page.close()
                    except Exception:
                        self._record("page_close_failed")
                if cleanup:
                    self.context = self.page = None
            else:
                # A failed creation call may have allocated remotely. No handle
                # is NOT proof of absence. Never discover/close borrowed contexts.
                cleanup = not creation_attempted
        checkpoint("adapter_complete")
        return ReadOnlyResult(self.stop_reason or "closed", self.attempts, self.fetched, cleanup, tuple(self.events))
