"""DeDi documents: the DeDi file (one registry), the manifest and the digests file.

Follows the DeDi protocol's "Publishing DeDi files" (LF Decentralized Trust,
decentralized-directory-protocol, docs/publishing-dedi-files.md, dedi_version 0.1;
"protocol v2" on dedi.global). Every document is signed the same way: the JCS form of
the document minus ``proof``, signed as a detached EdDSA JWS, with ``proof`` naming the
key. The official envelope schemas are vendored under ``schemas/`` (see its README for
the commit they were taken from), and every document is checked against them both when
it is built and when it is verified.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from importlib import resources

from . import jcs, jws

DEDI_VERSION = "0.1"
FILE_TYPE, MANIFEST_TYPE, DIGESTS_TYPE = "dedi-file", "dedi-manifest", "dedi-digests"
STATES = ("live", "inactive")


class DeDiError(ValueError):
    """A document is malformed, or fails one of the five verification steps."""

    def __init__(self, step: str, message: str):
        super().__init__(f"{step}: {message}")
        self.step = step


def utc(ts: datetime) -> str:
    """RFC 3339 in UTC, to the second, with a Z: the form the protocol's examples use."""
    return ts.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


# --- schemas -------------------------------------------------------------------------------------------------

def _format_checker():
    """Draft 7's format checker, with "uri" and "date-time" always enforced: jsonschema checks them only when
    optional packages are installed, and a silently skipped check is worse than none."""
    import jsonschema
    from urllib.parse import urlsplit

    checker = copy.deepcopy(jsonschema.Draft7Validator.FORMAT_CHECKER)

    @checker.checks("uri", raises=ValueError)
    def _uri(value):
        if not isinstance(value, str):
            return True
        parts = urlsplit(value)
        if not parts.scheme or not (parts.netloc or parts.scheme in ("urn", "mailto")) or any(c.isspace() for c in value):
            raise ValueError(f"{value!r} is not an absolute URI")
        return True

    @checker.checks("date-time", raises=ValueError)
    def _date_time(value):
        if not isinstance(value, str):
            return True
        if "T" not in value or not (value.endswith("Z") or value[-6] in "+-"):
            raise ValueError(f"{value!r} is not an RFC 3339 date-time")
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True

    return checker


def schema(name: str) -> dict:
    """A vendored schema by file name, e.g. ``dedi-file.schema.json``."""
    return json.loads(resources.files("lc_dedi").joinpath("schemas", name).read_text(encoding="utf-8"))


def _validator(name: str):
    import jsonschema  # a dependency of lc-dedi; imported lazily so the signing core stays light

    s = schema(name)
    if name == "dedi-manifest.schema.json":  # resolve the inline-entry $ref to the vendored file schema
        s = copy.deepcopy(s)
        inline = s["properties"]["files"]["items"]["oneOf"][1]
        inline["allOf"] = [schema("dedi-file.schema.json")]
    return jsonschema.Draft7Validator(s, format_checker=_format_checker())


