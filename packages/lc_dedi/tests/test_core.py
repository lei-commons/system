"""The DeDi core: JCS, the JWS profile, building and the five verification steps, and how each fails."""

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from lc_dedi import documents as dd
from lc_dedi import jcs, jws

NOW = datetime(2026, 11, 19, 4, 0, tzinfo=timezone.utc)
SEED = bytes(range(32))
DOMAIN = "lei-commons.example"


@pytest.fixture
def signer():
    return jws.SeedSigner("k1", SEED)


def make_file(signer, *, records=None, schema=None, next_update=NOW + timedelta(hours=36), state="live", name="things"):
    schema = schema or {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}
    return dd.build_file(domain=DOMAIN, signer=signer, source_url=f"https://{DOMAIN}/dedi/dedi.{name}.json",
                         next_update=next_update,
                         registry={"name": name, "schema": schema, "state": state, "updated_at": dd.utc(NOW)},
                         records=records if records is not None else [{"record_name": "a", "details": {"id": "a"}}])


def make_manifest(signer, files, keys=None, next_update=NOW + timedelta(days=7)):
    return dd.build_manifest(domain=DOMAIN, signer=signer, keys=keys or [signer.public_jwk()], updated_at=NOW,
                             next_update=next_update, files=files)


def entry(doc):
    return {"registry": doc["registry"]["name"], "url": doc["source_url"], "digest": dd.digest(doc)}


# ---- JCS (RFC 8785) ------------------------------------------------------------------------------------------

def test_jcs_sorts_by_utf16_code_units_not_code_points():
    # U+1F600 is a surrogate pair (D83D DE00) and sorts before U+FB33 in UTF-16, after it in code points.
    doc = {"דּ": 1, "\U0001f600": 2, "a": 3}
    assert jcs.canonicalize(doc) == '{"a":3,"\U0001f600":2,"דּ":1}'.encode()


def test_jcs_escapes_like_rfc8785():
    assert jcs.canonicalize("\u0000\b\f\n\r\t\"\\/é ") == b'"\\u0000\\b\\f\\n\\r\\t\\"\\\\/\xc3\xa9\xe2\x80\xa8"'


def test_jcs_refuses_floats_huge_ints_and_lone_surrogates():
    for bad in (1.5, 2**53, {"x": "\ud800"}, {1: "a"}, {"x": object()}):
        with pytest.raises(jcs.CanonicalizationError):
            jcs.canonicalize(bad)


def test_jcs_whitespace_and_order_never_matter():
    a = json.loads('{ "b" : [1, 2], "a": {"y": null, "x": true} }')
    b = json.loads('{"a":{"x":true,"y":null},"b":[1,2]}')
    assert jcs.canonicalize(a) == jcs.canonicalize(b) == b'{"a":{"x":true,"y":null},"b":[1,2]}'


# ---- the JWS profile -----------------------------------------------------------------------------------------

def test_header_is_dedi_cli_byte_for_byte():
    import base64
    assert base64.urlsafe_b64decode(jws.HEADER_B64 + "==") == b'{"alg":"EdDSA","b64":false,"crit":["b64"]}'


def test_sign_and_verify(signer):
    sig = jws.sign(signer, b"payload")
    head, body, tail = sig.split(".")
    assert head == jws.HEADER_B64 and body == "" and tail
    jws.verify(signer.public_jwk(), b"payload", sig)


@pytest.mark.parametrize("mutate", [
    lambda s: s.replace("..", ".cGF5bG9hZA."),                     # attached payload
    lambda s: "eyJhbGciOiJub25lIn0" + s[s.index("."):],              # alg none
    lambda s: s[:-4] + ("AAAA" if not s.endswith("AAAA") else "BBBB"),  # tampered signature
    lambda s: s + ".x",                                             # four segments
])
def test_verify_rejects_other_profiles_and_tampering(signer, mutate):
    with pytest.raises(jws.SignatureError):
        jws.verify(signer.public_jwk(), b"payload", mutate(jws.sign(signer, b"payload")))


def test_verify_rejects_wrong_key_and_private_material(signer):
    other = jws.SeedSigner("k2", bytes(32))
    with pytest.raises(jws.SignatureError):
        jws.verify(other.public_jwk(), b"p", jws.sign(signer, b"p"))
    with pytest.raises(jws.SignatureError, match="private"):
        jws.public_key_of(signer.public_jwk() | {"d": "x"})


