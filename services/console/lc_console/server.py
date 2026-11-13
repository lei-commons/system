"""The console's HTTP front end.

It shows the system state and performs the owner's actions (lc_core.actions):
pause, resume, stop, restart, manual mode, pausing one job, running a job now,
and approving data sources. The site takes the same actions inline on its own
pages; both run lc_core.owner, and a code works once across both.

Every action needs both factors at the moment it is taken: the owner's IAP
login and a fresh code from the authenticator, entered on the confirmation
page. A code works once, so a form posted from anywhere else can't act; the
Origin header is checked too. Every attempt, allowed or refused, is audited,
and every applied action is recorded in the decision log. The control state is
written only if it hasn't changed since it was read. Budget changes stay in the
`control` repository.
"""

from __future__ import annotations

import html
import json
import re
import os
import sys
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from datetime import datetime, timezone

from lc_core.actions import SOURCE_ACTIONS, catalogue, known, source_catalogue
from lc_core.approvals import effective
from lc_core.control import load_state

from .auth import SESSION_SECONDS, Sessions, verify_iap
from .store import SecretManagerStore
from .totp import Verifier, new_secret, otpauth_uri

COOKIE = "__Host-lc-console"
# A key waiting for its first good code. It is stored, not held in memory, so a
# restart can't swap the key after the owner has added it to their app.
PENDING = "pending:"
MAX_FORM = 4096
MAX_REASON = 300
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
STYLE = "body{font:16px/1.5 system-ui,sans-serif;max-width:40em;margin:3em auto;padding:0 1em}code{word-break:break-all}"


def page(title, body):
    return (f"<!DOCTYPE html><html lang=en><meta charset=utf-8><title>{html.escape(title)} · lei-commons</title>"
            f"<style>{STYLE}</style><h1>{html.escape(title)}</h1>{body}</html>").encode()


def qr_image(uri):
    """An inline QR code. Setup still works from the typed key if this fails."""
    try:
        import segno

        return f'<p><img alt="QR code" width=240 height=240 src="{segno.make(uri, error="m").svg_data_uri(scale=6, border=2)}"></p>'
    except Exception:
        return ""


CODE_FORM = ("<form method=post action={action}><label>Code from your authenticator app "
             "<input name=code inputmode=numeric autocomplete=one-time-code required autofocus></label> "
             "<button>Continue</button></form>")


