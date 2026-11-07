"""The private site and API.

Every request needs the owner's IAP-verified Google account. Nothing here is
public, and it stays that way until the owner approves going public.
"""

from __future__ import annotations

import html
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from lc_core.iap import HEADER, verify_iap

HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
           "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
STYLE = ("body{font:15px/1.5 system-ui,sans-serif;max-width:60em;margin:2em auto;padding:0 1em}"
         "table{border-collapse:collapse}td,th{padding:.2em .6em;border-bottom:1px solid #ddd;text-align:left}"
         ".note{color:#555;font-size:.9em}code{word-break:break-all}")
FOOTER = ("<p class=note>Private test instance, visible only to the owner. lei-commons is an independent open "
          "directory, not affiliated with GLEIF, and does not issue LEIs.</p>")


def page(title, body):
    nav = "<p><a href=/>Search</a> · <a href=/stats>Statistics</a> · <a href=/api/v1/stats>API</a></p>"
    return (f"<!DOCTYPE html><html lang=en><meta charset=utf-8><title>{html.escape(title)} · lei-commons</title>"
            f"<style>{STYLE}</style>{nav}<h1>{html.escape(title)}</h1>{body}{FOOTER}</html>").encode()


def table(rows, link=False):
    if not rows:
        return "<p>No results.</p>"
    cols = list(rows[0])
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = []
    for r in rows:
        cells = []
        for c in cols:
            v = "" if r[c] is None else str(r[c])
            if link and c == "directoryId":
                cells.append(f"<td><a href=/entity/{quote(v)}>{html.escape(v)}</a></td>")
            else:
                cells.append(f"<td>{html.escape(v)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><tr>{head}</tr>{''.join(body)}</table>"


class Handler(BaseHTTPRequestHandler):
    server_version = "lei-commons"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    owners: set = set()
    audience: str = ""
    snapshot = None
    iap = staticmethod(verify_iap)

    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type="text/html; charset=utf-8"):
        self.close_connection = True
        self.send_response(status)
        for k, v in HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        return self._send(status, json.dumps(obj, indent=2, default=str).encode(), "application/json")

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/health":
            return self._send(200, b"ok\n", "text/plain")
        email = self.iap(self.headers.get(HEADER), self.audience)
        if email is None or email not in self.owners:
            return self._send(403, page("Not allowed", "<p>This private instance is for the owner.</p>"))
        if self.snapshot is None:
            return self._send(503, page("No snapshot yet", "<p>No snapshot has been built yet.</p>"))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        snap, path = self.snapshot, url.path
        try:
            limit = int(q.get("limit", "25"))
        except ValueError:
            limit = 25

        if path == "/api/v1/search":
            return self._json(200, {"results": snap.search(q.get("q", ""), q.get("country", ""), limit)})
        if path.startswith("/api/v1/entities/"):
            record = snap.entity(path.rsplit("/", 1)[1])
            return self._json(200, record) if record else self._json(404, {"error": "not found"})
        if path == "/api/v1/stats":
            return self._json(200, snap.stats())
        if path == "/":
            term = q.get("q", "")
            form = (f"<form><input name=q value=\"{html.escape(term)}\" size=40 placeholder=\"Name, LEI, directory ID or "
                    f"registration number\" autofocus> <input name=country value=\"{html.escape(q.get('country', ''))}\" "
                    f"size=3 placeholder=GB> <button>Search</button></form>")
            meta = f"<p class=note>Snapshot {html.escape(str(snap.meta.get('date')))}: {snap.count:,} records.</p>"
            results = table(snap.search(term, q.get("country", ""), limit), link=True) if term else ""
            return self._send(200, page("lei-commons", form + meta + results))
        if path.startswith("/entity/"):
            record = snap.entity(path.rsplit("/", 1)[1])
            if not record:
                return self._send(404, page("Not found", "<p>No such record.</p>"))
            body = f"<pre>{html.escape(json.dumps(record, indent=2, ensure_ascii=False))}</pre>"
            return self._send(200, page(record.get("legalName", "Record"), body))
        if path == "/stats":
            s = snap.stats()
            body = (f"<p>Snapshot {html.escape(str(s['snapshot'].get('date')))}, root "
                    f"<code>{html.escape(str(s['snapshot'].get('snapshotRoot')))}</code>: {s['records']:,} records.</p>"
                    f"<h2>By country</h2>{table(s['byCountry'])}<h2>By source</h2>{table(s['bySources'])}"
                    f"<h2>By status</h2>{table(s['byStatus'])}")
            return self._send(200, page("Statistics", body))
        return self._send(404, page("Not found", ""))

    def do_HEAD(self):
        return self._send(405, b"")

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = do_HEAD


def load_latest(bucket_name: str):
    """Download the latest snapshot's files and load them. Returns None if there isn't one."""
    from google.cloud import storage

    from .data import Snapshot

    bucket = storage.Client().bucket(bucket_name)
    pointer = bucket.blob("latest.json")
    if not pointer.exists():
        return None
    latest = json.loads(pointer.download_as_bytes())
    target = tempfile.mkdtemp(prefix="snapshot-")
    for name in latest["files"]:
        bucket.blob(f"{latest['prefix']}/{name}").download_to_filename(os.path.join(target, name))
    return Snapshot(os.path.join(target, "*.parquet"), {k: latest[k] for k in ("date", "snapshotRoot")})


def make_server(snapshot, owners, audience, address, iap=verify_iap):
    attrs = {"snapshot": snapshot, "owners": {o.strip().lower() for o in owners if o.strip()}, "audience": audience,
             "iap": staticmethod(iap)}
    return ThreadingHTTPServer(address, type("Bound", (Handler,), attrs))


def main() -> int:
    try:
        owners, audience, bucket = os.environ["LC_SITE_OWNERS"].split(","), os.environ["LC_IAP_AUDIENCE"], os.environ["LC_SNAPSHOT_BUCKET"]
    except KeyError as exc:
        print(f"refusing to start: {exc} is not set", file=sys.stderr)
        return 1
    make_server(load_latest(bucket), owners, audience, ("0.0.0.0", int(os.environ.get("PORT", "8080")))).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
