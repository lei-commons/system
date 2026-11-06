"""Proves the network from the inside. Run as a job on the jobs VPC.

The gateway only enforces anything if jobs have no other way out, so this tries
to get out directly and exits non-zero if it can. It sends no data: the direct
attempts are bare TCP connects, which should never complete.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request

METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
DIRECT_TARGETS = (("1.1.1.1", 443), ("example.org", 443))


def tcp_connects(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_get(url: str, headers: dict):
    """Return (status, headers). Error statuses are results here, not failures."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers)


def id_token(audience: str) -> str:
    req = urllib.request.Request(f"{METADATA}?audience={audience}", headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode()


def direct_checks(connects=tcp_connects) -> list:
    results = []
    for host, port in DIRECT_TARGETS:
        escaped = connects(host, port)
        results.append((f"direct {host}:{port} is blocked", not escaped, "CONNECTED" if escaped else "blocked"))
    return results


def gateway_checks(label: str, gateway: str, audience: str, get=http_get, token=id_token) -> list:
    """Check one route to the gateway. A route that can't connect fails; it doesn't crash the job."""
    results = []
    try:
        status, _ = get(f"{gateway}/health", {})
        results.append((f"{label}: refuses callers without identity", status in (401, 403), str(status)))

        auth = {"X-Serverless-Authorization": f"Bearer {token(audience)}"}
        status, _ = get(f"{gateway}/health", auth)
        results.append((f"{label}: reachable with identity", status == 200, str(status)))

        status, headers = get(f"{gateway}/fetch", {**auth, "X-LC-Target": "https://api.gleif.org/api/v1/lei-records?page%5Bsize%5D=1",
                                                   "X-LC-Actor": "job:egress-check"})
        denied = {k.lower(): v for k, v in headers.items()}.get("x-lc-denied", "")
        results.append((f"{label}: applies the control state", status == 200 or denied == "system-not-running", f"{status} {denied}".strip()))
    except OSError as exc:
        results.append((f"{label}: connects", False, type(exc).__name__))
    return results


def main() -> int:
    audience = os.environ["LC_GATEWAY_AUDIENCE"].rstrip("/")
    results = direct_checks()
    results += gateway_checks("load balancer", os.environ["LC_GATEWAY_URL"].rstrip("/"), audience)
    results += gateway_checks("run.app", audience, audience)
    for name, ok, detail in results:
        print(json.dumps({"check": name, "ok": ok, "detail": detail}), flush=True)
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
