"""Bounded, exact public identity projection; never retain a raw URL."""

import re
from collections.abc import Mapping
from urllib.parse import urlparse

from kusamushiri.public_endpoint_catalog import FIXED_ROUTES, PUBLIC_OPERATIONS

GRAPHQL_TEMPLATE = "/i/api/graphql/{query_id}/{operation}"
ROUTE_TEMPLATES = frozenset(path for _, path in FIXED_ROUTES)
_API_HOSTS = frozenset({"api.x.com", "api.twitter.com"})
_WEB_HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})
_GRAPH_PATH = re.compile(r"/i/api/graphql/[A-Za-z0-9_-]{1,128}/([A-Za-z][A-Za-z0-9_]{0,79})")


def public_identity(url: object, method: object) -> dict[str, object]:
    """Emit only an exact catalog symbol/template, with no truncation or decoding."""
    unavailable: dict[str, object] = {"identity_status": "unavailable"}
    if not isinstance(url, str) or not 1 <= len(url) <= 8192 or not url.isascii():
        return unavailable
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        return unavailable
    if not isinstance(method, str) or method not in {"GET", "POST"}:
        return unavailable
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if parsed.scheme != "https" or parsed.params or host not in _WEB_HOSTS | _API_HOSTS:
            return unavailable
        if parsed.netloc.lower() not in {host, host + ":443"}:
            return unavailable
        graph = _GRAPH_PATH.fullmatch(parsed.path) if host in _WEB_HOSTS else None
        if graph and graph.group(1) in PUBLIC_OPERATIONS:
            return {"identity_status": "public_operation", "public_operation": graph.group(1),
                    "route_template": GRAPHQL_TEMPLATE}
        if (method, parsed.path) in FIXED_ROUTES and (
            (host in _API_HOSTS and parsed.path.startswith("/1.1/"))
            or (host in _WEB_HOSTS and parsed.path.startswith("/i/api/"))
        ):
            return {"identity_status": "fixed_route", "route_template": parsed.path}
    except (ValueError, UnicodeError):
        pass
    return unavailable


def sanitize_identity(value: Mapping[str, object]) -> dict[str, object]:
    """Revalidate exported identity fields; membership alone never grants an exemption."""
    status, operation, route = value.get("identity_status"), value.get("public_operation"), value.get("route_template")
    method = value.get("method")
    if not isinstance(method, str) or method not in {"GET", "POST"}:
        return {"identity_status": "unavailable"}
    if status == "public_operation" and isinstance(operation, str) and operation in PUBLIC_OPERATIONS and route == GRAPHQL_TEMPLATE:
        return {"identity_status": status, "public_operation": operation, "route_template": GRAPHQL_TEMPLATE}
    if status == "fixed_route" and operation is None and isinstance(route, str) and route in ROUTE_TEMPLATES and (method, route) in FIXED_ROUTES:
        return {"identity_status": status, "route_template": route}
    return {"identity_status": "unavailable"}
