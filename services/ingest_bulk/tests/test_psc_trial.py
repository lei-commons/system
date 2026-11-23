"""UK corporate owners on trial (web-asserted facts pilot, step 2; owner decision 2026-11-23): only rows whose
controller is an entity are read, each becomes a link with no names, and a trial only measures part 1."""

import io
import json
import os
import zipfile
from datetime import date

import pytest

from lc_core.allowlist import AllowList
from lc_ingest.gleif import Skip
from lc_ingest.profiles import PROFILES
from lc_ingest.psc import UK_PSC, RelationshipQuality, band, company_number, locate, relationship

from ingest_bulk.job import run
from test_batch_2026_11_22 import ReportStore

PAGE = """<html><body>
<a href="psc-snapshot-2026-11-22_1of3.zip">part 1</a> <a href="psc-snapshot-2026-11-22_2of3.zip">part 2</a>
<a href="psc-snapshot-2026-11-22_3of3.zip">part 3</a> <a href="psc-snapshot-2026-11-21_1of3.zip">old</a>
<a href="psc-snapshot-2026-11-22.zip">whole</a></body></html>"""

CORPORATE = {"company_number": "9145694", "data": {
    "kind": "corporate-entity-person-with-significant-control", "name": "PARENT HOLDINGS LIMITED",
    "identification": {"registration_number": "1234567", "place_registered": "Companies House",
                       "country_registered": "England", "legal_form": "Limited Company"},
    "address": {"premises": "1", "address_line_1": "High Street", "locality": "London"},
    "natures_of_control": ["ownership-of-shares-75-to-100-percent", "voting-rights-75-to-100-percent"],
    "notified_on": "2016-04-06"}}
FOREIGN = {"company_number": "SC123456", "data": {
    "kind": "corporate-entity-person-with-significant-control", "name": "MUTTER GMBH",
    "identification": {"registration_number": "HRB 12345", "place_registered": "Amtsgericht Hamburg",
                       "country_registered": "Germany"},
    "natures_of_control": ["right-to-appoint-and-remove-directors"], "notified_on": "2019-01-01",
    "ceased_on": "2021-06-30"}}
PERSON = {"company_number": "01234567", "data": {
    "kind": "individual-person-with-significant-control", "name": "Mr John Example",
    "date_of_birth": {"month": 1, "year": 1970}, "nationality": "British",
    "address": {"premises": "2", "address_line_1": "Home Road"}, "natures_of_control": ["ownership-of-shares-25-to-50-percent"],
    "notified_on": "2016-04-06"}}
STATEMENT = {"company_number": "01234567", "data": {"kind": "persons-with-significant-control-statement",
                                                     "statement": "no-individual-or-entity-with-signficant-control"}}
TOTALS = {"data": {"kind": "totals#persons-of-significant-control-snapshot", "persons_of_significant_control_count": 4}}


def zipped_lines(rows):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("psc-snapshot-2026-11-22_1of3.txt", "\n".join(json.dumps(r) for r in rows) + "\n")
    return out.getvalue()


class Gateway:
    def __init__(self, page=PAGE, body=b""):
        self.page, self.body, self.fetched = page, body, []

    def open(self, target, method="GET", headers=None):
        page = self.page

        class Resp:
            def read(self, n=-1):
                return page.encode()

        class Conn:
            def close(self):
                pass
        return Conn(), Resp()

    def download(self, target, file):
        self.fetched.append(target)
        file.write(self.body)
        return len(self.body)


def test_the_newest_snapshot_s_part_1_is_measured():
    key, url = locate(Gateway())
    assert key == "2026-11-22 part 1 of 3"
    assert url == "https://download.companieshouse.gov.uk/psc-snapshot-2026-11-22_1of3.zip"
    key, url = locate(Gateway(page='<a href="psc-snapshot-2026-11-20.zip">x</a>'))
    assert key == "2026-11-20 whole" and url.endswith("/psc-snapshot-2026-11-20.zip")
    with pytest.raises(ValueError):
        locate(Gateway(page='<a href="https://elsewhere.test/psc-snapshot-2026-11-22_1of3.zip">x</a>'))
    with pytest.raises(ValueError):
        locate(Gateway(page="<html>nothing</html>"))


