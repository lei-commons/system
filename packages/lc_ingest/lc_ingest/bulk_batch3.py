"""Bulk registers approved by the owner on 2026-11-22, batch 3, all on trial first (docs/source-trials.md).

Their layouts were read in the publishers' documentation, never in a downloaded file (the session never
downloads), so these profiles read columns by name, through a list of the names each column is documented
under (``Columns``). A file without a required column stops the run with the column names it has, never a
value; the first trial run then says what the file really holds. Encoding and delimiter are detected from the
file's first bytes, and a ZIP's largest matching member is read.

- Cyprus (Department of Registrar of Companies and Intellectual Property, data.gov.cy, CC BY 4.0): the monthly
  organisations CSV. Companies and overseas companies only: business names (often sole traders) and
  partnerships (Cyprus's "συνεταιρισμοί", usually people) are dropped, and so are a company's earlier names. The
  officials file is never read. GLEIF RA000161 is the same register.
- Slovenia (AJPES business register, podatki.gov.si, CC BY 4.0): the twice-monthly CSV. Sole traders (s.p.) and
  private room renters are people and are dropped, and so are a business's branch units.
- Netherlands (Belastingdienst ANBI list, CC0): public-benefit institutions by RSIN, an Excel workbook in a ZIP,
  read without any extra library (an .xlsx is itself a ZIP of XML).
- Scotland (OSCR, the Scottish Charity Register, OGL v3): the daily register download. A charity that is a
  company is in the directory through Companies House, and a cross-border charity through the Charity Commission
  of England and Wales, so both are skipped. The principal office or trustees' address, often a trustee's
  own, is never read; nor are the notes, objectives or any trustee names.
- Canada (Canada Revenue Agency, list of charities, OGL-Canada): the newest year's identification file. Only city
  and province are kept of the mailing address, which can be a director's own.
- United States (CMS NPPES, public domain): the monthly full file of health-care providers. Organisations only
  (entity type 2); individual practitioners (type 1) and organisation subparts are dropped, and no authorised
  official's name, title or phone is read.
"""

from __future__ import annotations

import csv
import gzip
import io
import re
import zipfile
from datetime import date

from lc_core.directory_id import mint

from .bulk import Profile, _clean, ckan_resource
from .gleif import Skip

DELIMITERS = (",", ";", "\t", "|")


def _key(name) -> str:
    """A column name compared loosely: case, accents' spacing, underscores and spaces don't matter."""
    return re.sub(r"[\s_]+", " ", _clean(name).lstrip("﻿")).casefold().strip()


class Columns:
    """Columns found by any of the names each is documented under. ``required`` must all be found."""

    def __init__(self, required: dict, optional: dict = None):
        self.required, self.optional = required, optional or {}

    def resolve(self, header, what: str) -> dict:
        have = {}
        for name in header:
            for k in {_key(name), _key(str(name).split("/")[0])}:  # "BN/NE": a bilingual header's English half
                have.setdefault(k, name)
        found, missing = {}, []
        for field, names in {**self.required, **self.optional}.items():
            match = next((have[_key(n)] for n in names if _key(n) in have), None)
            if match is not None:
                found[field] = match
            elif field in self.required:
                missing.append(field)
        if missing:
            raise ValueError(f"{what}: no column for {missing} (looked for "
                             f"{[self.required[m] for m in missing]}); the file has {[_clean(h) for h in header][:60]}")
        return found


def _encoding(head: bytes, fallback: str) -> str:
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 with its byte-order mark (a Windows export)
        return "utf-16"
    try:
        head[:len(head) - 4].decode("utf-8") if len(head) > 4 else head.decode("utf-8")
        return "utf-8-sig"
    except UnicodeDecodeError:
        return fallback


def _delimiter(first_line: str) -> str:
    return max(DELIMITERS, key=first_line.count)


def _rows(open_text, columns: Columns, what: str, fallback: str, literal_quotes: bool = False):
    """Rows of one CSV as {field: value}. ``open_text(encoding)`` opens it (as bytes when None), anew each time.
    ``literal_quotes``: a double quote is a plain character (files that never quote fields but hold bare quotes)."""
    with open_text(None) as probe:
        head = probe.read(1 << 20)
    encoding = _encoding(head, fallback)
    with open_text(encoding) as text:
        first = text.readline()
        reader = csv.reader(io.StringIO(first), delimiter=_delimiter(first))
        header = next(reader, [])
        found = columns.resolve(header, what)
        index = {field: header.index(name) for field, name in found.items()}
        quoting = {"quoting": csv.QUOTE_NONE} if literal_quotes else {}
        for values in csv.reader(text, delimiter=_delimiter(first), **quoting):
            if not values:
                continue
            yield {field: (values[i] if i < len(values) else "") for field, i in index.items()}


