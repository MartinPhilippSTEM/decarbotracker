import httpx

from decarbotracker.fetch.scrape import RobotsCache


def _client(status: int, body: str = "", ctype: str = "text/plain") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body, headers={"content-type": ctype})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_robots_4xx_means_allowed():
    # RFC 9309: nedostupný robots.txt (i 403 z ochrany proti botům) = bez omezení
    for status in (401, 403, 404):
        assert RobotsCache().allowed(_client(status), "https://example.org/news", "decarbotracker/1.0")


def test_robots_5xx_means_disallowed():
    assert not RobotsCache().allowed(_client(503), "https://example.org/news", "decarbotracker/1.0")


def test_robots_rules_respected():
    body = "User-agent: *\nDisallow: /private/\n"
    rc = RobotsCache()
    client = _client(200, body)
    assert rc.allowed(client, "https://example.org/news", "decarbotracker/1.0")
    assert not rc.allowed(client, "https://example.org/private/x", "decarbotracker/1.0")