def test_an_entity_controller_becomes_a_link_with_no_names():
    rel = relationship(CORPORATE)
    assert rel["startNode"] == "GB-COH:09145694" and rel["endNode"] == "GB-COH:01234567"
    assert rel["relationshipType"] == "IS_SIGNIFICANTLY_CONTROLLED_BY" and rel["relationshipStatus"] == "ACTIVE"
    assert rel["percentage"] == "75-100" and rel["periodStart"] == "2016-04-06" and rel["periodEnd"] is None
    assert "PARENT" not in repr(rel) and "High Street" not in repr(rel)
    foreign = relationship(FOREIGN)
    assert foreign["startNode"] == "GB-COH:SC123456" and foreign["endNode"] == "OTHER:GERMANY:HRB12345"
    assert foreign["relationshipStatus"] == "INACTIVE" and foreign["percentage"] is None


def test_rows_about_people_statements_and_totals_are_skipped_unread():
    for row, why in ((PERSON, "individual"), (STATEMENT, "not an entity controller"), (TOTALS, "not an entity controller")):
        with pytest.raises(Skip, match=why):
            relationship(row)
    no_number = json.loads(json.dumps(CORPORATE))
    no_number["data"]["identification"] = {"country_registered": "England"}
    with pytest.raises(Skip, match="no registration number"):
        relationship(no_number)


def test_numbers_and_bands():
    assert company_number("9145694") == "09145694" and company_number("sc 1234") == "SC001234"
    assert band(["voting-rights-50-to-75-percent"]) == "50-75"
    assert band(["voting-rights-25-to-50-percent", "ownership-of-shares-50-to-75-percent"]) == "50-75"  # shares first
    assert band(["significant-influence-or-control"]) is None


def test_the_measure_counts_links_and_anomalies():
    q = RelationshipQuality("gb-coh-psc", today=date(2026, 11, 23))
    for row in (CORPORATE, CORPORATE, FOREIGN):
        q.add(relationship(row))
    s = q.summary({"skipped: individual": 5})
    assert s["kind"] == "relationship" and s["records"] == 3 and s["skipped"] == {"individual": 5}
    assert dict(s["legalForms"]) == {"Companies House (links to a UK record)": 2, "another register": 1}
    assert dict(s["countries"]) == {"GERMANY": 1} and dict(s["statusWords"]) == {"ACTIVE": 2, "INACTIVE": 1}
    assert s["anomalies"] == {"same active link twice": 1} and s["fill"]["periodStart"] == 100.0


def test_a_trial_run_measures_part_1_and_keeps_nothing():
    assert PROFILES["gb-coh-psc"] is UK_PSC and UK_PSC.kind == "relationship"
    gateway = Gateway(body=zipped_lines([CORPORATE, PERSON, FOREIGN, STATEMENT, TOTALS]))
    store, events = ReportStore(), []
    os.environ["LC_CODE_VERSION"] = "abc123"
    try:
        outcome = run(UK_PSC, gateway, store, today=date(2026, 11, 23), gate=lambda: None,
                      allowlist=AllowList.load(), log=events.append, stage="trial")
    finally:
        del os.environ["LC_CODE_VERSION"]
    assert outcome == "done"
    assert gateway.fetched == ["https://download.companieshouse.gov.uk/psc-snapshot-2026-11-22_1of3.zip"]
    q = next(e for e in events if e["event"] == "quality")["quality"]
    assert q["records"] == 2 and q["skipped"] == {"individual": 1, "not an entity controller": 2}
    assert "John" not in repr(events) and "PARENT" not in repr(events)
    assert store.shards == {} and store.changes == {}  # a trial keeps no records
    report = next(v for k, v in store.json.items() if k.startswith("trial-reports/"))
    assert report["kind"] == "relationship" and len(report["sample"]) == 2 and "John" not in repr(report)


def test_the_source_is_on_trial_in_the_registry():
    from lc_core.sources import pipelines

    built = {p["id"]: p for p in pipelines()}
    assert built["gb-coh-psc"]["stage"] == "trial" and built["gb-coh-psc"]["job"] == "ingest-gb-psc"
