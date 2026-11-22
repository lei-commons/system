"""Sources approved by the owner on 2026-11-22, batch 1: the IRS exempt organisations and the Charity Commission.

Each names the columns it needs; a file without them stops with the missing and present column names only.
"""

import csv
import io
import zipfile
from datetime import date

import pytest

from lc_core.allowlist import AllowList
from lc_core.directory_id import mint
from lc_ingest.bulk import CCEW, IRS, PROFILES, ccew_record, irs_record, plain_csv, zipped_csv
from lc_ingest.gleif import Skip

from ingest_bulk.job import run
from test_canada import MemStore

IRS_HEADER = ("EIN,NAME,ICO,STREET,CITY,STATE,ZIP,GROUP,SUBSECTION,AFFILIATION,CLASSIFICATION,RULING,DEDUCTIBILITY,"
              "FOUNDATION,ACTIVITY,ORGANIZATION,STATUS,TAX_PERIOD,ASSET_CD,INCOME_CD,FILING_REQ_CD,PF_FILING_REQ_CD,"
              "ACCT_PD,ASSET_AMT,INCOME_AMT,REVENUE_AMT,NTEE_CD,SORT_NAME\n")
IRS_ROW = ("010018922,AMERICAN LEGION POST,% JOHN SMITH,PO BOX 12,BANGOR,ME,04401-0012,0925,19,3,1200,195001,2,00,"
           "000000000,5,01,202312,1,1,01,0,12,5000,6000,6000,W30,\n")
IRS_ABROAD = ("980123456,FRIENDS OF A SCHOOL ABROAD,,1 RUE X,PARIS,,75001,0000,03,3,1000,201001,1,15,"
              "000000000,1,01,202312,0,0,02,0,12,0,0,0,B11,\n")

CCEW_HEADER = ("date_of_extract\torganisation_number\tregistered_charity_number\tlinked_charity_number\tcharity_name\t"
               "charity_type\tcharity_registration_status\tdate_of_registration\tdate_of_removal\t"
               "charity_contact_address1\tcharity_contact_postcode\tcharity_contact_phone\tcharity_contact_email\t"
               "charity_contact_web\tcharity_company_registration_number\n")


def ccew_line(number, linked, name, status="Registered", company="", web="www.example.org.uk"):
    return (f"2026-11-21 00:00:00\t1\t{number}\t{linked}\t{name}\tTrust\t{status}\t1963-05-01 00:00:00\t\t"
            f"1 Vicarage Lane\tAB1 2CD\t01234 567890\tsomeone@example.org\t{web}\t{company}\n")


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def zipped(name, text):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(name, text)
    out.seek(0)
    return out


def test_irs_keeps_organisations_and_never_the_care_of_name():
    r = IRS.record(rows(IRS_HEADER + IRS_ROW)[0])
    assert r["directoryId"] == mint("US", "US-IRS", "010018922") and r["registrationNumber"] == "010018922"
    assert r["legalName"] == "AMERICAN LEGION POST" and r["legalForm"] == "Association, exempt under 501(c)(19)"
    assert r["legalStatus"] == "Unconditional exemption"
    assert r["legalAddress"] == {"addressLines": ["PO BOX 12"], "city": "BANGOR", "region": "ME", "postalCode": "04401",
                                 "country": "US"}
    assert "JOHN SMITH" not in repr(r)  # the in-care-of column names a person
    abroad = IRS.record(rows(IRS_HEADER + IRS_ABROAD)[0])
    assert "legalAddress" not in abroad  # no address or country guessed for an organisation abroad
    assert abroad["legalForm"] == "Corporation, exempt under 501(c)(3)"
    for bad in ({"EIN": "12345"}, {"NAME": " "}):
        with pytest.raises(Skip):
            irs_record({**rows(IRS_HEADER + IRS_ROW)[0], **bad})


def test_irs_reads_all_four_regions_once_a_week():
    class Gateway:
        def __init__(self):
            self.fetched = []

        def download(self, target, file):
            self.fetched.append(target)
            body = (IRS_HEADER + (IRS_ROW if target.endswith("eo1.csv") else "")).encode()
            file.write(body)
            return len(body)

    store, gw = MemStore(), Gateway()
    out = run(IRS, gw, store, today=date(2026, 11, 23), gate=lambda: None, allowlist=AllowList.load(), log=lambda r: None)
    assert out == "done" and [u.rsplit("/", 1)[1] for u in gw.fetched] == ["eo1.csv", "eo2.csv", "eo3.csv", "eo4.csv"]
    assert all(u.startswith("https://www.irs.gov/pub/irs-soi/") for u in gw.fetched)
    records = {k: v for s in store.shards.values() for k, v in s.items()}
    assert list(records) == [mint("US", "US-IRS", "010018922")]


def test_ccew_keeps_main_charities_that_are_not_companies_and_no_contact_details():
    text = (CCEW_HEADER + ccew_line("200001", "0", "THE VILLAGE HALL TRUST")
            + ccew_line("200001", "1", "THE VILLAGE HALL TRUST - BUILDING FUND")
            + ccew_line("200002", "0", "A CHARITABLE COMPANY", company="01234567")
            + ccew_line("200003", "0", "AN OLD CHARITY", status="Removed", web=""))
    read = CCEW.read(zipped("publicextract.charity.txt", text))
    kept, why = [], []
    for row in read:
        try:
            kept.append(CCEW.record(row))
        except Skip as exc:
            why.append(str(exc))
    assert [r["registrationNumber"] for r in kept] == ["200001", "200003"]
    assert why == ["a linked charity: part of its main charity's record",
                   "also a company: in the directory through Companies House"]
    first = kept[0]
    assert first["directoryId"] == mint("GB", "GB-CCEW", "200001") and first["legalForm"] == "Trust"
    assert first["incorporationDate"] == "1963-05-01" and first["website"] == "www.example.org.uk"
    assert first["legalAddress"] == {"country": "GB"}
    assert not any(x in repr(kept) for x in ("Vicarage", "AB1 2CD", "01234", "someone@"))  # often a trustee's own
    assert kept[1]["legalStatus"] == "Removed" and "website" not in kept[1]
    with pytest.raises(Skip):
        ccew_record({"registered_charity_number": "x", "charity_name": "A"})


