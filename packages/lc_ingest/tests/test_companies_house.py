import csv
import io
import json
import zipfile

import pytest

from lc_ingest.companies_house import read_zip, to_record
from lc_ingest.gleif import Skip
from lc_ingest.gleif import to_record as gleif_record

ROW = {
    "CompanyName": "EXAMPLE LIMITED", " CompanyNumber": "01234567", "RegAddress.CareOf": "MR JOHN EXAMPLE",
    "RegAddress.POBox": "", "RegAddress.AddressLine1": "1 Example Street", " RegAddress.AddressLine2": "Floor 2",
    "RegAddress.PostTown": "LONDON", "RegAddress.County": "", "RegAddress.Country": "ENGLAND",
    "RegAddress.PostCode": "EC1A 1AA", "CompanyCategory": "Private Limited Company", "CompanyStatus": "Active",
    "CountryOfOrigin": "United Kingdom", "DissolutionDate": "", "IncorporationDate": "03/02/2001",
    "SICCode.SicText_1": "62012 - Business and domestic software development", "SICCode.SicText_2": "None Supplied",
    "PreviousName_1.CONDATE": "01/01/2010", "PreviousName_1.CompanyName": "OLD EXAMPLE LIMITED",
    "LimitedPartnerships.NumGenPartners": "0", "URI": "http://business.data.gov.uk/id/company/01234567",
}


def test_maps_the_fields():
    r = to_record(ROW)
    assert r["legalName"] == "EXAMPLE LIMITED" and r["registrationNumber"] == "01234567"
    assert r["incorporationDate"] == "2001-02-03" and r["industryCodes"] == ["62012"]
    assert r["otherNames"] == ["OLD EXAMPLE LIMITED"] and r["legalStatus"] == "Active"
    assert r["legalAddress"] == {"addressLines": ["1 Example Street", "Floor 2"], "city": "LONDON",
                                 "country": "GB", "postalCode": "EC1A 1AA"}
    assert r["registryUrl"].endswith("/company/01234567") and r["recordMode"] == "mirror"


def test_same_id_as_gleif_for_the_same_company():
    assert to_record(ROW)["directoryId"] == "LECOZZFZ6LZCOJBDKT73"


def test_gleif_record_with_a_foreign_address_still_matches():
    raw = {"attributes": {"lei": "5493001KJTIIGC8Y1R12", "entity": {
        "legalName": {"name": "EXAMPLE LIMITED"}, "legalAddress": {"country": "JE", "city": "St Helier"},
        "registeredAt": {"id": "RA000585"}, "registeredAs": "01234567", "jurisdiction": "GB", "category": "GENERAL"}}}
    assert gleif_record(raw)["directoryId"] == to_record(ROW)["directoryId"]


def test_care_of_name_is_never_stored():
    assert "JOHN" not in json.dumps(to_record(ROW))


@pytest.mark.parametrize("country,expected", [("", "GB"), ("UNITED KINGDOM", "GB"), ("SCOTLAND", "GB"),
                                              ("Northern Ireland", "GB"), ("UNITED STATES", "UNITED STATES")])
def test_uk_country_names_become_gb(country, expected):
    assert to_record({**ROW, "RegAddress.Country": country})["legalAddress"]["country"] == expected


def test_po_box_is_kept_as_an_address_line():
    assert to_record({**ROW, "RegAddress.POBox": "123"})["legalAddress"]["addressLines"][0] == "PO BOX 123"


def test_dissolution_date():
    assert to_record({**ROW, "DissolutionDate": "31/12/2025"})["dissolutionDate"] == "2025-12-31"


@pytest.mark.parametrize("change", [{"CompanyName": ""}, {" CompanyNumber": ""}])
def test_no_name_or_number_is_skipped(change):
    with pytest.raises(Skip):
        to_record({**ROW, **change})


def test_bad_date_is_an_error_for_the_pipeline_to_count():
    with pytest.raises(ValueError):
        to_record({**ROW, "IncorporationDate": "not a date"})


def test_read_zip_strips_the_padded_headers():
    text = io.StringIO()
    w = csv.DictWriter(text, fieldnames=list(ROW))
    w.writeheader()
    w.writerow(ROW)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("part1.csv", text.getvalue())
    buf.seek(0)
    (row,) = list(read_zip(buf))
    assert to_record(row)["registrationNumber"] == "01234567"
