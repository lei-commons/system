"""Companies House basic company data to directory records.

The file describes companies, not people, but one column can name a person:
``RegAddress.CareOf``, the person mail is sent in care of. It is never read
(Article 3.2). The registered office itself is the company's address, which
decision 37 allows.
"""

from __future__ import annotations

import csv
import io
import zipfile

from lc_core.directory_id import mint

from .gleif import Skip

REGISTRY = "GB-COH"
UK_NAMES = {"", "ENGLAND", "WALES", "SCOTLAND", "NORTHERN IRELAND", "UNITED KINGDOM", "GREAT BRITAIN",
            "ENGLAND AND WALES", "UK", "GB"}
NO_SIC = {"", "NONE SUPPLIED"}
MAX_PREVIOUS_NAMES = 10
MAX_SIC = 4
PROFILE = "https://find-and-update.company-information.service.gov.uk/company/"


def _date(value: str):
    """dd/mm/yyyy to yyyy-mm-dd, or None."""
    value = (value or "").strip()
    if not value:
        return None
    day, month, year = value.split("/")
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _country(value: str) -> str:
    value = (value or "").strip().upper()
    return "GB" if value in UK_NAMES else value


def to_record(row: dict) -> dict:
    row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
    number, name = row["CompanyNumber"], row["CompanyName"]
    if not number or not name:
        raise Skip("no number or name")

    po_box = row.get("RegAddress.POBox")
    lines = [f"PO BOX {po_box}" if po_box else "", row.get("RegAddress.AddressLine1"), row.get("RegAddress.AddressLine2")]
    address = {
        "addressLines": [line for line in lines if line],
        "city": row.get("RegAddress.PostTown"),
        "region": row.get("RegAddress.County"),
        "country": _country(row.get("RegAddress.Country")),
        "postalCode": row.get("RegAddress.PostCode"),
    }
    sic = [row.get(f"SICCode.SicText_{i}", "") for i in range(1, MAX_SIC + 1)]
    record = {
        "directoryId": mint("GB", REGISTRY, number),
        "legalName": name,
        "otherNames": [row[f"PreviousName_{i}.CompanyName"] for i in range(1, MAX_PREVIOUS_NAMES + 1)
                       if row.get(f"PreviousName_{i}.CompanyName")],
        "legalForm": row.get("CompanyCategory"),
        "legalJurisdiction": "GB",
        "registrationAuthority": REGISTRY,
        "registrationNumber": number,
        "legalAddress": {k: v for k, v in address.items() if v},
        "legalStatus": row.get("CompanyStatus"),
        "incorporationDate": _date(row.get("IncorporationDate")),
        "dissolutionDate": _date(row.get("DissolutionDate")),
        "industryCodes": [s.split(" - ", 1)[0] for s in sic if s.upper() not in NO_SIC],
        "registryUrl": PROFILE + number,
        "recordMode": "mirror",
    }
    return {k: v for k, v in record.items() if v not in (None, "", [], {})}


def read_zip(binary_file):
    """Yield rows from a basic company data zip, one at a time."""
    with zipfile.ZipFile(binary_file) as archive:
        names = [n for n in archive.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in the archive, found {len(names)}")
        with archive.open(names[0]) as raw:
            yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
