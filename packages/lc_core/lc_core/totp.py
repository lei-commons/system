"""RFC 6238 one-time codes for owner actions, with replay protection shared by every service.

The owner's actions can be taken on the site (inline, on the page) or in the
owner console. Both check the same second factor. A code is accepted once,
system-wide: the last accepted step is kept in Cloud Storage and moved forward
only with a generation check, so the site and the console can't each accept
the same code. Wrong codes count towards a lockout, also kept there, so every
service shares one count and a restart doesn't reset it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import threading
import time

STEP = 30
DIGITS = 6
WINDOW = 4  # two minutes either way, as the console accepts for login
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300


def code_at(secret: str, step: int, digits: int = DIGITS, algorithm=hashlib.sha1) -> str:
    key = base64.b32decode(secret)
    mac = hmac.new(key, struct.pack(">Q", step), algorithm).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def matching_step(secret: str, code: str, now: float, window: int = WINDOW):
    """The time step ``code`` belongs to, or None."""
    if not (isinstance(code, str) and len(code) == DIGITS and code.isascii() and code.isdigit()):
        return None
    current = int(now) // STEP
    matched = None
    for step in range(current - window, current + window + 1):
        if hmac.compare_digest(code_at(secret, step), code):
            matched = step
    return matched


NO_LOCKOUT = {"failures": 0, "locked_until": 0.0}


class MemorySteps:
    """The last accepted step and the lockout, in memory: for tests and a single process."""

    def __init__(self):
        self.last = -1
        self._lockout = dict(NO_LOCKOUT)

    def claim(self, step: int) -> bool:
        if step <= self.last:
            return False
        self.last = step
        return True

    def read_lockout(self):
        return dict(self._lockout), None

    def write_lockout(self, state: dict, token) -> bool:
        self._lockout = dict(state)
        return True


class GcsSteps:
    """The last accepted step and the wrong-code lockout, in Cloud Storage objects every service shares, so the
    site and the console count wrong codes together and a restart doesn't reset the count."""

    def __init__(self, bucket, name: str = "totp-last-step.json", lockout_name: str = "totp-lockout.json"):
        self._blob = bucket.blob(name)
        self._lockout = bucket.blob(lockout_name)

    def read_lockout(self):
        from google.api_core.exceptions import NotFound

        try:
            self._lockout.reload()
            state = json.loads(self._lockout.download_as_bytes())
            return {"failures": int(state.get("failures", 0)), "locked_until": float(state.get("locked_until", 0))}, \
                self._lockout.generation
        except NotFound:
            return dict(NO_LOCKOUT), 0

    def write_lockout(self, state: dict, token) -> bool:
        from google.api_core.exceptions import PreconditionFailed

        try:
            self._lockout.upload_from_string(json.dumps(state), content_type="application/json",
                                             if_generation_match=token)
            return True
        except PreconditionFailed:
            return False

    def claim(self, step: int) -> bool:
        from google.api_core.exceptions import NotFound, PreconditionFailed

        for _ in range(3):
            try:
                self._blob.reload()
                generation, last = self._blob.generation, json.loads(self._blob.download_as_bytes())["step"]
            except NotFound:
                generation, last = 0, -1
            if step <= last:
                return False
            try:
                self._blob.upload_from_string(json.dumps({"step": step}), content_type="application/json",
                                              if_generation_match=generation)
                return True
            except PreconditionFailed:
                continue  # another service claimed a step meanwhile: read again
        return False


class Verifier:
    """Checks a code, claims its step system-wide, and locks out after repeated failures.

    The failure count and the lockout live with the steps when the store keeps them (``read_lockout`` /
    ``write_lockout``, as GcsSteps does), so every service shares one count; otherwise in this process.
    """

    def __init__(self, steps, clock=time.time):
        self._steps, self._clock = steps, clock
        self._lock = threading.Lock()
        if not hasattr(steps, "read_lockout"):
            self._local = MemorySteps()
            self._read, self._write = self._local.read_lockout, self._local.write_lockout
        else:
            self._read, self._write = steps.read_lockout, steps.write_lockout

    def locked(self) -> bool:
        with self._lock:
            return self._clock() < self._read()[0]["locked_until"]

    def _count_failure(self, now: float):
        for _ in range(3):  # another service may count at the same time: read again and retry
            state, token = self._read()
            failures = state["failures"] + 1
            new = ({"failures": 0, "locked_until": now + LOCKOUT_SECONDS} if failures >= MAX_FAILURES
                   else {"failures": failures, "locked_until": state["locked_until"]})
            if self._write(new, token):
                return

    def verify(self, secret: str, code: str) -> bool:
        with self._lock:
            now = self._clock()
            state, token = self._read()
            if now < state["locked_until"]:
                return False
            step = matching_step(secret, code, now)
            if step is not None and self._steps.claim(step):
                if state["failures"]:
                    self._write(dict(NO_LOCKOUT), token)
                return True
            self._count_failure(now)
            return False
