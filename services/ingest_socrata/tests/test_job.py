"""Colorado, Oregon and Connecticut: what is requested, what is kept, what is dropped, and the IDs."""

import csv
import gzip
import io
import json
import urllib.parse
from datetime import date

import pytest
from lc_core.allowlist import AllowList
from lc_core.directory_id import mint
from lc_ingest.gleif import Skip
from lc_ingest.socrata import CONNECTICUT, COLORADO, OREGON, PROFILES, co_record, ct_record, or_record, us_jurisdiction
from lc_ingest.store import decode_records

from ingest_socrata.job import profile_for, run


def as_csv(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


class FakeGateway:
    def __init__(self, body):
        self.body, self.fetched = body, []

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


PEOPLE_COLUMNS = {
    "us-co-sos": ["agentfirstname", "agentlastname", "agentprincipaladdress1", "mailingaddress1", "agentorganizationname"],
    "us-or-sos": ["first_name", "last_name", "middle_name", "address", "address_continued"],
    "us-ct-sots": ["business_email_address", "category_survey_email_address", "mailing_address", "records_address_street",
                  "woman_owned_organization", "organization_is_lgbtqi_owned", "billingstreet"],
}


@pytest.mark.parametrize("profile", [COLORADO, OREGON, CONNECTICUT])
def test_requests_never_ask_for_people(profile):
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(profile.url()).query)
    selected = query["$select"][0]
    for column in PEOPLE_COLUMNS[profile.source]:
        assert column not in selected, column
    assert profile.url().startswith(f"https://{profile.host}/resource/{profile.dataset}.csv?")
    assert int(query["$limit"][0]) >= 20_000_000


def test_every_registered_source_has_a_profile_and_the_registry_agrees():
    from lc_core.sources import pipelines

    built = {p["id"]: p for p in pipelines()}
    for source, profile in PROFILES.items():
        assert built[source]["job"] == profile.job and built[source]["store"] == profile.store
        assert built[source]["records_as"] == source


def test_unknown_source_is_refused():
    with pytest.raises(SystemExit):
        profile_for("us-xx-sos")


# ---- Colorado ----

CO = {"entityid": "19871266267", "entityname": "EXAMPLE MINING COMPANY", "entitystatus": "Good Standing",
      "jurisdictonofformation": "CO", "entitytype": "DPC", "entityformdate": "1987-06-01T00:00:00.000",
      "principalcity": "Denver", "principalstate": "CO", "principalzipcode": "80202", "principalcountry": "US"}


def test_colorado_record_and_its_id_matches_gleif():
    r = COLORADO.record(CO)
    assert r["directoryId"] == mint("US", "RA000599", "19871266267")  # GLEIF's registeredAs for RA000599 is this number
    assert r == {**r, "legalName": "EXAMPLE MINING COMPANY", "legalForm": "Profit corporation", "legalJurisdiction": "US-CO",
                 "legalStatus": "Good Standing", "incorporationDate": "1987-06-01", "registrationAuthority": "US-CO-SOS",
                 "legalAddress": {"city": "DENVER", "region": "CO", "postalCode": "80202", "country": "US"}}
    assert us_jurisdiction("Delaware") == "US-DE" and us_jurisdiction("Canada") is None


@pytest.mark.parametrize("kind,why", [("GP", "natural person"), ("SP", "natural person"), ("NEWTYPE", "not reviewed"),
                                      ("", "not reviewed")])
def test_colorado_drops_people_and_unreviewed_types(kind, why):
    with pytest.raises(Skip, match=why):
        co_record({**CO, "entitytype": kind})


# ---- Oregon ----

OR = {"registry_number": "100001494", "entity_name": "PROUD AMERICAN WOODWORKS LLC",
      "entity_kind": "DOMESTIC LIMITED LIABILITY COMPANY", "registered": "2014-03-05T10:59:31.000", "formed_in": "OR",
      "ppb_city": "BEND", "ppb_state": "OR", "ppb_zip": "97701"}


