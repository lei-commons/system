import csv
import gzip
import io
import json
import zipfile

import pytest

from lc_core.control import SystemHalted
from lc_core.fetch import FetchError
from lc_ingest.store import decode_records
from ingest_gb.job import ABSENT, PAGE, reconcile, run

FIELDS = ["CompanyName", " CompanyNumber", "RegAddress.CareOf", "RegAddress.AddressLine1", "RegAddress.PostTown",
          "RegAddress.Country", "RegAddress.PostCode", "CompanyCategory", "CompanyStatus", "IncorporationDate"]


def zipped(rows):
    text = io.StringIO()
    w = csv.writer(text)
    w.writerow(FIELDS)
    w.writerows(rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("part1.csv", text.getvalue())
    return buf.getvalue()


def row(number, status="Active"):
    return [f"COMPANY {number} LIMITED", number, "MRS JANE PERSON", "1 Street", "LONDON", "ENGLAND", "EC1A 1AA",
            "Private Limited Company", status, "03/02/2001"]


def page(date="2026-11-01", parts=range(1, 8)):
    return "".join(f'<a href="BasicCompanyData-{date}-part{k}_7.zip">BasicCompanyData-{date}-part{k}_7.zip (69Mb)</a>'
                   for k in parts).encode()


PAGE_HTML = page()


class FakeGateway:
    def __init__(self, files, page=PAGE_HTML):
        self.files, self.page, self.fetched = list(files), page, []

    def download(self, target, file):
        self.fetched.append(target)
        data = self.page if target == PAGE else self.files.pop(0)
        file.write(data)
        return len(data)


class MemStore:
    def __init__(self):
        self.manifest, self.shards, self.changes, self.sources = {"records": 0, "processed": []}, {}, {}, []
        self.seen, self.markers = {}, {}

    def read_manifest(self):
        return {**self.manifest, "processed": list(self.manifest["processed"])}

    def write_manifest(self, m):
        self.manifest = m

    def read_shard(self, n):
        return dict(self.shards.get(n, {}))

    def write_shard_file(self, n, path):
        with open(path, "rb") as f:
            self.shards[n] = decode_records(f.read())

    def write_changes_file(self, name, path):
        with gzip.open(path, "rt") as f:
            self.changes[name] = [json.loads(line) for line in f]

    def keep_source(self, name, path):
        self.sources.append(name)

    def read_seen(self, name):
        return self.seen.get(name)

    def keep_seen(self, name, ids):
        self.seen[name] = set(ids)

    def has_marker(self, name):
        return name in self.markers

    def write_marker(self, name, body):
        self.markers[name] = body

    def write_shard(self, n, recs):
        self.shards[n] = dict(recs)


def records(store):
    return {k: v for s in store.shards.values() for k, v in s.items()}


def go(gw, store, gate=lambda: None):
    return run(gw, store, gate=gate, log=lambda r: None)


def test_loads_the_part_1_file():
    store, gw = MemStore(), FakeGateway([zipped([row("01234567"), row("SC123456")])])
    assert go(gw, store) == "done" and store.manifest["records"] == 2
    assert gw.fetched[1] == "https://download.companieshouse.gov.uk/BasicCompanyData-2026-11-01-part1_7.zip"
    assert store.sources == ["BasicCompanyData-2026-11-01-part1_7.zip"] and "2026-11-01" in store.manifest["processed"]


def test_care_of_names_never_reach_storage():
    store = MemStore()
    go(FakeGateway([zipped([row("01234567")])]), store)
    assert "JANE" not in json.dumps([records(store), store.changes])


def test_each_run_takes_the_next_part_and_a_finished_month_only_reads_the_page():
    store = MemStore()
    for k in range(1, 8):
        gw = FakeGateway([zipped([row(f"0123456{k}")])])
        assert go(gw, store) == "done"
        assert gw.fetched[1].endswith(f"BasicCompanyData-2026-11-01-part{k}_7.zip")
    assert store.manifest["processed"][:2] == ["2026-11-01", "2026-11-01 part2"] and store.manifest["records"] == 7
    gw = FakeGateway([])
    assert go(gw, store) == "already-done" and gw.fetched == [PAGE]


def test_a_month_with_missing_parts_fails_loudly():
    with pytest.raises(FetchError):
        go(FakeGateway([], page=page(parts=range(1, 6))), MemStore())


def month(store, date, numbers_by_part):
    for k in range(1, 8):
        rows = [row(n) for n in numbers_by_part.get(k, [])] or [row(f"99{k}{date[5:7]}0000")]
        go(FakeGateway([zipped(rows)], page=page(date)), store)


def test_companies_missing_from_every_part_are_labelled_then_unlabelled_when_back():
    store = MemStore()
    month(store, "2026-11-01", {1: ["01000001", "01000002"], 2: ["01000003"]})
    assert reconcile(store, log=lambda r: None) == "reconciled"
    assert not any(ABSENT in r.get("labels", []) for r in records(store).values())

    month(store, "2026-12-01", {1: ["01000001"], 3: ["01000003"]})  # 01000002 is gone
    events = []
    assert reconcile(store, log=events.append) == "reconciled"
    gone = [r for r in records(store).values() if ABSENT in r.get("labels", [])]
    assert [r["registrationNumber"] for r in gone if r["registrationNumber"].startswith("0100")] == ["01000002"]
    assert gone[0]["legalStatus"] == "Active"  # absence alone doesn't change the status
    assert any(c["fieldPath"] == "labels" for c in store.changes["reconcile-2026-12-01"])
    assert events[-1]["event"] == "reconciled" and reconcile(store, log=lambda r: None) == "already-done"

    month(store, "2027-01-01", {1: ["01000001", "01000002"], 2: ["01000003"]})  # it came back
    back = next(r for r in records(store).values() if r["registrationNumber"] == "01000002")
    assert ABSENT not in back.get("labels", [])


def test_reconcile_waits_for_all_parts_and_skips_a_month_without_id_lists():
    store = MemStore()
    go(FakeGateway([zipped([row("01000001")])]), store)
    assert reconcile(store, log=lambda r: None) == "waiting"
    month(store, "2026-11-01", {})
    store.seen.pop("2026-11-01")  # as if part 1 was loaded before the IDs were kept
    assert reconcile(store, log=lambda r: None) == "skipped" and "reconciled-2026-11-01" in store.markers


def test_paused_fetches_nothing():
    def gate():
        raise SystemHalted("paused")

    gw = FakeGateway([])
    assert go(gw, MemStore(), gate) == "halted" and gw.fetched == []


def test_changed_page_layout_fails_loudly():
    with pytest.raises(FetchError):
        go(FakeGateway([], page=b"<html>moved</html>"), MemStore())
