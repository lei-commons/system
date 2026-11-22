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
is never read. GLEIF RA000188 uses the same business IDs ("0245992-3").

Australia (ASIC company dataset, data.gov.au, CC BY 3.0 AU): a monthly ZIP of tab-separated rows,
one row per name a company has had, in ACN order; located each run through data.gov.au's catalogue
and processed once per publication. Companies only (business names, often sole traders, are a
separate dataset); no addresses or officers. GLEIF RA000014 writes ACNs with spaces, which IDs ignore.

Every profile added from 2026-11-22 names the columns it needs: a file whose header lacks one stops the run
with an error naming the missing columns and the columns the file has (names only, never values), so a first
run against a file we haven't seen yet diagnoses itself in the status report.

Canada (Corporations Canada, open.canada.ca, OGL-Canada 2.0): four daily CSVs (active and inactive,
business and other federal corporations), all processed each week, one after another. The
registered office's street lines are kept when given (owner decision 2026-11-16). The files' corporation
numbers include the check digit, as GLEIF RA000072's do ("454461-7" is 4544617), but never a leading zero,
which GLEIF sometimes keeps ("046311-6" is 463116): config/sources.yaml gleif_identity drops it (2026-11-20).

United States IRS (Exempt Organizations Business Master File extract, public domain): four regional CSVs,
every tax-exempt organisation (about 1.9 million), processed weekly. Organisations only; the "in care of"
column, which usually names a person, is never read. No GLEIF code for EINs is used: IRS records mint under
US-IRS and don't merge with LEI records (an organisation's LEI sits under its state of incorporation).

England and Wales charities (Charity Commission register extract, OGL v3): one tab-separated file in a ZIP,
daily. Main charities only (linked charities share the number). A charity that is also a company is already
in the directory through Companies House, so it is skipped here. No contact address, phone or email is read:
for a small charity they are often a trustee's own. Minted under GB-CCEW.

Estonia (e-Business Register open data): the register's basic data, one semicolon-separated CSV in a ZIP,
daily. Sole proprietors (FIE, "füüsilisest isikust ettevõtja") are people and are dropped. GLEIF RA000181 is the
same register with the same 8-digit codes. Not in PROFILES: the portal asks for its terms of use to be accepted
before downloading, which agents never do (constitution Article 2), so it waits for the owner's decision.
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


def require_columns(reader: csv.DictReader, columns, what: str) -> csv.DictReader:
    """Check the header names ``columns``. The error lists only column names, never a value: a first run against
    a file whose layout we've only read about tells us, in the status report, what the file really holds."""
    have = [_clean(c).lstrip("\ufeff") for c in (reader.fieldnames or [])]
    missing = [c for c in columns if c not in have]
    if missing:
        raise ValueError(f"{what}: missing columns {missing}; the file has {have[:40]}")
    reader.fieldnames = have
    return reader


def zipped_csv(member_suffix=".csv", encoding="utf-8-sig", key=None, delimiter=",", columns=()):
    """A reader for a ZIP holding one CSV: rows as dicts. With ``key``, later rows repeating a key are skipped.
    With ``columns``, the header must name each of them (require_columns)."""
    def read(binary_file):
        with zipfile.ZipFile(binary_file) as z:
            names = [n for n in z.namelist() if n.lower().endswith(member_suffix)]
            if len(names) != 1:
                raise ValueError(f"expected one {member_suffix} file in the archive, found {names}")
            seen = set()
            with z.open(names[0]) as raw:
                rows = csv.DictReader(io.TextIOWrapper(raw, encoding=encoding, newline=""), delimiter=delimiter)
                if columns:
                    require_columns(rows, columns, names[0])
                for row in rows:
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
    parts: tuple = ()      # (name, url) for registers published as several files, all processed each run

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
                                _clean(street.get("entrance")), _clean(street.get("apartmentNumber")),
                                _clean(street.get("apartmentIdSuffix"))) if p)
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


# ---- Canada ----

CA_FILES = "https://d4bf66bykfyaf.cloudfront.net/"
CA_PARTS = tuple((name, CA_FILES + f"corporations-{name}-en.csv") for name in
                 ("active-cbca", "active-non-cbca", "inactive-or-dissolved-cbca", "inactive-or-dissolved-non-cbca"))
CA_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}