def test_oregon_record():
    r = OREGON.record(OR)
    assert r["directoryId"] == mint("US", "RA000631", "100001494") and r["legalStatus"] == "Active"
    assert r["legalForm"] == "Domestic limited liability company" and r["legalJurisdiction"] == "US-OR"
    assert r["legalAddress"] == {"city": "BEND", "region": "OR", "postalCode": "97701", "country": "US"}


@pytest.mark.parametrize("kind", ["ASSUMED BUSINESS NAME", "RESERVED NAME", "REGISTERED NAME", "SOMETHING NEW"])
def test_oregon_drops_names_that_are_not_entities(kind):
    with pytest.raises(Skip):
        or_record({**OR, "entity_kind": kind})


def test_oregon_groups_one_row_per_business_and_takes_only_the_principal_place():
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(OREGON.url()).query)
    assert query["$group"] == ["registry_number"]
    assert "PRINCIPAL PLACE OF BUSINESS" in query["$select"][0] and "REGISTERED AGENT" not in query["$select"][0]


# ---- Connecticut ----

CT = {"accountnumber": "0609487", "name": "SOUNDVIEW FARMS, LLC", "business_type": "LLC", "status": "Active",
      "date_registration": "1999-04-01T00:00:00.000", "dissolution_date": "", "citizenship": "Domestic",
      "state_or_territory_formation": "", "billingcity": "Westport", "billingstate": "CT", "billingpostalcode": "06880",
      "billingcountry": "United States", "naics_code": "111998"}


def test_connecticut_record_keeps_leading_zeros_like_gleif():
    r = CONNECTICUT.record(CT)
    assert r["directoryId"] == mint("US", "RA000600", "0609487") and r["registrationNumber"] == "0609487"
    assert r["legalJurisdiction"] == "US-CT" and r["industryCodes"] == ["111998"] and "dissolutionDate" not in r
    foreign = CONNECTICUT.record({**CT, "citizenship": "Foreign", "state_or_territory_formation": "DELAWARE"})
    assert foreign["legalJurisdiction"] == "US-DE"


@pytest.mark.parametrize("change", [{"business_type": "General Partnerships"}, {"business_type": "Unknown"},
                                    {"business_type": ""}, {"status": "Reserved"}, {"status": "Expired Reservation"},
                                    {"status": "Rejected"}, {"accountnumber": "0000000"}, {"name": " "}])
def test_connecticut_drops_what_is_not_a_company(change):
    with pytest.raises(Skip):
        ct_record({**CT, **change})


# ---- the job, end to end with a fake gateway and store ----

def test_a_run_loads_once_a_week_and_drops_people():
    rows = [CO, {**CO, "entityid": "20051000002", "entityname": "SMITH AND JONES", "entitytype": "GP"},
            {**CO, "entityid": "20061000003", "entityname": "ACME LLC", "entitytype": "DLLC", "jurisdictonofformation": "Delaware"}]
    store, gw = MemStore(), FakeGateway(as_csv(rows))
    out = run(COLORADO, gw, store, today=date(2026, 11, 18), gate=lambda: None, allowlist=AllowList.load(), log=lambda r: None)
    assert out == "done" and store.manifest["processed"] == ["2026-W47"]
    recs = {k: v for s in store.shards.values() for k, v in s.items()}
    assert set(recs) == {mint("US", "RA000599", "19871266267"), mint("US", "RA000599", "20061000003")}
    assert "SMITH AND JONES" not in json.dumps(recs) and recs[mint("US", "RA000599", "20061000003")]["legalJurisdiction"] == "US-DE"
    gw2 = FakeGateway(b"")
    assert run(COLORADO, gw2, store, today=date(2026, 11, 20), gate=lambda: None, allowlist=AllowList.load(),
               log=lambda r: None) == "already-done" and gw2.fetched == []


def test_a_halted_or_unapproved_source_fetches_nothing():
    from lc_core.control import SystemHalted

    def gate():
        raise SystemHalted("source us-or-sos is pending, not approved by the owner")

    store, gw, events = MemStore(), FakeGateway(b""), []
    assert run(OREGON, gw, store, today=date(2026, 11, 18), gate=gate, allowlist=AllowList.load(), log=events.append) == "halted"
    assert gw.fetched == [] and events[0]["event"] == "halted"
