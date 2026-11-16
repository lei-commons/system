"""National registers published as bulk files (zipped CSV and the like): one profile per register.

Each profile names its file, how to read it, GLEIF's code for the register (so a company with an
LEI merges with its register record) and the mapping. As with every source, only company data is
kept: natural persons are dropped, and no director or owner columns are read.

Ireland (CRO, opendata.cro.ie, CC BY 4.0): companies.csv in a ZIP, refreshed daily; companies only
(business names are a separate register, directors aren't published here). GLEIF RA000402, whose
numbers match the CRO's company numbers exactly.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date

from lc_core.directory_id import mint

from .gleif import Skip


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _date(value) -> str | None:
    value = _clean(value)[:10]
    return value or None


def zipped_csv(member_suffix=".csv", encoding="utf-8-sig", key=None):
    """A reader for a ZIP holding one CSV: rows as dicts. With ``key``, later rows repeating a key are skipped."""
    def read(binary_file):
        with zipfile.ZipFile(binary_file) as z:
            names = [n for n in z.namelist() if n.lower().endswith(member_suffix)]
            if len(names) != 1:
                raise ValueError(f"expected one {member_suffix} file in the archive, found {names}")
            seen = set()
            with z.open(names[0]) as raw:
                for row in csv.DictReader(io.TextIOWrapper(raw, encoding=encoding, newline="")):
                    if key:
                        k = _clean(row.get(key))
                        if k in seen:
                            continue
                        seen.add(k)
                    yield row
    return read


@dataclass(frozen=True)
class Profile:
    source: str          # the registry id (config/sources.yaml), which records also carry
    job: str
    store: str
    file_url: str
    filename: str        # what the downloaded file is kept as, before the key
    extension: str       # and after it, e.g. ".csv.zip"
    ra: str              # GLEIF's code for the register: directory IDs are minted with it
    read: object = None
    mapper: object = None

    def url(self) -> str:
        return self.file_url

    def key(self, today: date) -> str:
        """One run per ISO week, like the US registers: the register changes little from day to day."""
        year, week, _ = today.isocalendar()
        return f"{year}-W{week:02d}"

    def record(self, row: dict) -> dict:
        record = self.mapper(row)
        return {k: v for k, v in record.items() if v not in (None, "", [], {})}


# ---- Ireland ----

IE_COLUMNS = ("company_num", "company_name", "company_status", "company_type", "company_reg_date", "comp_dissolved_date",
              "company_address_1", "company_address_2", "company_address_3", "company_address_4", "eircode", "nace_v2_code")


def ie_record(row: dict) -> dict:
    number, name = _clean(row.get("company_num")), _clean(row.get("company_name"))
    if not number or not name or not number.isdigit():
        raise Skip("no number or name")
    lines = [_clean(row.get(f"company_address_{i}")).rstrip(",").strip() for i in range(1, 5)]
    nace = _clean(row.get("nace_v2_code"))
    if nace.endswith(".0"):
        nace = nace[:-2]
    kind = _clean(row.get("company_type"))
    status = _clean(row.get("company_status"))
    return {
        "directoryId": mint("IE", "RA000402", number),
        "legalName": name,
        "legalForm": kind if kind and kind != "Unknown" else None,
        "legalJurisdiction": "IE",
        "registrationAuthority": "IE-CRO",
        "registrationNumber": number,
        "legalStatus": status or None,
        "incorporationDate": _date(row.get("company_reg_date")),
        "dissolutionDate": _date(row.get("comp_dissolved_date")),
        "legalAddress": {k: v for k, v in {"addressLines": [line for line in lines if line],
                                           "postalCode": _clean(row.get("eircode")), "country": "IE"}.items() if v},
        "industryCodes": [nace] if nace.isdigit() else [],
        "recordMode": "mirror",
    }


IRELAND = Profile(
    source="ie-cro", job="ingest-ie", store="ie", filename="ie-cro-companies", extension=".csv.zip",
    file_url="https://opendata.cro.ie/dataset/bf6f837d-0946-4c14-9a99-82cd6980c121/resource/"
             "3fef41bc-b8f4-4b10-8434-ce51c29b1bba/download/companies.csv.zip",
    ra="RA000402", read=zipped_csv(key="company_num"), mapper=ie_record)


PROFILES = {p.source: p for p in (IRELAND,)}
