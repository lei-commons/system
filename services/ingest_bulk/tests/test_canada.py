"""Canada (Corporations Canada): four files processed in turn each week, and no street addresses kept."""

import gzip
import json
from datetime import date

from lc_core.allowlist import AllowList
from lc_core.directory_id import mint
from lc_ingest.bulk import CANADA, ca_record
from lc_ingest.gleif import Skip
from lc_ingest.store import decode_records

import pytest

from ingest_bulk.job import run

HEADER = ("Corporation number,Business number (BN),Corporate name - form 1,Corporate name - form 2,Governing legislation,Status,"
          "Status Detail,Anniversary date,Year of last annual filing,Date of last annual meeting,Street,Street 2,City/town,"
          "Province/territory,Country,Postal code,Minimum number of directors,Maximum number of directors\n")
ACTIVE = ('8284083,801423500,Superior HVAC Inc.,,Canada Business Corporations Act,Active,,2012-09-01,2024,2024-10-21,'
          '6 DeSantis Crt.,,Hamilton,ON,CA,L8W 3A9,1,1\n')
DISSOLVED = ('68217,119924504,LES PLACEMENTS X INC.,X HOLDINGS INC.,Canada Business Corporations Act,Dissolved,,1975-01-01,,,'
             '"Apt 2506, 30 Grand Trunk Cres",,Toronto,ON,CA,M5J 3A4,1,10\n')


class FakeGateway:
    def __init__(self, bodies):
        self.bodies, self.fetched = bodies, []

    def download(self, target, file):
        self.fetched.append(target)
        body = self.bodies.get(target.rsplit("/", 1)[-1], HEADER).encode("utf-8")
        file.write(body)
        return len(body)


class MemStore:
    def __init__(self):
        self.manifest, self.shards, self.changes = {"records": 0, "processed": []}, {}, {}

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
        pass


def test_canada_keeps_the_street_when_given_and_never_requires_it():
    import csv
    import io

    row = next(csv.DictReader(io.StringIO(HEADER + DISSOLVED)))
    r = CANADA.record(row)
    assert r["directoryId"] == mint("CA", "RA000072", "68217") and r["otherNames"] == ["X HOLDINGS INC."]
    assert r["legalAddress"] == {"addressLines": ["Apt 2506, 30 Grand Trunk Cres"], "city": "TORONTO", "region": "ON",
                                 "postalCode": "M5J 3A4", "country": "CA"}
    assert r["legalStatus"] == "Dissolved"
    bare = CANADA.record(dict(row, Street=""))
    assert "addressLines" not in bare["legalAddress"] and bare["legalAddress"]["city"] == "TORONTO"  # optional
    with pytest.raises(Skip):
        ca_record(dict(row, **{"Corporation number": ""}))


def test_all_four_files_are_processed_in_turn_once_a_week():
    files = {"corporations-active-cbca-en.csv": HEADER + ACTIVE,
             "corporations-inactive-or-dissolved-cbca-en.csv": HEADER + DISSOLVED}
    store, gw = MemStore(), FakeGateway(files)
    out = run(CANADA, gw, store, today=date(2026, 11, 18), gate=lambda: None, allowlist=AllowList.load(), log=lambda r: None)
    assert out == "done" and len(gw.fetched) == 4
    assert [k.split(" ", 1)[1] for k in store.manifest["processed"]] == [name for name, _ in CANADA.parts]
    recs = {k: v for s in store.shards.values() for k, v in s.items()}
    assert set(recs) == {mint("CA", "RA000072", "8284083"), mint("CA", "RA000072", "68217")}
    gw2 = FakeGateway(files)
    assert run(CANADA, gw2, store, today=date(2026, 11, 20), gate=lambda: None, allowlist=AllowList.load(),
               log=lambda r: None) == "already-done" and gw2.fetched == []
