import csv
import io
import json
import zipfile

import pytest

from lc_core.control import SystemHalted
from lc_core.fetch import FetchError
from ingest_gb.job import PAGE, run

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


PAGE_HTML = b'<a href="BasicCompanyData-2026-11-01-part1_7.zip">BasicCompanyData-2026-11-01-part1_7.zip (69Mb)</a>'


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

    def read_manifest(self):
        return {**self.manifest, "processed": list(self.manifest["processed"])}

    def write_manifest(self, m):
        self.manifest = m

    def read_shard(self, n):
        return dict(self.shards.get(n, {}))

    def write_shard(self, n, records):
        self.shards[n] = dict(records)

    def write_changes(self, name, changes):
        self.changes[name] = changes

    def keep_source(self, name, path):
        self.sources.append(name)


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


def test_same_month_twice_downloads_only_the_page():
    store = MemStore()
    go(FakeGateway([zipped([row("01234567")])]), store)
    gw = FakeGateway([])
    assert go(gw, store) == "already-done" and gw.fetched == [PAGE]


def test_paused_fetches_nothing():
    def gate():
        raise SystemHalted("paused")

    gw = FakeGateway([])
    assert go(gw, MemStore(), gate) == "halted" and gw.fetched == []


def test_changed_page_layout_fails_loudly():
    with pytest.raises(FetchError):
        go(FakeGateway([], page=b"<html>moved</html>"), MemStore())