def _readme(name: str) -> bool:
    """A notes file packed beside the data (README.txt, LEESMIJ.txt), never the data itself."""
    return bool(re.match(r"(readme|leesmij|lisezmoi|liesmich)\b", name.rsplit("/", 1)[-1], re.I))


def table(columns: Columns, member=r".*\.(csv|txt)$", fallback_encoding="cp1252", literal_quotes=False):
    """A reader for a CSV, plain or as the largest matching member of a ZIP (or gzip), with columns by name."""
    def read(binary_file):
        head = binary_file.read(4)
        binary_file.seek(0)
        if head[:2] == b"\x1f\x8b":
            binary_file = gzip.GzipFile(fileobj=binary_file)
            head = binary_file.read(4)
            binary_file.seek(0)
        if head.startswith(b"PK"):
            with zipfile.ZipFile(binary_file) as z:
                infos = [i for i in z.infolist() if re.match(member, i.filename.rsplit("/", 1)[-1], re.I)]
                infos = [i for i in infos if not _readme(i.filename)] or infos
                if not infos:
                    raise ValueError(f"no file matching {member} in the archive (it holds {z.namelist()[:10]})")
                name = max(infos, key=lambda i: i.file_size).filename

                def open_text(encoding):
                    raw = z.open(name)
                    return raw if encoding is None else io.TextIOWrapper(raw, encoding=encoding, errors="replace",
                                                                        newline="")
                yield from _rows(open_text, columns, name, fallback_encoding, literal_quotes)
            return

        def open_plain(encoding):
            binary_file.seek(0)
            view = io.BufferedReader(_Unclosing(binary_file))
            return view if encoding is None else io.TextIOWrapper(view, encoding=encoding, errors="replace", newline="")
        yield from _rows(open_plain, columns, "the CSV file", fallback_encoding, literal_quotes)
    return read


class _Unclosing(io.RawIOBase):
    """A view of an open binary file that leaves it open when closed (the runner owns it)."""

    def __init__(self, f):
        self.f = f

    def readable(self):
        return True

    def readinto(self, b):
        data = self.f.read(len(b))
        b[:len(data)] = data
        return len(data)



def _dmy(value) -> str | None:
    """dd/mm/yyyy (or dd.mm.yyyy, or ISO), optionally with a time, as ISO."""
    value = _clean(value).split(" ")[0]
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", value)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), m.group(3)
        return f"{y}-{mth:02d}-{d:02d}" if 1 <= mth <= 12 and 1 <= d <= 31 else None
    return value if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None


def _mdy(value) -> str | None:
    """mm/dd/yyyy (NPPES) as ISO."""
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", _clean(value))
    if not m:
        return None
    mth, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
    return f"{y}-{mth:02d}-{d:02d}" if 1 <= mth <= 12 and 1 <= d <= 31 else None


def _address(**parts) -> dict:
    return {k: v for k, v in parts.items() if v}


# ---- Cyprus: Registrar of Companies ----

CY_COLUMNS = Columns(
    {"name": ("ORGANISATION_NAME",), "number": ("REGISTRATION_NO",), "type": ("ORGANISATION_TYPE",),
     "status": ("ORGANISATION_STATUS",)},
    {"typeCode": ("ORGANISATION_TYPE_CODE",), "subType": ("ORGANISATION_SUB_TYPE",), "nameStatus": ("NAME_STATUS",),
     "registered": ("REGISTRATION_DATE",), "statusDate": ("ORGANISATION_STATUS_DATE",)})
CY_PREFIX = {"C": "HE", "O": "AE"}   # how the register writes company and overseas company numbers (ΗΕ, ΑΕ)
CY_STRUCK_OFF = ("Διαγραμμ", "Διαλυ")  # struck off, dissolved


