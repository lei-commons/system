"""How jobs reach the internet: only through the conduct gateway.

Redirects are followed here, one hop at a time, each as a new request through
the gateway, so every hop is checked against the conduct policy. A refusal
raises Denied with the gateway's rule, and is never retried as something else.
"""

from __future__ import annotations

import http.client
import json
import shutil
import time
import urllib.parse
import urllib.request

METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
MAX_REDIRECTS = 5
# Busy answers are retried: 429 and 502-504, whether from a target, from Cloud
# Run while the gateway starts or scales, or the gateway's own pacing
# (Article 2.4). Waits honour Retry-After, capped, with backoff.
RETRY_STATUSES = (429, 502, 503, 504)
RETRY_WAITS = (5, 15, 30, 60, 120)
MAX_RETRY_WAIT = 120
CHUNK = 1024 * 1024


class Denied(Exception):
    """The gateway refused the request. ``rule`` says which conduct rule applied."""

    def __init__(self, status: int, rule: str, reason: str):
        super().__init__(f"{status} {rule}: {reason}")
        self.status, self.rule, self.reason = status, rule, reason


class FetchError(Exception):
    """The target answered with an error, or could not be reached."""


class _Busy(Exception):
    """A busy answer, to be retried; carries the error to raise if retries run out."""

    def __init__(self, error: Exception, retry_after):
        super().__init__(str(error))
        self.error, self.retry_after = error, retry_after


def _seconds(value):
    try:
        return max(0, int(value)) if value else None
    except ValueError:
        return None


def metadata_token(audience: str) -> str:
    req = urllib.request.Request(f"{METADATA}?audience={urllib.parse.quote(audience, safe='')}", headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode()


class Gateway:
    def __init__(self, url: str, actor: str, token=metadata_token, timeout: float = 3600, sleep=time.sleep,
                 waits=RETRY_WAITS):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("gateway URL must be http(s)://host")
        self._url, self._parts, self._actor = url.rstrip("/"), parts, actor
        self._token, self._timeout = token, timeout
        self._sleep, self._waits = sleep, waits

    def _connect(self):
        cls = http.client.HTTPSConnection if self._parts.scheme == "https" else http.client.HTTPConnection
        return cls(self._parts.hostname, self._parts.port, timeout=self._timeout)

    def open(self, target: str, method: str = "GET", headers: dict = None):
        """Return (connection, response) for ``target``, after following redirects and retrying busy answers."""
        for attempt, wait in enumerate((*self._waits, None)):
            try:
                return self._open(target, method, headers)
            except _Busy as busy:
                if wait is None:
                    raise busy.error from None
                self._sleep(min(max(wait, busy.retry_after or 0), MAX_RETRY_WAIT))

    def _open(self, target: str, method: str, headers: dict):
        for _ in range(MAX_REDIRECTS + 1):
            conn = self._connect()
            sent = {**(headers or {}), "X-LC-Target": target, "X-LC-Actor": self._actor,
                    "X-Serverless-Authorization": f"Bearer {self._token(self._url)}"}
            try:
                conn.request(method, "/fetch", headers=sent)
                resp = conn.getresponse()
            except OSError as exc:
                conn.close()
                raise FetchError(f"gateway unreachable: {type(exc).__name__}") from exc
            denied = resp.getheader("X-LC-Denied")
            retry_after = _seconds(resp.getheader("Retry-After"))
            if denied:
                try:
                    reason = json.loads(resp.read(4096) or b"{}").get("reason", "")
                except ValueError:
                    reason = ""
                conn.close()
                if denied == "rate-limited":
                    raise _Busy(Denied(resp.status, denied, reason), retry_after)
                raise Denied(resp.status, denied, reason)
            location = resp.getheader("Location")
            if 300 <= resp.status < 400 and location:
                conn.close()
                target = urllib.parse.urljoin(target, location)
                continue
            if resp.status in RETRY_STATUSES:
                conn.close()
                raise _Busy(FetchError(f"{target} answered {resp.status}"), retry_after)
            if resp.status >= 400:
                conn.close()
                raise FetchError(f"{target} answered {resp.status}")
            return conn, resp
        raise FetchError(f"more than {MAX_REDIRECTS} redirects")

    def get_json(self, target: str):
        conn, resp = self.open(target, headers={"Accept": "application/json"})
        try:
            return json.loads(resp.read())
        finally:
            conn.close()

    def download(self, target: str, file) -> int:
        """Stream ``target`` into an open binary file. Returns the bytes written."""
        conn, resp = self.open(target)
        try:
            start = file.tell()
            shutil.copyfileobj(resp, file, CHUNK)
            return file.tell() - start
        finally:
            conn.close()
