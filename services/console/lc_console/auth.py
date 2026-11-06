"""The first factor (IAP) and the session.

Everything here refuses by default. IAP's signed token is checked on every
request, including the audience, so a token for another service is useless and
reaching the container by another route gets nothing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

IAP_KEYS = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
SESSION_SECONDS = 3600


def verify_iap(token, audience, verify=None):
    """Return the verified email, or None. Never raises."""
    if not token or not audience:
        return None
    try:
        if verify is None:
            from google.auth.transport import requests
            from google.oauth2 import id_token

            def verify(jwt, aud):
                return id_token.verify_token(jwt, requests.Request(), audience=aud, certs_url=IAP_KEYS)

        claims = verify(token, audience)
    except Exception:
        return None
    if claims.get("iss") != IAP_ISSUER or claims.get("aud") != audience:
        return None
    email = claims.get("email")
    return email.lower() if isinstance(email, str) and email else None


class Sessions:
    """Signed cookies. The key lives in memory, so a restart logs everyone out."""

    def __init__(self, key=None, clock=time.time):
        self._key = key or secrets.token_bytes(32)
        self._clock = clock

    def _sign(self, body: bytes) -> str:
        return base64.urlsafe_b64encode(hmac.new(self._key, body, hashlib.sha256).digest()).decode()

    def issue(self, email: str) -> str:
        body = f"{email}|{int(self._clock()) + SESSION_SECONDS}"
        encoded = base64.urlsafe_b64encode(body.encode()).decode()
        return f"{encoded}.{self._sign(body.encode())}"

    def valid(self, cookie, email: str) -> bool:
        """True only for an unexpired, correctly signed cookie issued to this same person."""
        if not cookie or "." not in cookie:
            return False
        encoded, _, signature = cookie.partition(".")
        try:
            body = base64.urlsafe_b64decode(encoded.encode())
            owner, _, expiry = body.decode().rpartition("|")
            if not hmac.compare_digest(self._sign(body), signature):
                return False
            return hmac.compare_digest(owner, email) and self._clock() < int(expiry)
        except (ValueError, UnicodeError):
            return False
