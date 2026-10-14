"""Closed diagnostic labels; never infer quota independence or grant exemptions."""

import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

RESOURCE_TYPES = frozenset({"document", "stylesheet", "image", "media", "font", "script", "texttrack",
                            "xhr", "fetch", "eventsource", "websocket", "manifest", "other"})
REQUEST_FAMILIES = frozenset({"graphql", "rest", "document", "asset", "other"})
# Matching these literal public operation labels is observation only. Unknown
# operations stay anonymous, and all previously unknown roles remain unknown.
API_LABELS = frozenset({"UserByScreenName", "UserByRestId", "UsersByIds", "UserTweets", "UserTweetsAndReplies",
                        "UserMedia", "TweetDetail", "TweetResultByRestId", "TweetResultsByRestIds", "SearchTimeline",
                        "HomeTimeline", "HomeLatestTimeline", "Bookmarks", "Likes", "Followers", "Following",
                        "ListLatestTweetsTimeline", "ListTweetsTimeline", "ViewerBadgeCounts", "DeleteTweet",
                        "DeleteRetweet", "UnfavoriteTweet", "fleetline", "unrecognized"})


def request_metadata(url: object, resource: object) -> tuple[str, str, str, tuple[str, str] | None]:
    resource_type = resource if isinstance(resource, str) and resource in RESOURCE_TYPES else "other"
    if not isinstance(url, str):
        return resource_type, "other", "unrecognized", None
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.rstrip("/")
    except ValueError:
        return resource_type, "other", "unrecognized", None
    if not any(host == name or host.endswith("." + name) for name in ("x.com", "twitter.com")):
        return resource_type, "other", "unrecognized", None
    graph = re.fullmatch(r"/i/api/graphql/[A-Za-z0-9_-]+/([A-Za-z0-9_]+)", path)
    label = "unrecognized"
    if graph:
        family = "graphql"
        operation = graph.group(1)
        label = operation if operation in API_LABELS else "unrecognized"
        routing_key = "/i/api/graphql/<hash>/" + operation
    else:
        routing_key = path
        family = "rest" if path.startswith("/i/api/") else "document" if resource_type == "document" else "asset" if resource_type in {"image", "media", "font", "script", "stylesheet", "manifest"} else "other"
        if path == "/i/api/fleets/v1/fleetline":
            label = "fleetline"
    # The routing key is memory-only; queries, fragments and user-info are ignored.
    return resource_type, family, label, (host, routing_key)


def runtime_source_fingerprint() -> str | None:
    try:
        root = Path(__file__).parent
        digest = hashlib.sha256()
        files = sorted(root.glob("*.py"))
        if not files:
            return None
        for path in files:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()
    except OSError:
        return None


def operation_digest(url: object) -> str | None:
    """One-way identity for API operation tokens; exclude hosts, IDs, paths and queries.

    Unknown names are never emitted. REST basenames deliberately do not identify
    a complete route; endpoint_group keeps distinct routes apart within a trace.
    """
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"} or parsed.username is not None:
            return None
        graph = re.fullmatch(r"/i/api/graphql/[A-Za-z0-9_-]+/([A-Za-z][A-Za-z_]{0,79})", parsed.path.rstrip("/"))
        if graph:
            token = "graphql:" + graph.group(1)
        elif parsed.path.startswith("/i/api/") and re.fullmatch(r"[A-Za-z][A-Za-z_-]{0,79}\.json", parsed.path.rsplit("/", 1)[-1]):
            token = "rest:" + parsed.path.rsplit("/", 1)[-1]
        else:
            return None
        return hashlib.sha256(token.encode("ascii")).hexdigest()
    except (ValueError, UnicodeError):
        return None
