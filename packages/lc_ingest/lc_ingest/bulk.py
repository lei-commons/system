"""National registers published as bulk files (zipped CSV and the like): one profile per register.

Each profile names its file, how to read it, GLEIF's code for the register (so a company with an
LEI merges with its register record) and the mapping. As with every source, only company data is
kept: natural persons are dropped, and no director or owner columns are read.

Ireland (CRO, opendata.cro.ie, CC BY 4.0): companies.csv in a ZIP, refreshed daily; companies only
(business names are a separate register, directors aren't published here). GLEIF RA000402, whose
numbers match the CRO's company numbers exactly.

Finland (PRH open data, CC BY 4.0): every valid business as one JSON array in a ZIP (about 1.5 GB
unzipped), read as a stream. Sole traders aren't in the open data; general partnerships, business
partnerships and bankrupts' estates are dropped as people. The c/o line, which can name a person,
and apartment numbers are never read. GLEIF RA000188 uses the same business IDs ("0245992-3").

Australia (ASIC company dataset, data.gov.au, CC BY 3.0 AU): a monthly ZIP of tab-separated rows,
one row per name a company has had, in ACN order; located each run through data.gov.au's catalogue
and processed once per publication. Companies only (business names, often sole traders, are a
separate dataset); no addresses or officers. GLEIF RA000014 writes ACNs with spaces, which IDs ignore.
"""

from __future__ import annotations

import csv
import io
import json
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


def zipped_json_array(chunk: int = 1 << 20):
    """A reader for a ZIP holding one JSON array of objects, streamed: never more than a chunk and one object in memory."""
    def read(binary_file):
        decoder = json.JSONDecoder()
        with zipfile.ZipFile(binary_file) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".json")]
            if len(names) != 1:
                raise ValueError(f"expected one .json file in the archive, found {names}")
            with z.open(names[0]) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8")  # decodes across chunk boundaries
                buf, pos, started, finished = "", 0, False, False
                while not finished:
                    data = text.read(chunk)
                    buf, pos = buf[pos:] + data, 0
                    while True:
                        while pos < len(buf) and buf[pos] in " \t\r\n,":
                            pos += 1
                        if pos >= len(buf):
                            break
                        if not started:
                            if buf[pos] != "[":
                                raise ValueError("not a JSON array")
                            started, pos = True, pos + 1
                            continue
                        if buf[pos] == "]":
                            finished = True
                            break
                        try:
                            obj, end = decoder.raw_decode(buf, pos)
                        except ValueError:
                            if not data:
                                raise ValueError("the JSON array ends part-way through a record")
                            break  # the rest of this object is in the next chunk
                        yield obj
                        pos = end
                    if not data and not finished:
                        raise ValueError("the JSON array isn't closed")
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
    locate: object = None  # gateway -> (key, url), for registers whose file moves with each publication

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


# ---- Finland ----

FI_PEOPLE = {"5", "57", "54"}  # partnership (avoin yhtiö), business partnership, bankrupt's estate
FI_FOREIGN = {"19", "60"}       # branch of a foreign company, foreign organisation
# PRH publishes these situation codes without an English description.
FI_SITUATIONS = {"SELTILA": "In liquidation", "KONK": "Bankruptcy", "SANE": "Restructuring"}


def _english(descriptions) -> str:
    return next((_clean(d.get("description")) for d in descriptions or [] if d.get("languageCode") == "3"), "")


def fi_record(company: dict) -> dict:
    forms = [f for f in company.get("companyForms") or [] if not f.get("endDate")]
    if not forms:
        raise Skip("no current company form")
    form = forms[0]
    if form.get("type") in FI_PEOPLE:
        raise Skip("natural person")
    number = _clean((company.get("businessId") or {}).get("value"))
    names = [n for n in company.get("names") or [] if not n.get("endDate")]
    main = next((_clean(n.get("name")) for n in names if n.get("type") == "1"), "")
    if not number or not main:
        raise Skip("no number or name")
    others = sorted({_clean(n.get("name")) for n in names if n.get("type") != "1" and _clean(n.get("name"))} - {main})
    situations = [_english(s.get("descriptions")) or FI_SITUATIONS.get(_clean(s.get("type")), _clean(s.get("type")))
                  for s in company.get("companySituations") or [] if not s.get("endDate")]
    street = next((a for a in company.get("addresses") or [] if a.get("type") == 1), None) or \
        next((a for a in company.get("addresses") or [] if a.get("type") == 2), None) or {}
    city = next((_clean(o.get("city")) for o in street.get("postOffices") or [] if o.get("languageCode") == "1"), "")
    line = " ".join(p for p in (_clean(street.get("street")), _clean(street.get("buildingNumber")),
                                _clean(street.get("entrance"))) if p)
    line = line or (f"PL {_clean(street.get('postOfficeBox'))}" if _clean(street.get("postOfficeBox")) else "")
    line_of_business = _clean((company.get("mainBusinessLine") or {}).get("type"))
    website = _clean((company.get("website") or {}).get("url"))
    return {
        "directoryId": mint("FI", "RA000188", number),
        "legalName": main,
        "otherNames": others,
        "legalForm": _english(form.get("descriptions")) or None,
        "legalJurisdiction": None if form.get("type") in FI_FOREIGN else "FI",
        "registrationAuthority": "FI-PRH",
        "registrationNumber": number,
        "legalStatus": situations[0] if situations else "Active",
        "incorporationDate": _date((company.get("businessId") or {}).get("registrationDate")),
        "legalAddress": {k: v for k, v in {"addressLines": [line] if line else [], "city": city,
                                           "postalCode": _clean(street.get("postCode")), "country": "FI"}.items() if v},
        "industryCodes": [line_of_business] if line_of_business.isdigit() else [],
        "website": website or None,
        "recordMode": "mirror",
    }


