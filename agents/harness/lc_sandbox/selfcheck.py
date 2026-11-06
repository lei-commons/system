"""Tries to escape from inside the sandbox. Fails if it can.

Run by test-sandbox.sh in CI, inside the real container and network.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
from urllib.parse import urlsplit


def direct(host, port):
    try:
        with socket.create_connection((host, port), timeout=5):
            return True
    except OSError:
        return False


def via_proxy(proxy, host):
    """Return the proxy's status for a tunnel to ``host``, or 0 if it can't be reached."""
    parts = urlsplit(proxy)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=20)
    try:
        conn.request("CONNECT", f"{host}:443", headers={"Host": f"{host}:443"})
        return conn.getresponse().status
    except OSError:
        return 0
    finally:
        conn.close()


def run_checks(proxy, direct=direct, via=via_proxy):
    return [
        ("direct 1.1.1.1:443 is blocked", not direct("1.1.1.1", 443)),
        ("direct pypi.org:443 is blocked", not direct("pypi.org", 443)),
        ("metadata server is blocked", not direct("169.254.169.254", 80)),
        ("proxy allows pypi.org", via(proxy, "pypi.org") == 200),
        ("proxy refuses example.org", via(proxy, "example.org") == 403),
    ]


def main():
    results = run_checks(os.environ["HTTPS_PROXY"])
    for name, ok in results:
        print(json.dumps({"check": name, "ok": ok}), flush=True)
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
