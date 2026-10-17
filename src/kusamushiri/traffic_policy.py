"""Name-based post-action traffic allowlist; report records decisions, enforce aborts.

Admission uses public GraphQL operation names, resource types and host classes,
never per-build query hashes or chunk lists, so a new X frontend build keeps working
until it renames or adds a required dependency. A blocked dependency stops the
batch with the item held; it never fabricates a response or a success.
"""

import re
from collections import OrderedDict
from collections.abc import Callable
from typing import Literal
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Request, Route

from kusamushiri.logger import logger

TrafficMode = Literal["off", "report", "enforce"]
TRAFFIC_MODES = ("off", "report", "enforce")
HostClass = Literal["x_web", "x_api", "x_subdomain", "twimg", "other"]
HOST_CLASSES = frozenset({"x_web", "x_api", "x_subdomain", "twimg", "other"})
TrafficRule = Literal["local", "target_document", "static_asset", "target_read", "armed_mutation",
                      "unarmed_mutation", "unlisted"]
TRAFFIC_RULES = frozenset({"local", "target_document", "static_asset", "target_read", "armed_mutation",
                           "unarmed_mutation", "unlisted"})
TrafficDecision = Literal["allow", "block"]
TRAFFIC_DECISIONS = frozenset({"allow", "block"})

TARGET_READS = frozenset({"TweetDetail", "TweetResultByRestId"})
POST_MUTATIONS = frozenset({"DeleteTweet", "DeleteRetweet", "UnfavoriteTweet"})
STATIC_RESOURCES = frozenset({"script", "stylesheet", "font"})
# The action code sets these phases immediately before its single confirmed click.
ARMED_PHASES = frozenset({"mutation_submit", "mutation_wait"})
_WEB_HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})
_API_HOSTS = frozenset({"api.x.com", "api.twitter.com"})
_GRAPH_PATH = re.compile(r"/i/api/graphql/[A-Za-z0-9_-]+/([A-Za-z][A-Za-z0-9_]{0,79})")
_TARGET_PATH = re.compile(r"/[A-Za-z0-9_]{1,50}/status/[0-9]{1,25}")
MAX_REMEMBERED_DECISIONS = 512


def host_class(host: str | None) -> HostClass:
    host = (host or "").lower()
    if host in _WEB_HOSTS:
        return "x_web"
    if host in _API_HOSTS:
        return "x_api"
    if host.endswith((".x.com", ".twitter.com")):
        return "x_subdomain"
    if host == "twimg.com" or host.endswith(".twimg.com"):
        return "twimg"
    return "other"


def decide(url: str, method: str, resource_type: str, *, armed: bool) -> tuple[HostClass, TrafficDecision, TrafficRule]:
    """Pure admission rule; anything not positively listed is blocked."""
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return "other", "block", "unlisted"
    if parsed.scheme in {"data", "blob", "about"}:
        return "other", "allow", "local"
    hosts = host_class(parsed.hostname)
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        return hosts, "block", "unlisted"
    path = parsed.path.rstrip("/")
    if hosts == "x_web" and method == "GET" and resource_type == "document" and _TARGET_PATH.fullmatch(path):
        return hosts, "allow", "target_document"
    if hosts in {"x_web", "twimg"} and method == "GET" and resource_type in STATIC_RESOURCES and not path.startswith("/i/api/"):
        return hosts, "allow", "static_asset"
    graph = _GRAPH_PATH.fullmatch(path) if hosts == "x_web" and resource_type in {"xhr", "fetch"} else None
    if graph:
        if method == "GET" and graph.group(1) in TARGET_READS:
            return hosts, "allow", "target_read"
        if method == "POST" and graph.group(1) in POST_MUTATIONS:
            return (hosts, "allow", "armed_mutation") if armed else (hosts, "block", "unarmed_mutation")
    return hosts, "block", "unlisted"


class TrafficPolicy:
    """Active only inside a post-action scope; startup and sign-in checks are untouched."""

    def __init__(self, diagnostics, on_failure: Callable[[], None]) -> None:
        self.diagnostics = diagnostics
        self.on_failure = on_failure
        self.mode: TrafficMode = "off"
        self.ready = False
        self.active = False
        self.failed = False
        self._operation: object = None
        self.blocked = 0
        self.mutations = 0
        # Holding the identity makes a recycled id() a miss, never another request's decision.
        self._decisions: OrderedDict[int, tuple[object, TrafficDecision]] = OrderedDict()

    @property
    def enforcing(self) -> bool:
        return self.mode == "enforce" and self.active and self.ready and not self.failed

    def install(self, context: BrowserContext, *, service_workers_blocked: bool) -> None:
        if self.mode == "report":
            self.ready = True
            return
        if self.mode != "enforce" or not service_workers_blocked:
            return
        try:
            context.route("**/*", self._route)
            # Restored tabs could have dispatched before routing; start every action from one clean page.
            for page in list(context.pages)[1:]:
                page.close()
            if context.pages:
                context.pages[0].goto("about:blank")
        except Exception:
            logger.warning("Traffic allowlist unavailable; post actions will not start.")
            self.failed = True
            return
        self.ready = True

    def fail(self) -> None:
        if not self.failed:
            self.failed = True
            logger.warning("Traffic allowlist route control failed; stopping post actions.")
        self.on_failure()

    def begin_operation(self, item: object) -> None:
        if item != self._operation:
            self._operation = item
            self.blocked = 0
            self.mutations = 0

    def _decision(self, request: object) -> TrafficDecision | None:
        identity = getattr(request, "_impl_obj", request)
        stored = self._decisions.get(id(identity))
        return stored[1] if stored is not None and stored[0] is identity else None

    def observe(self, request: Request) -> None:
        """Record one decision per request; the route handler reuses it."""
        if self.mode == "off" or not self.active or self._decision(request) is not None:
            return
        try:
            url, method, resource = request.url, request.method, request.resource_type
            armed = self.diagnostics.current_phase in ARMED_PHASES and self.mutations == 0
            hosts, decision, rule = decide(url, method, resource, armed=armed)
        except Exception:
            return
        if rule == "armed_mutation":
            self.mutations += 1
        if decision == "block":
            self.blocked += 1
        identity = getattr(request, "_impl_obj", request)
        self._decisions[id(identity)] = (identity, decision)
        while len(self._decisions) > MAX_REMEMBERED_DECISIONS:
            self._decisions.popitem(last=False)
        self.diagnostics.traffic_decision(request, hosts, decision, rule, enforced=self.enforcing)

    def _route(self, route: Route) -> None:
        if not self.active:
            route.fallback()
            return
        request = route.request
        decision = self._decision(request)
        if decision is None and not self.failed:
            # No observed decision (e.g. a request event not yet delivered): never admit blindly.
            self.observe(request)
            decision = self._decision(request)
        try:
            if decision == "allow" and not self.failed:
                route.fallback()
            else:
                route.abort("blockedbyclient")
        except Exception:
            self.fail()
