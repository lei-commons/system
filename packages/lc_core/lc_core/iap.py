"""Identity-Aware Proxy: the first factor for every private web service.

The signed token is checked on every request, including its audience, so a
token for another service is useless and reaching the container another way
gets nothing. Refuses by default and never raises.
"""

from __future__ import annotations

IAP_KEYS = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
HEADER = "X-Goog-IAP-JWT-Assertion"


def verify_iap(token, audience, verify=None):
    """Return the verified email, lower-cased, or None."""
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