class App:
    def __init__(self, *, owners, audience, store, read_state=load_state, sessions=None, verifier=None, iap=verify_iap, log=None,
                 backend=None, sources=(), site_url="", clock=None, registry=()):
        self.owners = {o.strip().lower() for o in owners if o.strip()}
        self.audience = audience
        self.store = store
        self.read_state = read_state
        self.sessions = sessions or Sessions()
        self.verifier = verifier or Verifier()
        self.iap = iap
        self.log = log or (lambda record: print(json.dumps(record, sort_keys=True), flush=True))
        self.lock = threading.Lock()
        self.backend, self.sources, self.site_url = backend, list(sources), site_url.rstrip("/")
        self.registry = list(registry)
        self.clock = clock or (lambda: datetime.now(timezone.utc))


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

    def _form(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return {}
        if not 0 < length <= MAX_FORM:
            return {}
        return {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()}

    def _code(self, form=None):
        return (form if form is not None else self._form()).get("code", "").strip()

    def _same_origin(self) -> bool:
        """Browsers say where a request came from in Sec-Fetch-Site. Origin alone isn't enough: this page's
        no-referrer policy makes browsers send "Origin: null" on its own form posts (the 2026-11-13 bug)."""
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None:
            return site == "same-origin"
        origin = self.headers.get("Origin")
        return origin is None or origin == f"https://{self.headers.get('Host', '')}"

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, b"ok\n")
        email = self._owner()
        if email is None:
            return None
        loaded = self._load()
        if loaded is None:
            return None
        secret, pending = loaded
        if secret is None:
            return self._enrol_page(email, pending)
        path, _, query = self.path.partition("?")
        if path == "/act":  # the confirmation asks for a fresh code itself, so no session is needed to see it
            params = parse_qs(query)
            return self._confirm_page(params.get("do", [""])[0], back=params.get("back", [""])[0])
        if not self._session_ok(email):
            return self._send(200, page("Second factor", CODE_FORM.format(action="/verify")))
        if path != "/":
            return self._send(404, page("Not found", ""))
        return self._send(200, self._status_page())

    def do_POST(self):
        email = self._owner()
        if email is None:
            return None
        form = self._form()
        code = self._code(form)
        loaded = self._load()
        if loaded is None:
            return None
        secret, pending = loaded
        if self.path == "/verify" and secret:
            ok = self.app.verifier.verify(secret, code)
            self._audit(email, "second-factor", ok)
            if ok:
                return self._send(303, b"", cookie=self.app.sessions.issue(email), location="/")
            return self._send(403, page("Second factor", f"<p>{self._failure()}</p>" + CODE_FORM.format(action="/verify")))
        if self.path == "/act" and secret:
            return self._act(email, secret, form)
        if self.path == "/enrol" and secret is None:
            ok = bool(pending) and self.app.verifier.verify(pending, code)
            self._audit(email, "enrolled", ok)
            if ok:
                try:
                    self.app.store.put(pending)
                except Exception as exc:
                    return self._unavailable(exc)
                return self._send(303, b"", cookie=self.app.sessions.issue(email), location="/")
            return self._enrol_page(email, pending, status=403, note=self._failure())
        return self._send(404, page("Not found", ""))

    def do_HEAD(self):
        return self._send(405, b"")

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = do_HEAD

    def _failure(self):
        if self.app.verifier.locked():
            return "Too many wrong codes. Wait five minutes before trying again, even with the right code."
        return "That code didn't work. Enter the code your app shows now."

    def _unavailable(self, exc):
        self.app.log({"event": "secret-store-error", "ok": False, "error": type(exc).__name__})
        return self._send(503, page("Unavailable", "<p>The console can't reach its second-factor secret.</p>"))

    def _load(self):
        """Return (secret, pending key), or None after answering 503. Creates a key if there is none."""
        try:
            with self.app.lock:
                value = self.app.store.get()
                if value is None:
                    value = PENDING + new_secret()
                    self.app.store.put(value)
        except Exception as exc:
            self._unavailable(exc)
            return None
        if value.startswith(PENDING):
            return None, value[len(PENDING):]
        return value, None

    def _enrol_page(self, email, pending, status=200, note=""):
        uri = otpauth_uri(pending, email)
        message = f"<p><strong>{html.escape(note)}</strong></p>" if note else ""
        body = (f"{message}<p>Scan this with your authenticator app, then enter the code it shows. "
                f"This key stays the same if you reload.</p>{qr_image(uri)}"
                f"<p>Or enter this key by hand, as a time-based key:</p><p><code>{pending}</code></p>"
                + CODE_FORM.format(action="/enrol"))
        return self._send(status, page("Set up your authenticator", body))

    def _describe(self, action: str):
        if self.app.backend is None:
            return None
        if action.partition(":")[0] in SOURCE_ACTIONS:
            decisions, _ = self.app.backend.read_approvals()
            offered = source_catalogue(self.app.registry, effective(self.app.registry, decisions))
        else:
            state, _ = self.app.backend.read_state()
            offered = catalogue(self.app.sources, state)
        return next((a for a in offered if a["id"] == action), None)

    def _confirm_page(self, action, status=200, note="", back=""):
        if not known(self.app.sources, action, self.app.registry) or self.app.backend is None:
            return self._send(404, page("No such action", "<p>This action isn't one the console offers.</p>"))
        a = self._describe(action)
        if a is None:
            return self._send(409, page("Not available now", "<p>That action doesn't apply to the system's current state.</p>"
                                        + self._back(back)))
        message = f"<p><strong>{html.escape(note)}</strong></p>" if note else ""
        body = (f"{message}<p>{html.escape(a['group'])}: <strong>{html.escape(a['label'])}</strong></p>"
                f"<p>{html.escape(a['help'])}</p>"
                f"<form method=post action=/act><input type=hidden name=do value=\"{html.escape(action, quote=True)}\">"
                f"<input type=hidden name=back value=\"{html.escape(_site(back) or '', quote=True)}\">"
                "<p><label>Why (for the decision log) <input name=reason maxlength=300 required size=40></label></p>"
                "<p><label>Code from your authenticator app "
                "<input name=code inputmode=numeric autocomplete=one-time-code required></label></p>"
                "<p>A code works once. If you have just signed in, use the next one.</p>"
                f"<button>{html.escape(a['label'])}</button></form>{self._back(back)}")
        return self._send(status, page("Confirm", body))

    def _back(self, back=""):
        site = _site(back) or self.app.site_url
        return f'<p><a href="{html.escape(site, quote=True)}/#/system">Back to the System page</a></p>' if site else ""

    def _act(self, email, secret, form):
        from lc_core.owner import perform

        action = form.get("do", "")
        back = form.get("back", "")
        if not self._same_origin():
            self._audit(email, f"action-refused:{action}", False)
            return self._send(403, page("Not allowed", "<p>Actions must come from this console or the site's own pages.</p>"))
        if self.app.backend is None:
            return self._send(404, page("No such action", ""))
        outcome = perform(backend=self.app.backend, verifier=self.app.verifier, secret=secret, sources=self.app.sources,
                          registry=self.app.registry, action=action, reason=form.get("reason", ""), code=self._code(form),
                          email=email, now=self.app.clock().strftime("%Y-%m-%dT%H:%M:%SZ"), trigger="owner-console",
                          log=self.app.log)
        self._audit(email, f"action:{action}", outcome.ok)
        if outcome.status in (400, 403) and known(self.app.sources, action, self.app.registry):
            return self._confirm_page(action, status=outcome.status, note=outcome.message, back=back)
        title = {200: "Done", 404: "No such action", 409: "Refused"}.get(outcome.status, "Didn't work")
        extra = ("<p class=muted>Recorded in the decision log. Jobs read the new state at their next check.</p>"
                 if outcome.ok else "")
        return self._send(outcome.status, page(title, f"<p>{html.escape(outcome.message)}</p>{extra}{self._back(back)}"))

    def _status_page(self):
        try:
            st = self.app.read_state()
        except Exception as exc:
            return page("System status", f"<p><strong>Control state unreadable</strong> ({html.escape(type(exc).__name__)}). "
                                         "Agents fail closed while this is true.</p>")
        rows = [("State", st.state), ("Paused components", ", ".join(st.paused_components) or "none"),
                ("Monthly budget (USD)", "not set" if st.monthly_budget_usd is None else f"{st.monthly_budget_usd:g}")]
        table = "".join(f"<tr><th align=left>{html.escape(k)}<td>{html.escape(v)}" for k, v in rows)
        site = f'<a href="{html.escape(self.app.site_url)}/#/system">System page</a>' if self.app.site_url else "System page"
        return page("System status", f"<table>{table}</table><p>Actions start from the {site}: each asks here for a fresh "
                                     "code. Budget changes run from the "
                                     "<a href=https://github.com/lei-commons/control/actions>control repository</a>.</p>")


