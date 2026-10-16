"""Closed HTTP evidence categories; a bare 403 never proves authentication failure."""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urlparse

AUTH_CODES = frozenset({32, 89, 99, 135, 215, 220})
ACCOUNT_CODES = frozenset({326})
KNOWN_CODES = AUTH_CODES | ACCOUNT_CODES
HTTP_CATEGORIES = frozenset({"http401", "auth_code", "account_code", "unknown403", "target403",
                             "mutation403", "ancillary403", "unreadable_envelope", "invalid_envelope", "other_error_code"})
HTTP_PROVENANCE = frozenset({"http_status", "error_code", "unavailable"})
HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})


class _BodyReader(Protocol):
    def body(self) -> bytes: ...


@dataclass(frozen=True)
class HttpFailure:
    status: int
    role: str
    endpoint: str
    category: str
    provenance: str = "http_status"
    codes: tuple[int, ...] = ()
    response_id: str | None = None

    @property
    def auth_confirmed(self) -> bool:
        return self.category in {"http401", "auth_code", "account_code"}

    def fields(self) -> dict[str, object]:
        return {"http_status": self.status, "http_role": self.role, "http_endpoint": self.endpoint,
                "http_category": self.category, "http_provenance": self.provenance,
                "known_error_codes": list(self.codes), "http_response": self.response_id}


def _role(response: object) -> tuple[str, str]:
    try:
        url = getattr(response, "url", None)
        request = getattr(response, "request", None)
        if not isinstance(url, str) or getattr(request, "url", None) != url:
            return "unknown", "unclassified"
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.username is not None:
            return "unknown", "unclassified"
        method = getattr(request, "method", None)
        graph = re.fullmatch(r"/i/api/graphql/[A-Za-z0-9_-]+/([A-Za-z0-9_]+)", parsed.path.rstrip("/"))
        if graph:
            operation = graph.group(1)
            if method == "POST" and operation in {"DeleteTweet", "DeleteRetweet", "UnfavoriteTweet"}:
                return "mutation", operation
            if method == "GET" and operation in {"TweetDetail", "TweetResultByRestId", "TweetResultsByRestIds"}:
                return "target", operation
            if method == "GET" and operation in {"ViewerBadgeCounts", "CreatorStudioTabBarItemQuery"}:
                return "ancillary", operation
        if method == "POST" and parsed.path.rstrip("/") == "/i/api/1.1/friendships/destroy.json":
            return "mutation", "destroy.json"
        if method == "GET" and parsed.path.rstrip("/") == "/i/api/fleets/v1/fleetline":
            return "ancillary", "fleetline"
        if method == "GET" and getattr(request, "resource_type", None) == "document" and re.fullmatch(r"/[A-Za-z0-9_]+/status/[0-9]+", parsed.path):
            return "target", "target-document"
    except Exception:
        pass
    return "unknown", "unclassified"


def http_failure(response: object) -> HttpFailure | None:
    """Never retain a body/message; inspect only bounded, declared API JSON errors."""
    failure = None
    try:
        status = getattr(response, "status", None)
        if type(status) is not int or status not in {401, 403}:
            return None
        role, endpoint = _role(response)
        if status == 401:
            return HttpFailure(status, role, endpoint, "http401")
        category = {"target": "target403", "mutation": "mutation403", "ancillary": "ancillary403"}.get(role, "unknown403")
        failure = HttpFailure(status, role, endpoint, category)
        url = getattr(response, "url", None)
        if not isinstance(url, str):
            return failure
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.username is not None or not parsed.path.startswith("/i/api/"):
            return failure
        headers = getattr(response, "headers", None)
        if not isinstance(headers, Mapping):
            return failure
        length, content_type = headers.get("content-length"), headers.get("content-type")
        if not isinstance(length, str) or not re.fullmatch(r"[0-9]{1,4}", length) or int(length) > 8192:
            return failure
        if not isinstance(content_type, str) or content_type.split(";", 1)[0].strip().lower() != "application/json":
            return failure
        request = getattr(response, "request", None)
        if getattr(request, "failure", None) is not None:
            return failure
        timing = getattr(request, "timing", None)
        end = timing.get("responseEnd") if isinstance(timing, Mapping) else None
        if not isinstance(end, (int, float)) or isinstance(end, bool) or not math.isfinite(end) or end < 0:
            # response events can precede body completion; never wait for a body
            # merely to enrich diagnostics after a stop has already been installed.
            return failure
        try:
            payload = cast(_BodyReader, response).body()
        except Exception:
            return HttpFailure(status, role, endpoint, "unreadable_envelope", "unavailable")
        if not isinstance(payload, bytes) or len(payload) > 8192:
            return HttpFailure(status, role, endpoint, "invalid_envelope", "unavailable")
        try:
            body = json.loads(payload)
        except (ValueError, RecursionError):
            return HttpFailure(status, role, endpoint, "invalid_envelope", "unavailable")
        errors = body.get("errors") if isinstance(body, dict) else None
        if not isinstance(errors, list) or not 1 <= len(errors) <= 8 or not all(isinstance(item, dict) and type(item.get("code")) is int for item in errors):
            return HttpFailure(status, role, endpoint, "invalid_envelope", "unavailable")
        codes = {item["code"] for item in errors}
        if not codes <= KNOWN_CODES:
            return HttpFailure(status, role, endpoint, "other_error_code", "unavailable")
        return HttpFailure(status, role, endpoint, "account_code" if codes & ACCOUNT_CODES else "auth_code",
                           "error_code", tuple(sorted(codes)))
    except Exception:
        # Metadata/body failure cannot relax the existing stop gate.
        return failure
