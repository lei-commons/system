"""Australia (ASIC): a company's name rows grouped into one record, IDs that match GLEIF, the catalogue lookup."""

import csv
import io
import zipfile

import pytest
from lc_core.directory_id import mint
from lc_ingest.bulk import AUSTRALIA, au_locate, au_record, grouped_tsv
from lc_ingest.gleif import Skip

AU_HEADER = ["Company Name", "ACN", "Type", "Class", "Sub Class", "Status", "Date of Registration", "Date of Deregistration",
             "Previous State of Registration", "State Registration number", "Modified since last report",
             "Current Name Indicator", "ABN", "Current Name", "Current Name Start Date"]


def au_rows(*rows):
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=AU_HEADER, delimiter="\t")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in AU_HEADER})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("COMPANY_202611.csv", "﻿" + text.getvalue())
    return io.BytesIO(out.getvalue())


OLD = {"Company Name": "LOVINI HOLDINGS PTY LTD", "ACN": "000000019", "Type": "APTY", "Class": "LMSH", "Status": "REGD",
       "Date of Registration": "08/01/1990", "Current Name": "MONAKA PTY LTD"}
NOW = dict(OLD, **{"Company Name": "MONAKA PTY LTD", "Current Name Indicator": "Y", "Current Name": ""})


def test_australia_groups_a_companys_names_and_its_id_matches_gleif():
    (company,) = list(AUSTRALIA.read(au_rows(OLD, NOW)))
    r = AUSTRALIA.record(company)
    assert r["directoryId"] == mint("AU", "RA000014", "000 000 019")  # GLEIF writes ACNs with spaces
    assert r["legalName"] == "MONAKA PTY LTD" and r["otherNames"] == ["LOVINI HOLDINGS PTY LTD"]
    assert r["legalForm"] == "Australian proprietary company, limited by shares" and r["legalStatus"] == "Registered"
    assert r["incorporationDate"] == "1990-01-08" and r["legalJurisdiction"] == "AU"
    foreign = AUSTRALIA.record({"key": "1", "rows": [dict(NOW, Type="FNOS")]})
    assert "legalJurisdiction" not in foreign and foreign["legalForm"].startswith("Foreign company")


def test_australia_needs_acn_order_and_a_current_name():
    with pytest.raises(ValueError, match="order"):
        list(grouped_tsv("ACN")(au_rows(dict(NOW, ACN="000000028"), NOW)))
    with pytest.raises(Skip):
        au_record({"key": "000000019", "rows": [OLD]})


class Catalogue:
    def __init__(self, resources):
        self.resources = resources

    def get_json(self, url):
        assert url.startswith("https://data.gov.au/")
        return {"result": {"resources": self.resources}}


def test_the_current_extract_is_found_in_the_catalogue():
    good = {"format": "ZIP", "name": "Company Dataset - Current",
            "url": "https://data.gov.au/data/dataset/x/resource/y/download/company_202611.zip"}
    other = {"format": "CSV", "name": "Company Dataset - Current", "url": "x"}
    assert au_locate(Catalogue([good, other])) == ("company_202611", good["url"])
    with pytest.raises(ValueError, match="one current ZIP"):
        au_locate(Catalogue([good, good]))
    with pytest.raises(ValueError, match="unexpected place"):
        au_locate(Catalogue([dict(good, url="https://elsewhere.example/company.zip")]))
