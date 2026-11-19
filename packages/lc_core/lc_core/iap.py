"""Identity-Aware Proxy: the first factor for every private web service.

The signed token is checked on every request, including its audience, so a
token for another service is useless and reaching the container another way
gets nothing. Refuses by default and never raises.
"""

from __future__ import annotations

IAP_KEYS = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
HEADER = "X-Goog-IAP-JWT-Assertion"


def audiences(audience) -> list:
    """The accepted audiences: one, or several comma-separated (while the site moves behind a load balancer,
    both its direct-IAP audience and the load balancer's are accepted; after the move, only the latter)."""
    if isinstance(audience, (list, tuple)):
        values = audience
    else:
        values = str(audience or "").split(",")
    return [a.strip() for a in values if a and a.strip()]


def verify_iap(token, audience, verify=None):
    """Return the verified email, lower-cased, or None. ``audience``: one or several (see ``audiences``)."""
    accepted = audiences(audience)
    if not token or not accepted:
        return None
    try:
        if verify is None:
            from google.auth.transport import requests
            from google.oauth2 import id_token

            def verify(jwt, aud):
                return id_token.verify_token(jwt, requests.Request(), audience=aud, certs_url=IAP_KEYS)

        claims = verify(token, accepted if len(accepted) > 1 else accepted[0])
    except Exception:
        return None
    if claims.get("iss") != IAP_ISSUER or claims.get("aud") not in accepted:
        return None
    email = claims.get("email")
    return email.lower() if isinstance(email, str) and email else None