def test_a_file_without_the_expected_columns_names_them_and_nothing_else():
    with pytest.raises(ValueError) as exc:
        list(plain_csv(columns=("EIN", "NAME"))(io.BytesIO(b"ein,org_name\n123456789,SECRET VALUE\n")))
    assert "missing columns ['EIN', 'NAME']" in str(exc.value) and "['ein', 'org_name']" in str(exc.value)
    assert "SECRET VALUE" not in str(exc.value)
    with pytest.raises(ValueError, match="missing columns"):
        list(zipped_csv(member_suffix=".txt", delimiter="\t", columns=("charity_name",))(zipped("a.txt", "x\ty\n1\t2\n")))
    # A byte-order mark or stray spaces in the header don't hide a column.
    assert list(plain_csv(columns=("EIN",))(io.BytesIO("﻿ EIN ,NAME\n1,A\n".encode("utf-8"))))[0]["EIN"] == "1"


def test_the_new_profiles_are_registered_and_estonia_waits():
    from lc_core.sources import pipelines

    built = {p["id"]: p for p in pipelines()}
    for source in ("us-irs-eo-bmf", "gb-ccew"):
        assert source in PROFILES and built[source]["job"] == PROFILES[source].job
    assert "ee-rik" not in PROFILES and "ee-rik" not in built  # its terms must be accepted: the owner decides


class ReportStore(MemStore):
    def __init__(self):
        super().__init__()
        self.json = {}

    def write_json(self, name, data):
        self.json[name] = data


def irs_gateway(body):
    class Gateway:
        def download(self, target, file):
            text = (IRS_HEADER + (body if target.endswith("eo1.csv") else "")).encode()
            file.write(text)
            return len(text)
    return Gateway()


def test_a_trial_run_reports_its_quality_with_counts_only_in_the_log():
    body = IRS_ROW + IRS_ABROAD + IRS_ROW.replace("010018922", "010018923") + IRS_ROW.replace("010018922", "12")
    store, events = ReportStore(), []
    out = run(IRS, irs_gateway(body), store, today=date(2026, 11, 23), gate=lambda: None, allowlist=AllowList.load(),
              log=events.append, stage="trial")
    assert out == "done"
    quality = [e for e in events if e["event"] == "quality"]
    assert len(quality) == 4  # one per regional file; the three empty ones measure nothing
    q = quality[0]["quality"]
    assert q["records"] == 3 and q["skipped"] == {"no number or name": 1}
    assert q["fill"]["legalName"] == 100.0 and q["addressFill"]["country"] == 66.7 and q["statusUnmapped"] == 0
    assert q["anomalies"] == {"same name, country and postcode as an earlier record": 1}  # the repeated post
    assert q["numberShapes"] == [["999999999", 3]] or q["numberShapes"] == [("999999999", 3)]
    assert "AMERICAN LEGION" not in repr(quality)  # no names in the public log
    report = store.json["trial-report.json"]
    assert report["source"] == "us-irs-eo-bmf" and len(report["sample"]) <= 25
    region_1 = store.json["trial-reports/2026-W48_region-1.json"]  # one report per file; trial-report.json is the latest
    assert region_1["records"] == 3 and region_1["sample"][0]["legalName"] == "AMERICAN LEGION POST"  # private sample


def test_a_production_run_measures_nothing():
    store, events = ReportStore(), []
    run(IRS, irs_gateway(IRS_ROW), store, today=date(2026, 11, 23), gate=lambda: None, allowlist=AllowList.load(),
        log=events.append, stage="production")
    assert not [e for e in events if e["event"] == "quality"] and store.json == {}


def test_quality_flags_implausible_data():
    from lc_ingest.quality import Quality

    q = Quality("x", today=date(2026, 11, 22))
    base = {"directoryId": "LE1", "legalName": "GOOD CO", "registrationNumber": "123", "legalAddress": {"country": "US"}}
    for record in (base, {**base, "directoryId": "LE1"}, {**base, "directoryId": "LE2", "legalName": "1234"},
                   {**base, "directoryId": "LE3", "legalName": "X", "incorporationDate": "2030-01-01"},
                   {**base, "directoryId": "LE4", "legalName": "OLD", "incorporationDate": "1999-01-01",
                    "dissolutionDate": "1990-01-01"},
                   {**base, "directoryId": "LE5", "legalName": "CALL CO", "otherNames": ["ring +44 20 7946 0000"]},
                   {**base, "directoryId": "LE6", "legalName": "MAIL CO", "website": "info@mail.example.com"}):
        q.add(record)
    a = q.summary()["anomalies"]
    assert a["directory ID repeated in the file"] == 1 and a["name without letters"] == 1
    assert a["name shorter than 2 characters"] == 1 and a["incorporationDate in the future"] == 1
    assert a["dissolved before incorporated"] == 1 and a["contact-like value (email or phone)"] == 2