def plain_csv(encoding="utf-8-sig", errors="strict", columns=()):
    """A reader for an uncompressed CSV file: rows as dicts. With ``columns``, the header must name each of them."""
    def read(binary_file):
        rows = csv.DictReader(io.TextIOWrapper(binary_file, encoding=encoding, errors=errors, newline=""))
        if columns:
            require_columns(rows, columns, "the CSV file")
        yield from rows
    return read


def ca_record(row: dict) -> dict:
    number = _clean(row.get("Corporation number"))
    name = _clean(row.get("Corporate name - form 1"))
    if not number.isdigit() or not name:
        raise Skip("no number or name")
    province = _clean(row.get("Province/territory")).upper()
    other = _clean(row.get("Corporate name - form 2"))
    return {
        "directoryId": mint("CA", "RA000072", number),
        "legalName": name,
        "otherNames": [other] if other and other != name else [],
        "legalForm": _clean(row.get("Governing legislation")) or None,
        "legalJurisdiction": "CA",
        "registrationAuthority": "CA-CORPORATIONS-CANADA",
        "registrationNumber": number,
        "legalStatus": _clean(row.get("Status")) or None,
        "incorporationDate": _date(row.get("Anniversary date")),
        "legalAddress": {k: v for k, v in {"addressLines": [x for x in (_clean(row.get("Street")), _clean(row.get("Street 2"))) if x],
                                           "city": _clean(row.get("City/town")).upper(),
                                           "region": province if province in CA_PROVINCES else "",
                                           "postalCode": _clean(row.get("Postal code")).upper(),
                                           "country": "CA" if _clean(row.get("Country")).upper() == "CA" else ""}.items() if v},
        "recordMode": "mirror",
    }


CANADA = Profile(
    source="ca-ised", job="ingest-ca", store="ca", filename="ca-corporations", extension=".csv", file_url="",
    ra="RA000072", read=plain_csv(), mapper=ca_record, parts=CA_PARTS)


# ---- United States: IRS exempt organisations ----

IRS_FILES = "https://www.irs.gov/pub/irs-soi/"
IRS_PARTS = tuple((f"region-{n}", f"{IRS_FILES}eo{n}.csv") for n in (1, 2, 3, 4))
IRS_COLUMNS = ("EIN", "NAME", "STREET", "CITY", "STATE", "ZIP", "SUBSECTION", "ORGANIZATION", "STATUS")
IRS_ORGANIZATION = {"1": "Corporation", "2": "Trust", "3": "Co-operative", "4": "Partnership", "5": "Association"}
IRS_STATUS = {"01": "Unconditional exemption", "02": "Conditional exemption", "12": "Trust described in 4947(a)(2)",
              "25": "Terminating private foundation status"}


def _irs_form(row: dict) -> str | None:
    kind = IRS_ORGANIZATION.get(_clean(row.get("ORGANIZATION")).lstrip("0"), "")
    subsection = _clean(row.get("SUBSECTION"))
    if subsection == "92":
        law = "4947(a)(1) charitable trust"
    elif subsection.isdigit() and int(subsection) > 0:
        law = f"exempt under 501(c)({int(subsection)})"
    else:
        law = ""
    return ", ".join(p for p in (kind, law) if p) or None


def irs_record(row: dict) -> dict:
    from .registers import US_CODES

    ein, name = _clean(row.get("EIN")), _clean(row.get("NAME"))
    if not (ein.isdigit() and len(ein) == 9) or not name:
        raise Skip("no number or name")
    state = _clean(row.get("STATE")).upper()
    domestic = state in US_CODES
    zip_code = _clean(row.get("ZIP"))
    status = _clean(row.get("STATUS"))
    return {
        "directoryId": mint("US", "US-IRS", ein),
        "legalName": name,
        "legalForm": _irs_form(row),
        "registrationAuthority": "US-IRS",
        "registrationNumber": ein,
        "legalStatus": IRS_STATUS.get(status, status) or None,
        "legalAddress": {k: v for k, v in {
            "addressLines": [_clean(row.get("STREET"))] if domestic and _clean(row.get("STREET")) else [],
            "city": _clean(row.get("CITY")).upper() if domestic else "",
            "region": state if domestic else "",
            "postalCode": zip_code[:5] if domestic and zip_code[:5].isdigit() else "",
            "country": "US" if domestic else ""}.items() if v},
        "recordMode": "mirror",
    }


