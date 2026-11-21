"""The admin area's second-factor page: set up the authenticator, or rotate its key (lc_core.second_factor).

GET  /api/v1/admin/second-factor  the status; while nothing is set up, the key to add to the app (created on
                                  the first visit and kept, so a reload shows the same key).
POST /api/v1/admin/second-factor  {"step": ..., "code": ...}, same-origin JSON like every owner action:
  setup            a code from the key being set up; it becomes the active key
  rotate           a code from the current key; answers with a new key, which waits beside the current one
  confirm-rotation a code from the new key; it replaces the current one
  cancel-rotation  a code from the current key; the new key is dropped

Once a key is set up, no plain page load ever shows a key: a new key appears only in the answer to ``rotate``,
which needs a code from the current one. Every step, and every refused code, is kept in the decision log.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from lc_core import second_factor as sf
from lc_core.owner import Outcome

STEPS = ("setup", "rotate", "confirm-rotation", "cancel-rotation")
MAX_BODY = 1024


def qr_svg(uri: str) -> str | None:
    """The key as a QR code (an SVG data URI), or None: the typed key works without it."""
    try:
        import segno

        return segno.make(uri, error="m").svg_data_uri(scale=5, dark="#111", light="#fff")
    except Exception:
        return None


class SecondFactor:
    """``store`` has get() -> str | None and put(value); ``record`` keeps a decision-log event."""

    def __init__(self, *, store, verifier, record, clock=None, log=None):
        self.store, self.verifier, self._record = store, verifier, record
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.log = log or (lambda r: print(json.dumps(r, sort_keys=True), flush=True))
        self._lock = threading.Lock()

    def _key_view(self, key: str, email: str) -> dict:
        uri = sf.otpauth_uri(key, email)
        return {"key": key, "uri": uri, "qr": qr_svg(uri)}

    def view(self, email: str) -> dict:
        with self._lock:
            value = self.store.get()
            first = sf.begin_setup(value)
            if first is not None:
                self.store.put(first)
                value = first
        state = sf.status(value)
        out = {"status": state}
        if state == "setting-up":
            out.update(self._key_view(sf.parse(value)["waiting"], email))
        return out

    def _event(self, email, step, decision, reason):
        now = self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        event = {"ts": now, "actor": f"owner:{email}", "action": f"second-factor:{step}", "subject": "second factor",
                 "trigger": "site", "reason": reason, "decision": decision, "component": "second-factor"}
        try:
            self._record(event)
        except Exception as exc:  # the step stands; failing to log it is itself logged
            self.log({"event": "decision-unrecorded", "action": event["action"], "error": type(exc).__name__})
        return event

    def handle(self, email: str, headers, body: bytes) -> tuple[Outcome, dict]:
        """(outcome, extra fields for the answer)."""
        if headers.get("Sec-Fetch-Site") != "same-origin":
            return Outcome(403, "Changes must come from this site's own pages."), {}
        if not (headers.get("Content-Type") or "").startswith("application/json"):
            return Outcome(415, "Send the step as JSON."), {}
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return Outcome(400, "That isn't JSON."), {}
        step, code = data.get("step"), str(data.get("code") or "").strip()
        if step not in STEPS:
            return Outcome(400, "Not a step: setup, rotate, confirm-rotation or cancel-rotation."), {}
        with self._lock:
            try:
                value = self.store.get()
            except Exception as exc:
                return Outcome(503, f"The key can't be read right now ({type(exc).__name__}), so nothing changed."), {}
            state, keys = sf.status(value), sf.parse(value)
            needs = {"setup": ("setting-up", "waiting"), "rotate": ("active", "active"),
                     "confirm-rotation": ("rotating", "waiting"), "cancel-rotation": ("rotating", "active")}[step]
            if state != needs[0]:
                return Outcome(409, f"Nothing changed: the second factor is {state.replace('-', ' ')}."), {}
            try:
                ok = self.verifier.verify(keys[needs[1]], code)
            except Exception as exc:
                return Outcome(503, f"The code couldn't be checked ({type(exc).__name__}), so nothing changed."), {}
            if not ok:
                self.log({"event": "second-factor-code-refused", "step": step})
                self._event(email, step, "rejected", "the code was wrong or already used")
                if self.verifier.locked():
                    return Outcome(403, "Too many wrong codes. Wait five minutes, then try again."), {}
                which = "the new key" if needs[1] == "waiting" and step != "setup" else "your app"
                return Outcome(403, f"That code didn't work, or it was already used. Enter the code {which} shows now."), {}
            new = {"setup": sf.confirm, "rotate": sf.begin_rotation, "confirm-rotation": sf.confirm,
                   "cancel-rotation": sf.cancel_rotation}[step](value)
            try:
                self.store.put(new)
            except Exception as exc:
                self._event(email, step, "failed", f"the key couldn't be stored ({type(exc).__name__})")
                return Outcome(502, f"It didn't work: the key couldn't be stored ({type(exc).__name__})."), {}
        messages = {
            "setup": "Your authenticator is set up. Every owner action now needs a code from it.",
            "rotate": "Add this new key to your app, then enter a code from it to finish. Until then your current "
                      "key keeps working.",
            "confirm-rotation": "The new key is now the only one. Remove the old entry from your app.",
            "cancel-rotation": "The new key was dropped; your current key stays.",
        }
        event = self._event(email, step, "done", {"setup": "set up the authenticator",
                                                   "rotate": "started a key rotation",
                                                   "confirm-rotation": "rotated the key",
                                                   "cancel-rotation": "cancelled the key rotation"}[step])
        extra = self._key_view(sf.parse(new)["waiting"], email) if step == "rotate" else {}
        return Outcome(200, messages[step], event), extra
