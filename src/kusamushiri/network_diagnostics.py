"""Observation only: bounded request correlation and closed-schema sanitized logs.

No request is issued and no payload/body is retained. Declared small API403 JSON
errors may be read for closed numeric categories, never messages or body output.
No rate-limit decision is changed.
An operation association means an event occurred during that operation, not causation.
"""

import json
import math
import re
import time
import uuid
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from kusamushiri.endpoint_identity import public_identity, sanitize_identity
from kusamushiri.http_failures import HTTP_CATEGORIES, HTTP_PROVENANCE, KNOWN_CODES, HttpFailure, http_failure
from kusamushiri.logger import logger
from kusamushiri.models import PostActionResult, PostActionTarget
from kusamushiri.parsing import extract_post_id
from kusamushiri.rate_limits import ANCILLARY_READS, MUTATIONS, TARGET_READS, classify_rate_limit
from kusamushiri.request_metadata import (
    API_LABELS,
    REQUEST_FAMILIES,
    RESOURCE_TYPES,
    operation_digest,
    request_metadata,
    runtime_source_fingerprint,
)
from kusamushiri.retry import parse_retry_after

PREFIX = "KS_DIAG "
PHASES = {
    "idle",
    "browser_start",
    "home_load",
    "operation_start",
    "target_load",
    "menu",
    "confirmation",
    "submission_blocked",
    "mutation_submit",
    "mutation_wait",
    "outcome",
    "network_pause",
    "network_resume",
    "unattributed",
}
EVENTS = {
    "trace_start",
    "operation_start",
    "phase",
    "navigation",
    "request_start",
    "response",
    "request_failed",
    "operation_outcome",
    "operation_end",
    "rate_signal_clear",
    "ancillary_quarantine",
    "request_suppressed",
    "isolation_fallback",
    "view_reused",
    "batch_summary",
    "verification_stop",
}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "OTHER"}
ENDPOINTS = MUTATIONS | TARGET_READS | ANCILLARY_READS | {"target-document", "unclassified"}
ROLES = {"mutation", "target", "ancillary", "unknown"}
OUTCOMES = {"success", "deferred", "auth", "ambiguous", "permanent", "retryable", "unknown", "blocked"}


def _number(value: object) -> float | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            return float(value)
    except (OverflowError, ValueError):
        pass
    return None


def _identifier(value: object, prefix: str) -> bool:
    return isinstance(value, str) and re.fullmatch(prefix + r"[0-9]{1,9}", value) is not None


