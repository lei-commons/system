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


# ---- a person's pace per host (HostPacer) ----

import threading as _threading

from conduct_proxy.limits import HostPacer

PACING = {"min_seconds_between_requests": 5.0, "extra_random_seconds": 10.0, "seconds_after_an_error": 120.0,
          "max_queue_seconds": 900.0}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_the_next_request_waits_for_the_pause():
    c = Clock()
    p = HostPacer(clock=c, sleep=c.sleep, rand=lambda: 0.5)
    assert p.acquire("h", 900) is None
    p.release("h", p.pause_after(PACING, 200))  # 5 + 0.5 * 10 = 10 s
    start = c.t
    assert p.acquire("h", 900) is None and c.t - start == 10


def test_errors_and_retry_after_lengthen_the_pause_up_to_an_hour():
    p = HostPacer(rand=lambda: 0.0)
    assert p.pause_after(PACING, 200) == 5
    assert p.pause_after(PACING, 404) == 5  # not an error about us
    assert p.pause_after(PACING, 403) == 120 and p.pause_after(PACING, 503) == 120 and p.pause_after(PACING, 0) == 120
    assert p.pause_after(PACING, 429, retry_after=600) == 600
    assert p.pause_after(PACING, 429, retry_after=99999) == 3600
    assert p.pause_after(PACING, 200, crawl_delay=30) == 30


def test_a_wait_longer_than_the_queue_says_when_to_come_back():
    c = Clock()
    p = HostPacer(clock=c, sleep=c.sleep)
    assert p.acquire("h", 900) is None
    p.release("h", 2000)
    assert p.acquire("h", 900) == 2000
    assert p.acquire("other", 900) is None  # other hosts aren't held back


def test_one_request_at_a_time_per_host():
    p = HostPacer(rand=lambda: 0.0, sleep=lambda s: None)
    assert p.acquire("h", 900) is None
    got = []
    t = _threading.Thread(target=lambda: got.append(p.acquire("h", 5)))
    t.start()
    t.join(0.3)
    assert t.is_alive() and not got  # waiting while the first request is still going
    p.release("h", 0)
    t.join(2)
    assert got == [None]
    assert p.acquire("h", 0.2) == 1  # still busy (the second holds it): come back later
