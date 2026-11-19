"""DeDi's one signature profile: a detached JWS (RFC 7515) with an unencoded payload (RFC 7797).

The protected header is fixed: ``{"alg":"EdDSA","b64":false,"crit":["b64"]}``. The signing
input is ``BASE64URL(header) + "." + payload``, where the payload is the JCS form of the
document without its proof; the JWS is written as ``header..signature`` (empty payload
segment). This is exactly what dedi-cli produces and checks, and nothing else is accepted,
which rules out algorithm confusion by construction.

Signers are pluggable: ``SeedSigner`` holds a raw Ed25519 seed (tests, or a seed kept in
Secret Manager); ``KmsSigner`` asks Cloud KMS (EC_SIGN_ED25519, PureEdDSA over raw data),
so the private key never leaves KMS.
"""

from __future__ import annotations

import base64
import json
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

HEADER = {"alg": "EdDSA", "b64": False, "crit": ["b64"]}
# The same bytes dedi-cli's Go struct marshals: {"alg":"EdDSA","b64":false,"crit":["b64"]}
HEADER_B64 = base64.urlsafe_b64encode(json.dumps(HEADER, separators=(",", ":")).encode()).rstrip(b"=").decode()


class SignatureError(ValueError):
    """A JWS is malformed, uses another profile, or doesn't match."""


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer(Protocol):
    kid: str

    def public_jwk(self) -> dict: ...

    def sign_raw(self, message: bytes) -> bytes: ...


def jwk_of(kid: str, public_key: Ed25519PublicKey) -> dict:
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"kid": kid, "kty": "OKP", "crv": "Ed25519", "x": b64u(raw)}


def public_key_of(jwk: dict) -> Ed25519PublicKey:
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise SignatureError(f"key {jwk.get('kid')!r} is not an Ed25519 OKP key")
    if "d" in jwk:
        raise SignatureError(f"key {jwk.get('kid')!r} contains private material")
    raw = unb64u(jwk.get("x", ""))
    if len(raw) != 32:
        raise SignatureError(f"key {jwk.get('kid')!r}: x is {len(raw)} bytes, want 32")
    return Ed25519PublicKey.from_public_bytes(raw)


class SeedSigner:
    """An Ed25519 key from its 32-byte seed (RFC 8037 ``d``)."""

    def __init__(self, kid: str, seed: bytes):
        if len(seed) != 32:
            raise ValueError("an Ed25519 seed is 32 bytes")
        self.kid, self._key = kid, Ed25519PrivateKey.from_private_bytes(seed)

    @classmethod
    def from_private_jwk(cls, jwk: dict) -> "SeedSigner":
        """dedi-cli's key.json format: {kid, kty: OKP, crv: Ed25519, x, d}."""
        signer = cls(jwk["kid"], unb64u(jwk["d"]))
        if jwk.get("x") and signer.public_jwk()["x"] != jwk["x"]:
            raise SignatureError("private JWK: x does not match d")
        return signer

    def public_jwk(self) -> dict:
        return jwk_of(self.kid, self._key.public_key())

    def private_jwk(self) -> dict:
        """The private JWK, dedi-cli's key.json format. Only for storing the key where it is kept."""
        return self.public_jwk() | {"d": b64u(self._key.private_bytes_raw())}

    def sign_raw(self, message: bytes) -> bytes:
        return self._key.sign(message)


class KmsSigner:
    """A Cloud KMS EC_SIGN_ED25519 key version. Needs google-cloud-kms; the caller's identity needs
    roles/cloudkms.signerVerifier on the key (and viewer for the public key)."""

    def __init__(self, kid: str, key_version: str, client=None):
        self.kid, self._name = kid, key_version
        if client is None:
            from google.cloud import kms  # imported here: only jobs that sign need it

            client = kms.KeyManagementServiceClient()
        self._client, self._jwk = client, None

    def public_jwk(self) -> dict:
        if self._jwk is None:
            pem = self._client.get_public_key(request={"name": self._name}).pem.encode()
            key = serialization.load_pem_public_key(pem)
            if not isinstance(key, Ed25519PublicKey):
                raise SignatureError(f"{self._name} is not an Ed25519 key")
            self._jwk = jwk_of(self.kid, key)
        return dict(self._jwk)

    def sign_raw(self, message: bytes) -> bytes:
        import google_crc32c  # a dependency of google-cloud-kms

        response = self._client.asymmetric_sign(request={
            "name": self._name, "data": message, "data_crc32c": google_crc32c.value(message)})
        if not response.verified_data_crc32c:
            raise SignatureError("KMS did not confirm the data checksum")
        signature = bytes(response.signature)
        public_key_of(self.public_jwk()).verify(signature, message)  # never publish a signature we can't check
        return signature


def sign(signer: Signer, payload: bytes) -> str:
    """The detached JWS over ``payload`` (already canonical bytes)."""
    signature = signer.sign_raw(HEADER_B64.encode() + b"." + payload)
    return f"{HEADER_B64}..{b64u(signature)}"


def verify(jwk: dict, payload: bytes, jws: str) -> None:
    """Raises SignatureError unless ``jws`` is DeDi's profile over ``payload`` by ``jwk``."""
    parts = (jws or "").split(".")
    if len(parts) != 3:
        raise SignatureError("malformed JWS: want three segments")
    header_b64, body, signature_b64 = parts
    if body != "":
        raise SignatureError("not a detached JWS (the payload segment is not empty)")
    try:
        header = json.loads(unb64u(header_b64))
    except ValueError as exc:
        raise SignatureError("unreadable JWS header") from exc
    if header != HEADER:
        raise SignatureError(f"JWS header {header} is not DeDi's EdDSA/b64:false profile")
    try:
        public_key_of(jwk).verify(unb64u(signature_b64), header_b64.encode() + b"." + payload)
    except InvalidSignature as exc:
        raise SignatureError("signature does not match (wrong key, or the document changed after signing)") from exc
