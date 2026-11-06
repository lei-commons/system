"""The console's HTTP front end.

M1 is read-only: it shows the system state and has no commands. The owner's
commands run from the `control` repository until the lei-commons-control App
exists, so a bug here can't change anything.
"""

from __future__ import annotations

import html
import json
import os
import sys
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from lc_core.control import load_state

from .auth import SESSION_SECONDS, Sessions, verify_iap
from .store import SecretManagerStore
from .totp import Verifier, new_secret, otpauth_uri

COOKIE = "__Host-lc-console"
MAX_FORM = 1024
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
STYLE = "body{font:16px/1.5 system-ui,sans-serif;max-width:40em;margin:3em auto;padding:0 1em}code{word-break:break-all}"


def page(title, body):
    return (f"<!DOCTYPE html><html lang=en><meta charset=utf-8><title>{html.escape(title)} · lei-commons</title>"
            f"<style>{STYLE}</style><h1>{html.escape(title)}</h1>{body}</html>").encode()


CODE_FORM = ("<form method=post action={action}><label>Code from your authenticator app "
             "<input name=code inputmode=numeric autocomplete=one-time-code required autofocus></label> "
             "<button>Continue</button></form>")


class App:
    def __init__(self, *, owners, audience, store, read_state=load_state, sessions=None, verifier=None, iap=verify_iap, log=None):
        self.owners = {o.strip().lower() for o in owners if o.strip()}
        self.audience = audience
        self.store = store
        self.read_state = read_state
        self.sessions = sessions or Sessions()
        self.verifier = verifier or Verifier()
        self.iap = iap
        self.log = log or (lambda record: print(json.dumps(record, sort_keys=True), flush=True))
        self.pending = None   # secret shown at enrolment, until a code proves it works
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "lei-commons-console"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: App = None

    def log_message(self, *args):
        pass

    def _send(self, status, body, cookie=None, location=None):
        self.close_connection = True
        self.send_response(status)
        for k, v in HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if location:
            self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", f"{COOKIE}={cookie}; Path=/; Max-Age={SESSION_SECONDS}; Secure; HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(body)

    def _audit(self, email, event, ok):
        self.app.log({"actor": email or "unknown", "event": event, "ok": ok, "path": self.path.split("?", 1)[0]})

    def _owner(self):
        """Run the first factor. Returns the owner's email, or answers 403 and returns None."""
        email = self.app.iap(self.headers.get("X-Goog-IAP-JWT-Assertion"), self.app.audience)
        if email is None or email not in self.app.owners:
            self._audit(email, "refused", False)
            self._send(403, page("Not allowed", "<p>This page is for the owner.</p>"))
            return None
        return email

    def _session_ok(self, email):
        try:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        morsel = jar.get(COOKIE)
        return self.app.sessions.valid(morsel.value if morsel else None, email)

    def _code(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return ""
        if not 0 < length <= MAX_FORM:
            return ""
        return parse_qs(self.rfile.read(length).decode("ascii", "replace")).get("code", [""])[0].strip()

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, b"ok\n")
        email = self._owner()
        if email is None:
            return None
        secret = self._load_secret()
        if secret is False:
            return None
        if secret is None:
            return self._enrol_page(email)
        if not self._session_ok(email):
            return self._send(200, page("Second factor", CODE_FORM.format(action="/verify")))
        if self.path != "/":
            return self._send(404, page("Not found", ""))
        return self._send(200, self._status_page())

    def do_POST(self):
        email = self._owner()
        if email is None:
            return None
        code = self._code()
        secret = self._load_secret()
        if secret is False:
            return None
        if self.path == "/verify" and secret:
            ok = self.app.verifier.verify(secret, code)
            self._audit(email, "second-factor", ok)
            if ok:
                return self._send(303, b"", cookie=self.app.sessions.issue(email), location="/")
            message = "Locked for five minutes after repeated failures." if self.app.verifier.locked() else "That code didn't work."
            return self._send(403, page("Second factor", f"<p>{message}</p>" + CODE_FORM.format(action="/verify")))
        if self.path == "/enrol" and secret is None:
            with self.app.lock:
                pending = self.app.pending
            if pending and self.app.verifier.verify(pending, code):
                self.app.store.put(pending)
                with self.app.lock:
                    self.app.pending = None
                self._audit(email, "enrolled", True)
                return self._send(303, b"", cookie=self.app.sessions.issue(email), location="/")
            self._audit(email, "enrolled", False)
            return self._send(403, page("Set up your authenticator", "<p>That code didn't work. Reload to try again.</p>"))
        return self._send(404, page("Not found", ""))

    def do_HEAD(self):
        return self._send(405, b"")

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = do_HEAD

    def _load_secret(self):
        """Return the secret, None if nobody has enrolled, or False after answering 503."""
        try:
            return self.app.store.get()
        except Exception as exc:
            self.app.log({"event": "secret-store-error", "ok": False, "error": type(exc).__name__})
            self._send(503, page("Unavailable", "<p>The console can't read its second-factor secret.</p>"))
            return False

    def _enrol_page(self, email):
        with self.app.lock:
            if self.app.pending is None:
                self.app.pending = new_secret()
            pending = self.app.pending
        body = (f"<p>Add this key to your authenticator app, then enter the code it shows.</p><p><code>{pending}</code></p>"
                f"<p><code>{html.escape(otpauth_uri(pending, email))}</code></p>" + CODE_FORM.format(action="/enrol"))
        return self._send(200, page("Set up your authenticator", body))

    def _status_page(self):
        try:
            st = self.app.read_state()
        except Exception as exc:
            return page("System status", f"<p><strong>Control state unreadable</strong> ({html.escape(type(exc).__name__)}). "
                                         "Agents fail closed while this is true.</p>")
        rows = [("State", st.state), ("Paused components", ", ".join(st.paused_components) or "none"),
                ("Monthly budget (USD)", "not set" if st.monthly_budget_usd is None else f"{st.monthly_budget_usd:g}")]
        table = "".join(f"<tr><th align=left>{html.escape(k)}<td>{html.escape(v)}" for k, v in rows)
        return page("System status", f"<table>{table}</table><p>Commands run from the "
                                     "<a href=https://github.com/lei-commons/control/actions>control repository</a>.</p>")


def make_server(app: App, address) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(address, type("BoundHandler", (Handler,), {"app": app}))


def main() -> int:
    try:
        app = App(owners=os.environ["LC_CONSOLE_OWNERS"].split(","), audience=os.environ["LC_IAP_AUDIENCE"],
                  store=SecretManagerStore(os.environ["GCP_PROJECT_ID"]))
    except KeyError as exc:
        print(f"refusing to start: {exc} is not set", file=sys.stderr)
        return 1
    make_server(app, ("0.0.0.0", int(os.environ.get("PORT", "8080")))).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
