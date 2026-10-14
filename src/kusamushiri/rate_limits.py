"""Conservative request-role labels, never claims about X quota buckets."""

import re
from typing import Literal
from urllib.parse import urlparse

RateLimitScope = Literal["mutation", "target", "ancillary", "unknown"]
MUTATIONS = {"DeleteTweet", "DeleteRetweet", "UnfavoriteTweet", "destroy.json"}
TARGET_READS = {"TweetDetail", "TweetResultByRestId", "TweetResultsByRestIds"}
ANCILLARY_READS = {"fleetline", "fleetline.json", "ViewerBadgeCounts"}


def classify_rate_limit(url: str) -> tuple[RateLimitScope, str]:
    """Only label recognized paths; unknown requests retain conservative global pacing."""
    path = urlparse(url).path.rstrip("/")
    name = path.rsplit("/", 1)[-1]
    if name in MUTATIONS:
        return "mutation", name
    if name in TARGET_READS:
        return "target", name
    if re.fullmatch(r"/[A-Za-z0-9_]+/status/[0-9]+(?:/[^?#]*)?", path):
        return "target", "target-document"
    if name in ANCILLARY_READS:
        return "ancillary", name
    return "unknown", "unclassified"
