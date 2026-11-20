import gzip
import io
import json
import urllib.parse
from datetime import date

from lc_core.allowlist import AllowList
from lc_core.directory_id import mint
from lc_ingest.registers import NY_COLUMNS, ny_record
from lc_ingest.store import decode_records
from ingest_ny.job import run, url

CSV = (b'"dos_id","current_entity_name","initial_dos_filing_date","county","jurisdiction","entity_type",'
       b'"location_address_1","location_address_2","location_city","location_state","location_zip"\n'
       b'"1108058","OPPENHEIMER ASSET MANAGEMENT INC.","1986-06-27T00:00:00.000","New York","Delaware",'
       b'"FOREIGN BUSINESS CORPORATION","ONE WORLD FINANCIAL CENTER","200 LIBERTY STREET","New York","NY","10004"\n'
       b'"5000001","ACME WIDGETS LLC","2015-03-02T00:00:00.000","Albany","New York",'
       b'"DOMESTIC LIMITED LIABILITY COMPANY","C/O JANE ROE","100 MAIN STREET","Albany","NY","12207"\n'
       b'"10","THE TRUSTEES","1876-01-01T00:00:00.000",,"New York","DOMESTIC NOT-FOR-PROFIT CORPORATION",,,,,\n')


class FakeGateway:
    def __init__(self, body=CSV):
        self.fetched, self.body = [], body

    def download(self, target, file):
        self.fetched.append(target)
        file.write(self.body)
        return len(self.body)


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
    selected = urllib.parse.parse_qs(urllib.parse.urlsplit(url()).query)["$select"][0].split(",")
    assert selected == NY_COLUMNS
    assert {"location_address_1", "location_address_2"} <= set(selected)  # the company's street (2026-11-20)
    for column in selected:
        for banned in ("dos_process", "chairman", "registered_agent", "agent", "process", "mailing", "location_name"):
            assert banned not in column


def test_the_street_is_kept_and_a_care_of_line_dropped():
    row = {"dos_id": "1", "current_entity_name": "X LLC", "location_address_1": "c/o Jane Roe",
           "location_address_2": " 100  Main Street ", "location_city": "Albany", "location_state": "NY"}
    assert ny_record(row)["legalAddress"]["addressLines"] == ["100 Main Street"]
    for line in ("C/O JANE ROE", "Attn: Jane Roe", "IN CARE OF JANE ROE", "JANE ROE C/O ACME"):
        assert "addressLines" not in ny_record({**row, "location_address_1": line, "location_address_2": ""})["legalAddress"]
    kept = ny_record({**row, "location_address_1": "1 COLOMBO PLAZA", "location_address_2": "SUITE 5"})
    assert kept["legalAddress"]["addressLines"] == ["1 COLOMBO PLAZA", "SUITE 5"]


def test_loads_once_a_week_with_ids_that_match_gleif():
    store, gw = MemStore(), FakeGateway()
    assert run(gw, store, today=date(2026, 11, 9), gate=lambda: None, allowlist=AllowList.load(), log=lambda r: None) == "done"
    recs = {k: v for s in store.shards.values() for k, v in s.items()}
    oppenheimer = recs[mint("US", "RA000628", "1108058")]
    assert oppenheimer["legalJurisdiction"] == "US-DE" and oppenheimer["legalAddress"] == {
        "addressLines": ["ONE WORLD FINANCIAL CENTER", "200 LIBERTY STREET"],
        "city": "NEW YORK", "region": "NY", "postalCode": "10004", "country": "US"}
    acme = recs[mint("US", "RA000628", "5000001")]
    assert acme["legalAddress"]["addressLines"] == ["100 MAIN STREET"]
    assert "JANE ROE" not in json.dumps(store.shards) and "JANE ROE" not in json.dumps(store.changes)
    assert store.manifest["processed"] == ["2026-W46"]
    gw2 = FakeGateway()
    assert run(gw2, store, today=date(2026, 11, 13), gate=lambda: None, allowlist=AllowList.load(),
               log=lambda r: None) == "already-done" and gw2.fetched == []


def many(n, street: bool) -> bytes:
    header = '"dos_id","current_entity_name","location_address_1","location_address_2","location_city","location_state","location_zip"\n'
    rows = [f'"{i}","COMPANY {i} INC.","{f"{i} MAIN STREET" if street else ""}",,"Albany","NY","12207"\n' for i in range(1, n + 1)]
    return (header + "".join(rows)).encode()


def loaded_without_streets(n=150):
    store = MemStore()
    assert run(FakeGateway(many(n, street=False)), store, today=date(2026, 11, 16), gate=lambda: None,
               allowlist=AllowList.load(), log=lambda r: None) == "done"
    assert store.manifest["records"] == n and store.manifest["processed"] == ["2026-W47"]
    return store


def test_the_street_upgrade_goes_through_once_in_the_week_the_owner_approved():
    store, events = loaded_without_streets(), []
    # 2026-11-23 is in 2026-W48, the publication config/sources.yaml names (volume_guard.mapping_upgrades).
    assert run(FakeGateway(many(150, street=True)), store, today=date(2026, 11, 23), gate=lambda: None,
               allowlist=AllowList.load(), log=events.append) == "done"
    upgrade = [e for e in events if e["event"] == "mapping-upgrade"]
    assert len(upgrade) == 1 and upgrade[0]["source"] == "us-ny-dos" and upgrade[0]["publish"] == "2026-W48"
    assert upgrade[0]["changed"] == 150 and upgrade[0]["total"] == 150 and upgrade[0]["approved_by"] == "owner"
    assert not any(e["event"] == "held" for e in events)
    assert store.manifest["processed"] == ["2026-W47", "2026-W48"]
    recs = {k: v for s in store.shards.values() for k, v in s.items()}
    assert recs[mint("US", "RA000628", "7")]["legalAddress"]["addressLines"] == ["7 MAIN STREET"]


def test_any_other_week_still_holds_the_mass_change():
    store, events = loaded_without_streets(), []
    before = json.dumps(store.shards, sort_keys=True)
    assert run(FakeGateway(many(150, street=True)), store, today=date(2026, 11, 30), gate=lambda: None,
               allowlist=AllowList.load(), log=events.append) == "held"
    assert [e["event"] for e in events if e["event"] in ("held", "mapping-upgrade")] == ["held"]
    assert store.manifest["processed"] == ["2026-W47"] and json.dumps(store.shards, sort_keys=True) == before
