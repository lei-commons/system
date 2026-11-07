"""How jobs reach the internet: only through the conduct gateway.

Redirects are followed here, one hop at a time, each as a new request through
the gateway, so every hop is checked against the conduct policy. A refusal
raises Denied with the gateway's rule, and is never retried as something else.
"""

from __future__ import annotations

import http.client
import json
import shutil
import urllib.parse
import urllib.request

METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
MAX_REDIRECTS = 5
CHUNK = 1024 * 1024


class Denied(Exception):
    """The gateway refused the request. ``rule`` says which conduct rule applied."""

    def __init__(self, status: int, rule: str, reason: str):
        super().__init__(f"{status} {rule}: {reason}")
        self.status, self.rule, self.reason = status, rule, reason


class FetchError(Exception):
    """The target answered with an error, or could not be reached."""


def metadata_token(audience: str) -> str:
    req = urllib.request.Request(f"{METADATA}?audience={urllib.parse.quote(audience, safe='')}", headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode()


class Gateway:
    def __init__(self, url: str, actor: str, token=metadata_token, timeout: float = 3600):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("gateway URL must be http(s)://host")
        self._url, self._parts, self._actor = url.rstrip("/"), parts, actor
        self._token, self._timeout = token, timeout

    def _connect(self):
        cls = http.client.HTTPSConnection if self._parts.scheme == "https" else http.client.HTTPConnection
        return cls(self._parts.hostname, self._parts.port, timeout=self._timeout)

    def open(self, target: str, method: str = "GET", headers: dict = None):
        """Return (connection, response) for ``target``, after following redirects."""
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
            if denied:
                try:
                    reason = json.loads(resp.read(4096) or b"{}").get("reason", "")
                except ValueError:
                    reason = ""
                conn.close()
                raise Denied(resp.status, denied, reason)
            location = resp.getheader("Location")
            if 300 <= resp.status < 400 and location:
                conn.close()
                target = urllib.parse.urljoin(target, location)
                continue
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