def _site(url: str):
    """The site's address, if ``url`` is one: https and a run.app host only, so the link can't lead elsewhere."""
    m = re.fullmatch(r"https://[a-z0-9-]+(\.[a-z0-9-]+)*\.run\.app", (url or "").strip().rstrip("/"))
    return m.group(0) if m else None


def make_server(app: App, address) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(address, type("BoundHandler", (Handler,), {"app": app}))


def main() -> int:
    try:
        from lc_core.approvals import registry
        from lc_core.sources import pipelines

        from google.cloud import storage
        from lc_core.owner import GoogleBackend
        from lc_core.totp import GcsSteps
        from lc_core.totp import Verifier as SharedVerifier

        project = os.environ["GCP_PROJECT_ID"]
        app = App(owners=os.environ["LC_CONSOLE_OWNERS"].split(","), audience=os.environ["LC_IAP_AUDIENCE"],
                  store=SecretManagerStore(project),
                  backend=GoogleBackend(project, os.environ.get("GCP_REGION", "us-central1")),
                  sources=pipelines(os.environ.get("LC_SOURCES_CONFIG", "/app/config/sources.yaml")),
                  registry=registry(os.environ.get("LC_SOURCES_CONFIG", "/app/config/sources.yaml")),
                  site_url=os.environ.get("LC_SITE_URL", ""),
                  # One code, once, across the console and the site: login and actions share the record.
                  verifier=SharedVerifier(GcsSteps(storage.Client().bucket(f"{project}-control"))))
    except KeyError as exc:
        print(f"refusing to start: {exc} is not set", file=sys.stderr)
        return 1
    make_server(app, ("0.0.0.0", int(os.environ.get("PORT", "8080")))).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
