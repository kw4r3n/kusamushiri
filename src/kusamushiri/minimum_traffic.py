"""Offline-only minimum-traffic state-machine prototype; no browser/API integration.

This module is deliberately unused by production commands. It accepts synthetic
request/response evidence only, never credentials, headers or Playwright objects.
A real dependency capsule is unavailable: bootstrap must fail closed until verified.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import ParseResult, urlparse

TARGET_READ_CANDIDATES = frozenset({"TweetDetail", "TweetResultByRestId"})
_GRAPH = re.compile(r"/i/api/graphql/[A-Za-z0-9_-]{1,128}/(TweetDetail|TweetResultByRestId|DeleteTweet)")


@dataclass(frozen=True)
class OfflineDependencyCapsule:
    # Synthetic fixtures declare their full static closure explicitly. No host
    # wildcard, catalog-wide API membership, or unknown dynamic chunk is trusted.
    assets: frozenset[tuple[str, str]] = frozenset()
    target_reads: frozenset[str] = TARGET_READ_CANDIDATES
    bootstrap_complete: bool = False
    scope: str = "unresolved"
    # Exact method, canonical full URL (including persisted-query hash), resource.
    # Empty by default: an operation name alone never grants dispatch.
    api_requests: frozenset[tuple[str, str, str]] = frozenset()


@dataclass(frozen=True)
class SyntheticRequest:
    url: str
    method: str
    resource: str
    target_id: str | None = None
    # A fixture-level binding, NOT a verified production GraphQL variables parser.
    owner: str = "owned-page"
    frame: str = "main"
    redirected: bool = False


@dataclass
class OfflineRoute:
    request: SyntheticRequest
    fail_abort: bool = False
    aborted: bool = False
    dispatched: bool = False

    def abort(self) -> None:
        if self.fail_abort:
            raise RuntimeError("synthetic abort failure")
        self.aborted = True


@dataclass(frozen=True)
class SyntheticReceipt:
    request: SyntheticRequest
    status: int
    # Determined by the existing action-response evaluator in a future adapter.
    semantic_outcome: str = "accepted"
    dom_complete: bool = True


def _canonical(url: object) -> ParseResult | None:
    if not isinstance(url, str) or not 1 <= len(url) <= 8192 or not url.isascii():
        return None
    if "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlparse(url)
        if (parsed.scheme == "https" and not parsed.params and not parsed.fragment and not parsed.query
                and parsed.geturl() == url):
            return parsed
    except ValueError:
        pass
    return None


@dataclass
class MinimumTrafficPrototype:
    target_url: str
    capsule: OfflineDependencyCapsule
    experimental_opt_in: bool = False
    service_workers_blocked: bool = False
    routing_ready: bool = False
    state: str = field(init=False, default="unstarted")
    stop_reason: str | None = field(init=False, default=None)
    document_count: int = field(init=False, default=0)
    read_counts: dict[str, int] = field(init=False, default_factory=dict)
    asset_counts: dict[tuple[str, str], int] = field(init=False, default_factory=dict)
    mutation_request: SyntheticRequest | None = field(init=False, default=None)
    confirmed: bool = field(init=False, default=False)
    sent: bool = field(init=False, default=False)
    server_status: int | None = field(init=False, default=None)
    retry_after_seconds: float | None = field(init=False, default=None)
    events: list[str] = field(init=False, default_factory=list)

    def start(self) -> bool:
        if self.state != "unstarted":
            return False
        if not self.experimental_opt_in:
            return self.stop("opt-in-required")
        parsed = _canonical(self.target_url)
        if (parsed is None or parsed.netloc != "x.com"
                or re.fullmatch(r"/[A-Za-z0-9_]{1,15}/status/[0-9]+", parsed.path) is None or parsed.query):
            return self.stop("invalid-target")
        if not self.service_workers_blocked or not self.routing_ready:
            return self.stop("routing-guarantee-missing")
        if self.capsule.scope != "offline-fixture" or not self.capsule.bootstrap_complete or not self.capsule.assets:
            return self.stop("bootstrap-dependencies-unresolved")
        if not self.capsule.target_reads or not self.capsule.target_reads <= TARGET_READ_CANDIDATES:
            return self.stop("target-read-dependencies-unresolved")
        for url, resource in self.capsule.assets:
            parsed_asset = _canonical(url)
            if (parsed_asset is None or parsed_asset.netloc != "abs.twimg.com" or parsed_asset.query
                    or resource not in {"script", "stylesheet", "font"}
                    or not parsed_asset.path.startswith("/responsive-web/")):
                return self.stop("invalid-static-capsule")
        if not self.capsule.api_requests:
            return self.stop("api-identities-unresolved")
        listed_reads: set[str] = set()
        listed_mutation = False
        for method, url, resource in self.capsule.api_requests:
            api = _canonical(url)
            graph = _GRAPH.fullmatch(api.path) if api is not None and api.netloc == "x.com" else None
            if graph is None or resource not in {"xhr", "fetch"}:
                return self.stop("invalid-api-capsule")
            operation = graph.group(1)
            if operation in self.capsule.target_reads and method == "GET":
                listed_reads.add(operation)
            elif operation == "DeleteTweet" and method == "POST":
                listed_mutation = True
            else:
                return self.stop("invalid-api-capsule")
        if listed_reads != self.capsule.target_reads or not listed_mutation:
            return self.stop("api-identities-unresolved")
        self.state = "loading"
        return True

    @property
    def target_id(self) -> str:
        return self.target_url.rsplit("/", 1)[-1]

    def stop(self, reason: str) -> bool:
        if self.stop_reason is None:
            self.stop_reason = reason
        self.state = "stopped"
        self.events.append("stopped")
        return False

    def route(self, route: OfflineRoute) -> bool:
        # Runtime Playwright route objects cannot enter this offline prototype.
        if type(route) is not OfflineRoute or type(route.request) is not SyntheticRequest:
            raise TypeError("Offline synthetic routes only")
        request = route.request
        allowed = self._allow(request)
        if allowed:
            route.dispatched = True  # fixture accounting only; no network method
            self.events.append("fixture-dispatch")
            return True
        try:
            route.abort()
        except Exception:
            self.stop("abort-control-failed")
            return False
        self.stop("unestablished-or-excess-traffic")
        return False

    def _allow(self, request: SyntheticRequest) -> bool:
        if self.state not in {"loading", "verified", "armed"} or self.stop_reason:
            return False
        if request.owner != "owned-page" or request.frame != "main" or request.redirected:
            return False
        parsed = _canonical(request.url)
        if parsed is None:
            return False
        asset = (request.url, request.resource)
        if request.method == "GET" and asset in self.capsule.assets:
            count = self.asset_counts.get(asset, 0)
            if count >= 1:
                return False
            self.asset_counts[asset] = count + 1
            return True
        if request.url == self.target_url and request.method == "GET" and request.resource == "document":
            if self.state != "loading" or self.document_count:
                return False
            self.document_count += 1
            return True
        if ((request.method, request.url, request.resource) not in self.capsule.api_requests
                or parsed.netloc != "x.com" or request.target_id != self.target_id):
            return False
        graph = _GRAPH.fullmatch(parsed.path)
        if graph is None:
            return False
        operation = graph.group(1)
        if operation in self.capsule.target_reads and request.method == "GET" and self.state == "loading":
            count = self.read_counts.get(operation, 0)
            if count >= 1:
                return False
            self.read_counts[operation] = count + 1
            return True
        if operation == "DeleteTweet" and request.method == "POST" and self.state == "armed" and not self.sent:
            self.mutation_request = request
            self.sent = True
            self.state = "submitted"
            return True
        return False

    def verify_view(self, *, logged_in: bool, owned_target_article: bool, correct_target_id: bool,
                    delete_menu: bool, confirmation: bool) -> bool:
        if self.state != "loading" or not self.document_count:
            return self.stop("required-view-unavailable")
        # One rendered view can establish all these predicates without separate
        # network calls; no invented identity/auth API is added to the capsule.
        if not all((logged_in, owned_target_article, correct_target_id, delete_menu, confirmation)):
            return self.stop("required-view-unavailable")
        self.state = "verified"
        return True

    def arm_delete(self) -> bool:
        if self.state != "verified":
            return self.stop("mutation-not-authorized-by-view")
        self.state = "armed"
        return True

    def observe_http(self, *, status: int, retry_after_seconds: float | None = None) -> None:
        if status in {401, 403, 420, 429}:
            self.server_status = status
            if retry_after_seconds is not None:
                self.retry_after_seconds = max(self.retry_after_seconds or 0.0, retry_after_seconds)
            self.stop("auth-or-http-block" if status in {401, 403} else "server-rate-limit")

    def finish(self, receipt: SyntheticReceipt | None) -> str:
        # The original owned request is required. A late/cross-operation/clone
        # wrapper alone, DOM disappearance, or synthetic success is insufficient.
        if receipt is not None and type(receipt) is not SyntheticReceipt:
            raise TypeError("Offline synthetic receipts only")
        if self.confirmed:
            # Completion is monotonic. New faults still stop future work, but do
            # not rewrite the independently established mutation acknowledgement.
            if receipt is not None and type(receipt.status) is int and receipt.status in {401, 403, 420, 429}:
                self.observe_http(status=receipt.status)
            elif (receipt is None or receipt.request is not self.mutation_request
                  or type(receipt.status) is not int or not 200 <= receipt.status < 300
                  or receipt.semantic_outcome != "accepted" or not receipt.dom_complete):
                self.stop("post-completion-observation-fault")
            return "success"
        if not self.sent:
            self.stop("unsent")
            return "unsent"
        if receipt is None or receipt.request is not self.mutation_request:
            self.stop("uncertain-outcome-no-replay")
            return "ambiguous"
        if type(receipt.status) is not int:
            self.stop("uncertain-outcome-no-replay")
            return "ambiguous"
        if receipt.status in {401, 403, 420, 429}:
            self.observe_http(status=receipt.status)
            return "blocked" if receipt.status in {401, 403} else "rate-refused"
        if 200 <= receipt.status < 300 and receipt.semantic_outcome == "explicit-refusal":
            self.stop("explicit-refusal-no-prototype-retry")
            return "refused"
        if (200 <= receipt.status < 300 and receipt.semantic_outcome == "accepted" and receipt.dom_complete):
            self.confirmed = True
            self.state = "stopped" if self.stop_reason else "complete"
            return "success"
        self.stop("uncertain-outcome-no-replay")
        return "ambiguous"
