"""RFC 6238 one-time codes, with replay protection and a lockout.

The console checks the second factor itself, so it holds whatever the Google
account's settings are. It runs as a single instance, so this state can live
in memory. A restart forgets it, which at worst reopens a code for 90 seconds.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import threading
import time

STEP = 30
DIGITS = 6
WINDOW = 1            # one step either side, for clock drift
ENROL_WINDOW = 4      # two minutes either way, so a slow phone clock can't block setup
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def code_at(secret: str, step: int, digits: int = DIGITS, algorithm=hashlib.sha1) -> str:
    key = base64.b32decode(secret)
    mac = hmac.new(key, struct.pack(">Q", step), algorithm).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def otpauth_uri(secret: str, account: str) -> str:
    from urllib.parse import quote

    return f"otpauth://totp/lei-commons:{quote(account)}?secret={secret}&issuer=lei-commons&digits={DIGITS}&period={STEP}"


class Verifier:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._last_step = -1
        self._failures = 0
        self._locked_until = 0.0

    def locked(self) -> bool:
        with self._lock:
            return self._clock() < self._locked_until

    def verify(self, secret: str, code: str, window: int = WINDOW) -> bool:
        with self._lock:
            now = self._clock()
            if now < self._locked_until:
                return False
            matched = None
            if isinstance(code, str) and len(code) == DIGITS and code.isascii() and code.isdigit():
                current = int(now) // STEP
                for step in range(current - window, current + window + 1):
                    if hmac.compare_digest(code_at(secret, step), code):
                        matched = step
            if matched is None or matched <= self._last_step:  # wrong, or already used
                self._failures += 1
                if self._failures >= MAX_FAILURES:
                    self._locked_until = now + LOCKOUT_SECONDS
                    self._failures = 0
                return False
            self._last_step = matched
            self._failures = 0
            return True