def sanitize_event(value: object) -> dict[str, object] | None:
    """Project onto a closed schema; never echo arbitrary log strings or fields."""
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value.get("schema") != 1:
        return None
    trace = value.get("trace")
    event = value.get("event")
    if (
        not isinstance(trace, str)
        or not re.fullmatch(r"[0-9a-f]{12}", trace)
        or not isinstance(event, str)
        or event not in EVENTS
    ):
        return None
    clean: dict[str, object] = {"schema": 1, "trace": trace, "event": event}
    id_prefixes = {
        "request": "q",
        "response": "r",
        "operation": "op",
        "item": "item",
        "navigation": "nav",
        "origin_operation": "op",
        "origin_item": "item",
        "origin_navigation": "nav",
        "request_event_operation": "op",
        "request_event_navigation": "nav",
        "endpoint_group": "g",
        "http_response": "r",
    }
    for key, prefix in id_prefixes.items():
        item = value.get(key)
        if key in value and (item is None or _identifier(item, prefix)):
            clean[key] = item
    enum_fields = {
        "verification_stage": {"caret", "delete_menu", "confirm"},
        "verification_reason": {"wrong_control_stage", "identity_unestablished", "control_changed", "overlay_present",
                                "menu_count", "control_count", "menu_changed", "dialog_ownership_changed",
                                "control_reused", "read_failed"},
        "phase": PHASES,
        "origin_phase": PHASES,
        "request_event_phase": PHASES,
        "method": METHODS,
        "endpoint": ENDPOINTS,
        "role": ROLES,
        "outcome": OUTCOMES,
        "action": {"delete", "undo_repost", "unlike", "unknown"},
        "attribution": {"request_event", "start_predates_operation", "start_predates_navigation", "unmatched"},
        "resource_type": RESOURCE_TYPES,
        "request_family": REQUEST_FAMILIES,
        "api_label": API_LABELS,
        "stop_reason": {"completed", "cancelled", "cooldown_limit", "wait_limit", "retry_limit", "auth", "ambiguous", "permanent", "unexpected_error", "storage_error", "view_changed", "blocked", "held_receipt"},
        "http_role": ROLES,
        "http_endpoint": ENDPOINTS,
        "http_category": HTTP_CATEGORIES,
        "http_provenance": HTTP_PROVENANCE,
    }
    for key, allowed in enum_fields.items():
        if isinstance(value.get(key), str) and value[key] in allowed:
            clean[key] = value[key]
    for key in ("utc", "request_event_utc", "reported_start_utc", "retry_until_utc"):
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00", item):
            clean[key] = item
    for key in (
        "seq",
        "attempt",
        "origin_attempt",
        "elapsed_ms",
        "since_request_event_ms",
        "retry_after_seconds",
        "unattributed_mutation_requests",
        "total", "successes", "skipped", "failed", "unprocessed", "deferrals", "retries", "cooldowns", "waited_seconds", "held_receipts",
    ):
        if key in value:
            number = _number(value[key])
            if number is not None:
                clean[key] = number
            elif value[key] is None:
                clean[key] = None
    if isinstance(value.get("status"), int) and not isinstance(value["status"], bool) and 100 <= value["status"] <= 599:
        clean["status"] = value["status"]
    if type(value.get("http_status")) is int and value["http_status"] in {200, 401, 403}:
        clean["http_status"] = value["http_status"]
    codes = value.get("known_error_codes")
    if isinstance(codes, list):
        clean["known_error_codes"] = sorted({code for code in codes[:8] if type(code) is int and code in KNOWN_CODES})
    for key in ("cross_operation", "cross_navigation", "submitted", "graphql_code88", "faulted", "outcome_recorded", "view_reuse_invalid"):
        if isinstance(value.get(key), bool):
            clean[key] = value[key]
    for key, prefix in (
        ("owned_mutation_requests", "q"),
        ("other_mutation_requests", "q"),
        ("blocking_responses", "r"),
    ):
        items = value.get(key)
        if isinstance(items, list):
            clean[key] = [item for item in items[:100] if _identifier(item, prefix)]
    fingerprint = value.get("package_source_sha256")
    if isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        clean["package_source_sha256"] = fingerprint
    op_digest = value.get("operation_sha256")
    if isinstance(op_digest, str) and re.fullmatch(r"[0-9a-f]{64}", op_digest):
        clean["operation_sha256"] = op_digest
    if event in {"request_start", "response", "request_failed", "ancillary_quarantine", "request_suppressed", "isolation_fallback"}:
        clean.update(sanitize_identity(value))
    return clean


@dataclass(frozen=True)
class _Context:
    operation: str | None = None
    item: str | None = None
    attempt: int | None = None
    navigation: str | None = None
    phase: str = "idle"
    operation_ms: float | None = None
    navigation_ms: float | None = None


@dataclass
class _Request:
    identity: object
    local_id: str
    origin: _Context
    observed: _Context
    observed_utc: str
    monotonic: float
    method: str
    role: str
    endpoint: str
    attribution: str
    reported_utc: str | None = None
    metadata: dict[str, object] | None = None