FINLAND = Profile(
    source="fi-prh", job="ingest-fi", store="fi", filename="fi-prh-all-companies", extension=".json.zip",
    file_url="https://avoindata.prh.fi/opendata-ytj-api/v3/all_companies", ra="RA000188",
    read=zipped_json_array(), mapper=fi_record)


# ---- Australia ----

AU_TYPES = {"APTY": "Australian proprietary company", "APUB": "Australian public company", "FNOS": "Foreign company",
            "RACN": "Registered Australian body"}
AU_CLASSES = {"LMSH": "limited by shares", "LMGT": "limited by guarantee", "LMSG": "limited by shares and guarantee",
              "UNLM": "unlimited", "NLIA": "no liability"}
AU_STATUS = {"REGD": "Registered", "DRGD": "Deregistered", "SOFF": "Strike-off action in progress",
             "EXAD": "External administration", "NOAC": "Not active", "CNCL": "Cancelled", "DISS": "Dissolved"}


def grouped_tsv(key: str, encoding="utf-8-sig"):
    """A reader for a ZIP holding one tab-separated file sorted by ``key``: one dict per key, with all its rows."""
    def read(binary_file):
        with zipfile.ZipFile(binary_file) as z:
            names = [n for n in z.namelist() if n.lower().endswith((".csv", ".txt", ".tsv"))]
            if len(names) != 1:
                raise ValueError(f"expected one data file in the archive, found {names}")
            with z.open(names[0]) as raw:
                group, last = [], None
                for row in csv.DictReader(io.TextIOWrapper(raw, encoding=encoding, newline=""), delimiter="\t"):
                    k = _clean(row.get(key))
                    if group and k != last:
                        if k < last:
                            raise ValueError(f"the file isn't in {key} order at {k}")
                        yield {"key": last, "rows": group}
                        group = []
                    group.append(row)
                    last = k
                if group:
                    yield {"key": last, "rows": group}
    return read


def _au_date(value) -> str | None:
    value = _clean(value)
    if len(value) == 10 and value[2] == "/" and value[5] == "/":
        return f"{value[6:]}-{value[3:5]}-{value[:2]}"
    return None


def au_record(company: dict) -> dict:
    rows = company["rows"]
    current = next((r for r in rows if _clean(r.get("Current Name Indicator")) == "Y"), None)
    acn = _clean(company["key"])
    if current is None or not acn.isdigit():
        raise Skip("no current name or number")
    name = _clean(current.get("Company Name"))
    if not name:
        raise Skip("no number or name")
    kind, klass = _clean(current.get("Type")), _clean(current.get("Class"))
    form = AU_TYPES.get(kind, kind)
    if klass in AU_CLASSES:
        form = f"{form}, {AU_CLASSES[klass]}"
    others = sorted({_clean(r.get("Company Name")) for r in rows if r is not current} - {name, ""})
    status = _clean(current.get("Status"))
    return {
        "directoryId": mint("AU", "RA000014", acn),
        "legalName": name,
        "otherNames": others,
        "legalForm": form or None,
        "legalJurisdiction": None if kind == "FNOS" else "AU",
        "registrationAuthority": "AU-ASIC",
        "registrationNumber": acn,
        "legalStatus": AU_STATUS.get(status, status) or None,
        "incorporationDate": _au_date(current.get("Date of Registration")),
        "dissolutionDate": _au_date(current.get("Date of Deregistration")),
        "legalAddress": {"country": "AU"},
        "recordMode": "mirror",
    }


AU_PACKAGE = "https://data.gov.au/data/api/3/action/package_show?id=asic-companies"


def au_locate(gateway):
    """The current extract, from the catalogue: (its name, e.g. company_202611, and its URL)."""
    resources = gateway.get_json(AU_PACKAGE)["result"]["resources"]
    zips = [r for r in resources if (r.get("format") or "").upper() == "ZIP" and "current" in (r.get("name") or "").lower()]
    if len(zips) != 1:
        raise ValueError(f"expected one current ZIP in the ASIC dataset, found {len(zips)}")
    url = zips[0]["url"]
    if not url.startswith("https://data.gov.au/"):
        raise ValueError(f"the ASIC extract is served from an unexpected place: {url}")
    return url.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower(), url


AUSTRALIA = Profile(
    source="au-asic", job="ingest-au", store="au", filename="au-asic", extension=".zip", file_url="", ra="RA000014",
    read=grouped_tsv("ACN"), mapper=au_record, locate=au_locate)


PROFILES = {p.source: p for p in (IRELAND, FINLAND, AUSTRALIA)}
