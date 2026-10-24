"""Conservative request-role labels, never claims about X quota buckets."""

import re
from typing import Literal
from urllib.parse import urlparse

RateLimitScope = Literal["mutation", "target", "ancillary", "unknown"]
WEB_HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})
API_HOSTS = frozenset({"api.x.com", "api.twitter.com"})
POST_MUTATIONS = frozenset({"DeleteTweet", "DeleteRetweet", "UnfavoriteTweet"})
TARGET_READS = frozenset({"TweetDetail", "TweetResultByRestId", "TweetResultsByRestIds"})
ANCILLARY_OPERATIONS = frozenset({"ViewerBadgeCounts", "CreatorStudioTabBarItemQuery"})
MUTATIONS = POST_MUTATIONS | {"destroy.json"}
ANCILLARY_READS = ANCILLARY_OPERATIONS | {"fleetline", "fleetline.json", "account/settings.json"}


def classify_rate_limit(url: str) -> tuple[RateLimitScope, str]:
    """Only label recognized X paths; unknown requests retain global pacing."""
    try:
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname not in WEB_HOSTS | API_HOSTS
                or parsed.username is not None or parsed.password is not None):
            return "unknown", "unclassified"
        path = parsed.path.rstrip("/")
        if parsed.hostname in WEB_HOSTS:
            graph = re.fullmatch(r"/i/api/graphql/[A-Za-z0-9_-]+/([A-Za-z0-9_]+)", path)
            if graph:
                name = graph.group(1)
                if name in POST_MUTATIONS:
                    return "mutation", name
                if name in TARGET_READS:
                    return "target", name
                if name in ANCILLARY_OPERATIONS:
                    return "ancillary", name
            if path == "/i/api/1.1/friendships/destroy.json":
                return "mutation", "destroy.json"
            if re.fullmatch(r"/[A-Za-z0-9_]+/status/[0-9]+(?:/[^?#]*)?", path):
                return "target", "target-document"
            if path in {"/i/api/fleetline", "/i/api/fleets/v1/fleetline", "/i/api/fleets/v1/fleetline.json"}:
                return "ancillary", path.rsplit("/", 1)[-1]
        if parsed.hostname in API_HOSTS and path == "/1.1/friendships/destroy.json":
            return "mutation", "destroy.json"
        if path == "/1.1/account/settings.json":
            return "ancillary", "account/settings.json"
    except (TypeError, ValueError):
        pass
    return "unknown", "unclassified"
