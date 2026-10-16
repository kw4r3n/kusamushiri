"""Opt-in endpoint quarantine; never retries or fabricates a server response."""

import re
import time
from collections import OrderedDict
from collections.abc import Callable
from urllib.parse import parse_qsl, urlparse

from playwright.sync_api import BrowserContext, Response, Route

from kusamushiri.logger import logger
from kusamushiri.network_diagnostics import NetworkDiagnostics
from kusamushiri.retry import parse_retry_after

MAX_SUPPRESSED_REQUESTS = 128


def isolatable_endpoint(url: str, method: str) -> str | None:
    """Exact observed API paths only, not the broader diagnostic role labels."""
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return None
    if parsed.scheme != "https" or method != "GET":
        return None
    if parsed.netloc in {"api.x.com", "api.twitter.com"}:
        # Boot-time settings read; repeats on every target load.
        return "account/settings.json" if parsed.path == "/1.1/account/settings.json" else None
    if parsed.netloc not in {"x.com", "twitter.com"}:
        return None
    if parsed.path == "/i/api/fleets/v1/fleetline" and parse_qsl(parsed.query, keep_blank_values=True) == [("only_spaces", "true")]:
        return "fleetline"
    graph = re.fullmatch(r"/i/api/graphql/[A-Za-z0-9_-]+/(ViewerBadgeCounts|CreatorStudioTabBarItemQuery)", parsed.path)
    if graph:
        return graph.group(1)
    return None


def _identity(response: Response) -> object:
    return getattr(response, "_impl_obj", response)


class AncillaryIsolation:
    def __init__(self, diagnostics: NetworkDiagnostics, on_failure: Callable[[Response, float], None]) -> None:
        self.diagnostics = diagnostics
        self.on_failure = on_failure
        self.ready = False
        self.active = False
        self.failed = False
        self.deadlines: dict[str, float] = {}
        self.suppressed = 0
        self._operation_item: object = None
        self._responses: OrderedDict[int, object] = OrderedDict()
        self._last_response: Response | None = None
        self.persist_deadline: Callable[[float], None] | None = None

    @property
    def deadline(self) -> float:
        return max(self.deadlines.values(), default=0.0)

    def install(self, context: BrowserContext, *, service_workers_blocked: bool) -> None:
        if not service_workers_blocked:
            return
        try:
            context.route(lambda url: isolatable_endpoint(url, "GET") is not None, self._route)
        except Exception:
            logger.warning("Ancillary isolation unavailable; retaining global rate-limit handling.")
            return
        self.ready = True

    def begin_operation(self, item: object) -> None:
        # The storm cap is per item: polling across a long batch must not exhaust it,
        # while retries of the same item keep accumulating toward the cap.
        if item != self._operation_item:
            self._operation_item = item
            self.suppressed = 0

    def owns(self, response: Response) -> bool:
        identity = _identity(response)
        return self.active and not self.failed and self._responses.get(id(identity)) is identity

    def quarantine(self, response: Response, fallback_seconds: float) -> bool:
        if not (self.active and self.ready and not self.failed):
            return False
        if self.owns(response):
            return True
        try:
            endpoint = isolatable_endpoint(response.url, response.request.method)
            if endpoint is None or response.status not in {420, 429}:
                return False
            after = parse_retry_after(response.headers.get("retry-after"))
        except Exception:
            return False
        # No expiry-triggered retry: this endpoint stays quarantined until context
        # close. Persist the latest server deadline before closing/restarting.
        deadline = time.time() + max(fallback_seconds, after or 0.0)
        self.deadlines[endpoint] = max(self.deadlines.get(endpoint, 0.0), deadline)
        self._last_response = response
        try:
            if self.persist_deadline is None:
                raise RuntimeError("Durable deadline storage unavailable")
            self.persist_deadline(self.deadline)
        except Exception:
            logger.warning("Ancillary deadline could not be persisted; stopping conservatively.")
            self.fail()
            return False
        identity = _identity(response)
        self._responses[id(identity)] = identity
        while len(self._responses) > 256:
            self._responses.popitem(last=False)
        self.diagnostics.isolation_event("ancillary_quarantine", endpoint, self.deadlines[endpoint], after, response=response)
        logger.warning("Ancillary endpoint quarantined: %s; deadline %.3f; other action evidence remains required.",
                       endpoint, self.deadlines[endpoint])
        return True

    def fail(self) -> None:
        if self.failed:
            return
        self.failed = True
        self.diagnostics.isolation_event("isolation_fallback", "unclassified", self.deadline, response=self._last_response)
        logger.warning("Ancillary isolation control or required view is unavailable; stopping with the deadline retained.")
        if self._last_response is not None:
            self.on_failure(self._last_response, self.deadline)

    def _route(self, route: Route) -> None:
        endpoint = isolatable_endpoint(route.request.url, route.request.method)
        if endpoint is None or endpoint not in self.deadlines:
            route.fallback()
            return
        self.suppressed += 1
        self.diagnostics.isolation_event("request_suppressed", endpoint, self.deadlines[endpoint])
        try:
            route.abort("blockedbyclient")
        except Exception:
            self.fail()
            return  # Never continue a request whose retry deadline is protected.
        if self.suppressed >= MAX_SUPPRESSED_REQUESTS:
            self.fail()