def cy_record(row: dict) -> dict:
    name, number = _clean(row.get("name")), _clean(row.get("number")).upper().replace(" ", "")
    kind, code = _clean(row.get("type")), _clean(row.get("typeCode")).upper()
    if not (re.search(r"(^|\s)εταιρ", kind.casefold()) or code in CY_PREFIX):  # not "συνεταιρισμός" (partnership)
        raise Skip("business name or partnership (usually people)")
    name_status = _clean(row.get("nameStatus"))
    if name_status and "Τελευταίο" not in name_status:
        raise Skip("an earlier name of a company")
    if not number or not name:
        raise Skip("no number or name")
    if number.isdigit() and code in CY_PREFIX:
        number = CY_PREFIX[code] + number
    status = _clean(row.get("status"))
    struck_off = any(status.startswith(p) for p in CY_STRUCK_OFF)
    return {
        "directoryId": mint("CY", "RA000161", number),
        "legalName": name,
        "legalForm": " - ".join(p for p in (kind, _clean(row.get("subType"))) if p) or None,
        "legalJurisdiction": "CY",
        "registrationAuthority": "CY-DRCOR",
        "registrationNumber": number,
        "legalStatus": status or None,
        "incorporationDate": _dmy(row.get("registered")),
        "dissolutionDate": _dmy(row.get("statusDate")) if struck_off else None,
        "legalAddress": {"country": "CY"},
        "recordMode": "mirror",
    }


CYPRUS = Profile(
    source="cy-drcor", job="ingest-cy", store="cy", filename="cy-drcor-organisations", extension=".csv",
    file_url="https://data.gov.cy/sites/default/files/organisations_103.csv", guard_names=True, ra="RA000161",
    read=table(CY_COLUMNS, fallback_encoding="cp1253"), mapper=cy_record)


# ---- Slovenia: AJPES business register ----

SI_COLUMNS = Columns(
    {"number": ("Ident_stevilka", "Maticna_stevilka", "Matična številka", "MATICNA_STEVILKA", "MATICNA"),
     "name": ("Popolno_ime", "Dolgi naziv poslovnega subjekta", "Naziv", "POPOLNO_IME", "NAZIV")},
    {"short": ("Kratko_ime", "Kratki naziv enote poslovnega registra"),
     "form": ("Oblika", "Pravno organizacijska oblika", "Pravnoorganizacijska oblika", "PRAVNO_ORG_OBLIKA"),
     "traderName": ("Skrajsano_ime", "Skrajšano ime samostojnega podjetnika"),
     "postcode": ("Postna_stevilka", "Poštna številka"), "city": ("Postni_kraj", "Poštni kraj"),
     "registered": ("Datum_vpisa", "Datum vpisa", "Datum_ustanovitve"), "activity": ("Skd", "Glavna_dejavnost_skd"),
     "deleted": ("Zbrisano", "Oznaka brisanja"), "deletedOn": ("Datum_izbrisa", "Datum izbrisa")})
SI_PEOPLE = re.compile(r"samostojn|podjetnik|sobodaj|fizičn|fizicn|\bs\.\s?p\.", re.I)
# Forms that register one person for an activity, not an organisation. The first measured trial (2026-W48) kept
# farm-activity holders (5,250), registered lawyers (1,098), private doctors and health workers (698), professional
# athletes (583) and private sports workers (215); the rest are forms of the same kind the register also uses.
SI_PERSON_ACTIVITY = re.compile(
    r"na kmetiji|kmetijsk\w* gospodar|odvetnik|notar|zdravnik|zdravstven\w* delav|športnik|športn\w* delav"
    r"|sportnik|sportn\w* delav|novinar|samozaposlen|raziskovalec|izvršitelj|izvrsitelj|upravitelj|tolmač|tolmac"
    r"|izvedenec|cenilec|mediator|detektiv|\bzastopnik", re.I)


