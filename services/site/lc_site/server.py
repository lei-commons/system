"""The private site: a single-page app and the JSON API it uses.

Every request needs the owner's IAP-verified Google account. Nothing here is
public, and it stays that way until the owner approves going public.

The app is plain JavaScript modules served from this service. The content
policy lets the page run only this service's own scripts and styles and talk
only to this service, so nothing loads from anywhere else.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from lc_core.iap import HEADER, verify_iap

STATIC = Path(__file__).resolve().parent / "static"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
       "font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
HEADERS = {"Content-Security-Policy": CSP, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
           "Referrer-Policy": "no-referrer", "Cross-Origin-Opener-Policy": "same-origin"}
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".svg": "image/svg+xml", ".json": "application/json", ".txt": "text/plain; charset=utf-8",
         ".md": "text/plain; charset=utf-8"}
PLAIN = ("<!DOCTYPE html><html lang=en><meta charset=utf-8><title>lei-commons</title>"
         "<body><p>{}</p></body></html>")


def static_files() -> dict:
    """Every file the app may serve, by URL path, fixed at startup. Nothing else is reachable."""
    files = {}
    for path in STATIC.rglob("*"):
        if path.is_file() and path.suffix in TYPES:
            files["/static/" + path.relative_to(STATIC).as_posix()] = path
    return files


class Handler(BaseHTTPRequestHandler):
    server_version = "lei-commons"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    owners: set = set()
    audience: str = ""
    snapshot = None
    system = None
    decisions = None
    files: dict = {}
    iap = staticmethod(verify_iap)

    def log_message(self, *args):
        pass

    def _send(self, status, body: bytes, content_type="text/html; charset=utf-8"):
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
        return self._send(status, json.dumps(obj, default=str).encode(), "application/json")

    def _plain(self, status, message):
        return self._send(status, PLAIN.format(message).encode())

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path
        if path == "/health":
            return self._send(200, b"ok\n", "text/plain")
        email = self.iap(self.headers.get(HEADER), self.audience)
        if email is None or email not in self.owners:
            if path.startswith("/api/"):
                return self._json(403, {"error": "not allowed"})
            return self._plain(403, "This private instance is for the owner.")

        if path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes())
        if path.startswith("/static/"):
            file = self.files.get(path)
            if file is None:
                return self._plain(404, "Not found.")
            return self._send(200, file.read_bytes(), TYPES[file.suffix])
        if not path.startswith("/api/"):
            return self._plain(404, "Not found.")
        return self._api(path, url.query)

    def _api(self, path, query):
        if path == "/api/v1/log" or path.startswith("/api/v1/log/"):
            if self.decisions is None:
                return self._json(503, {"error": "the decision log isn't configured"})
            if path == "/api/v1/log":
                return self._json(200, self.decisions.days())
            day = self.decisions.day(path[len("/api/v1/log/"):])
            return self._json(200, day) if day else self._json(404, {"error": "not a day"})
        snap = self.snapshot
        if snap is None:
            return self._json(503, {"error": "no snapshot yet"})
        q = {k: v[0] for k, v in parse_qs(query).items()}

        def number(key, default):
            try:
                return int(q.get(key, default))
            except ValueError:
                return default

        if path == "/api/v1/meta":
            return self._json(200, {"snapshot": snap.meta, "records": snap.count, "relationships": snap.relationship_count})
        if path == "/api/v1/suggest":
            return self._json(200, {"results": snap.suggest(q.get("q", ""))})
        if path == "/api/v1/search":
            return self._json(200, snap.search(q.get("q", ""), q.get("country", ""), q.get("status", ""), q.get("source", ""),
                                               number("limit", 25), number("page", 1)))
        if path.startswith("/api/v1/graph/"):
            graph = snap.graph(path[len("/api/v1/graph/"):], depth=number("depth", 1))
            return self._json(200, graph) if graph else self._json(404, {"error": "not found"})
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
        if path == "/api/v1/system":
            if self.system is None:
                return self._json(503, {"error": "the system view isn't configured"})
            return self._json(200, self.system.collect({k: snap.meta.get(k) for k in ("date", "snapshotRoot")}))
        return self._json(404, {"error": "not found"})

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


def make_server(snapshot, owners, audience, address, iap=verify_iap, system=None, decisions=None):
    attrs = {"snapshot": snapshot, "owners": {o.strip().lower() for o in owners if o.strip()}, "audience": audience,
             "iap": staticmethod(iap), "system": system, "decisions": decisions, "files": static_files()}
    return ThreadingHTTPServer(address, type("Bound", (Handler,), attrs))


def main() -> int:
    try:
        owners, audience, bucket = os.environ["LC_SITE_OWNERS"].split(","), os.environ["LC_IAP_AUDIENCE"], os.environ["LC_SNAPSHOT_BUCKET"]
    except KeyError as exc:
        print(f"refusing to start: {exc} is not set", file=sys.stderr)
        return 1
    system = decisions = None
    try:
        from google.cloud import storage

        from .decision_log import DecisionLogReader

        decisions = DecisionLogReader(storage.Client().bucket(f"{os.environ['GCP_PROJECT_ID']}-decision-log"))
    except Exception as exc:  # the rest of the site works without it
        print(json.dumps({"event": "decision-log-unavailable", "error": type(exc).__name__}), flush=True)
    try:
        from .system import GoogleReader, SystemView

        project = os.environ["GCP_PROJECT_ID"]
        buckets = {"data": f"{project}-data", "control": f"{project}-control", "decision_log": f"{project}-decision-log"}
        system = SystemView(GoogleReader(project, os.environ.get("GCP_REGION", "us-central1"), buckets))
    except Exception as exc:  # the rest of the site works without it
        print(json.dumps({"event": "system-view-unavailable", "error": type(exc).__name__}), flush=True)
    make_server(choose_directory(bucket), owners, audience, ("0.0.0.0", int(os.environ.get("PORT", "8080"))),
                system=system, decisions=decisions).serve_forever()
    return 0


def choose_directory(bucket, open_database=None, load_snapshot=None):
    """The serving database if it is configured and loaded; otherwise the snapshot in memory.

    The database can be empty, before its first load, or unreachable. Either
    way the site keeps working from the snapshot instead of failing to start.
    """
    load_snapshot = load_snapshot or (lambda: load_latest(bucket))
    if not os.environ.get("LC_DB_HOST"):
        return load_snapshot()
    try:
        directory = (open_database or _open_database)()
        if directory.count > 0:
            print(json.dumps({"event": "serving-from-database", "records": directory.count}), flush=True)
            return directory
        print(json.dumps({"event": "database-empty", "fallback": "snapshot in memory"}), flush=True)
    except Exception as exc:
        print(json.dumps({"event": "database-unavailable", "error": type(exc).__name__, "fallback": "snapshot in memory"}),
              flush=True)
    return load_snapshot()


def _open_database():
    from lc_directory.pg import Directory, token_pool

    return Directory(token_pool(os.environ["LC_DB_HOST"], os.environ.get("LC_DB_NAME", "lei_commons"), os.environ["LC_DB_USER"]))



if __name__ == "__main__":
    sys.exit(main())
