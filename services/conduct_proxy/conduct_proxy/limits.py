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