def test_private_jwk_round_trip(signer):
    private = signer.public_jwk() | {"d": jws.b64u(SEED)}
    assert jws.SeedSigner.from_private_jwk(private).public_jwk() == signer.public_jwk()
    with pytest.raises(jws.SignatureError):
        jws.SeedSigner.from_private_jwk(private | {"x": jws.b64u(bytes(32))})


class FakeKms:
    def __init__(self, signer, corrupt=False, confirm=True):
        self.signer, self.corrupt, self.confirm, self.calls = signer, corrupt, confirm, []

    def get_public_key(self, request):
        from cryptography.hazmat.primitives import serialization
        pem = self.signer._key.public_key().public_bytes(serialization.Encoding.PEM,
                                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        return type("R", (), {"pem": pem})()

    def asymmetric_sign(self, request):
        self.calls.append(request)
        sig = self.signer.sign_raw(request["data"])
        if self.corrupt:
            sig = bytes(64)
        return type("R", (), {"signature": sig, "verified_data_crc32c": self.confirm})()


def test_kms_signer_signs_and_checks_its_own_output(signer, monkeypatch):
    import sys
    import types
    if "google_crc32c" not in sys.modules:  # installed with google-cloud-kms; a stand-in otherwise
        monkeypatch.setitem(sys.modules, "google_crc32c", types.SimpleNamespace(value=lambda b: 0))
    kms = jws.KmsSigner("k1", "projects/p/.../cryptoKeyVersions/1", client=FakeKms(signer))
    assert kms.public_jwk() == signer.public_jwk()
    jws.verify(kms.public_jwk(), b"x", jws.sign(kms, b"x"))
    bad = jws.KmsSigner("k1", "v", client=FakeKms(signer, corrupt=True))
    with pytest.raises(Exception):
        jws.sign(bad, b"x")
    unconfirmed = jws.KmsSigner("k1", "v", client=FakeKms(signer, confirm=False))
    with pytest.raises(jws.SignatureError, match="checksum"):
        jws.sign(unconfirmed, b"x")


# ---- building and verifying -----------------------------------------------------------------------------------

def test_a_file_and_manifest_verify(signer):
    doc = make_file(signer)
    manifest = make_manifest(signer, [entry(doc)])
    dd.verify_manifest(manifest, served_host=DOMAIN, now=NOW)
    dd.verify_file(doc, manifest, now=NOW, served_bytes=dd.serialize(doc))


def test_pretty_printing_never_breaks_the_signature(signer):
    doc = make_file(signer)
    manifest = make_manifest(signer, [entry(doc)])
    relayed = json.loads(json.dumps(doc, indent=7))  # a mirror re-serialises it
    dd.verify_file(relayed, manifest, now=NOW)


@pytest.mark.parametrize("step,mutate", [
    ("shape", lambda d: d.pop("source_url")),
    ("shape", lambda d: d.update(type="dedi-files")),
    ("shape", lambda d: d["registry"].update(state="retired")),
    ("shape", lambda d: d.update(extra=1)),
    ("integrity", lambda d: d["records"][0]["details"].update(id="tampered")),
    ("integrity", lambda d: d["proof"].update(verification_method="other")),
    ("shape", lambda d: d["proof"].update(canonicalization="none")),  # the envelope schema allows only JCS
])
def test_each_tampering_fails_its_step(signer, step, mutate):
    doc = make_file(signer)
    manifest = make_manifest(signer, [entry(doc)])
    bad = copy.deepcopy(doc)
    mutate(bad)
    with pytest.raises(dd.DeDiError) as e:
        dd.verify_file(bad, manifest, now=NOW)
    assert e.value.step == step


def test_a_key_not_in_the_manifest_is_integrity_valid_but_not_authentic(signer):
    rogue = jws.SeedSigner("k1", bytes(32))  # same kid, other key
    doc = make_file(rogue)
    manifest = make_manifest(signer, [entry(doc)])
    with pytest.raises(dd.DeDiError) as e:
        dd.verify_file(doc, manifest, now=NOW)
    assert e.value.step == "authenticity"


def test_a_revoked_key_fails_everywhere_at_once(signer):
    doc = make_file(signer)
    new = jws.SeedSigner("k2", bytes(32))
    rotated = make_manifest(new, [entry(doc)])  # k1 removed from keys
    with pytest.raises(dd.DeDiError, match="not authenticated"):
        dd.verify_file(doc, rotated, now=NOW)
    both = make_manifest(new, [entry(doc)], keys=[new.public_jwk(), signer.public_jwk()])  # during rotation
    dd.verify_file(doc, both, now=NOW)


def test_digest_mismatch_and_wrong_domain_or_host(signer):
    doc = make_file(signer)
    manifest = make_manifest(signer, [entry(doc)])
    with pytest.raises(dd.DeDiError, match="digest"):
        dd.verify_file(doc, manifest, now=NOW, served_bytes=dd.serialize(doc) + b" ")
    with pytest.raises(dd.DeDiError, match="served from"):
        dd.verify_manifest(manifest, served_host="evil.example", now=NOW)


def test_stale_and_inactive(signer):
    doc = make_file(signer)
    manifest = make_manifest(signer, [entry(doc)])
    with pytest.raises(dd.DeDiError) as e:
        dd.verify_file(doc, manifest, now=NOW + timedelta(hours=37))
    assert e.value.step == "freshness"
    with pytest.raises(dd.DeDiError) as e:
        dd.verify_manifest(manifest, now=NOW + timedelta(days=8))
    assert e.value.step == "freshness"
    retired = make_file(signer, state="inactive")
    with pytest.raises(dd.DeDiError) as e:
        dd.verify_file(retired, make_manifest(signer, [entry(retired)]), now=NOW)
    assert e.value.step == "state"


def test_records_must_be_unique_and_match_the_schema(signer):
    with pytest.raises(dd.DeDiError, match="duplicate"):
        make_file(signer, records=[{"record_name": "a", "details": {"id": "a"}}] * 2)
    with pytest.raises(dd.DeDiError, match="record 'a'"):
        make_file(signer, records=[{"record_name": "a", "details": {"id": 7}}])


def test_manifest_rules(signer):
    doc = make_file(signer)
    with pytest.raises(dd.DeDiError, match="not among"):
        make_manifest(signer, [entry(doc)], keys=[jws.SeedSigner("k9", bytes(32)).public_jwk()])
    with pytest.raises(dd.DeDiError, match="share a registry"):
        make_manifest(signer, [entry(doc), entry(doc)])
    inline = copy.deepcopy(doc)  # an inline entry is a whole DeDi file
    m = make_manifest(signer, [inline])
    dd.verify_manifest(m, now=NOW)


def test_digests_file(signer):
    doc = make_file(signer)
    digests = dd.build_digests(domain=DOMAIN, signer=signer, updated_at=NOW, next_update=NOW + timedelta(hours=36),
                               digests={"things": dd.digest(doc)})
    manifest = make_manifest(signer, [{"registry": "things", "url": doc["source_url"]}])
    got = dd.verify_digests(digests, manifest, now=NOW)
    dd.verify_file(doc, manifest, now=NOW, digests=got, served_bytes=dd.serialize(doc))
    stale = dict(got, things="sha-256:" + "0" * 64)
    with pytest.raises(dd.DeDiError, match="digest"):
        dd.verify_file(doc, manifest, now=NOW, digests=stale, served_bytes=dd.serialize(doc))
    foreign = dd.build_digests(domain=DOMAIN, signer=jws.SeedSigner("k7", bytes(32)), updated_at=NOW,
                               next_update=NOW + timedelta(hours=36), digests={"things": dd.digest(doc)})
    with pytest.raises(dd.DeDiError, match="does not list"):
        dd.verify_digests(foreign, manifest, now=NOW)


def test_content_unchanged_ignores_only_updated_at(signer):
    doc = make_file(signer)
    reg = dict(doc["registry"], updated_at="2030-01-01T00:00:00Z")
    assert dd.content_unchanged(doc, reg, doc["records"])
    assert not dd.content_unchanged(doc, reg, [{"record_name": "b", "details": {"id": "b"}}])
    assert not dd.content_unchanged(None, reg, doc["records"])
