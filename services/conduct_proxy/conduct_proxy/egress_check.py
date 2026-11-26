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

from conduct_proxy.upstream import UPSTREAM_TIMEOUT_S

# Wait longer than the gateway waits for its target, so a slow target comes back as the
# gateway's own 502 upstream-error instead of a timeout here.
FETCH_TIMEOUT_S = UPSTREAM_TIMEOUT_S + 10
METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
DIRECT_TARGETS = (("1.1.1.1", 443), ("example.org", 443))
# The target's own answers that say it is busy or failing, passed on by the gateway.
TARGET_BUSY_OR_DOWN = (429, 500, 502, 503, 504)


def tcp_connects(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_get(url: str, headers: dict, timeout: float = FETCH_TIMEOUT_S):
    """Return (status, headers). Error statuses are results here, not failures."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def gateway_checks(label: str, gateway: str, audience: str, get=http_get, token=id_token, fetch=True) -> list:
    """Check one route to the gateway. A route that can't connect fails; it doesn't crash the job.

    ``fetch=False``: the control state was already shown on another route but the target failed there, so
    the gateway now leaves that host alone for ``pacing.seconds_after_an_error`` (120 s), longer than this
    job may run. The fetch isn't repeated: the control state is the one gateway's, not a route's.
    """
    results = []
    try:
        status, _ = get(f"{gateway}/health", {})
        results.append((f"{label}: refuses callers without identity", status in (401, 403), str(status)))

        auth = {"X-Serverless-Authorization": f"Bearer {token(audience)}"}
        status, _ = get(f"{gateway}/health", auth)
        results.append((f"{label}: reachable with identity", status == 200, str(status)))

        if not fetch:
            results.append((f"{label}: applies the control state", True,
                            "shown on the first route; not repeated while the host pauses after an error"))
            return results
        status, headers = get(f"{gateway}/fetch", {**auth, "X-LC-Target": "https://api.gleif.org/api/v1/lei-records?page%5Bsize%5D=1",
                                                   "X-LC-Actor": "job:egress-check"})
        denied = {k.lower(): v for k, v in headers.items()}.get("x-lc-denied", "")
        # The gateway decided by the control state if it refused because the system isn't running, or let the
        # request through: 200, the target's own busy or server error (no X-LC-Denied), 502 upstream-error (the
        # target slow or down) or 429 rate-limited (the host's turn not yet come) all mean it was running and
        # checked. A policy refusal of an approved host, or an answer that isn't the gateway's, is a failure.
        applied = (status == 200 or denied in ("system-not-running", "upstream-error", "rate-limited")
                   or (not denied and status in TARGET_BUSY_OR_DOWN))
        results.append((f"{label}: applies the control state", applied, f"{status} {denied}".strip()))
    except OSError as exc:
        results.append((f"{label}: connects", False, type(exc).__name__))
    return results


def main() -> int:
    audience = os.environ["LC_GATEWAY_AUDIENCE"].rstrip("/")
    results = direct_checks()
    first = gateway_checks("load balancer", os.environ["LC_GATEWAY_URL"].rstrip("/"), audience)
    applied = [r for r in first if r[0].endswith("applies the control state")]
    # 2026-11-25 21:42 UTC: GLEIF was slow, the first route got 502 upstream-error (a pass), and the second
    # route's fetch then waited out the gateway's pause after that error until it timed out.
    repeat = bool(applied) and applied[0][1] and applied[0][2] == "200"
    results += first
    results += gateway_checks("run.app", audience, audience, fetch=repeat)
    for name, ok, detail in results:
        print(json.dumps({"check": name, "ok": ok, "detail": detail}), flush=True)
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