IRS = Profile(
    source="us-irs-eo-bmf", job="ingest-us-irs", store="us-irs", filename="us-irs-eo-bmf", extension=".csv",
    file_url="", ra="US-IRS", read=plain_csv(errors="replace", columns=IRS_COLUMNS), mapper=irs_record,
    parts=IRS_PARTS)


# ---- England and Wales: Charity Commission ----

CCEW_COLUMNS = ("registered_charity_number", "linked_charity_number", "charity_name", "charity_type",
                "charity_registration_status", "date_of_registration", "charity_company_registration_number")


def ccew_record(row: dict) -> dict:
    number, name = _clean(row.get("registered_charity_number")), _clean(row.get("charity_name"))
    if not number.isdigit() or not name:
        raise Skip("no number or name")
    if _clean(row.get("linked_charity_number")) not in ("", "0"):
        raise Skip("a linked charity: part of its main charity's record")
    if _clean(row.get("charity_company_registration_number")):
        raise Skip("also a company: in the directory through Companies House")
    website = _clean(row.get("charity_contact_web"))
    return {
        "directoryId": mint("GB", "GB-CCEW", number),
        "legalName": name,
        "legalForm": _clean(row.get("charity_type")) or None,
        "legalJurisdiction": "GB",
        "registrationAuthority": "GB-CCEW",
        "registrationNumber": number,
        "legalStatus": _clean(row.get("charity_registration_status")) or None,
        "incorporationDate": _date(row.get("date_of_registration")),
        "legalAddress": {"country": "GB"},
        "website": website if website.startswith(("http://", "https://", "www.")) else None,
        "recordMode": "mirror",
    }


CCEW = Profile(
    source="gb-ccew", job="ingest-gb-ccew", store="gb-ccew", filename="gb-ccew-charities", extension=".zip",
    file_url="https://ccewuksprdoneregsadata1.blob.core.windows.net/data/txt/publicextract.charity.zip",
    ra="GB-CCEW", read=zipped_csv(member_suffix=".txt", delimiter="\t", columns=CCEW_COLUMNS), mapper=ccew_record)


# ---- Estonia: e-Business Register ----

EE_COLUMNS = ("nimi", "ariregistri_kood", "ettevotja_oiguslik_vorm", "ettevotja_staatus_tekstina",
              "ettevotja_esmakande_kpv")


def _ee_date(value) -> str | None:
    value = _clean(value)
    if len(value) == 10 and value[2] == "." and value[5] == ".":
        return f"{value[6:]}-{value[3:5]}-{value[:2]}"
    return None


def ee_record(row: dict) -> dict:
    code, name = _clean(row.get("ariregistri_kood")), _clean(row.get("nimi"))
    if not code.isdigit() or not name:
        raise Skip("no number or name")
    form = _clean(row.get("ettevotja_oiguslik_vorm"))
    if "isikust ettevõtja" in form.lower() or form.upper() == "FIE":
        raise Skip("natural person")
    address = _clean(row.get("ads_normaliseeritud_taisaadress")) or _clean(row.get("ettevotja_aadress"))
    return {
        "directoryId": mint("EE", "RA000181", code),
        "legalName": name,
        "legalForm": form or None,
        "legalJurisdiction": "EE",
        "registrationAuthority": "EE-RIK",
        "registrationNumber": code,
        "legalStatus": _clean(row.get("ettevotja_staatus_tekstina")) or None,
        "incorporationDate": _ee_date(row.get("ettevotja_esmakande_kpv")),
        "legalAddress": {k: v for k, v in {"addressLines": [address] if address else [],
                                           "postalCode": _clean(row.get("indeks_ettevotja_aadressis")),
                                           "country": "EE"}.items() if v},
        "recordMode": "mirror",
    }


ESTONIA = Profile(
    source="ee-rik", job="ingest-ee", store="ee", filename="ee-rik-basic-data", extension=".csv.zip",
    file_url="https://avaandmed.ariregister.rik.ee/sites/default/files/avaandmed/ettevotja_rekvisiidid__lihtandmed.csv.zip",
    ra="RA000181", read=zipped_csv(delimiter=";", columns=EE_COLUMNS), mapper=ee_record)


PROFILES = {p.source: p for p in (IRELAND, FINLAND, AUSTRALIA, CANADA, IRS, CCEW)}