def si_record(row: dict) -> dict:
    number, name = re.sub(r"\D", "", _clean(row.get("number"))), _clean(row.get("name"))
    form = _clean(row.get("form"))
    if SI_PEOPLE.search(form) or _clean(row.get("traderName")) or re.search(r"[ ,]s\.\s?p\.$", name, re.I):
        raise Skip("sole trader or private room renter (people)")
    if SI_PERSON_ACTIVITY.search(form):
        raise Skip("a person registered for an activity (farm, profession, sport)")
    if not number or not name:
        raise Skip("no number or name")
    if len(number) == 10 and not number.endswith("000"):
        raise Skip("a branch unit of a registered business")
    deleted = _clean(row.get("deleted")).upper() in ("1", "D", "DA", "Y", "X", "TRUE") or bool(_dmy(row.get("deletedOn")))
    short, activity = _clean(row.get("short")), _clean(row.get("activity"))
    return {
        "directoryId": mint("SI", "SI-PRS", number),
        "legalName": name,
        "otherNames": [short] if short and short != name else [],
        "legalForm": form or None,
        "legalJurisdiction": "SI",
        "registrationAuthority": "SI-AJPES",
        "registrationNumber": number,
        "legalStatus": "Deleted" if deleted else None,
        "incorporationDate": _dmy(row.get("registered")),
        "dissolutionDate": _dmy(row.get("deletedOn")) if deleted else None,
        "industryCodes": [activity] if activity else [],
        "legalAddress": _address(city=_clean(row.get("city")).upper(), postalCode=re.sub(r"\D", "", _clean(row.get("postcode"))),
                                 country="SI"),
        "recordMode": "mirror",
    }


SLOVENIA = Profile(
    source="si-ajpes-prs", job="ingest-si", store="si", filename="si-ajpes-prs", extension=".csv",
    file_url=("https://podatki.gov.si/dataset/9ee1a9aa-c224-4995-b2ad-3760d7af0748/resource/"
              "beb70929-3d0d-41c6-9af2-25d525d906d3/download/opsiprs.csv"),
    guard_names=True, ra="SI-PRS", read=table(SI_COLUMNS, fallback_encoding="cp1250"), mapper=si_record)


# ---- Netherlands: ANBI public-benefit institutions ----

NL_COLUMNS = Columns(
    {"rsin": ("fiscaalNummer", "RSIN", "Fiscaal nummer", "RSIN/fiscaal nummer", "fiscaal_nummer"),
     "name": ("naam", "Statutaire naam", "Naam instelling")},
    {"city": ("vestigingsPlaats", "Vestigingsplaats", "Plaats"), "website": ("webSite", "Website", "Internetadres"),
     "alias": ("aliasNaam", "Aliasnaam", "Alias"), "since": ("ingangsDatum", "Ingangsdatum")})