def check_shape(doc: dict, schema_name: str) -> None:
    errors = sorted(_validator(schema_name).iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        e = errors[0]
        where = "/".join(str(p) for p in e.absolute_path) or "(document)"
        raise DeDiError("shape", f"{where}: {e.message}" + (f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""))


def check_records(doc: dict, registry_schema: dict | None) -> None:
    """record_name unique (the protocol makes duplicates invalid), and every ``details`` valid against the
    registry's schema when it is available (inline, or vendored by the caller)."""
    seen = set()
    for r in doc["records"]:
        name = r["record_name"]
        if name in seen:
            raise DeDiError("shape", f"duplicate record_name {name!r}")
        seen.add(name)
    if registry_schema is None:
        return
    import jsonschema

    v = jsonschema.Draft7Validator(registry_schema, format_checker=_format_checker())
    for r in doc["records"]:
        errors = list(v.iter_errors(r["details"]))
        if errors:
            raise DeDiError("shape", f"record {r['record_name']!r}: {errors[0].message}")


# --- building and signing ------------------------------------------------------------------------------------

def signing_input(doc: dict) -> bytes:
    return jcs.canonicalize({k: v for k, v in doc.items() if k != "proof"})


def sign_document(doc: dict, signer: jws.Signer) -> dict:
    """``doc`` with its ``proof``, signed by ``signer``."""
    unsigned = {k: v for k, v in doc.items() if k != "proof"}
    return unsigned | {"proof": {"verification_method": signer.kid, "canonicalization": "JCS",
                                 "jws": jws.sign(signer, jcs.canonicalize(unsigned))}}


def digest(doc_or_bytes) -> str:
    """``sha-256:<hex>`` over the bytes as served (a dict is serialised as ``serialize`` writes it)."""
    data = doc_or_bytes if isinstance(doc_or_bytes, (bytes, bytearray)) else serialize(doc_or_bytes)
    return "sha-256:" + hashlib.sha256(data).hexdigest()


def serialize(doc: dict) -> bytes:
    """The bytes to publish: readable (indented, sorted), UTF-8. The signature covers the JCS form, so
    pretty-printing never matters to it; the digest covers these exact bytes."""
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def build_file(*, domain: str, signer: jws.Signer, source_url: str, next_update: datetime, registry: dict,
               records: list, namespace: str | None = None) -> dict:
    """A signed DeDi file. ``registry``: name, schema (URL or object), state, updated_at, and optional
    description and meta. ``records``: [{record_name, details, (description), (meta)}]."""
    doc = {
        "dedi_version": DEDI_VERSION,
        "type": FILE_TYPE,
        "source_url": source_url,
        "next_update": utc(next_update),
        "publisher": {"domain": domain, "key": signer.public_jwk()},
        "namespace": namespace or domain,
        "registry": registry,
        "records": records,
    }
    signed = sign_document(doc, signer)
    check_shape(signed, "dedi-file.schema.json")
    check_records(signed, registry["schema"] if isinstance(registry["schema"], dict) else None)
    return signed


def build_manifest(*, domain: str, signer: jws.Signer, keys: list, updated_at: datetime, next_update: datetime,
                   files: list, name: str = None, description: str = None, digests_url: str = None) -> dict:
    """The signed ``/.well-known/dedi.index.json``. ``keys``: public JWKs; the signer's must be among them.
    ``files``: referenced entries ({registry, url, (digest), (schema), (state)}) or inline DeDi files."""
    if signer.public_jwk() not in keys:
        raise DeDiError("authenticity", f"the signing key {signer.kid!r} is not among the manifest's keys")
    names = [f["registry"] if isinstance(f["registry"], str) else f["registry"]["name"] for f in files]
    if len(set(names)) != len(names):
        raise DeDiError("shape", "two files[] entries share a registry name")
    doc = {"dedi_version": DEDI_VERSION, "type": MANIFEST_TYPE, "domain": domain, "keys": keys,
           "updated_at": utc(updated_at), "next_update": utc(next_update), "files": files}
    if name:
        doc["name"] = name
    if description:
        doc["description"] = description
    if digests_url:
        doc["digests"] = digests_url
    signed = sign_document(doc, signer)
    check_shape(signed, "dedi-manifest.schema.json")
    return signed


def build_digests(*, domain: str, signer: jws.Signer, updated_at: datetime, next_update: datetime,
                  digests: dict) -> dict:
    doc = {"dedi_version": DEDI_VERSION, "type": DIGESTS_TYPE, "domain": domain, "updated_at": utc(updated_at),
           "next_update": utc(next_update), "digests": dict(sorted(digests.items()))}
    signed = sign_document(doc, signer)
    check_shape(signed, "dedi-digests.schema.json")
    return signed


def content_unchanged(previous: dict | None, registry: dict, records: list) -> bool:
    """True if a previously published file holds the same registry (apart from updated_at) and records:
    then its updated_at is kept, as the protocol asks (it moves only when content changes)."""
    if not previous:
        return False
    strip = lambda reg: {k: v for k, v in reg.items() if k != "updated_at"}  # noqa: E731
    return strip(previous.get("registry", {})) == strip(registry) and previous.get("records") == records


# --- verification (the protocol's five steps) ------------------------------------------------------------------

def verify_signature(doc: dict, key: dict) -> None:
    proof = doc.get("proof") or {}
    if proof.get("canonicalization") != "JCS":
        raise DeDiError("integrity", "proof.canonicalization must be JCS")
    try:
        jws.verify(key, signing_input(doc), proof.get("jws", ""))
    except jws.SignatureError as exc:
        raise DeDiError("integrity", str(exc)) from exc


def verify_manifest(manifest: dict, *, served_host: str = None, now: datetime = None) -> dict:
    """Checks the manifest's shape, its self-signature and freshness; returns its keys by kid."""
    check_shape(manifest, "dedi-manifest.schema.json")
    keys = {k["kid"]: k for k in manifest["keys"]}
    kid = manifest["proof"]["verification_method"]
    if kid not in keys:
        raise DeDiError("integrity", f"proof names {kid!r}, which the manifest does not list")
    verify_signature(manifest, keys[kid])
    if served_host is not None and served_host.lower() != manifest["domain"].lower():
        raise DeDiError("authenticity", f"served from {served_host}, but the manifest is for {manifest['domain']}")
    _fresh(manifest, now)
    return keys


def verify_file(doc: dict, manifest: dict, *, now: datetime = None, digests: dict = None, served_bytes: bytes = None,
                registry_schema: dict = None) -> None:
    """The protocol's verification, in order: (1) shape, (2) integrity against the embedded key, (3) the key is
    in the (already verified) manifest's keys and the namespace is the manifest's domain, (4) freshness,
    (5) the registry is live. Also checks the file's digest when the publisher committed to one."""
    check_shape(doc, "dedi-file.schema.json")
    check_records(doc, registry_schema or (doc["registry"]["schema"] if isinstance(doc["registry"]["schema"], dict) else None))
    key = doc["publisher"]["key"]
    if doc["proof"]["verification_method"] != key["kid"]:
        raise DeDiError("integrity", "proof.verification_method must name publisher.key.kid")
    verify_signature(doc, key)
    if doc["publisher"]["domain"].lower() != manifest["domain"].lower():
        raise DeDiError("authenticity", "publisher.domain is not the manifest's domain")
    if key not in manifest["keys"]:
        raise DeDiError("authenticity", "integrity-valid but not authenticated: the key is not in the manifest")
    if doc["namespace"] != manifest["domain"]:
        raise DeDiError("authenticity", "namespace must be the manifest's domain (dedi.global rejects others)")
    name = doc["registry"]["name"]
    committed = (digests or {}).get(name) or next(
        (f.get("digest") for f in manifest["files"] if f.get("registry") == name), None)
    if committed and served_bytes is not None and digest(served_bytes) != committed:
        raise DeDiError("authenticity", f"digest of {name} does not match the publisher's commitment")
    _fresh(doc, now)
    if doc["registry"]["state"] != "live":
        raise DeDiError("state", f"registry {name} is {doc['registry']['state']}: not authoritative")


def verify_digests(doc: dict, manifest: dict, *, now: datetime = None) -> dict:
    check_shape(doc, "dedi-digests.schema.json")
    if doc["domain"] != manifest["domain"]:
        raise DeDiError("authenticity", "digests domain is not the manifest's domain")
    keys = {k["kid"]: k for k in manifest["keys"]}
    kid = doc["proof"]["verification_method"]
    if kid not in keys:
        raise DeDiError("authenticity", f"digests signed by {kid!r}, which the manifest does not list")
    verify_signature(doc, keys[kid])
    _fresh(doc, now)
    return doc["digests"]


def _fresh(doc: dict, now: datetime | None) -> None:
    now = now or datetime.now(timezone.utc)
    if now > parse_utc(doc["next_update"]):
        raise DeDiError("freshness", f"stale since {doc['next_update']}: re-fetch before relying on it")
