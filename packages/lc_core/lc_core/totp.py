"""RFC 6238 one-time codes for owner actions, with replay protection shared by every service.

The owner's actions can be taken on the site (inline, on the page) or in the
owner console. Both check the same second factor. A code is accepted once,
system-wide: the last accepted step is kept in Cloud Storage and moved forward
only with a generation check, so the site and the console can't each accept
the same code. Wrong codes count towards a lockout.
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


class MemorySteps:
    """The last accepted step, in memory: for tests and a single process."""

    def __init__(self):
        self.last = -1

    def claim(self, step: int) -> bool:
        if step <= self.last:
            return False
        self.last = step
        return True


class GcsSteps:
    """The last accepted step, in one Cloud Storage object every service shares."""

    def __init__(self, bucket, name: str = "totp-last-step.json"):
        self._blob = bucket.blob(name)

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
    """Checks a code, claims its step system-wide, and locks out after repeated failures."""

    def __init__(self, steps, clock=time.time):
        self._steps, self._clock = steps, clock
        self._lock = threading.Lock()
        self._failures, self._locked_until = 0, 0.0

    def locked(self) -> bool:
        with self._lock:
            return self._clock() < self._locked_until

    def verify(self, secret: str, code: str) -> bool:
        with self._lock:
            now = self._clock()
            if now < self._locked_until:
                return False
            step = matching_step(secret, code, now)
            if step is not None and self._steps.claim(step):
                self._failures = 0
                return True
            self._failures += 1
            if self._failures >= MAX_FAILURES:
                self._locked_until, self._failures = now + LOCKOUT_SECONDS, 0
            return False
