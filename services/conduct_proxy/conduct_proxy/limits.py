"""Per-host pacing and robots.txt handling (Article 2.4)."""

from __future__ import annotations

import threading
import time
from urllib.robotparser import RobotFileParser

ROBOTS_TTL = 24 * 3600
ROBOTS_FAILURE_TTL = 300
ROBOTS_MAX_BYTES = 500 * 1024
AGENT_TOKEN = "lei-commons-crawler"


class RateLimiter:
    """Hands out one send slot per host, spaced by at least the interval."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._next = {}
        self._lock = threading.Lock()

    def reserve(self, host: str, interval: float, max_wait: float):
        """Book the next slot. Returns seconds to wait, or None if that exceeds max_wait."""
        with self._lock:
            now = self._clock()
            slot = max(now, self._next.get(host, 0.0))
            wait = slot - now
            if wait > max_wait:
                return None
            self._next[host] = slot + interval
            return wait


class RobotsCache:
    """Caches one parsed robots.txt per scheme and host.

    Follows RFC 9309: a 4xx means no restrictions; a 5xx or a failed fetch means
    assume everything is disallowed, for a short time before trying again.
    ``fetch(url)`` returns (status, body bytes) and raises on failure.
    """

    def __init__(self, fetch, clock=time.monotonic):
        self._fetch = fetch
        self._clock = clock
        self._cache = {}
        self._lock = threading.Lock()

    def _load(self, scheme: str, host: str):
        key = (scheme, host)
        with self._lock:
            hit = self._cache.get(key)
        if hit and hit[0] > self._clock():
            return hit[1]
        parser = RobotFileParser()
        ttl = ROBOTS_TTL
        try:
            status, body = self._fetch(f"{scheme}://{host}/robots.txt")
        except Exception:
            status, body = 0, b""
        if 200 <= status < 300:
            parser.parse(body[:ROBOTS_MAX_BYTES].decode("utf-8", "replace").splitlines())
        elif 400 <= status < 500:
            parser.allow_all = True
        else:
            parser.disallow_all = True
            ttl = ROBOTS_FAILURE_TTL
        with self._lock:
            self._cache[key] = (self._clock() + ttl, parser)
        return parser

    def allowed(self, scheme: str, host: str, target: str) -> bool:
        return self._load(scheme, host).can_fetch(AGENT_TOKEN, f"{scheme}://{host}{target}")

    def crawl_delay(self, scheme: str, host: str) -> float:
        try:
            return float(self._load(scheme, host).crawl_delay(AGENT_TOKEN) or 0)
        except (TypeError, ValueError):
            return 0.0


class HostPacer:
    """A person's pace in a browser, per host (owner decision 2026-11-18; stricter than Article 2.4).

    One request to a host at a time: the next waits until the last has finished (a whole download, not
    just its start), then a pause of a few seconds plus a random extra, so requests never arrive back to
    back or like clockwork. After an error from the host (403, 429, 5xx) the pause is much longer, or as
    long as its Retry-After asks. ``acquire`` waits for the host's turn; ``release`` sets the next one.
    """

    def __init__(self, clock=time.monotonic, sleep=time.sleep, rand=None):
        import random

        self._clock, self._sleep, self._rand = clock, sleep, rand or random.random
        self._cond = threading.Condition()
        self._busy, self._ready_at = set(), {}

    def acquire(self, host: str, max_wait: float):
        """Wait for ``host``'s turn. Returns None when it is this request's turn, or the seconds to come back
        after (the queue is longer than ``max_wait``)."""
        deadline = self._clock() + max_wait
        with self._cond:
            while host in self._busy:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return max(1, int(max_wait))
                self._cond.wait(remaining)
            wait = self._ready_at.get(host, 0.0) - self._clock()
            if wait > deadline - self._clock():
                return max(1, int(wait + 0.999))
            self._busy.add(host)
        if wait > 0:
            self._sleep(wait)
        return None

    def release(self, host: str, pause: float) -> None:
        """This request to ``host`` has finished; the next may start ``pause`` seconds from now."""
        with self._cond:
            self._ready_at[host] = max(self._ready_at.get(host, 0.0), self._clock() + pause)
            self._busy.discard(host)
            self._cond.notify_all()

    def pause_after(self, pacing: dict, status: int, retry_after: float = None, crawl_delay: float = 0.0) -> float:
        """How long to leave ``host`` alone after a request that answered ``status``."""
        pause = pacing["min_seconds_between_requests"] + self._rand() * pacing["extra_random_seconds"]
        pause = max(pause, crawl_delay)
        if status in (403, 429) or status >= 500 or status == 0:
            pause = max(pause, pacing["seconds_after_an_error"], min(retry_after or 0, 3600))
        return pause
