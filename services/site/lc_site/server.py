"""The private site and API.

Every request needs the owner's IAP-verified Google account. Nothing here is
public, and it stays that way until the owner approves going public.

Pages: search with filters and paging, entity pages with relationships, a
graph explorer drawn on the server as SVG (so the page needs no scripts), and
statistics. The JSON API mirrors them.
"""

from __future__ import annotations

import html
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from lc_core.iap import HEADER, verify_iap

HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; "
                                      "frame-ancestors 'none'",
           "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
STYLE = ("body{font:15px/1.5 system-ui,sans-serif;max-width:68em;margin:2em auto;padding:0 1em;color:#111}"
         "table{border-collapse:collapse}td,th{padding:.25em .6em;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}"
         ".note{color:#555;font-size:.9em}code{word-break:break-all}.facets{display:flex;gap:2em;flex-wrap:wrap}"
         ".facets ul{list-style:none;padding:0;margin:.3em 0}.facets li{margin:.1em 0}a{color:#0b57d0}"
         "svg a text{fill:#0b57d0}svg text{font:12px system-ui,sans-serif}")
FOOTER = ("<p class=note>Private test instance, visible only to the owner. lei-commons is an independent open "
          "directory, not affiliated with GLEIF, and does not issue LEIs.</p>")
PAGE_SIZE = 25


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def page(title, body):
    nav = ("<p><a href=/>Search</a> · <a href=/stats>Statistics</a> · <a href=/api/v1/stats>API</a></p>")
    return (f"<!DOCTYPE html><html lang=en><meta charset=utf-8><title>{esc(title)} · lei-commons</title>"
            f"<style>{STYLE}</style>{nav}<h1>{esc(title)}</h1>{body}{FOOTER}</html>").encode()


