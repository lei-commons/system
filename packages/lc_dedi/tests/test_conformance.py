"""Conformance with the protocol's own tooling: dedi-cli (nfh-trust-labs/dedi-cli, Go) must verify what we
sign, and we must verify what it signs. Set DEDI_CLI to its path; skipped when it isn't installed (CI
installs it: .github/workflows/ci-dedi.yml)."""

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from lc_dedi import documents as dd
from lc_dedi import jws

CLI = os.environ.get("DEDI_CLI") or shutil.which("dedi-cli")
if os.environ.get("LC_REQUIRE_DEDI_CLI") and not (CLI and os.access(CLI, os.X_OK)):
    raise RuntimeError("LC_REQUIRE_DEDI_CLI is set (CI) but dedi-cli isn't installed: conformance would go untested")
pytestmark = pytest.mark.skipif(not CLI, reason="dedi-cli not installed")
SOON = datetime.now(timezone.utc) + timedelta(days=5)


def run(*args):
    return subprocess.run([CLI, *args], capture_output=True, text=True, timeout=60)


def tricky_records():
    # Non-ASCII, escapes, a surrogate-pair character, keys that sort differently in UTF-16 and code points.
    return [{"record_name": "BR", "details": {"id": "Brasília \"S/A\" \\ \t 😀", "דּ": 1, "\U0001f600": 2, "n": 9007199254740991}},
            {"record_name": "Ω", "details": {"id": "", "nested": {"z": [True, False, None], "a": "x"}}}]


def test_dedi_cli_verifies_our_file_and_manifest(tmp_path):
    signer = jws.SeedSigner("lc-dedi-1", os.urandom(32))
    doc = dd.build_file(domain="lei-commons.example", signer=signer, source_url="https://lei-commons.example/dedi/dedi.t.json",
                        next_update=SOON, registry={"name": "t", "schema": {"type": "object"}, "state": "live",
                                                    "updated_at": dd.utc(datetime.now(timezone.utc))},
                        records=tricky_records())
    manifest = dd.build_manifest(domain="lei-commons.example", signer=signer, keys=[signer.public_jwk()],
                                 updated_at=datetime.now(timezone.utc), next_update=SOON,
                                 files=[{"registry": "t", "url": doc["source_url"], "digest": dd.digest(doc)}])
    digests = dd.build_digests(domain="lei-commons.example", signer=signer, updated_at=datetime.now(timezone.utc),
                               next_update=SOON, digests={"t": dd.digest(doc)})
    for name, d in (("file.json", doc), ("manifest.json", manifest)):
        path = tmp_path / name
        path.write_bytes(dd.serialize(d))
        r = run("verify", "--in", str(path))
        assert r.returncode == 0, r.stdout + r.stderr
        pub = tmp_path / "pub.json"
        pub.write_text(json.dumps(signer.public_jwk()))
        r = run("verify", "--in", str(path), "--key", str(pub))
        assert r.returncode == 0, r.stdout + r.stderr
    # the digests file has neither domain-as-manifest nor publisher shape dedi-cli auto-detects; check its
    # signature over the same canonical bytes by wrapping nothing: our own verifier covers it, and the
    # canonical form is the one dedi-cli just accepted for the other two documents.
    dd.verify_digests(digests, manifest)


def test_a_tampered_file_fails_dedi_cli(tmp_path):
    signer = jws.SeedSigner("k", os.urandom(32))
    doc = dd.build_file(domain="lei-commons.example", signer=signer, source_url="https://lei-commons.example/dedi/dedi.t.json",
                        next_update=SOON, registry={"name": "t", "schema": {"type": "object"}, "state": "live",
                                                    "updated_at": dd.utc(datetime.now(timezone.utc))},
                        records=tricky_records())
    doc["records"][0]["details"]["id"] = "changed"
    path = tmp_path / "f.json"
    path.write_bytes(dd.serialize(doc))
    assert run("verify", "--in", str(path)).returncode != 0


def test_we_verify_what_dedi_cli_signs(tmp_path):
    key = tmp_path / "key.json"
    assert run("keygen", "--kid", "cli-1", "--out", str(key)).returncode == 0
    private = json.loads(key.read_text())
    unsigned = {"dedi_version": "0.1", "type": "dedi-file", "source_url": "https://lei-commons.example/dedi/dedi.t.json",
                "next_update": dd.utc(SOON), "publisher": {"domain": "lei-commons.example"},
                "namespace": "lei-commons.example",
                "registry": {"name": "t", "schema": {"type": "object"}, "state": "live", "updated_at": dd.utc(datetime.now(timezone.utc))},
                "records": tricky_records()}
    src, out = tmp_path / "u.json", tmp_path / "s.json"
    src.write_text(json.dumps(unsigned, ensure_ascii=False))
    r = run("sign", "--key", str(key), "--in", str(src), "--out", str(out))
    assert r.returncode == 0, r.stdout + r.stderr
    signed = json.loads(out.read_text())
    signer = jws.SeedSigner.from_private_jwk(private)
    assert signed["publisher"]["key"] == signer.public_jwk()
    manifest = dd.build_manifest(domain="lei-commons.example", signer=signer, keys=[signer.public_jwk()],
                                 updated_at=datetime.now(timezone.utc), next_update=SOON,
                                 files=[{"registry": "t", "url": signed["source_url"]}])
    dd.verify_file(signed, manifest)
    # and our own signature over the same document is byte-identical: Ed25519 is deterministic
    assert dd.sign_document(signed, signer)["proof"]["jws"] == signed["proof"]["jws"]
