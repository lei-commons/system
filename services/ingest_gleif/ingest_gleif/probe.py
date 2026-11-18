"""One polite request through the conduct gateway, to see exactly how a source answers us.

    python -m ingest_gleif.probe <https URL on an allow-listed registry host>

Run as the ingest-gleif job with its arguments overridden (gateway-probe.yml). It asks for the first
kilobyte only (Range: bytes=0-1023), goes through the gateway like every fetch (allow-list, pacing, the
fixed address), and prints the status, the response headers the gateway passes back, and, for an error,
the first 600 characters of the body: a site's own explanation (a firewall's block page, a rate-limit
note). It changes nothing and stores nothing. (2026-11-18: GLEIF's file storage answered 403 only to us.)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse

from lc_core.fetch import Gateway

COMPONENT = "job:ingest-gleif"


def probe(gateway: Gateway, url: str) -> dict:
    conn = gateway._connect()
    try:
        headers = {"X-LC-Target": url, "X-LC-Actor": f"{COMPONENT}/probe", "Range": "bytes=0-1023",
                   "X-Serverless-Authorization": f"Bearer {gateway._token(gateway._url)}"}
        conn.request("GET", "/fetch", headers=headers)
        resp = conn.getresponse()
        body = resp.read(4096)
        out = {"url": url, "status": resp.status, "gatewayDenied": resp.getheader("X-LC-Denied"),
               "headers": {k.lower(): v for k, v in resp.getheaders()
                           if k.lower() in ("content-type", "content-range", "retry-after", "cache-control", "location")}}
        if resp.status >= 400 or resp.getheader("X-LC-Denied"):
            out["body"] = body.decode("utf-8", "replace")[:600]
        else:
            out["bytes"] = len(body)
        return out
    finally:
        conn.close()


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or urllib.parse.urlsplit(args[0]).scheme != "https":
        print("usage: python -m ingest_gleif.probe <https URL>", file=sys.stderr)
        return 2
    result = probe(Gateway(os.environ["LC_GATEWAY_URL"], f"{COMPONENT}/probe"), args[0])
    print(json.dumps({"event": "probe", **result}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
