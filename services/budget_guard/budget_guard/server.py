"""Receives Pub/Sub pushes. A failed write returns 500, so Pub/Sub tries again."""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .guard import decide, parse_push

MAX_BODY = 64 * 1024


class Conflict(Exception):
    """The state changed while we were deciding. Pub/Sub retries with a fresh read."""


class GcsState:
    def __init__(self, bucket: str, log_bucket: str):
        from google.cloud import storage

        client = storage.Client()
        self._blob = client.bucket(bucket).blob("state.json")
        self._log = client.bucket(log_bucket)

    def read(self):
        """Return (state, generation). The generation guards against a racing owner command."""
        self._blob.reload()
        generation = self._blob.generation
        return json.loads(self._blob.download_as_bytes(if_generation_match=generation)), generation

    def write(self, state, generation):
        from google.api_core.exceptions import PreconditionFailed

        try:
            self._blob.upload_from_string(json.dumps(state, indent=2, sort_keys=True), content_type="application/json",
                                          if_generation_match=generation)
        except PreconditionFailed as exc:
            raise Conflict() from exc

    def record(self, event):
        name = f"budget-guard/{int(time.time() * 1000)}.json"
        self._log.blob(name).upload_from_string(json.dumps(event), content_type="application/json", if_generation_match=0)


def handle(body, store, log):
    """Return the HTTP status for one push."""
    notice = parse_push(body)
    if notice is None:
        log({"event": "ignored", "reason": "not a budget notification"})
        return 204  # retrying won't help
    if notice["cost"] < notice["budget"]:
        return 204  # the normal case, several times a day: no need to read anything
    try:
        state, generation = store.read()
    except Exception as exc:
        log({"event": "error", "stage": "read", "error": type(exc).__name__})
        return 500
    new = decide(notice, state)
    if new is None:
        log({"event": "cap-reached", "action": "none", "state": state.get("state") if isinstance(state, dict) else None})
        return 204
    try:
        store.write(new, generation)
    except Exception as exc:
        log({"event": "error", "stage": "write", "error": type(exc).__name__})
        return 500
    event = {"actor": "service:budget-guard", "action": "pause", "subject": "control state", "decision": "paused",
             "reason": f"spend {notice['cost']:g} reached the cap of {notice['budget']:g} {notice['currency']}".strip(),
             "trigger": "budget-notification", "ts": time.time()}
    log({"event": "paused", **event})
    try:
        store.record(event)
    except Exception as exc:  # the pause has happened, and that's what matters
        log({"event": "error", "stage": "record", "error": type(exc).__name__})
    return 204


class Handler(BaseHTTPRequestHandler):
    store = None
    log_fn = None

    def log_message(self, *args):
        pass

    def _reply(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._reply(200 if self.path == "/health" else 404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._reply(400)
        if not 0 < length <= MAX_BODY:
            return self._reply(400)
        self._reply(handle(self.rfile.read(length), self.store, type(self).log_fn))


def _log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def main() -> int:
    handler = type("Bound", (Handler,), {"store": GcsState(os.environ["LC_CONTROL_BUCKET"], os.environ["LC_LOG_BUCKET"]),
                                         "log_fn": staticmethod(_log)})
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
