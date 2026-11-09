import gzip
import io
import json
from datetime import date

from lc_core.allowlist import AllowList
from lc_core.directory_id import mint
from lc_ingest.store import decode_records
from ingest_ny.job import run, url

CSV = (b'"dos_id","current_entity_name","initial_dos_filing_date","county","jurisdiction","entity_type",'
       b'"location_city","location_state","location_zip"\n'
       b'"1108058","OPPENHEIMER ASSET MANAGEMENT INC.","1986-06-27T00:00:00.000","New York","Delaware",'
       b'"FOREIGN BUSINESS CORPORATION","New York","NY","10004"\n'
       b'"10","THE TRUSTEES","1876-01-01T00:00:00.000",,"New York","DOMESTIC NOT-FOR-PROFIT CORPORATION",,,\n')


class FakeGateway:
    def __init__(self):
        self.fetched = []

    def download(self, target, file):
        self.fetched.append(target)
        file.write(CSV)
        return len(CSV)


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


def test_the_request_never_asks_for_people():
    for field in ("dos_process", "chairman", "registered_agent", "location_name", "location_address"):
        assert field not in url()


def test_loads_once_a_week_with_ids_that_match_gleif():
    store, gw = MemStore(), FakeGateway()
    assert run(gw, store, today=date(2026, 11, 9), gate=lambda: None, allowlist=AllowList.load(), log=lambda r: None) == "done"
    recs = {k: v for s in store.shards.values() for k, v in s.items()}
    oppenheimer = recs[mint("US", "RA000628", "1108058")]
    assert oppenheimer["legalJurisdiction"] == "US-DE" and oppenheimer["legalAddress"] == {
        "city": "NEW YORK", "region": "NY", "postalCode": "10004", "country": "US"}
    assert store.manifest["processed"] == ["2026-W46"]
    gw2 = FakeGateway()
    assert run(gw2, store, today=date(2026, 11, 13), gate=lambda: None, allowlist=AllowList.load(),
               log=lambda r: None) == "already-done" and gw2.fetched == []
