"""The private site: a single-page app and the JSON API it uses.

Every request needs the owner's IAP-verified Google account. Nothing here is
public, and it stays that way until the owner approves going public.

The admin area (owner decision 2026-11-21) is separate: the pages under /admin/ and the API under
/api/v1/admin/ (the System, Data sources, Trials, Log and Approvals pages, the second factor, every owner action). The load
balancer sends those paths to a backend of their own, where IAP asks for a recent sign-in with 2-step
verification (config/site.yaml admin). Admin requests are accepted only with that backend's IAP token
(LC_IAP_ADMIN_AUDIENCE), so the admin paths can't be reached through the ordinary backend. Until the
admin backend exists (the owner's bootstrap rerun), that variable is unset and admin requests are checked
like the rest, which the site logs at startup.

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

ADMIN_PAGES = ("/admin", "/admin/")
ADMIN_API = "/api/v1/admin/"
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
    admin_audience: str = ""
    second_factor = None
    approvals = None
    snapshot = None
    system = None
    decisions = None
    downloads = None
    actions = None
    dedi = None
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
        admin = is_admin(path)
        if self._owner(admin) is None:
            if path.startswith("/api/"):
                return self._json(403, {"error": "not allowed"})
            return self._plain(403, "This private instance is for the owner.")

        if path in ("/", "/index.html") or path in ADMIN_PAGES:
            return self._send(200, (STATIC / "index.html").read_bytes())
        if path.startswith(ADMIN_API):
            return self._admin_api(path)
        if admin:
            return self._plain(404, "Not found.")
        if path.startswith("/download/"):
            return self._download(path[len("/download/"):])
        if path == "/.well-known/dedi.index.json" or path.startswith(("/dedi/", "/data/")):
            return self._dedi(path)
        if path.startswith("/static/"):
            file = self.files.get(path)
            if file is None:
                return self._plain(404, "Not found.")
            return self._send(200, file.read_bytes(), TYPES[file.suffix])
        if not path.startswith("/api/"):
            return self._plain(404, "Not found.")
        return self._api(path, url.query)

    def _owner(self, admin: bool):
        """The owner's email if the IAP token is good for this path, else None. Admin paths take only the admin
        backend's token once it exists."""
        audience = self.admin_audience if admin and self.admin_audience else self.audience
        email = self.iap(self.headers.get(HEADER), audience)
        return email if email is not None and email in self.owners else None

    def _admin_api(self, path):
        if path == "/api/v1/admin/sources":
            if self.system is None:
                return self._json(503, {"error": "the sources view isn't configured"})
            return self._json(200, self.system.sources_page())
        if path == "/api/v1/admin/trials":
            # Sources on trial and their data's quality (owner, 2026-11-22): read before promoting one.
            if self.system is None:
                return self._json(503, {"error": "the trials view isn't configured"})
            return self._json(200, self.system.trials())
        if path == "/api/v1/admin/system":
            if self.system is None:
                return self._json(503, {"error": "the system view isn't configured"})
            meta = self.snapshot.meta if self.snapshot is not None else {}
            return self._json(200, self.system.collect({k: meta.get(k) for k in ("date", "snapshotRoot")}))
        if path == "/api/v1/admin/log" or path.startswith("/api/v1/admin/log/"):
            # The decision log is the admin area's (owner, 2026-11-22): every job's decisions, admin action,
            # merge, deploy and Terraform apply, a day at a time.
            if self.decisions is None:
                return self._json(503, {"error": "the decision log isn't configured"})
            if path == "/api/v1/admin/log":
                return self._json(200, self.decisions.days())
            day = self.decisions.day(path[len("/api/v1/admin/log/"):])
            return self._json(200, day) if day else self._json(404, {"error": "not a day"})
        if path == "/api/v1/admin/approvals":
            if self.approvals is None:
                return self._json(503, {"error": "approvals aren't configured"})
            try:
                return self._json(200, self.approvals.pending())
            except Exception as exc:
                return self._json(502, {"error": f"GitHub couldn't be read right now ({type(exc).__name__})"})
        if path == "/api/v1/admin/second-factor":
            if self.second_factor is None:
                return self._json(503, {"error": "the second factor isn't configured"})
            try:
                return self._json(200, self.second_factor.view(self._owner(True)))
            except Exception as exc:
                return self._json(503, {"error": f"the key can't be read right now ({type(exc).__name__})"})
        return self._json(404, {"error": "not found"})

    def _download(self, rest):
        from .downloads import CHUNK, content_type

        if self.downloads is None:
            return self._plain(503, "Downloads aren't configured.")
        date, _, name = rest.partition("/")
        blob = self.downloads.blob(date, name)
        if blob is None:
            return self._plain(404, "Not found.")
        self.close_connection = True
        self.send_response(200)
        for k, v in HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", content_type(name))
        self.send_header("Content-Disposition", f'attachment; filename="lei-commons-{date}-{name}"')
        self.send_header("Transfer-Encoding", "chunked")  # Cloud Run allows large responses only when chunked
        self.send_header("Connection", "close")
        self.end_headers()
        with blob.open("rb") as f:
            for block in iter(lambda: f.read(CHUNK), b""):
                self.wfile.write(f"{len(block):x}\r\n".encode() + block + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def _dedi(self, path):
        """A DeDi file, or a snapshot file it points to (lc_site.dedi_files), streamed from the bucket."""
        from .dedi_files import headers_for

        if self.dedi is None:
            return self._plain(503, "DeDi files aren't configured.")
        obj, blob = self.dedi.blob(path)
        if blob is None:
            return self._plain(404, "Not found.")
        self.close_connection = True
        self.send_response(200)
        for k, v in (HEADERS | headers_for(obj)).items():
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        with blob.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                self.wfile.write(f"{len(block):x}\r\n".encode() + block + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def _api(self, path, query):
        if path == "/api/v1/dedi":
            if self.dedi is None:
                return self._json(503, {"error": "DeDi files aren't configured"})
            return self._json(200, self.dedi.status())
        if path == "/api/v1/source-names":
            return self._json(200, source_names())
        if path == "/api/v1/snapshots":
            if self.downloads is None:
                return self._json(503, {"error": "downloads aren't configured"})
            return self._json(200, self.downloads.snapshots())
        if path.startswith("/api/v1/snapshots/") and path.endswith("/recipe"):
            if self.downloads is None:
                return self._json(503, {"error": "downloads aren't configured"})
            return self._json(*self.downloads.recipe(path[len("/api/v1/snapshots/"):-len("/recipe")]))
        if path == "/api/v1/me":  # which tabs to show: the admin area's only to an admin
            return self._json(200, {"admin": self._owner(False) is not None})
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
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        """Only the admin area's changes: owner actions (lc_site.actions) and second-factor steps
        (lc_site.second_factor). IAP first (the admin backend's token), then same-origin JSON and a fresh code."""
        path = urlsplit(self.path).path
        handlers = {"/api/v1/admin/act": self.actions, "/api/v1/admin/second-factor": self.second_factor,
                    "/api/v1/admin/approvals": self.approvals}
        if path not in handlers:
            return self._send(405, b"")
        email = self._owner(True)
        if email is None:
            return self._json(403, {"ok": False, "message": "not allowed"})
        handler = handlers[path]
        if handler is None:
            return self._json(503, {"ok": False, "message": "This isn't configured on this site."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        from .actions import MAX_BODY

        body = self.rfile.read(length) if 0 < length <= MAX_BODY else b""
        if handler is self.second_factor:
            outcome, extra = self.second_factor.handle(email, self.headers, body)
        else:
            outcome, extra = handler.handle(email, self.headers, body), {}
        return self._json(outcome.status, {"ok": outcome.ok, "message": outcome.message, **extra})

    def do_HEAD(self):
        return self._send(405, b"")

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = do_HEAD


def source_names() -> dict:
    """{id records carry: the name shown}, from the sources registry."""
    from lc_core.sources import names

    return names(os.environ.get("LC_SOURCES_CONFIG"))


def load_latest(bucket_name: str):
    """Download the latest snapshot's files and load them. Returns None if there isn't one.

    A v2 day is its full snapshot's parts plus each later day's changes (docs/snapshots-v2.md);
    every file is checked against the SHA-256 in its manifest first."""
    from google.cloud import storage

    bucket = storage.Client().bucket(bucket_name)
    pointer = bucket.blob("latest.json")
    if not pointer.exists():
        return None
    return load_from(bucket, json.loads(pointer.download_as_bytes()))


def load_from(bucket, latest: dict):
    import hashlib

    from lc_core.snapshot_format import from_anchor

    from .data import Snapshot

    target = tempfile.mkdtemp(prefix="snapshot-")
    downloaded = []

    def fetch(obj, sha256=None):
        local = os.path.join(target, obj.replace("/", "_"))
        bucket.blob(obj).download_to_filename(local)
        downloaded.append(local)
        if sha256:
            with open(local, "rb") as f:
                if "sha256:" + hashlib.sha256(f.read()).hexdigest() != sha256:
                    raise ValueError(f"{obj} doesn't match its manifest")
        return local

    def read_manifest(date):
        blob = bucket.blob(f"{date}/manifest.json")
        return json.loads(blob.download_as_bytes()) if blob.exists() else None

    try:
        if latest.get("schemaVersion", 1) >= 2:
            manifest = read_manifest(latest["date"])
            chain = from_anchor(read_manifest, manifest)
            anchor, steps = chain[0], chain[1:]
            files = [fetch(f["object"], f["sha256"]) for f in anchor["files"] if f["kind"] == "entities"]
            rels = [fetch(f["object"], f["sha256"]) for f in anchor["files"] if f["kind"] == "relationships"]
            changes = [([fetch(f["object"], f["sha256"]) for f in m["files"] if f["kind"] == "changes"],
                        [fetch(f["object"], f["sha256"]) for f in m["files"] if f["kind"] == "relationship-changes"])
                       for m in steps]
            return Snapshot(files, {k: manifest[k] for k in ("date", "snapshotRoot")}, rels, changes=changes,
                            sources=manifest.get("sources") or ())
        files = [fetch(f"{latest['prefix']}/{name}") for name in latest.get("files", [])]
        rels = [fetch(f"{latest['prefix']}/{name}") for name in latest.get("relationships", [])]
        return Snapshot(files, {k: latest[k] for k in ("date", "snapshotRoot")}, rels)
    finally:
        for local in downloaded:  # loaded into the database; the disk is memory on Cloud Run
            if os.path.exists(local):
                os.remove(local)


def is_admin(path: str) -> bool:
    """Whether a path belongs to the admin area (config/site.yaml admin.paths)."""
    return path in ADMIN_PAGES or path.startswith("/admin/") or path.startswith(ADMIN_API)


def make_server(snapshot, owners, audience, address, iap=verify_iap, system=None, decisions=None, downloads=None,
                actions=None, dedi=None, admin_audience="", second_factor=None, approvals=None):
    attrs = {"snapshot": snapshot, "owners": {o.strip().lower() for o in owners if o.strip()}, "audience": audience,
             "admin_audience": admin_audience, "second_factor": second_factor, "approvals": approvals,
             "iap": staticmethod(iap), "system": system, "decisions": decisions, "downloads": downloads,
             "actions": actions, "dedi": dedi, "files": static_files()}
    return ThreadingHTTPServer(address, type("Bound", (Handler,), attrs))


def main() -> int:
    try:
        owners, audience, bucket = os.environ["LC_SITE_OWNERS"].split(","), os.environ["LC_IAP_AUDIENCE"], os.environ["LC_SNAPSHOT_BUCKET"]
    except KeyError as exc:
        print(f"refusing to start: {exc} is not set", file=sys.stderr)
        return 1
    system = decisions = downloads = dedi = None
    try:
        from google.cloud import storage

        from .decision_log import DecisionLogReader
        from .dedi_files import DediFiles
        from .downloads import SnapshotFiles

        client = storage.Client()
        decisions = DecisionLogReader(client.bucket(f"{os.environ['GCP_PROJECT_ID']}-decision-log"))
        downloads = SnapshotFiles(client.bucket(bucket))
        dedi = DediFiles(client.bucket(bucket))
    except Exception as exc:  # the rest of the site works without it
        print(json.dumps({"event": "decision-log-unavailable", "error": type(exc).__name__}), flush=True)
    try:
        from .system import GoogleReader, SystemView

        project = os.environ["GCP_PROJECT_ID"]
        buckets = {"data": f"{project}-data", "control": f"{project}-control", "decision_log": f"{project}-decision-log"}
        system = SystemView(GoogleReader(project, os.environ.get("GCP_REGION", "us-central1"), buckets))
    except Exception as exc:  # the rest of the site works without it
        print(json.dumps({"event": "system-view-unavailable", "error": type(exc).__name__}), flush=True)
    actions = second = approvals = None
    try:
        actions = _owner_actions(system)
        second = _second_factor(actions)
    except Exception as exc:  # the rest of the site works without it; actions answer 503
        print(json.dumps({"event": "owner-actions-unavailable", "error": type(exc).__name__}), flush=True)
    try:
        approvals = _approvals(actions) if actions else None
    except Exception as exc:  # the Approvals page answers 503; everything else works
        print(json.dumps({"event": "approvals-unavailable", "error": type(exc).__name__}), flush=True)
    admin_audience = os.environ.get("LC_IAP_ADMIN_AUDIENCE", "")
    if not admin_audience:  # before the admin backend exists: admin paths are checked like the rest
        print(json.dumps({"event": "admin-without-reauth", "why": "LC_IAP_ADMIN_AUDIENCE is not set"}), flush=True)
    make_server(choose_directory(bucket), owners, audience, ("0.0.0.0", int(os.environ.get("PORT", "8080"))),
                system=system, decisions=decisions, downloads=downloads, actions=actions, dedi=dedi,
                admin_audience=admin_audience, second_factor=second, approvals=approvals).serve_forever()
    return 0


def _owner_actions(system):
    from google.cloud import secretmanager, storage
    from lc_core.approvals import registry
    from lc_core.owner import GoogleBackend
    from lc_core.sources import pipelines
    from lc_core.totp import GcsSteps, Verifier

    from .actions import OwnerActions

    project, region = os.environ["GCP_PROJECT_ID"], os.environ.get("GCP_REGION", "us-central1")
    secrets = secretmanager.SecretManagerServiceClient()

    def read_secret():
        name = f"projects/{project}/secrets/console-totp/versions/latest"
        return secrets.access_secret_version(name=name).payload.data.decode("ascii")

    path = os.environ.get("LC_SOURCES_CONFIG")
    return OwnerActions(backend=GoogleBackend(project, region),
                        verifier=Verifier(GcsSteps(storage.Client().bucket(f"{project}-control"))),
                        read_secret=read_secret, sources=pipelines(path) if path else pipelines(),
                        registry=registry(path), on_change=(system.invalidate if system else None))


class SecretStore:
    """The second-factor key in Secret Manager (secret console-totp): read the latest version, add a new one."""

    def __init__(self, project: str, secret_id: str = "console-totp"):
        from google.cloud import secretmanager

        self._client = secretmanager.SecretManagerServiceClient()
        self._parent = f"projects/{project}/secrets/{secret_id}"

    def get(self):
        from google.api_core.exceptions import FailedPrecondition, NotFound

        try:
            response = self._client.access_secret_version(name=f"{self._parent}/versions/latest")
        except (NotFound, FailedPrecondition):
            return None
        return response.payload.data.decode("ascii")

    def put(self, value: str) -> None:
        self._client.add_secret_version(parent=self._parent, payload={"data": value.encode("ascii")})


def _second_factor(actions):
    from .second_factor import SecondFactor

    return SecondFactor(store=SecretStore(os.environ["GCP_PROJECT_ID"]), verifier=actions.verifier,
                        record=actions.backend.record)


def _approvals(actions):
    """The Approvals page: GitHub as the guardian App (config/approvals.yaml), the same code check as actions."""
    from lc_core.approval_rules import load

    from .approvals import Approvals, GitHubApp

    rules = load()
    key = SecretStore(os.environ["GCP_PROJECT_ID"], rules.key_secret)
    domain = os.environ.get("LC_SITE_DOMAIN", "")
    return Approvals(github=GitHubApp(rules.app_id, rules.installation_id, key.get), rules=rules,
                     verifier=actions.verifier, read_secret=actions.secret, record=actions.backend.record,
                     admin_url=f"https://{domain}/admin/#/approvals" if domain else "")


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
