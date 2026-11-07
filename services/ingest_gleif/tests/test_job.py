import csv
import gzip
import io
import json
import zipfile

import pytest

from lc_core.control import SystemHalted
from lc_core.fetch import Denied
from lc_ingest.store import decode_records, shard_of
from ingest_gleif.job import LISTING, choose, run

FIELDS = ["LEI", "Entity.LegalName", "Entity.LegalAddress.FirstAddressLine", "Entity.LegalAddress.City",
          "Entity.LegalAddress.Country", "Entity.RegistrationAuthority.RegistrationAuthorityID",
          "Entity.RegistrationAuthority.RegistrationAuthorityEntityID", "Entity.LegalJurisdiction",
          "Entity.EntityCategory", "Entity.EntityStatus"]


def row(lei, number, name="EXAMPLE LIMITED", category="GENERAL", status="ACTIVE"):
    return [lei, name, "1 Example Street", "London", "GB", "RA000585", number, "GB", category, status]


def zipped(rows):
    text = io.StringIO()
    w = csv.writer(text)
    w.writerow(FIELDS)
    w.writerows(rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("golden.csv", text.getvalue())
    return buf.getvalue()


class FakeGateway:
    def __init__(self, files, publish="2026-11-07 08:00:00", deny=None):
        self.files, self.publish, self.deny = list(files), publish, deny
        self.fetched = []

    def get_json(self, target):
        assert target == LISTING
        if self.deny:
            raise Denied(503, self.deny, "paused")
        delta = {k: {"csv": {"url": f"https://goldencopy.gleif.org/{k}.zip"}} for k in ("LastDay", "LastWeek", "LastMonth")}
        return {"data": {"publish_date": self.publish,
                         "lei2": {"full_file": {"csv": {"url": "https://goldencopy.gleif.org/full.zip"}}, "delta_files": delta}}}

    def download(self, target, file):
        self.fetched.append(target)
        data = self.files.pop(0)
        file.write(data)
        return len(data)


class MemStore:
    def __init__(self):
        self.manifest, self.shards, self.changes, self.sources, self.writes = {"records": 0, "processed": []}, {}, {}, [], []

    def read_manifest(self):
        return json.loads(json.dumps(self.manifest))

    def write_manifest(self, m):
        self.writes.append("manifest")
        self.manifest = m

    def read_shard(self, n):
        return dict(self.shards.get(n, {}))

    def write_shard_file(self, n, path):
        self.writes.append("shard")
        with open(path, "rb") as f:
            self.shards[n] = decode_records(f.read())

    def write_changes_file(self, name, path):
        self.writes.append("changes")
        with gzip.open(path, "rt") as f:
            self.changes[name] = [json.loads(line) for line in f]

    def keep_source(self, name, path):
        self.sources.append(name)


def all_records(store):
    return {k: v for s in store.shards.values() for k, v in s.items()}


def go(gw, store, gate=lambda: None):
    logs = []
    return run(gw, store, gate=gate, log=logs.append), logs


def lei(i):
    return f"5493001KJTIIGC8Y{i:04d}"


def test_first_run_loads_the_full_file():
    store, gw = MemStore(), FakeGateway([zipped([row(lei(1), "01234567"), row(lei(2), "07654321")])])
    out, _ = go(gw, store)
    assert out == "done" and gw.fetched == ["https://goldencopy.gleif.org/full.zip"]
    assert store.manifest["records"] == 2 and len(all_records(store)) == 2
    assert store.sources == ["2026-11-07T080000-full.zip"] and store.manifest["lastKey"] == "2026-11-07 08:00:00"
    assert all(shard_of(k) == n for n, s in store.shards.items() for k in s)


@pytest.mark.parametrize("last,publish,kind", [
    ("2026-11-06 08:00:00", "2026-11-07 08:00:00", "LastDay"),
    ("2026-11-05 08:00:00", "2026-11-07 08:00:00", "LastWeek"),
    ("2026-10-20 08:00:00", "2026-11-07 08:00:00", "LastMonth"),
    ("2026-09-01 08:00:00", "2026-11-07 08:00:00", "full"),
])
def test_the_delta_always_covers_the_gap(last, publish, kind):
    assert choose({"records": 5, "lastKey": last}, publish) == kind


@pytest.mark.parametrize("manifest", [{"records": 0}, {"records": 16717}, {"records": 0, "lastKey": "2026-11-06 08:00:00"}])
def test_no_baseline_means_a_full_load(manifest):
    assert choose(manifest, "2026-11-07 08:00:00") == "full"


def test_writes_changes_then_records_then_manifest():
    store = MemStore()
    go(FakeGateway([zipped([row(lei(1), "01234567")])]), store)
    assert store.writes[0] == "changes" and store.writes[-1] == "manifest" and set(store.writes[1:-1]) == {"shard"}


def test_same_publish_twice_does_nothing():
    store = MemStore()
    go(FakeGateway([zipped([row(lei(1), "01234567")])]), store)
    gw = FakeGateway([])
    assert go(gw, store)[0] == "already-done" and gw.fetched == []


def test_paused_system_fetches_nothing():
    def gate():
        raise SystemHalted("system is paused")

    gw, store = FakeGateway([]), MemStore()
    assert go(gw, store, gate)[0] == "halted" and gw.fetched == [] and store.writes == []


def test_gateway_refusal_is_reported_not_crashed():
    store = MemStore()
    assert go(FakeGateway([], deny="system-not-running"), store)[0] == "denied" and store.writes == []


def test_next_day_takes_the_day_delta_and_records_only_what_changed():
    store = MemStore()
    go(FakeGateway([zipped([row(lei(1), "01234567")])], publish="2026-11-06 08:00:00"), store)
    gw = FakeGateway([zipped([row(lei(1), "01234567", status="INACTIVE")])])
    go(gw, store)
    assert gw.fetched == ["https://goldencopy.gleif.org/LastDay.zip"]
    (change,) = store.changes["2026-11-07T080000-LastDay"]
    assert change["fieldPath"] == "legalStatus" and change["newValue"] == "INACTIVE" and store.manifest["records"] == 1


def test_crash_before_manifest_reruns_without_duplicates():
    store = MemStore()
    files = [zipped([row(lei(1), "01234567")])] * 2
    gw = FakeGateway(files)
    store.write_manifest = lambda m: (_ for _ in ()).throw(RuntimeError("crash"))
    with pytest.raises(RuntimeError):
        go(gw, store)
    del store.write_manifest
    out, _ = go(gw, store)
    assert out == "done" and store.changes["2026-11-07T080000-full"] == [] and len(all_records(store)) == 1


def test_sole_proprietors_never_stored():
    store = MemStore()
    go(FakeGateway([zipped([row(lei(1), "01234567", category="SOLE_PROPRIETOR")])]), store)
    assert all_records(store) == {} and store.manifest["records"] == 0


def test_shared_registration_kept_apart_across_shards():
    store = MemStore()
    go(FakeGateway([zipped([row(lei(1), "01234567"), row(lei(2), "01234567")])]), store)
    records = all_records(store)
    assert len(records) == 2 and store.manifest["records"] == 2
    assert any(r.get("labels") for r in records.values())
    assert all(shard_of(k) == n for n, s in store.shards.items() for k in s)


def test_mass_change_to_existing_records_is_held_and_nothing_written():
    store = MemStore()
    go(FakeGateway([zipped([row(lei(i), f"{i:08d}") for i in range(200)])], publish="2026-11-06 08:00:00"), store)
    before = list(store.writes)
    rows = [row(lei(i), f"{i:08d}", status="INACTIVE") for i in range(200)]
    out, _ = go(FakeGateway([zipped(rows)]), store)
    assert out == "held" and store.writes == before and "2026-11-07 08:00:00" not in store.manifest["processed"]


def test_many_new_records_are_not_a_mass_change():
    """A full load adds millions of records; only edits to existing ones look suspicious."""
    store = MemStore()
    go(FakeGateway([zipped([row(lei(i), f"{i:08d}") for i in range(150)])], publish="2026-11-06 08:00:00"), store)
    rows = [row(lei(i), f"{i:08d}") for i in range(150, 2150)]
    out, _ = go(FakeGateway([zipped(rows)]), store)
    assert out == "done" and store.manifest["records"] == 2150


def test_every_record_lands_in_its_own_shard_at_scale():
    store = MemStore()
    go(FakeGateway([zipped([row(lei(i), f"{i:08d}") for i in range(3000)])]), store)
    records = all_records(store)
    assert len(records) == 3000 and len(store.shards) > 200
    assert all(shard_of(k) == n for n, s in store.shards.items() for k in s)