def table(rows, link=False):
    if not rows:
        return "<p>No results.</p>"
    cols = list(rows[0])
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    body = []
    for r in rows:
        cells = []
        for c in cols:
            if link and c == "directoryId" and r[c]:
                cells.append(f"<td><a href=/entity/{quote(str(r[c]))}>{esc(r[c])}</a></td>")
            else:
                cells.append(f"<td>{esc(r[c])}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><tr>{head}</tr>{''.join(body)}</table>"


def search_page(snap, q: dict) -> str:
    term = q.get("q", "")
    filters = {k: q.get(k, "") for k in ("country", "status", "source")}
    form = (f"<form><input name=q value=\"{esc(term)}\" size=48 placeholder=\"Name, LEI, directory ID or registration "
            f"number\" autofocus> <button>Search</button>"
            + "".join(f"<input type=hidden name={k} value=\"{esc(v)}\">" for k, v in filters.items() if v) + "</form>")
    meta = (f"<p class=note>Snapshot {esc(snap.meta.get('date'))}: {snap.count:,} records, "
            f"{snap.relationship_count:,} relationships.</p>")
    if not term:
        return form + meta
    try:
        number = int(q.get("page", "1"))
    except ValueError:
        number = 1
    found = snap.search(term, filters["country"], filters["status"], filters["source"], PAGE_SIZE, number)
    active = "".join(f" <b>{esc(k)}: {esc(v)}</b> (<a href=\"/?{esc(urlencode({**q, k: '', 'page': 1}))}\">remove</a>)"
                     for k, v in filters.items() if v)
    summary = f"<p>{found['total']:,} matches.{active}</p>"
    facets = []
    for name, rows in found["facets"].items():
        items = "".join(f"<li><a href=\"/?{esc(urlencode({**q, name: r['value'], 'page': 1}))}\">{esc(r['value'] or 'none')}"
                        f"</a> {r['records']:,}</li>" for r in rows)
        facets.append(f"<div><b>{esc(name.title())}</b><ul>{items}</ul></div>")
    pages = []
    if number > 1:
        pages.append(f"<a href=\"/?{esc(urlencode({**q, 'page': number - 1}))}\">previous</a>")
    if number * PAGE_SIZE < found["total"]:
        pages.append(f"<a href=\"/?{esc(urlencode({**q, 'page': number + 1}))}\">next</a>")
    nav = f"<p>Page {number} {' · '.join(pages)}</p>" if pages else ""
    return form + meta + summary + f"<div class=facets>{''.join(facets)}</div>" + table(found["results"], link=True) + nav


def relationship_rows(rel: dict) -> str:
    def rows(items):
        out = []
        for r in items:
            name = esc(r["otherName"] or r["otherLei"])
            link = f"<a href=/entity/{quote(r['otherId'])}>{name}</a>" if r["otherId"] else f"{name} (not in the directory)"
            pct = f" ({esc(r['percentage'])}%)" if r["percentage"] else ""
            out.append(f"<tr><td>{esc(r['role'])}</td><td>{link}{pct}</td><td>{esc(r['otherCountry'])}</td>"
                       f"<td>{esc(r['relationshipStatus'])}</td></tr>")
        return "".join(out)

    if not rel["up"] and not rel["down"]:
        return "<p>No relationships recorded.</p>"
    more = f"<p class=note>Showing {len(rel['down'])} of {rel['downTotal']:,} below.</p>" if rel["downTotal"] > len(rel["down"]) else ""
    return ("<table><tr><th>Relationship</th><th>Entity</th><th>Country</th><th>Status</th></tr>"
            + rows(rel["up"]) + rows(rel["down"]) + "</table>" + more)


def graph_svg(record: dict, rel: dict) -> str:
    """Parents above, the entity in the middle, children below. Nodes we hold are links."""
    width, box_w, box_h, gap = 1100, 200, 34, 12
    up, down = rel["up"][:12], rel["down"][:24]

    def row_positions(n, y):
        per_row = max(1, (width + gap) // (box_w + gap))
        out = []
        for i in range(n):
            r, c = divmod(i, per_row)
            in_row = min(per_row, n - r * per_row)
            x0 = (width - (in_row * box_w + (in_row - 1) * gap)) / 2
            out.append((x0 + c * (box_w + gap), y + r * (box_h + 40)))
        return out

    up_rows = (len(up) + 4) // 5
    centre_y = 20 + up_rows * (box_h + 40) + (30 if up else 0)
    centre = ((width - box_w) / 2, centre_y)
    up_pos = row_positions(len(up), 20)
    down_pos = row_positions(len(down), centre_y + box_h + 70)
    height = (down_pos[-1][1] + box_h + 30) if down else centre_y + box_h + 30

    def label(text, n=30):
        text = text or ""
        return esc(text if len(text) <= n else text[: n - 1] + "…")

    def node(x, y, name, sub, href=None, main=False):
        fill = "#e8f0fe" if main else ("#fff" if href else "#f3f3f3")
        inner = (f"<rect x={x:.0f} y={y:.0f} width={box_w} height={box_h} rx=5 fill=\"{fill}\" stroke=\"#999\"/>"
                 f"<text x={x + 8:.0f} y={y + 14:.0f}>{label(name)}</text>"
                 f"<text x={x + 8:.0f} y={y + 28:.0f} fill=\"#555\">{label(sub, 34)}</text>")
        return f"<a href=\"{esc(href)}\">{inner}</a>" if href else inner

    parts = []
    cx, cy = centre[0] + box_w / 2, centre[1]
    for (x, y), r in zip(up_pos, up):
        parts.append(f"<line x1={x + box_w / 2:.0f} y1={y + box_h:.0f} x2={cx:.0f} y2={cy:.0f} stroke=\"#bbb\"/>")
    for (x, y), r in zip(down_pos, down):
        parts.append(f"<line x1={x + box_w / 2:.0f} y1={y:.0f} x2={cx:.0f} y2={cy + box_h:.0f} stroke=\"#bbb\"/>")
    for (x, y), r in zip(up_pos, up):
        href = f"/graph/{quote(r['otherId'])}" if r["otherId"] else None
        parts.append(node(x, y, r["otherName"] or r["otherLei"], r["role"], href))
    parts.append(node(*centre, record.get("legalName"), record.get("country") or "", main=True))
    for (x, y), r in zip(down_pos, down):
        href = f"/graph/{quote(r['otherId'])}" if r["otherId"] else None
        parts.append(node(x, y, r["otherName"] or r["otherLei"], r["role"], href))
    return (f"<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"100%\" viewBox=\"0 0 {width} {height:.0f}\" "
            f"role=img aria-label=\"Relationship graph\">{''.join(parts)}</svg>")


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
        q = {k: v[0] for k, v in parse_qs(url.query).items() if k in ("q", "country", "status", "source", "page", "limit")}
        snap, path = self.snapshot, url.path

        def number(key, default):
            try:
                return int(q.get(key, default))
            except ValueError:
                return default

        if path == "/api/v1/search":
            return self._json(200, snap.search(q.get("q", ""), q.get("country", ""), q.get("status", ""), q.get("source", ""),
                                               number("limit", 25), number("page", 1)))
        if path.startswith("/api/v1/entities/"):
            rest = path[len("/api/v1/entities/"):]
            if rest.endswith("/relationships"):
                directory_id = rest[: -len("/relationships")]
                if not snap.entity(directory_id):
                    return self._json(404, {"error": "not found"})
                return self._json(200, snap.relationships(directory_id))
            record = snap.entity(rest)
            return self._json(200, record) if record else self._json(404, {"error": "not found"})
        if path == "/api/v1/stats":
            return self._json(200, snap.stats())
        if path == "/":
            return self._send(200, page("lei-commons", search_page(snap, q)))
        if path.startswith("/entity/") or path.startswith("/graph/"):
            directory_id = path.rsplit("/", 1)[1]
            record = snap.entity(directory_id)
            if not record:
                return self._send(404, page("Not found", "<p>No such record.</p>"))
            rel = snap.relationships(directory_id)
            links = f"<p><a href=/entity/{quote(directory_id)}>Record</a> · <a href=/graph/{quote(directory_id)}>Graph</a></p>"
            if path.startswith("/graph/"):
                body = links + graph_svg(record, rel) + relationship_rows(rel)
            else:
                body = (links + "<h2>Relationships</h2>" + relationship_rows(rel) + "<h2>Record</h2>"
                        f"<pre>{esc(json.dumps(record, indent=2, ensure_ascii=False))}</pre>")
            return self._send(200, page(record.get("legalName", "Record"), body))
        if path == "/stats":
            s = snap.stats()
            body = (f"<p>Snapshot {esc(s['snapshot'].get('date'))}, root <code>{esc(s['snapshot'].get('snapshotRoot'))}</code>: "
                    f"{s['records']:,} records and {s['relationships']:,} relationships.</p>"
                    f"<h2>By country</h2>{table(s['byCountry'])}<h2>By source</h2>{table(s['bySources'])}"
                    f"<h2>By status</h2>{table(s['byStatus'])}<h2>Relationship types</h2>{table(s['relationshipTypes'])}")
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
    paths = {"files": [], "relationships": []}
    for group in paths:
        for name in latest.get(group, []):
            local = os.path.join(target, name)
            bucket.blob(f"{latest['prefix']}/{name}").download_to_filename(local)
            paths[group].append(local)
    snap = Snapshot(paths["files"], {k: latest[k] for k in ("date", "snapshotRoot")}, paths["relationships"])
    for local in paths["files"] + paths["relationships"]:
        os.remove(local)  # loaded into the database; the disk is memory on Cloud Run
    return snap


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
