"""HTTP front end of the gateway.

Callers send their request to /fetch with the destination in X-LC-Target:

    GET /fetch
    X-LC-Target: https://api.gleif.org/api/v1/lei-records/...
    X-LC-Actor: job:ingest-gleif

This is deliberately not a CONNECT proxy. CONNECT would tunnel TLS past the
policy, leaving only the host name visible. Here the gateway makes the outbound
TLS connection itself, so it sees the method, path and headers of every request
without needing an interception CA.

Redirects are returned to the caller, never followed, so each hop goes through
the policy as a new request.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin

from lc_core.control import SystemHalted, require_running

from .limits import ROBOTS_MAX_BYTES, RateLimiter, RobotsCache
from .policy import CRAWLER, ConfigError, Decision, Policy
from .upstream import BlockedAddress, Upstream

COMPONENT = "conduct-proxy"
MAX_BODY = 10 * 1024 * 1024
MAX_QUEUE_WAIT = 30.0
GATE_TTL = 30.0
CHUNK = 64 * 1024

# Only these response headers come back. Set-Cookie is absent by design.
RETURN_HEADERS = (
    "content-type", "content-length", "content-encoding", "content-range", "accept-ranges",
    "etag", "last-modified", "cache-control", "location", "retry-after",
)


class Gate:
    """Article 1.2: when the system is not running, nothing leaves. Fails closed."""

    def __init__(self, check=lambda: require_running(COMPONENT), clock=time.monotonic):
        self._check, self._clock = check, clock
        self._until, self._error = 0.0, None
        self._lock = threading.Lock()

    def blocked(self):
        """Return why traffic is blocked, or None when it may flow."""
        with self._lock:
            if self._clock() >= self._until:
                try:
                    self._check()
                    self._error = None
                except SystemHalted as exc:
                    self._error = str(exc)
                except Exception as exc:
                    self._error = f"control state check failed: {type(exc).__name__}"
                self._until = self._clock() + GATE_TTL
            return self._error


class Gateway:
    def __init__(self, policy: Policy, upstream=None, gate=None, log=None, sleep=time.sleep):
        self.policy = policy
        self.upstream = upstream or Upstream()
        self.gate = gate or Gate()
        self.limiter = RateLimiter()
        self.robots = RobotsCache(self._get_robots)
        self.log = log or _log_stdout
        self.sleep = sleep

    def _get_robots(self, url):
        """Fetch robots.txt, following up to five redirects (RFC 9309) through the policy."""
        for _ in range(6):
            d = self.policy.evaluate("GET", url, {})
            if not d.allow:
                return 0, b""
            wait = self.limiter.reserve(d.host, self.policy.min_interval, MAX_QUEUE_WAIT)
            if wait is None:
                return 0, b""
            self.sleep(wait)
            conn, resp = self.upstream.open(d.scheme, d.host, d.port, "GET", d.target, d.headers)
            try:
                location = resp.getheader("location")
                if 300 <= resp.status < 400 and location:
                    url = urljoin(url, location)
                    continue
                return resp.status, resp.read(ROBOTS_MAX_BYTES)
            finally:
                conn.close()
        return 0, b""

    def pace(self, d: Decision):
        """Apply robots.txt and per-host pacing. Returns (status, rule, reason, retry_after)."""
        if d.profile == CRAWLER and not self.robots.allowed(d.scheme, d.host, d.target):
            return 403, "robots-disallowed", "robots.txt does not allow this path (Article 2.4)", None
        if not self.policy.rate_limited(d.profile):
            return None
        interval = self.policy.min_interval
        if d.profile == CRAWLER:
            interval = max(interval, self.robots.crawl_delay(d.scheme, d.host))
        wait = self.limiter.reserve(d.host, interval, MAX_QUEUE_WAIT)
        if wait is None:
            return 429, "rate-limited", "per-host queue is full (Article 2.4)", int(MAX_QUEUE_WAIT)
        self.sleep(wait)
        return None


def _log_stdout(record: dict) -> None:
    print(json.dumps(record, sort_keys=True), file=sys.stdout, flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "lei-commons-conduct-proxy"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    gateway: Gateway = None

    def log_message(self, *args):  # structured records only; the default line includes the query
        pass

    def _record(self, d, status, rule):
        self.gateway.log({
            "ts": time.time(), "actor": (self.headers.get("X-LC-Actor") or "unknown")[:80],
            "method": self.command, "host": d.host if d else "", "path": d.log_path if d else "",
            "profile": d.profile if d else "", "allowed": rule == "allowed", "rule": rule, "status": status,
        })

    def _refuse(self, status, rule, reason, d=None, retry_after=None):
        body = json.dumps({"denied": True, "rule": rule, "reason": reason}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-LC-Denied", rule)
        self.send_header("Connection", "close")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._record(d, status, rule)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if not 0 <= length <= MAX_BODY:
            return None
        return self.rfile.read(length)

    def _handle(self):
        self.close_connection = True  # one request per connection: nothing to smuggle into
        if self.command == "GET" and self.path == "/health":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/fetch":
            return self._refuse(403, "not-a-gateway-request", "use /fetch with X-LC-Target; CONNECT is not offered")

        gw = self.gateway
        halted = gw.gate.blocked()
        if halted:
            return self._refuse(503, "system-not-running", halted)

        client_headers = {k: v for k, v in self.headers.items() if not k.lower().startswith("x-lc-")}
        d = gw.policy.evaluate(self.command, self.headers.get("X-LC-Target", ""), client_headers)
        if not d.allow:
            return self._refuse(403, d.rule, d.reason, d)

        body = None
        if self.command == "POST":
            body = self._read_body()
            if body is None:
                return self._refuse(413, "bad-body", "POST needs a Content-Length within the limit", d)

        try:
            refused = gw.pace(d)
            if refused:
                status, rule, reason, retry = refused
                return self._refuse(status, rule, reason, d, retry)
            conn, resp = gw.upstream.open(d.scheme, d.host, d.port, self.command, d.target, d.headers, body)
        except BlockedAddress as exc:
            return self._refuse(403, "non-public-address", str(exc), d)
        except (OSError, http.client.HTTPException) as exc:
            return self._refuse(502, "upstream-error", type(exc).__name__, d)

        try:
            self.send_response(resp.status)
            for name in RETURN_HEADERS:
                value = resp.getheader(name)
                if value is not None:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            self._record(d, resp.status, "allowed")
        except (OSError, http.client.HTTPException):
            self.close_connection = True  # headers are already sent, so all we can do is cut it
            self._record(d, resp.status, "stream-failed")
        finally:
            conn.close()

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _handle
    do_OPTIONS = do_TRACE = do_CONNECT = _handle


def make_server(gateway: Gateway, address) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"gateway": gateway})
    return ThreadingHTTPServer(address, handler)


def main() -> int:
    approved = {s for s in os.environ.get("LC_APPROVED_KEY_SOURCES", "").split(",") if s}
    try:
        policy = Policy.from_file(os.environ.get("LC_EGRESS_CONFIG", "config/egress-allowlist.yaml"), approved)
    except ConfigError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 1
    server = make_server(Gateway(policy), ("0.0.0.0", int(os.environ.get("PORT", "8080"))))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
