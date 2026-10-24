"""Request role vocabularies stay consistent across independent policy gates."""

from types import SimpleNamespace

import pytest

from kusamushiri.ancillary_isolation import isolatable_endpoint
from kusamushiri.http_failures import http_failure
from kusamushiri.rate_limits import (
    ANCILLARY_OPERATIONS,
    API_HOSTS,
    POST_MUTATIONS,
    TARGET_READS,
    WEB_HOSTS,
    classify_rate_limit,
)
from kusamushiri.traffic_policy import decide


def failure_role(url, method):
    response = SimpleNamespace(url=url, status=403, request=SimpleNamespace(url=url, method=method), headers={})
    failure = http_failure(response)
    assert failure is not None
    return failure.role, failure.endpoint


@pytest.mark.parametrize("host", sorted(WEB_HOSTS))
@pytest.mark.parametrize("operation", sorted(TARGET_READS))
def test_every_target_read_uses_the_same_role_and_admission(host, operation):
    url = f"https://{host}/i/api/graphql/fixture/{operation}"
    assert classify_rate_limit(url) == failure_role(url, "GET") == ("target", operation)
    assert decide(url, "GET", "xhr", armed=False) == ("x_web", "allow", "target_read")


@pytest.mark.parametrize("operation", sorted(POST_MUTATIONS))
def test_every_post_mutation_uses_the_same_role_and_admission(operation):
    url = f"https://x.com/i/api/graphql/fixture/{operation}"
    assert classify_rate_limit(url) == failure_role(url, "POST") == ("mutation", operation)
    assert decide(url, "POST", "xhr", armed=True) == ("x_web", "allow", "armed_mutation")


@pytest.mark.parametrize("host", sorted(WEB_HOSTS))
@pytest.mark.parametrize("operation", sorted(ANCILLARY_OPERATIONS))
def test_ancillary_web_hosts_include_www_aliases(host, operation):
    url = f"https://{host}/i/api/graphql/fixture/{operation}"
    assert classify_rate_limit(url) == failure_role(url, "GET") == ("ancillary", operation)
    assert isolatable_endpoint(url, "GET") == operation
    fleets = f"https://{host}/i/api/fleets/v1/fleetline?only_spaces=true"
    assert classify_rate_limit(fleets) == ("ancillary", "fleetline")
    assert isolatable_endpoint(fleets, "GET") == "fleetline"


@pytest.mark.parametrize("host", sorted(WEB_HOSTS | API_HOSTS))
def test_only_known_friendship_destroy_route_is_a_mutation(host):
    prefix = "/i/api" if host in WEB_HOSTS else ""
    assert classify_rate_limit(f"https://{host}{prefix}/1.1/friendships/destroy.json") == ("mutation", "destroy.json")
    assert classify_rate_limit(f"https://{host}/other/destroy.json") == ("unknown", "unclassified")


@pytest.mark.parametrize("url", [
    "https://foreign.example/i/api/1.1/friendships/destroy.json",
    "https://foreign.example/i/api/graphql/fixture/TweetDetail",
    "https://foreign.example/person/status/123",
    "https://foreign.example/1.1/account/settings.json",
    "https://user@x.com/i/api/1.1/friendships/destroy.json",
    "http://x.com/i/api/1.1/friendships/destroy.json",
    "https://x.com/other/DeleteTweet",
    "https://[invalid/",
])
def test_untrusted_or_unrecognized_routes_stay_unknown(url):
    assert classify_rate_limit(url) == ("unknown", "unclassified")