class NetworkDiagnostics:
    def __init__(
        self,
        *,
        sink: Callable[[dict[str, object]], None] | None = None,
        wall: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
        capacity: int = 2048,
    ) -> None:
        self.trace = uuid.uuid4().hex[:12]
        self._sink = sink or (lambda event: logger.info("%s%s", PREFIX, json.dumps(event, separators=(",", ":"))))
        self._wall, self._monotonic = wall or time.time, monotonic or time.monotonic
        self._start = self._monotonic()
        self._capacity = max(1, capacity)
        self._context = _Context()
        self._requests: OrderedDict[int, _Request] = OrderedDict()
        self._responses: OrderedDict[int, tuple[object, str, HttpFailure | None]] = OrderedDict()
        self._items: OrderedDict[tuple[str, str], tuple[str, int]] = OrderedDict()
        self._seq = self._request_seq = self._response_seq = self._operation_seq = self._item_seq = self._nav_seq = 0
        self._blocking: list[str] = []
        self._outcomes: set[str] = set()
        self._groups: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self._group_seq = 0
        self._last_navigation: tuple[str, float] | None = None
        self._fingerprint = runtime_source_fingerprint()
        self._emit("trace_start", package_source_sha256=self._fingerprint)

    def _metadata(self, url: object, method: str, resource: object) -> dict[str, object]:
        resource_type, family, label, route = request_metadata(url, resource)
        group = None
        if route is not None:
            key = (method, *route)
            group = self._groups.get(key)
            if group is None:
                self._group_seq += 1
                group = f"g{self._group_seq}"
                self._groups[key] = group
                if len(self._groups) > self._capacity:
                    self._groups.popitem(last=False)
            else:
                self._groups.move_to_end(key)
        return {"endpoint_group": group, "resource_type": resource_type, "request_family": family, "api_label": label,
                "operation_sha256": operation_digest(url), **public_identity(url, method)}

    @staticmethod
    def _identity(value: object) -> object:
        return getattr(value, "_impl_obj", value)

    @staticmethod
    def _endpoint(url: object) -> tuple[str, str] | None:
        if not isinstance(url, str):
            return None
        host = (urlparse(url).hostname or "").lower()
        if not any(host == name or host.endswith("." + name) for name in ("x.com", "twitter.com")):
            return None
        return classify_rate_limit(url)

    def _utc(self, seconds: float | None = None) -> str:
        return datetime.fromtimestamp(self._wall() if seconds is None else seconds, UTC).isoformat(
            timespec="milliseconds"
        )

    def _emit(self, event: str, **fields: object) -> None:
        # Logging must never alter submission, cancellation or rate-limit handling.
        try:
            self._seq += 1
            context = self._context
            record = sanitize_event(
                {
                    "schema": 1,
                    "trace": self.trace,
                    "event": event,
                    "seq": self._seq,
                    "utc": self._utc(),
                    "elapsed_ms": round((self._monotonic() - self._start) * 1000, 3),
                    "operation": context.operation,
                    "item": context.item,
                    "attempt": context.attempt,
                    "navigation": context.navigation,
                    "phase": context.phase,
                    **fields,
                }
            )
            if record is not None:
                self._sink(record)
        except Exception:
            pass

    def phase(self, phase: str) -> None:
        if phase in PHASES:
            self._context = replace(self._context, phase=phase)
            self._emit("phase")

    def verification_stop(self, stage: str, reason: str) -> None:
        """Closed control metadata only; no DOM text, URLs or exception strings."""
        self._emit("verification_stop", verification_stage=stage, verification_reason=reason)

    def navigation(self) -> None:
        try:
            self._nav_seq += 1
            self._context = replace(
                self._context, navigation=f"nav{self._nav_seq}", navigation_ms=self._wall() * 1000, phase="target_load"
            )
            self._last_navigation = (f"nav{self._nav_seq}", self._wall() * 1000)
            self._emit("navigation")
        except Exception:
            pass

    def view_reused(self) -> None:
        if self._last_navigation is not None:
            navigation, started = self._last_navigation
            self._context = replace(self._context, navigation=navigation, navigation_ms=started, phase="target_load")
            self._emit("view_reused")

    def batch_summary(self, **fields: object) -> None:
        self._emit("batch_summary", **fields)

    def describe_response(self, response: object) -> dict[str, object]:
        try:
            request = getattr(response, "request", None)
            record = self._lookup(request) if request is not None else None
            return dict(record.metadata or {}) if record else {}
        except Exception:
            return {}

    @contextmanager
    def operation(self, target: PostActionTarget) -> Iterator[None]:
        previous = self._context
        operation: str | None = None
        try:
            key = (target.kind, extract_post_id(target.url))
            known = self._items.get(key)
            if known is None:
                self._item_seq += 1
                known = (f"item{self._item_seq}", 0)
            item, attempt = known[0], known[1] + 1
            self._items[key] = (item, attempt)
            self._items.move_to_end(key)
            if len(self._items) > self._capacity:
                self._items.popitem(last=False)
            self._operation_seq += 1
            operation = f"op{self._operation_seq}"
            self._context = _Context(
                operation, item, attempt, phase="operation_start", operation_ms=self._wall() * 1000
            )
            self._emit(
                "operation_start",
                action={"post": "delete", "repost": "undo_repost", "like": "unlike"}.get(target.kind, "unknown"),
            )
        except Exception:
            self._context = previous
        faulted = False
        try:
            yield
        except BaseException:
            faulted = True
            raise
        finally:
            if operation is not None:
                self._emit("operation_end", faulted=faulted, outcome_recorded=operation in self._outcomes)
                self._outcomes.discard(operation)
            self._context = previous

    def _lookup(self, request: object) -> _Request | None:
        identity = self._identity(request)
        record = self._requests.get(id(identity))
        return record if record is not None and record.identity is identity else None

    def request(self, request: object) -> None:
        try:
            if self._lookup(request) is not None:
                return
            endpoint = self._endpoint(getattr(request, "url", None))
            if endpoint is None:
                return
            identity = self._identity(request)
            self._request_seq += 1
            observed = self._context
            origin = observed
            attribution = "request_event"
            reported = None
            timing = getattr(request, "timing", None)
            start_ms = _number(timing.get("startTime")) if isinstance(timing, dict) else None
            if start_ms is not None and start_ms > 0:
                reported = self._utc(start_ms / 1000)
                if observed.operation_ms is not None and start_ms < observed.operation_ms:
                    origin, attribution = _Context(phase="unattributed"), "start_predates_operation"
                elif observed.navigation_ms is not None and start_ms < observed.navigation_ms:
                    origin = replace(observed, navigation=None, phase="unattributed")
                    attribution = "start_predates_navigation"
            method = getattr(request, "method", None)
            method = method.upper() if isinstance(method, str) and method.upper() in METHODS else "OTHER"
            record = _Request(
                identity,
                f"q{self._request_seq}",
                origin,
                observed,
                self._utc(),
                self._monotonic(),
                method,
                endpoint[0],
                endpoint[1],
                attribution,
                reported,
                self._metadata(getattr(request, "url", None), method, getattr(request, "resource_type", None)),
            )
            self._requests[id(identity)] = record
            if len(self._requests) > self._capacity:
                self._requests.popitem(last=False)
            self._emit("request_start", **self._request_fields(record))
        except Exception:
            pass

    @staticmethod
    def _request_fields(record: _Request) -> dict[str, object]:
        return {
            **(record.metadata or {}),
            "request": record.local_id,
            "method": record.method,
            "role": record.role,
            "endpoint": record.endpoint,
            "origin_operation": record.origin.operation,
            "origin_item": record.origin.item,
            "origin_attempt": record.origin.attempt,
            "origin_navigation": record.origin.navigation,
            "origin_phase": record.origin.phase,
            "request_event_operation": record.observed.operation,
            "request_event_navigation": record.observed.navigation,
            "request_event_phase": record.observed.phase,
            "request_event_utc": record.observed_utc,
            "reported_start_utc": record.reported_utc,
            "attribution": record.attribution,
        }

    def response(self, response: object) -> None:
        try:
            identity = self._identity(response)
            existing = self._responses.get(id(identity))
            if existing is not None and existing[0] is identity:
                return
            endpoint = self._endpoint(getattr(response, "url", None))
            if endpoint is None:
                return
            self._response_seq += 1
            local_id = f"r{self._response_seq}"
            self._responses[id(identity)] = (identity, local_id, None)
            if len(self._responses) > self._capacity:
                self._responses.popitem(last=False)
            request = getattr(response, "request", None)
            record = self._lookup(request) if request is not None else None
            fields: dict[str, object] = (
                self._request_fields(record)
                if record
                else {
                    "request": None,
                    "origin_operation": None,
                    "origin_navigation": None,
                    "origin_phase": "unattributed",
                    "attribution": "unmatched",
                    "role": endpoint[0],
                    "endpoint": endpoint[1],
                }
            )
            if record is None:
                method = getattr(request, "method", None)
                method = method.upper() if isinstance(method, str) and method.upper() in METHODS else "OTHER"
                fields["method"] = method
                fields.update(self._metadata(getattr(response, "url", None), method, getattr(request, "resource_type", None)))
            headers = getattr(response, "headers", None)
            delay = (
                parse_retry_after(headers.get("retry-after"), now=self._wall())
                if isinstance(headers, Mapping)
                else None
            )
            status = getattr(response, "status", None)
            if status in (420, 429):
                self._blocking.append(local_id)
                self._blocking = self._blocking[-100:]
            fields.update(
                {
                    "response": local_id,
                    "status": status,
                    "retry_after_seconds": delay,
                    "since_request_event_ms": round((self._monotonic() - record.monotonic) * 1000, 3)
                    if record
                    else None,
                    "cross_operation": record.origin.operation != self._context.operation
                    if record and record.origin.operation is not None
                    else None,
                    "cross_navigation": record.origin.navigation != self._context.navigation
                    if record and record.origin.navigation is not None
                    else None,
                }
            )
            failure = http_failure(response)
            if failure is not None:
                failure = replace(failure, response_id=local_id)
            self._responses[id(identity)] = (identity, local_id, failure)
            if len(self._responses) > self._capacity:
                self._responses.popitem(last=False)
            if failure is not None:
                fields.update(failure.fields())
            self._emit("response", **fields)
        except Exception:
            pass

    def http_failure_for(self, response: object) -> HttpFailure | None:
        """Reuse the response's closed evidence; never retain a decoded body."""
        identity = self._identity(response)
        recorded = self._responses.get(id(identity))
        if recorded is not None and recorded[0] is identity:
            if recorded[2] is not None and recorded[2].category in {"unknown403", "target403", "mutation403", "ancillary403"}:
                # Recheck only metadata readiness; the parser will not fetch an
                # unfinished body. An outcome may enrich the earlier header event.
                failure = http_failure(response)
                if failure is not None and failure.category != recorded[2].category:
                    self._responses[id(identity)] = (identity, recorded[1], replace(failure, response_id=recorded[1]))
                    return self._responses[id(identity)][2]
            return recorded[2]
        return http_failure(response)

    def request_failed(self, request: object) -> None:
        try:
            record = self._lookup(request)
            endpoint = self._endpoint(getattr(request, "url", None))
            if endpoint is None:
                return
            fields = (
                self._request_fields(record)
                if record
                else {
                    "request": None,
                    "origin_operation": None,
                    "origin_phase": "unattributed",
                    "attribution": "unmatched",
                    "role": endpoint[0],
                    "endpoint": endpoint[1],
                }
            )
            self._emit("request_failed", **fields)
        except Exception:
            pass

    def clear_rate_signal(self) -> None:
        self._emit("rate_signal_clear", blocking_responses=self._blocking)
        self._blocking = []

    def isolation_event(self, event: str, endpoint: str, deadline: float, after: float | None = None,
                        *, response: object | None = None) -> None:
        try:
            fields: dict[str, object] = {"endpoint": endpoint, "role": "ancillary", "retry_after_seconds": after}
            with suppress(OverflowError, OSError, ValueError):
                fields["retry_until_utc"] = datetime.fromtimestamp(deadline, UTC).isoformat(timespec="milliseconds")
            if response is not None:
                self.response(response)
                identity = self._identity(response)
                recorded = self._responses.get(id(identity))
                if recorded is not None and recorded[0] is identity:
                    fields["response"] = recorded[1]
                    if event == "ancillary_quarantine":
                        self._blocking = [item for item in self._blocking if item != recorded[1]]
                    elif event == "isolation_fallback":
                        self._blocking = (self._blocking + [recorded[1]])[-100:]
                request = getattr(response, "request", None)
                record = self._lookup(request) if request is not None else None
                if record is not None:
                    fields.update(self._request_fields(record))
            self._emit(event, **fields)
        except Exception:
            pass

    def outcome(
        self, result: PostActionResult, requests: Sequence[object], *, submitted: bool, graphql_code88: bool = False
    ) -> None:
        try:
            owned, other = [], []
            unattributed = 0
            for request in requests:
                record = self._lookup(request)
                if record is None or record.origin.operation is None:
                    unattributed += 1
                elif record.origin.operation == self._context.operation:
                    owned.append(record.local_id)
                else:
                    other.append(record.local_id)
            self.phase("outcome")
            outcome = (
                "success" if result.success else "deferred" if result.deferred else result.failure_kind or "unknown"
            )
            self._emit(
                "operation_outcome",
                outcome=outcome,
                submitted=submitted,
                owned_mutation_requests=owned,
                other_mutation_requests=other,
                unattributed_mutation_requests=unattributed,
                graphql_code88=graphql_code88,
                blocking_responses=self._blocking,
                view_reuse_invalid=result.view_reuse_invalid,
                **(result.http_failure.fields() if result.http_failure is not None else {}),
            )
            if self._context.operation is not None:
                self._outcomes.add(self._context.operation)
        except Exception:
            pass


