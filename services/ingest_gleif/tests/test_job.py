import csv
import io
import zipfile

import pytest

from lc_core.control import SystemHalted
from lc_core.fetch import Denied
from ingest_gleif.job import LISTING, run
from ingest_gleif.store import shard_of

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
        z.writestr("delta.csv", text.getvalue())
    return buf.getvalue()


class FakeGateway:
    def __init__(self, files, publish="2026-11-07 08:00:00", deny=None):
        self.files, self.publish, self.deny = files, publish, deny
        self.fetched = []

    def get_json(self, target):
        assert target == LISTING
        if self.deny:
            raise Denied(503, self.deny, "paused")
        return {"data": {"publish_date": self.publish,
                         "lei2": {"delta_files": {"LastDay": {"csv": {"url": "https://goldencopy.gleif.org/d.zip"}}}}}}

    def download(self, target, file):
        self.fetched.append(target)
        data = self.files.pop(0)
        file.write(data)
        return len(data)


class MemStore:
    def __init__(self):
        self.manifest, self.shards, self.changes, self.sources, self.writes = {"records": 0, "processed": []}, {}, {}, [], []

    def read_manifest(self):
        return {**self.manifest, "processed": list(self.manifest["processed"])}

    def write_manifest(self, m):
        self.writes.append("manifest")
        self.manifest = m

    def read_shard(self, n):
        return dict(self.shards.get(n, {}))

    def write_shard(self, n, records):
        self.writes.append("shard")
        self.shards[n] = dict(records)

    def write_changes(self, name, changes):
        self.writes.append("changes")
        self.changes[name] = changes

    def keep_source(self, name, path):
        self.sources.append(name)


def all_records(store):
    return {k: v for s in store.shards.values() for k, v in s.items()}


def go(gw, store, gate=lambda: None):
    logs = []
    return run(gw, store, gate=gate, log=logs.append), logs


def test_first_run_loads_records_and_keeps_the_source():
    store = MemStore()
    out, logs = go(FakeGateway([zipped([row("5493001KJTIIGC8Y1R12", "01234567"), row("529900T8BM49AURSDO55", "07654321")])]), store)
    assert out == "done" and store.manifest["records"] == 2 and len(all_records(store)) == 2
    assert store.sources == ["2026-11-07T080000-LastDay.zip"]
    assert all(shard_of(k) == n for n, s in store.shards.items() for k in s)


def test_writes_changes_then_records_then_manifest():
    store = MemStore()
    go(FakeGateway([zipped([row("5493001KJTIIGC8Y1R12", "01234567")])]), store)
    assert store.writes == ["changes", "shard", "manifest"]


def test_same_publish_twice_does_nothing():
    store = MemStore()
    go(FakeGateway([zipped([row("5493001KJTIIGC8Y1R12", "01234567")])]), store)
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


def test_next_day_records_only_what_changed():
    store = MemStore()
    go(FakeGateway([zipped([row("5493001KJTIIGC8Y1R12", "01234567")])], publish="2026-11-06 08:00:00"), store)
    go(FakeGateway([zipped([row("5493001KJTIIGC8Y1R12", "01234567", status="INACTIVE")])]), store)
    (change,) = store.changes["2026-11-07T080000-LastDay"]
    assert change["fieldPath"] == "legalStatus" and change["newValue"] == "INACTIVE"
    assert store.manifest["records"] == 1


def test_crash_before_manifest_reruns_without_duplicates():
    store = MemStore()
    files = [zipped([row("5493001KJTIIGC8Y1R12", "01234567")])] * 2
    gw = FakeGateway(list(files))
    store.write_manifest = lambda m: (_ for _ in ()).throw(RuntimeError("crash"))
    with pytest.raises(RuntimeError):
        go(gw, store)
    del store.write_manifest
    out, _ = go(gw, store)
    assert out == "done" and store.changes["2026-11-07T080000-LastDay"] == [] and len(all_records(store)) == 1


def test_sole_proprietors_never_stored():
    store = MemStore()
    go(FakeGateway([zipped([row("5493001KJTIIGC8Y1R12", "01234567", category="SOLE_PROPRIETOR")])]), store)
    assert all_records(store) == {} and store.manifest["records"] == 0


def test_shared_registration_kept_apart_across_shards():
    store = MemStore()
    go(FakeGateway([zipped([row("5493001KJTIIGC8Y1R12", "01234567"), row("529900T8BM49AURSDO55", "01234567")])]), store)
    records = all_records(store)
    assert len(records) == 2 and store.manifest["records"] == 2
    assert any(r.get("labels") for r in records.values())


def test_mass_change_is_held_and_nothing_written():
    store = MemStore()
    rows = [row(f"5493001KJTIIGC8Y{i:04d}", f"{i:08d}") for i in range(200)]
    go(FakeGateway([zipped(rows)], publish="2026-11-06 08:00:00"), store)
    before = list(store.writes)
    out, logs = go(FakeGateway([zipped([row(f"5493001KJTIIGC8Y{i:04d}", f"{i:08d}", status="INACTIVE") for i in range(200)])]), store)
    assert out == "held" and store.writes == before and "2026-11-07 08:00:00" not in store.manifest["processed"]
