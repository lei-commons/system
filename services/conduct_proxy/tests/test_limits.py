import pytest

from conduct_proxy.limits import RateLimiter, RobotsCache
from conduct_proxy.upstream import BlockedAddress, is_public, resolve_public


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def test_slots_are_spaced_per_host():
    c = Clock()
    r = RateLimiter(c)
    assert r.reserve("a", 1, 30) == 0
    assert r.reserve("a", 1, 30) == 1
    assert r.reserve("a", 1, 30) == 2
    assert r.reserve("b", 1, 30) == 0
    c.t += 10
    assert r.reserve("a", 1, 30) == 0


def test_full_queue_books_nothing():
    r = RateLimiter(Clock())
    r.reserve("a", 10, 5)
    assert r.reserve("a", 10, 5) is None
    assert r.reserve("a", 10, 10) == 10


def robots(status, body=b""):
    return RobotsCache(lambda url: (status, body), Clock())


def test_robots_rules_applied():
    r = robots(200, b"User-agent: *\nDisallow: /private\nCrawl-delay: 5\n")
    assert r.allowed("https", "e.test", "/public")
    assert not r.allowed("https", "e.test", "/private/x")
    assert r.crawl_delay("https", "e.test") == 5


def test_robots_404_means_no_restrictions():
    assert robots(404).allowed("https", "e.test", "/x")


@pytest.mark.parametrize("status", [500, 503, 0, 302])
def test_robots_server_error_means_disallow(status):
    assert not robots(status).allowed("https", "e.test", "/x")


def test_robots_fetch_failure_means_disallow():
    def boom(url):
        raise OSError("down")

    assert not RobotsCache(boom, Clock()).allowed("https", "e.test", "/x")


def test_robots_cached_then_retried_after_failure():
    calls, c = [], Clock()

    def fetch(url):
        calls.append(url)
        return (503, b"") if len(calls) == 1 else (200, b"")

    r = RobotsCache(fetch, c)
    assert not r.allowed("https", "e.test", "/x")
    assert not r.allowed("https", "e.test", "/y")
    assert len(calls) == 1
    c.t += 301
    assert r.allowed("https", "e.test", "/x")
    assert len(calls) == 2


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "224.0.0.1",
])
def test_non_public_addresses(ip):
    assert not is_public(ip)


def test_public_address():
    assert is_public("8.8.8.8") and is_public("2001:4860:4860::8888")


def fake_dns(*ips):
    return lambda host, port, type=0: [(2, 1, 6, "", (ip, port)) for ip in ips]


def test_resolve_returns_public_address():
    assert resolve_public("e.test", 443, fake_dns("8.8.8.8")) == "8.8.8.8"


def test_one_private_answer_rejects_host():
    with pytest.raises(BlockedAddress):
        resolve_public("e.test", 443, fake_dns("8.8.8.8", "169.254.169.254"))


def test_resolution_failure_blocks():
    def fail(*a, **k):
        raise OSError("nxdomain")

    with pytest.raises(BlockedAddress):
        resolve_public("e.test", 443, fail)