def diagnostic_excerpt(path: Path, *, trace: str | None = None, max_events: int | None = 300) -> list[dict[str, object]]:
    """Read current and three rotated logs, emitting validated JSON only."""
    records: list[dict[str, object]] = []
    pattern = re.compile(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - kusamushiri - INFO - \[[^\]]+\] KS_DIAG (\{.*\})$"
    )
    for candidate in [path.with_name(path.name + f".{index}") for index in (3, 2, 1)] + [path]:
        if not candidate.is_file():
            continue
        with candidate.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if len(line) > 16384:
                    continue
                match = pattern.fullmatch(line.rstrip("\r\n"))
                if not match:
                    continue
                try:
                    event = sanitize_event(json.loads(match.group(1)))
                except (ValueError, TypeError):
                    continue
                if event is not None:
                    records.append(event)
    if not records:
        return []
    selected = trace or str(records[-1]["trace"])
    selected_records = [event for event in records if event["trace"] == selected]
    return selected_records if max_events is None else selected_records[-max_events:]


def diagnostic_summary(records: Sequence[dict[str, object]]) -> dict[str, object]:
    records = [clean for record in records if (clean := sanitize_event(record)) is not None]
    responses = [event for event in records if event.get("event") == "response"]
    limits = [event for event in responses if event.get("status") in (420, 429)]
    group_counts: Counter[tuple[object, ...]] = Counter()
    http_counts: Counter[tuple[object, ...]] = Counter()
    for event in responses:
        if event.get("status") in (401, 403):
            error_codes = event.get("known_error_codes")
            known_codes = tuple(error_codes) if isinstance(error_codes, list) else ()
            http_counts[(event.get("endpoint_group"), event.get("method"), event.get("request_family"),
                         event.get("api_label"), event.get("operation_sha256"), event.get("http_role"),
                         event.get("http_endpoint"), event.get("status"), event.get("http_category"),
                         event.get("http_provenance"), known_codes, event.get("identity_status"),
                         event.get("public_operation"), event.get("route_template"))] += 1
    for event in limits:
        group_counts[(event.get("endpoint_group"), event.get("method"), event.get("resource_type"),
                      event.get("request_family"), event.get("api_label"), event.get("role"), event.get("endpoint"),
                      event.get("status"), event.get("operation_sha256"), event.get("identity_status"),
                      event.get("public_operation"), event.get("route_template"))] += 1
    return {
        "trace": records[-1].get("trace") if records else None,
        "available_events": len(records), "complete_trace_start_available": any(event.get("event") == "trace_start" for event in records),
        "package_source_sha256": next((event["package_source_sha256"] for event in records if "package_source_sha256" in event), None),
        "request_events": sum(event.get("event") == "request_start" for event in records),
        "response_events": len(responses), "navigation_events": sum(event.get("event") == "navigation" for event in records),
        "reused_views": sum(event.get("event") == "view_reused" for event in records),
        "outcomes": dict(Counter(str(event.get("outcome")) for event in records if event.get("event") == "operation_outcome")),
        "rate_limits": [{"endpoint_group": key[0], "method": key[1], "resource_type": key[2], "request_family": key[3],
                         "api_label": key[4], "role": key[5], "endpoint": key[6], "status": key[7],
                         "operation_sha256": key[8], "identity_status": key[9], "public_operation": key[10],
                         "route_template": key[11], "responses": count}
                        for key, count in group_counts.items()],
        "batch_summary": next((event for event in reversed(records) if event.get("event") == "batch_summary"), None),
        "http_errors": [{"endpoint_group": key[0], "method": key[1], "request_family": key[2], "api_label": key[3],
                         "operation_sha256": key[4], "http_role": key[5], "http_endpoint": key[6], "status": key[7],
                         "http_category": key[8], "http_provenance": key[9], "known_error_codes": list(codes) if isinstance(codes := key[10], tuple) else [],
                         "identity_status": key[11], "public_operation": key[12], "route_template": key[13],
                         "responses": count} for key, count in http_counts.items()],
        "action_http_evidence": [event for event in records if event.get("event") == "operation_outcome" and "http_category" in event],
    }
