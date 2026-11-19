"""DeDi publication (lc_publish.dedi): the three registries, the digests and the manifest, and every way a
publication can fail without ever leaving a bad set published. Days are synthetic v2 manifests here (no
Parquet needed); tests/test_dedi_e2e.py runs the real snapshot build first."""

import copy
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest

from lc_dedi import documents as dd
from lc_dedi import jws
from lc_publish import dedi
from lc_publish.testing import FakeBucket

T0 = datetime(2026, 11, 19, 4, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[3]
CLI = os.environ.get("DEDI_CLI") or shutil.which("dedi-cli")
if os.environ.get("LC_REQUIRE_DEDI_CLI") and not (CLI and os.access(CLI, os.X_OK)):
    raise RuntimeError("LC_REQUIRE_DEDI_CLI is set (CI) but dedi-cli isn't installed: the DeDi files would go unchecked")


def h(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def part(country, sources, i, of, records, full):
    group = f"{country}.{'_'.join(sources)}"
    name = f"{group}.p{i}of{of}.parquet"
    p = {"kind": "entities", "name": name, "group": group, "country": country, "sources": sources, "part": i, "of": of,
         "records": records, "root": h("root" + name + str(records))}
    if full:
        sha = h("bytes" + name + str(records))
        p.update(sha256=sha, bytes=records * 85, object=f"parts/{sha.split(':')[1]}.parquet")
    return p


def day(date, kind, anchor, base, extra=0):
    full = kind == "full"
    parts = [part("BR", ["br-rfb"], 0, 2, 5000 + extra, full), part("BR", ["br-rfb"], 1, 2, 5100, full),
             part("BR", ["br-rfb", "gleif"], 0, 1, 40, full), part("GB", ["gb-coh", "gleif"], 0, 1, 900, full),
             part("DE", ["gleif"], 0, 1, 700, full), part("XX", ["gleif"], 0, 1, 3, full)]
    rel = {"kind": "relationships", "name": "relationships.p0of1.parquet", "group": "relationships", "part": 0, "of": 1,
           "records": 150, "root": h("rel" + date)}
    if full:
        rel.update(sha256=h("relbytes" + date), bytes=9000, object=f"parts/{h('relbytes' + date).split(':')[1]}.parquet")
    parts.append(rel)
    files = [{k: p[k] for k in ("name", "kind", "records", "root", "sha256", "bytes", "object")} for p in parts if full]
    return {"schemaVersion": 2, "date": date, "kind": kind, "why": "test", "anchor": anchor,
            "base": {"date": base, "snapshotRoot": h("snap" + base)} if base else None,
            "records": sum(p["records"] for p in parts if p["kind"] == "entities"),
            "recordsPerSource": {"br-rfb": 10140 + extra, "gb-coh": 900, "gleif": 1643}, "inMoreThanOneSource": 940,
            "relationships": 150, "relationshipsBothEndsInDirectory": 140, "snapshotRoot": h("snap" + date),
            "parts": parts, "changes": {"files": [], "added": extra, "changed": 2, "deleted": 0, "relationshipsAdded": 0,
                                        "relationshipsChanged": 0, "relationshipsDeleted": 0},
            "files": files, "layout": {}, "stats": {}, "settings": {},
            "sources": [{"id": "br-rfb", "name": "CNPJ", "publisher": "Receita Federal", "licence": "ODbL-1.0",
                         "attribution": "", "citation": "Receita Federal: CNPJ."},
                        {"id": "gb-coh", "name": "Companies House basic company data", "publisher": "Companies House",
                         "licence": "OGL-UK-3.0 (unconfirmed)",
                         "attribution": "Contains public sector information licensed under the Open Government Licence v3.0.",
                         "citation": "Companies House."},
                        {"id": "gleif", "name": "GLEIF LEI golden copy", "publisher": "GLEIF", "licence": "CC0-1.0",
                         "attribution": "", "citation": "GLEIF."}],
            "decisionLog": {"day": base or date, "head": h("head" + date), "events": 12}}


def put(bucket, name, obj):
    bucket.blob(name).upload_from_string(json.dumps(obj, indent=2, sort_keys=True))


def seed_bucket(bucket, days=(("2026-11-17", "full", None), ("2026-11-18", "changes", "2026-11-17"),
                              ("2026-11-19", "changes", "2026-11-18")), signed=("2026-11-17", "2026-11-18"), extra=0):
    for i, (date, kind, base) in enumerate(days):
        put(bucket, f"{date}/manifest.json", day(date, kind, "2026-11-17", base, extra=extra if i == len(days) - 1 else 0))
        if date in signed:
            bucket.blob(f"{date}/manifest.json.sigstore.json").upload_from_string("{}")
    last = days[-1]
    put(bucket, "latest.json", {"schemaVersion": 2, "date": last[0], "kind": last[1], "anchor": "2026-11-17",
                                "manifest": f"{last[0]}/manifest.json"})


SOURCES = [
    {"id": "gleif", "name": "GLEIF golden copy", "country": "*", "access": "open", "method": "bulk", "licence": "CC0-1.0",
     "cadence": "daily", "url": "https://www.gleif.org/x", "terms_url": "https://www.gleif.org/terms",
     "assessment": {"risk": "low", "commercial": "allowed", "attribution": "not required", "summary": "CC0.\n  Free.", "verified": False},
     "approval": "approved", "approved_at": "2026-11-11",
     "pipeline": {"job": "ingest-gleif", "store": "gleif", "records_as": "gleif", "label": "GLEIF", "refresh": "daily",
                  "dataset": "GLEIF LEI golden copy", "attribution": "", "role": "lei",
                  "record_url": "https://search.gleif.org/#/record/{lei}"}},
    {"id": "gb-coh", "name": "UK Companies House", "country": "GB", "access": "open", "licence": "OGL-UK-3.0 (unconfirmed)",
     "url": "https://download.companieshouse.gov.uk/", "approval": "approved", "assessment": {"verified": True},
     "pipeline": {"job": "ingest-gb", "store": "gb", "records_as": "gb-coh", "label": "Companies House", "refresh": "monthly",
                  "dataset": "x", "attribution": "Contains public sector information licensed under the Open Government Licence v3.0.",
                  "registration_authority": "RA000585"}},
    {"id": "br-rfb-cnpj", "name": "Receita Federal CNPJ open data", "country": "BR", "access": "open", "licence": "ODbL-1.0",
     "url": "https://dados.gov.br/x", "approval": "approved",
     "pipeline": {"job": "ingest-br", "store": "br", "records_as": "br-rfb", "label": "Receita Federal (Brazil)",
                  "refresh": "monthly", "dataset": "CNPJ", "attribution": "", "registration_authority": "RA000681"}},
    {"id": "us-co-sos", "name": "Colorado", "country": "US", "access": "open", "licence": "to-be-verified",
     "url": "https://data.colorado.gov/", "approval": "pending"},
    {"id": "cz-justice", "name": "Czech register", "country": "CZ", "access": "open", "licence": "restricted",
     "url": "https://or.justice.cz/", "approval": "pending"},
]


def effective(denied=()):
    out = {}
    for s in SOURCES:
        status = "denied" if s["id"] in denied else s.get("approval", "pending")
        out[s["id"]] = {"status": status, "by": "owner", "at": s.get("approved_at"), "reason": None, "decided": False}
    return out


@pytest.fixture
def cfg(tmp_path):
    text = (ROOT / "config" / "dedi.yaml").read_text()
    path = tmp_path / "dedi.yaml"
    path.write_text(text)
    return dedi.load_config(path)


@pytest.fixture
def signer():
    return jws.SeedSigner("lc-dedi-1", bytes(range(32)))


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def do_publish(bucket, cfg, signer, now=T0, events=None, **kw):
    events = [] if events is None else events
    return dedi.publish(bucket, cfg, signer, sources=SOURCES, effective=kw.pop("eff", effective()), now=Clock(now),
                        record=events.append, wait=lambda s: None, **kw)


def read(bucket, name):
    blob = bucket.get_blob(name)
    return json.loads(blob.download_as_bytes()) if blob else None


def objects(bucket):
    return {n: o["data"] for n, o in bucket.objects.items() if n.startswith("dedi/")}


# ---- configuration -------------------------------------------------------------------------------------------

def test_the_committed_config_is_valid(cfg):
    assert cfg.enabled and cfg.base_url.split("/")[2] == cfg.domain and cfg.files_hours >= 25


@pytest.mark.parametrize("edit,why", [
    (lambda c: c.update(domain="Lei-Commons.example"), "lower-case"),
    (lambda c: c.update(base_url="http://lei-commons.example"), "https"),
    (lambda c: c.update(base_url="https://elsewhere.example"), "served from the domain"),
    (lambda c: c["freshness_hours"].update(files=20), "25..96"),
    (lambda c: c["freshness_hours"].update(manifest=48), "manifest"),
    (lambda c: c["key"].update(signer="hsm"), "kms or seed"),
    (lambda c: c.update(surprise=1), "unknown"),
    (lambda c: c["registries"].pop("countries"), "missing"),
    (lambda c: c["registries"]["snapshots"].update(max_records=0), "1..5000"),
])
def test_bad_config_is_refused(tmp_path, edit, why):
    import yaml
    raw = yaml.safe_load((ROOT / "config" / "dedi.yaml").read_text())
    edit(raw)
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match=why):
        dedi.load_config(path)


# ---- what is published -----------------------------------------------------------------------------------------

def test_publishes_a_complete_verifiable_set(cfg, signer):
    bucket = FakeBucket()
    seed_bucket(bucket)
    events = []
    status = do_publish(bucket, cfg, signer, events=events)
    assert status["ok"] and status["date"] == "2026-11-19"
    names = set(objects(bucket))
    for obj in ("dedi/dedi.index.json", "dedi/dedi.digests.json", "dedi/dedi.data-sources.json", "dedi/dedi.countries.json",
                "dedi/dedi.snapshots.json", "dedi/schemas/data-source.v1.json", "dedi/schemas/country-snapshot.v1.json",
                "dedi/schemas/snapshot-edition.v1.json", "dedi/status.json"):
        assert obj in names, obj
    assert "2026-11-19/dedi/dedi.index.json" in bucket.objects  # the day's copy
    report = dedi.check(bucket, cfg, now=T0)
    assert report["ok"], report["problems"]
    assert [e["event"] for e in events] == ["dedi-published"]
    manifest = read(bucket, "dedi/dedi.index.json")
    assert manifest["domain"] == cfg.domain and manifest["digests"] == f"{cfg.base_url}/dedi/dedi.digests.json"
    assert [f["registry"] for f in manifest["files"]] == ["data-sources", "countries", "snapshots"]
    assert all(f["url"].startswith(cfg.base_url + "/dedi/") for f in manifest["files"])
    assert cfg.url("dedi/dedi.index.json") == cfg.base_url + "/.well-known/dedi.index.json"


def test_countries_point_at_the_latest_full_snapshot_and_today(cfg, signer):
    bucket = FakeBucket()
    seed_bucket(bucket)
    do_publish(bucket, cfg, signer)
    doc = read(bucket, "dedi/dedi.countries.json")
    by = {r["record_name"]: r["details"] for r in doc["records"]}
    assert sorted(by) == ["BR", "DE", "GB", "XX"]
    br = by["BR"]
    anchor_bytes = bucket.objects["2026-11-17/manifest.json"]["data"]
    assert br["data_url_checksum"] == hashlib.sha256(anchor_bytes).hexdigest()
    assert br["data_url"] == f"{cfg.data_base_url}/2026-11-17/manifest.json"
    assert br["full_snapshot"]["date"] == "2026-11-17" and br["full_snapshot"]["records"] == 5000 + 5100 + 40
    assert br["full_snapshot"]["signature_url"].endswith("2026-11-17/manifest.json.sigstore.json")
    assert [p["name"] for p in br["parts"]] == ["BR.br-rfb.p0of2.parquet", "BR.br-rfb.p1of2.parquet", "BR.br-rfb_gleif.p0of1.parquet"]
    for p in br["parts"]:  # permanent, content-addressed
        assert p["url"] == f"{cfg.data_base_url}/parts/{p['sha256'].split(':')[1]}.parquet"
    assert br["current"]["date"] == "2026-11-19" and br["current"]["recipe_url"].endswith("/api/v1/snapshots/2026-11-19/recipe")
    assert {s["source_id"] for s in br["sources"]} == {"br-rfb", "gleif"}
    assert by["GB"]["sources"][0]["attribution"].startswith("Contains public sector information")
    assert by["XX"]["country_name"] == "Unknown or not stated" and by["GB"]["country_name"] == "United Kingdom"
    # every record is also a valid DeDi Public-Data-Set
    public = dd.schema("Public-Data-Set.json")
    for r in doc["records"]:
        jsonschema.Draft7Validator(public).validate(r["details"])


def test_data_sources_carry_approvals_licences_and_records(cfg, signer):
    bucket = FakeBucket()
    seed_bucket(bucket)
    do_publish(bucket, cfg, signer, eff=effective(denied=("cz-justice",)))
    by = {r["record_name"]: r["details"] for r in read(bucket, "dedi/dedi.data-sources.json")["records"]}
    assert set(by) == {s["id"] for s in SOURCES}
    assert by["gleif"]["in_use"] and by["gleif"]["records"] == 1643 and by["gleif"]["licence"] == "CC0-1.0"
    assert by["br-rfb-cnpj"]["registration_authority"] == "RA000681" and by["br-rfb-cnpj"]["records_as"] == "br-rfb"
    assert by["us-co-sos"] == by["us-co-sos"] | {"approval": "pending", "in_use": False, "records": None}
    assert by["cz-justice"]["approval"] == "denied"
    assert by["gb-coh"]["licence_verified"] and not by["gleif"]["licence_verified"]
    assert all("assessment" not in d for d in by.values())  # off by default: the owner decides


def test_data_source_options(cfg, signer):
    regs = copy.deepcopy(cfg.registries)
    regs["data-sources"].update(include_pending=False, include_assessment=True)
    cfg2 = replace(cfg, registries=regs)
    bucket = FakeBucket()
    seed_bucket(bucket)
    do_publish(bucket, cfg2, signer)
    by = {r["record_name"]: r["details"] for r in read(bucket, "dedi/dedi.data-sources.json")["records"]}
    assert set(by) == {"gleif", "gb-coh", "br-rfb-cnpj"}
    assert by["gleif"]["assessment"] == {"risk": "low", "commercial_use": "allowed", "attribution_requirement": "not required",
                                         "summary": "CC0. Free."}


def test_snapshots_list_editions_newest_first(cfg, signer):
    bucket = FakeBucket()
    seed_bucket(bucket)
    do_publish(bucket, cfg, signer)
    recs = read(bucket, "dedi/dedi.snapshots.json")["records"]
    assert [r["record_name"] for r in recs] == ["2026-11-19", "2026-11-18", "2026-11-17"]
    by = {r["record_name"]: r["details"] for r in recs}
    assert by["2026-11-17"]["kind"] == "full" and len(by["2026-11-17"]["relationship_parts"]) == 1
    assert "relationship_parts" not in by["2026-11-19"] and by["2026-11-19"]["signature_url"] is None
    assert by["2026-11-18"]["base"] == "2026-11-17" and by["2026-11-18"]["decision_log"]["events"] == 12
    data = bucket.objects["2026-11-18/manifest.json"]["data"]
    assert by["2026-11-18"]["manifest_sha256"] == "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.mark.skipif(not CLI, reason="dedi-cli not installed")
def test_dedi_cli_accepts_every_published_document(cfg, signer, tmp_path):
    bucket = FakeBucket()
    seed_bucket(bucket)
    do_publish(bucket, cfg, signer)
    pub = tmp_path / "pub.json"
    pub.write_text(json.dumps(signer.public_jwk()))
    for obj in ("dedi/dedi.index.json", "dedi/dedi.data-sources.json", "dedi/dedi.countries.json", "dedi/dedi.snapshots.json"):
        path = tmp_path / obj.replace("/", "_")
        path.write_bytes(bucket.objects[obj]["data"])
        r = subprocess.run([CLI, "verify", "--in", str(path), "--key", str(pub)], capture_output=True, text=True)
        assert r.returncode == 0, obj + r.stdout + r.stderr


# ---- day after day ---------------------------------------------------------------------------------------------

def test_an_unchanged_day_keeps_updated_at_and_the_manifest(cfg, signer):
    bucket = FakeBucket()
    seed_bucket(bucket)
    do_publish(bucket, cfg, signer)
    first = objects(bucket)
    later = T0 + timedelta(hours=20)
    status = do_publish(bucket, cfg, signer, now=later)
    assert "dedi/dedi.index.json" in status["unchanged"]  # keys and files unchanged, far from stale
    for name in ("dedi.data-sources", "dedi.countries", "dedi.snapshots"):
        before, after = json.loads(first[f"dedi/{name}.json"]), read(bucket, f"dedi/{name}.json")
        assert after["registry"]["updated_at"] == before["registry"]["updated_at"]  # content didn't change
        assert after["next_update"] > before["next_update"]  # but it was re-issued: still fresh
    # a re-issued file has new bytes (its next_update moved), so its digest, and the digests file, change
    assert read(bucket, "dedi/dedi.digests.json")["updated_at"] == dd.utc(later)
    assert dedi.check(bucket, cfg, now=later)["ok"]


def test_a_new_day_moves_updated_at(cfg, signer):
    bucket = FakeBucket()
    seed_bucket(bucket)
    do_publish(bucket, cfg, signer)
    seed_bucket(bucket, days=(("2026-11-17", "full", None), ("2026-11-18", "changes", "2026-11-17"),
                              ("2026-11-19", "changes", "2026-11-18"), ("2026-11-20", "changes", "2026-11-19")), extra=7)
    t1 = T0 + timedelta(days=1)
    do_publish(bucket, cfg, signer, now=t1)
    c = read(bucket, "dedi/dedi.countries.json")
    assert c["registry"]["updated_at"] == dd.utc(t1) and c["registry"]["meta"]["built_from"] == "2026-11-20"
    assert read(bucket, "dedi/dedi.digests.json")["updated_at"] == dd.utc(t1)
    assert dedi.check(bucket, cfg, now=t1)["ok"]


def test_the_manifest_is_reissued_before_it_goes_stale(cfg, signer):
    bucket = FakeBucket()
    seed_bucket(bucket)
    do_publish(bucket, cfg, signer)
    first = read(bucket, "dedi/dedi.index.json")
    t = T0 + timedelta(hours=cfg.manifest_hours) - dedi.MANIFEST_REISSUE_BEFORE + timedelta(hours=1)
    do_publish(bucket, cfg, signer, now=t)
    again = read(bucket, "dedi/dedi.index.json")
    assert again["next_update"] > first["next_update"] and again["updated_at"] == first["updated_at"]


def test_key_rotation_lists_both_keys_and_signs_with_the_new_one(cfg, signer):
    bucket = FakeBucket()
    seed_bucket(bucket)
    do_publish(bucket, cfg, signer)
    new = jws.SeedSigner("lc-dedi-2", bytes(range(1, 33)))
    rotating = replace(cfg, kid="lc-dedi-2", also_list=(signer.public_jwk(),))
    do_publish(bucket, rotating, new, now=T0 + timedelta(hours=1))
    m = read(bucket, "dedi/dedi.index.json")
    assert [k["kid"] for k in m["keys"]] == ["lc-dedi-2", "lc-dedi-1"] and m["proof"]["verification_method"] == "lc-dedi-2"
    assert m["updated_at"] == dd.utc(T0 + timedelta(hours=1))  # the key list changed
    assert dedi.check(bucket, rotating, now=T0 + timedelta(hours=1))["ok"]


# ---- failures: nothing bad is ever left published ----------------------------------------------------------------

def good_set(cfg, signer):
    bucket = FakeBucket()
    seed_bucket(bucket)
    do_publish(bucket, cfg, signer)
    return bucket, objects(bucket)


def test_no_snapshot_yet_fails_cleanly(cfg, signer):
    bucket, events = FakeBucket(), []
    with pytest.raises(dedi.PublishError, match="latest.json"):
        do_publish(bucket, cfg, signer, events=events)
    assert set(objects(bucket)) == {"dedi/status.json"}
    status = read(bucket, "dedi/status.json")
    assert not status["ok"] and "latest.json" in status["error"] and status["lastGood"] is None
    assert [e["event"] for e in events] == ["dedi-failed"]  # deterministic: not retried


def test_a_missing_anchor_is_not_retried_and_keeps_the_previous_set(cfg, signer):
    bucket, before = good_set(cfg, signer)
    del bucket.objects["2026-11-17/manifest.json"]
    events = []
    with pytest.raises(dedi.PublishError, match="anchor"):
        do_publish(bucket, cfg, signer, now=T0 + timedelta(hours=2), events=events)
    after = objects(bucket)
    assert {k: v for k, v in after.items() if k != "dedi/status.json"} == {k: v for k, v in before.items() if k != "dedi/status.json"}
    assert read(bucket, "dedi/status.json")["lastGood"] == dd.utc(T0)
    assert [e["event"] for e in events] == ["dedi-failed"]
    assert dedi.check(bucket, cfg, now=T0 + timedelta(hours=2))["ok"]  # the previous set still verifies


class FlakySigner:
    """Fails the first ``n`` signatures, like a KMS outage."""

    def __init__(self, inner, n):
        self.inner, self.n, self.kid = inner, n, inner.kid

    def public_jwk(self):
        return self.inner.public_jwk()

    def sign_raw(self, message):
        if self.n > 0:
            self.n -= 1
            raise ConnectionError("KMS unavailable")
        return self.inner.sign_raw(message)


def test_a_kms_outage_is_retried_then_succeeds(cfg, signer):
    bucket = FakeBucket()
    seed_bucket(bucket)
    events, waits = [], []
    status = dedi.publish(bucket, cfg, FlakySigner(signer, 2), sources=SOURCES, effective=effective(), now=Clock(),
                          record=events.append, wait=waits.append)
    assert status["ok"] and status["attempts"] == 3 and waits == [10, 20]
    assert [e["event"] for e in events] == ["dedi-retry", "dedi-retry", "dedi-published"]


def test_a_lasting_kms_outage_fails_after_the_retries_and_keeps_the_previous_set(cfg, signer):
    bucket, before = good_set(cfg, signer)
    events = []
    with pytest.raises(dedi.PublishError, match="KMS unavailable"):
        dedi.publish(bucket, cfg, FlakySigner(signer, 99), sources=SOURCES, effective=effective(),
                     now=Clock(T0 + timedelta(hours=3)), record=events.append, wait=lambda s: None)
    assert [e["event"] for e in events] == ["dedi-retry", "dedi-retry", "dedi-failed"]
    assert all(objects(bucket)[k] == v for k, v in before.items() if k != "dedi/status.json")
    status = read(bucket, "dedi/status.json")
    assert not status["ok"] and status["lastGood"] == dd.utc(T0)
    # the set still published (the last good one) stays listed on the System page
    assert status["files"] and status["files"] == read(bucket, "dedi/status.json")["files"] and status["date"]


class CorruptingBucket(FakeBucket):
    """Stores damaged bytes for ``name`` the first ``times`` writes (a storage fault), or only for writes
    ``spare`` doesn't match."""

    def __init__(self, name, times, spare=lambda data: False):
        super().__init__()
        self.target, self.times, self.spare = name, times, spare

    def blob(self, name):
        b = super().blob(name)
        if name == self.target:
            original = b.upload_from_string

            def upload(data, content_type=None, if_generation_match=None):
                data = data.encode() if isinstance(data, str) else data
                if self.times > 0 and not self.spare(data):
                    self.times -= 1
                    data = data + b"garbage"
                original(data, content_type=content_type)
            b.upload_from_string = upload
        return b


def test_a_damaged_write_is_rolled_back_and_retried(cfg, signer):
    bucket = CorruptingBucket("dedi/dedi.digests.json", 1)
    seed_bucket(bucket)
    events = []
    status = do_publish(bucket, cfg, signer, events=events)
    assert status["ok"] and status["attempts"] == 2
    assert [e["event"] for e in events] == ["dedi-restored", "dedi-retry", "dedi-published"]
    assert dedi.check(bucket, cfg, now=T0)["ok"]


def test_a_lasting_storage_fault_restores_the_previous_set(cfg, signer):
    good, before = good_set(cfg, signer)
    # every write of a new digests file is damaged (it is rewritten every day; the manifest may be reused),
    # while putting the previous one back works
    old = before["dedi/dedi.digests.json"]
    bucket = CorruptingBucket("dedi/dedi.digests.json", 99, spare=lambda data: data == old)
    bucket.objects = copy.deepcopy(good.objects)
    seed_bucket(bucket, days=(("2026-11-17", "full", None), ("2026-11-18", "changes", "2026-11-17"),
                              ("2026-11-19", "changes", "2026-11-18"), ("2026-11-20", "changes", "2026-11-19")), extra=3)
    t = T0 + timedelta(days=1)
    with pytest.raises(dedi.PublishError):
        do_publish(bucket, replace(cfg, kid="lc-dedi-1"), jws.SeedSigner("lc-dedi-1", bytes(range(32))), now=t)
    for k, v in before.items():  # the 19th's set is back, byte for byte
        if k != "dedi/status.json":
            assert bucket.objects[k]["data"] == v, k
    report = dedi.check(bucket, cfg, now=t)
    assert not report["ok"] and any("not the latest day 2026-11-20" in p for p in report["problems"])  # honest: behind


def test_a_failed_first_publication_leaves_nothing_but_the_status(cfg, signer):
    bucket = CorruptingBucket("dedi/dedi.index.json", 99)
    seed_bucket(bucket)
    with pytest.raises(dedi.PublishError):
        do_publish(bucket, cfg, signer)
    assert set(objects(bucket)) == {"dedi/status.json"}


def test_an_invalid_record_never_reaches_the_bucket(cfg, signer):
    bucket, before = good_set(cfg, signer)
    bad = copy.deepcopy(SOURCES)
    bad[0]["url"] = "not a url"
    with pytest.raises(dedi.PublishError, match="url"):
        dedi.publish(bucket, cfg, signer, sources=bad, effective=effective(), now=Clock(T0 + timedelta(hours=1)),
                     record=lambda e: None, wait=lambda s: None)
    assert all(objects(bucket)[k] == v for k, v in before.items() if k != "dedi/status.json")


def test_disabled_publication_and_registries(cfg, signer):
    bucket = FakeBucket()
    seed_bucket(bucket)
    with pytest.raises(dedi.PublishError, match="disabled"):
        do_publish(bucket, replace(cfg, enabled=False), signer)
    regs = copy.deepcopy(cfg.registries)
    regs["snapshots"]["enabled"] = False
    do_publish(bucket, replace(cfg, registries=regs), signer)
    m = read(bucket, "dedi/dedi.index.json")
    assert [f["registry"] for f in m["files"]] == ["data-sources", "countries"]
    assert dedi.check(bucket, replace(cfg, registries=regs), now=T0)["ok"]


# ---- the independent check ----------------------------------------------------------------------------------

@pytest.mark.parametrize("damage,expect", [
    (lambda b: b.objects.pop("dedi/dedi.countries.json"), "countries: fetch"),
    (lambda b: b.objects["dedi/dedi.countries.json"].update(data=b.objects["dedi/dedi.countries.json"]["data"].replace(b'"BR"', b'"BX"', 1)), "countries"),
    (lambda b: b.objects["dedi/dedi.digests.json"].update(data=b"{}"), "digests"),
    (lambda b: b.objects["dedi/dedi.index.json"].update(data=b"not json"), "manifest"),
    (lambda b: b.objects.pop("dedi/dedi.index.json"), "manifest: fetch"),
])
def test_check_finds_damage(cfg, signer, damage, expect):
    bucket, _ = good_set(cfg, signer)
    damage(bucket)
    report = dedi.check(bucket, cfg, now=T0)
    assert not report["ok"] and any(p.startswith(expect) for p in report["problems"]), report["problems"]


def test_check_warns_before_anything_goes_stale(cfg, signer):
    bucket, _ = good_set(cfg, signer)
    assert dedi.check(bucket, cfg, now=T0 + timedelta(hours=20), min_hours_left=12)["ok"]
    late = dedi.check(bucket, cfg, now=T0 + timedelta(hours=30), min_hours_left=12)
    assert not late["ok"] and any("goes stale" in p for p in late["problems"])
    gone = dedi.check(bucket, cfg, now=T0 + timedelta(hours=40))
    assert not gone["ok"] and any("freshness" in p for p in gone["problems"])


def test_check_sees_a_set_behind_the_latest_day(cfg, signer):
    bucket, _ = good_set(cfg, signer)
    put(bucket, "latest.json", {"schemaVersion": 2, "date": "2026-11-20", "anchor": "2026-11-17"})
    report = dedi.check(bucket, cfg, now=T0)
    assert not report["ok"] and sum("not the latest day" in p for p in report["problems"]) == 2


def test_check_rejects_a_set_signed_by_a_key_we_do_not_list(cfg, signer):
    bucket, _ = good_set(cfg, signer)
    rogue = jws.SeedSigner("lc-dedi-1", bytes(32))
    doc = read(bucket, "dedi/dedi.countries.json")
    doc["publisher"]["key"] = rogue.public_jwk()
    bucket.blob("dedi/dedi.countries.json").upload_from_string(dd.serialize(dd.sign_document(doc, rogue)))
    report = dedi.check(bucket, cfg, now=T0)
    assert not report["ok"] and any("countries" in p for p in report["problems"])


# ---- the signing key ----------------------------------------------------------------------------------------

class NotFound(Exception):
    pass


class Secrets:
    def __init__(self):
        self.versions = []

    def read(self, name):
        assert name.endswith("/versions/latest")
        if not self.versions:
            raise NotFound(name)
        return self.versions[-1]

    def write(self, name, data):
        assert not name.endswith("latest")
        self.versions.append(data)


def test_the_key_is_created_once_by_the_first_publication_and_then_reused(cfg):
    store, events = Secrets(), []
    first = dedi.make_signer(cfg, store.read, store.write, record=events.append)
    again = dedi.make_signer(cfg, store.read, store.write, record=events.append)
    assert first.public_jwk() == again.public_jwk() and first.kid == cfg.kid
    assert len(store.versions) == 1 and [e["event"] for e in events] == ["dedi-key-created"]
    assert "d" in json.loads(store.versions[0]) and "d" not in events[0]  # the private part goes only to the secret


def test_a_key_with_another_kid_or_an_unreadable_secret_stops_publication(cfg):
    store = Secrets()
    store.versions.append(json.dumps(jws.SeedSigner("someone-else", bytes(32)).private_jwk()).encode())
    with pytest.raises(ValueError, match="someone-else"):
        dedi.make_signer(cfg, store.read, store.write)

    def denied(name):
        raise PermissionError("403")
    with pytest.raises(PermissionError):  # never a new key because the old one couldn't be read
        dedi.make_signer(cfg, denied, store.write)
    assert len(store.versions) == 1