def xlsx_rows(data: bytes):
    """The first worksheet of an .xlsx as lists of cell texts (no extra library: an .xlsx is a ZIP of XML)."""
    import xml.etree.ElementTree as ET

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as x:
        shared = []
        if "xl/sharedStrings.xml" in x.namelist():
            for si in ET.parse(x.open("xl/sharedStrings.xml")).getroot().iter(f"{ns}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
        sheets = sorted(n for n in x.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not sheets:
            raise ValueError(f"no worksheet in the workbook (it holds {x.namelist()[:10]})")
        with x.open(sheets[0]) as sheet:
            for _, el in ET.iterparse(sheet):
                if el.tag != f"{ns}row":
                    continue
                cells = {}
                for c in el.iter(f"{ns}c"):
                    col = re.sub(r"\d", "", c.get("r", ""))
                    index = 0
                    for ch in col:
                        index = index * 26 + ord(ch) - 64
                    kind, v = c.get("t"), c.find(f"{ns}v")
                    if kind == "s" and v is not None:
                        text = shared[int(v.text)]
                    elif kind == "inlineStr":
                        text = "".join(t.text or "" for t in c.iter(f"{ns}t"))
                    else:
                        text = v.text if v is not None else ""
                    cells[(index or len(cells) + 1) - 1] = text or ""
                el.clear()
                if cells:
                    yield [cells.get(i, "") for i in range(max(cells) + 1)]


def anbi_read(binary_file):
    """The ANBI list: an Excel workbook (or a CSV) inside a ZIP or gzip; rows as {field: value}."""
    data = binary_file.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if not data.startswith(b"PK"):
        raise ValueError("the ANBI file is neither a ZIP nor a gzip")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if "xl/workbook.xml" in names:  # the download is the workbook itself
            book = data
        else:
            files = [n for n in names if not n.endswith("/") and not _readme(n)]
            books = [n for n in files if n.lower().endswith(".xlsx")]
            if not books:
                csvs = [n for n in files if n.lower().endswith((".csv", ".txt"))]
                if csvs:
                    yield from table(NL_COLUMNS)(io.BytesIO(data))
                    return
                xmls = [n for n in files if n.lower().endswith(".xml")]
                if xmls:
                    with z.open(max(xmls, key=lambda n: z.getinfo(n).file_size)) as f:
                        yield from _by_columns(xml_records(f), "the ANBI XML file")
                    return
                jsons = [n for n in files if n.lower().endswith(".json")]
                if jsons:
                    import json
                    doc = json.loads(z.read(max(jsons, key=lambda n: z.getinfo(n).file_size)).decode("utf-8-sig"))
                    yield from _by_columns(json_records(doc), "the ANBI JSON file")
                    return
                raise ValueError("no .xlsx, .csv, .xml or .json data file in the archive (it holds "
                                 f"{[(n, z.getinfo(n).file_size) for n in names][:10]})")
            book = z.read(books[0])
    rows = xlsx_rows(book)
    header = next(rows, [])
    found = NL_COLUMNS.resolve(header, "the ANBI workbook")
    index = {field: header.index(name) for field, name in found.items()}
    for values in rows:
        yield {field: (values[i] if i < len(values) else "") for field, i in index.items()}


def _by_columns(records, what: str):
    """Records as {name: text} read through NL_COLUMNS; the names are resolved on the first record."""
    found = None
    for rec in records:
        if found is None:
            found = NL_COLUMNS.resolve(list(rec), what)
        yield {field: rec.get(name, "") for field, name in found.items()}
    if found is None:
        raise ValueError(f"{what}: no records")


def xml_records(binary_file):
    """The repeated records of an XML file, as {child name: text}: the first element whose children are all
    leaves (at least two) sets the record tag, and every element with that tag is a record. Streamed."""
    import xml.etree.ElementTree as ET

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    record_tag = None
    for _, el in ET.iterparse(binary_file):
        children = list(el)
        if record_tag is None and len(children) >= 2 and all(len(c) == 0 for c in children):
            record_tag = el.tag
        if el.tag == record_tag:
            yield {local(c.tag): (c.text or "").strip() for c in children}
            el.clear()


def json_records(doc):
    """The records of a JSON file: a list of objects, or the largest list of objects inside an object."""
    if isinstance(doc, dict):
        lists = [v for v in doc.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        doc = max(lists, key=len) if lists else []
    for rec in doc if isinstance(doc, list) else []:
        if isinstance(rec, dict):
            yield {k: "" if v is None else str(v) for k, v in rec.items() if not isinstance(v, (dict, list))}


def anbi_record(row: dict) -> dict:
    rsin, name = re.sub(r"\D", "", _clean(row.get("rsin"))), _clean(row.get("name"))
    if rsin and len(rsin) < 9:
        rsin = rsin.zfill(9)  # a number cell drops leading zeros
    if len(rsin) != 9 or not name:
        raise Skip("no number or name")
    alias, website = _clean(row.get("alias")), _clean(row.get("website"))
    return {
        "directoryId": mint("NL", "NL-RSIN", rsin),
        "legalName": name,
        "otherNames": [alias] if alias and alias.casefold() != name.casefold() else [],
        "legalForm": "ANBI (public-benefit institution)",
        "legalJurisdiction": "NL",
        "registrationAuthority": "NL-BELASTINGDIENST-ANBI",
        "registrationNumber": rsin,
        "legalStatus": "ANBI",
        "website": website if website.lower().startswith(("http://", "https://", "www.")) else None,
        "legalAddress": _address(city=_clean(row.get("city")).upper(), country="NL"),
        "recordMode": "mirror",
    }


NETHERLANDS_ANBI = Profile(
    source="nl-anbi", job="ingest-nl-anbi", store="nl-anbi", filename="nl-anbi", extension=".zip",
    file_url="https://download.belastingdienst.nl/data/anbi/anbi.zip", guard_names=True, ra="NL-RSIN",
    read=anbi_read, mapper=anbi_record)


# ---- Scotland: OSCR charity register ----

OSCR_COLUMNS = Columns(
    {"number": ("Charity Number",), "name": ("Charity Name",), "status": ("Charity Status",),
     "form": ("Constitutional Form",)},
    {"registered": ("Registered Date", "Registration Date"),
     "knownAs": ("Known As", "The Name the Charity is known by"), "location": ("Main Operating Location",),
     "website": ("Website",), "regulatory": ("Regulatory Type",)})


def oscr_record(row: dict) -> dict:
    number, name = _clean(row.get("number")).upper().replace(" ", ""), _clean(row.get("name"))
    if not re.fullmatch(r"SC\d{6}", number) or not name:
        raise Skip("no number or name")
    form = _clean(row.get("form"))
    if form.lower().startswith("company"):
        raise Skip("also a company: in the directory through Companies House")
    if "cross" in _clean(row.get("regulatory")).lower():
        raise Skip("cross-border: in the directory through the Charity Commission")
    known, website, location = _clean(row.get("knownAs")), _clean(row.get("website")), _clean(row.get("location"))
    return {
        "directoryId": mint("GB", "GB-SC", number),
        "legalName": name,
        "otherNames": [known] if known and known.casefold() != name.casefold() else [],
        "legalForm": form or None,
        "legalJurisdiction": "GB",
        "registrationAuthority": "GB-OSCR",
        "registrationNumber": number,
        "legalStatus": _clean(row.get("status")) or None,
        "incorporationDate": _dmy(row.get("registered")),
        "website": website if website.lower().startswith(("http://", "https://", "www.")) else None,
        "legalAddress": _address(region=location if location and "outwith" not in location.lower() else "",
                                 country="GB"),
        "recordMode": "mirror",
    }


SCOTLAND_OSCR = Profile(
    source="gb-oscr", job="ingest-gb-oscr", store="gb-oscr", filename="gb-oscr-register", extension=".bin",
    file_url="https://www.oscr.org.uk/download/charity-register", guard_names=True, ra="GB-SC",
    read=table(OSCR_COLUMNS), mapper=oscr_record)


# ---- Canada: CRA list of charities ----

CRA_SEARCH = ("https://open.canada.ca/data/api/action/package_search?q=%22List%20of%20charities%22"
              "&fq=organization:cra-arc&rows=50")
CRA_COLUMNS = Columns(
    {"bn": ("BN", "BN/NE", "Business Number"), "name": ("Legal Name", "Legal Name/Nom légal")},
    {"account": ("Account Name",), "designation": ("Designation",), "category": ("Category",),
     "city": ("City",), "province": ("Province",), "country": ("Country",)})
CRA_DESIGNATION = {"A": "Public foundation", "B": "Private foundation", "C": "Charitable organization"}
CA_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}


def _name(value) -> str:
    if isinstance(value, dict):  # bilingual catalogue fields
        return _clean(value.get("en") or next(iter(value.values()), ""))
    return _clean(value)


def cra_locate(gateway):
    """The newest year's "Identification" file: (its key, its URL)."""
    results = gateway.get_json(CRA_SEARCH)["result"]["results"]
    years = []
    for package in results:
        m = re.fullmatch(r"(\d{4}) List of charities", _name(package.get("title_translated") or package.get("title")))
        ident = next((r for r in package.get("resources") or []
                      if _name(r.get("name_translated") or r.get("name")).lower() == "identification"), None)
        if m and ident:
            years.append((int(m.group(1)), ident))
    if not years:
        raise ValueError(f"no yearly list of charities with an Identification file ({len(results)} packages)")
    year, ident = max(years, key=lambda pair: pair[0])
    if ident["url"].startswith("/"):  # an uploaded file's link, relative to the catalogue (2026-11-23)
        ident = dict(ident, url="https://open.canada.ca" + ident["url"])
    if not ident["url"].startswith("https://open.canada.ca/"):
        raise ValueError(f"the file is served from an unexpected place: {ident['url'][:120]}")
    stamp = re.sub(r"\D", "", ident.get("last_modified") or ident.get("created") or "")[:8] or "latest"
    return f"{year} {stamp}", ident["url"]


def cra_record(row: dict) -> dict:
    bn, name = _clean(row.get("bn")).upper().replace(" ", ""), _clean(row.get("name"))
    if not re.fullmatch(r"\d{9}RR\d{4}", bn) or not name:
        raise Skip("no number or name")
    account = _clean(row.get("account"))
    province, country = _clean(row.get("province")).upper(), _clean(row.get("country")).upper()
    domestic = country in ("", "CA")
    return {
        "directoryId": mint("CA", "CA-CRA", bn),
        "legalName": name,
        "otherNames": [account] if account and account.casefold() != name.casefold() else [],
        "legalForm": CRA_DESIGNATION.get(_clean(row.get("designation")).upper()) or "Registered charity",
        "legalJurisdiction": "CA",
        "registrationAuthority": "CA-CRA",
        "registrationNumber": bn,
        "legalStatus": "Registered",
        "legalAddress": _address(city=_clean(row.get("city")).upper() if domestic else "",
                                 region=province if domestic and province in CA_PROVINCES else "",
                                 country="CA" if domestic else ""),
        "recordMode": "mirror",
    }


CANADA_CRA = Profile(
    source="ca-cra-charities", job="ingest-ca-cra", store="ca-cra", filename="ca-cra-identification", extension=".csv",
    file_url="", guard_names=True, ra="CA-CRA", read=table(CRA_COLUMNS), mapper=cra_record, locate=cra_locate)


# ---- United States: CMS NPPES, organisation providers ----

NPPES_FILES = "https://download.cms.gov/nppes/"
NPPES_COLUMNS = Columns(
    {"npi": ("NPI",), "type": ("Entity Type Code",),
     "name": ("Provider Organization Name (Legal Business Name)",)},
    {"other": ("Provider Other Organization Name",),
     "city": ("Provider Business Practice Location Address City Name",),
     "state": ("Provider Business Practice Location Address State Name",),
     "postcode": ("Provider Business Practice Location Address Postal Code",),
     "country": ("Provider Business Practice Location Address Country Code (If outside U.S.)",),
     "enumerated": ("Provider Enumeration Date",), "deactivated": ("NPI Deactivation Date",),
     "reactivated": ("NPI Reactivation Date",), "subpart": ("Is Organization Subpart",)})
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


def nppes_locate(gateway, today: date = None):
    """This month's full file if it's out, else last month's: (its key, its URL). CMS names the files by month."""
    today = today or date.today()
    tried = []
    for back in (0, 1):
        year, month = (today.year, today.month - back) if today.month > back else (today.year - 1, 12)
        url = f"{NPPES_FILES}NPPES_Data_Dissemination_{MONTHS[month - 1]}_{year}_V2.zip"
        try:
            conn, _ = gateway.open(url, method="HEAD")
            conn.close()
            return f"{year}-{month:02d}", url
        except Exception as exc:  # noqa: BLE001 - not published yet: try the month before
            tried.append(f"{MONTHS[month - 1]} {year}: {type(exc).__name__}")
    raise ValueError(f"no NPPES full file found ({'; '.join(tried)})")


def nppes_record(row: dict) -> dict:
    if _clean(row.get("type")) != "2":
        raise Skip("an individual practitioner (a person)")
    npi, name = _clean(row.get("npi")), _clean(row.get("name"))
    if _clean(row.get("subpart")).upper() == "Y":
        raise Skip("an organisation subpart, not a separate organisation")
    if not (npi.isdigit() and len(npi) == 10) or not name:
        raise Skip("no number or name")
    deactivated, reactivated = _mdy(row.get("deactivated")), _mdy(row.get("reactivated"))
    inactive = bool(deactivated) and not (reactivated and reactivated >= deactivated)
    other = _clean(row.get("other"))
    foreign = _clean(row.get("country")).upper() not in ("", "US")
    postcode = re.sub(r"\D", "", _clean(row.get("postcode")))[:5]
    return {
        "directoryId": mint("US", "US-NPI", npi),
        "legalName": name,
        "otherNames": [other] if other and other.casefold() != name.casefold() else [],
        "legalForm": "Health-care provider organisation",
        "registrationAuthority": "US-CMS-NPPES",
        "registrationNumber": npi,
        "legalStatus": "Deactivated" if inactive else "Active",
        "legalAddress": _address(city="" if foreign else _clean(row.get("city")).upper(),
                                 region="" if foreign else _clean(row.get("state")).upper(),
                                 postalCode="" if foreign or len(postcode) != 5 else postcode,
                                 country="" if foreign else "US"),
        "recordMode": "mirror",
    }


US_NPPES = Profile(
    source="us-cms-nppes-org", job="ingest-us-nppes", store="us-nppes", filename="us-nppes", extension=".zip",
    file_url="", guard_names=True, ra="US-NPI",
    read=table(NPPES_COLUMNS, member=r"npidata_pfile_.*\.csv$"), mapper=nppes_record, locate=nppes_locate)


BATCH3 = (CYPRUS, SLOVENIA, NETHERLANDS_ANBI, SCOTLAND_OSCR, CANADA_CRA, US_NPPES)
