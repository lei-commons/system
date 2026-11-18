"""Japan's National Tax Agency corporate numbers (source jp-nta), from the monthly nationwide full file.

The NTA offers the file only through its download page's form (a one-time token and a session cookie),
so a real browser fetches it (lc_browser; owner exception 2026-11-16). This module is the page parsing,
the reader and the mapper: pure, testable without a browser.

The CSV (Unicode) has no header: 30 columns as in the NTA's published specification. Corporate numbers
are given to corporations and associations, never to individuals, so no record is a natural person.
Text is normalised (NFKC: full-width letters and digits become ordinary ones). A corporation that asked
to be left out of the NTA's search (検索対象除外) is left out here too. Attribution, as the Public Data
License 1.0 requires, is on every record via the registry (config/sources.yaml).
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile

from lc_core.directory_id import mint

from .pipeline import Skip

RA = "RA001075"  # GLEIF: Japan, Corporate Number Publication Site
PAGE = "https://www.houjin-bangou.nta.go.jp/download/zenken/"
HOST = "www.houjin-bangou.nta.go.jp"
SECTION = "CSV形式・Unicode"

COLUMNS = [
    "sequenceNumber", "corporateNumber", "process", "correct", "updateDate", "changeDate", "name", "nameImageId",
    "kind", "prefectureName", "cityName", "streetNumber", "addressImageId", "prefectureCode", "cityCode",
    "postCode", "addressOutside", "addressOutsideImageId", "closeDate", "closeCause", "successorCorporateNumber",
    "changeCause", "assignmentDate", "latest", "enName", "enPrefectureName", "enCityName", "enAddressOutside",
    "furigana", "hihyoji",
]

KINDS = {
    "101": "National government agency", "201": "Local public body", "301": "Kabushiki kaisha (stock company)",
    "302": "Yugen kaisha (limited company)", "303": "Gomei kaisha (general partnership company)",
    "304": "Goshi kaisha (limited partnership company)", "305": "Godo kaisha (limited liability company)",
    "399": "Other registered corporation", "401": "Foreign company", "499": "Other corporation or association",
}
CLOSED = {"01": "Closed: liquidation completed", "11": "Closed: dissolved by merger",
          "21": "Closed by the registrar", "31": "Closed: other liquidation completed"}

ERA = re.compile(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日")


def _text(value) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def page_date(html: str) -> str:
    """The date of the nationwide Unicode CSV on the download page ("令和8年10月30日更新"), as YYYY-MM-DD."""
    start = html.find(SECTION)
    if start < 0:
        raise ValueError(f"the download page has no {SECTION} section")
    m = ERA.search(html, start)
    if not m or m.start() - start > 2000:
        raise ValueError("no update date in the Unicode CSV section")
    year, month, day = 2018 + int(m.group(1)), int(m.group(2)), int(m.group(3))  # Reiwa 1 = 2019
    return f"{year:04d}-{month:02d}-{day:02d}"


def read_zip(binary):
    """Rows of the nationwide Unicode CSV inside the ZIP (its .asc signature file is skipped)."""
    with zipfile.ZipFile(binary) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in the NTA file, found {len(names)}")
        with z.open(names[0]) as raw:
            for values in csv.reader(io.TextIOWrapper(raw, encoding="utf-8", newline="")):
                if len(values) != len(COLUMNS):
                    raise ValueError(f"an NTA row has {len(values)} columns, not {len(COLUMNS)}")
                yield dict(zip(COLUMNS, values))


def _postcode(code: str) -> str:
    code = _text(code)
    return f"{code[:3]}-{code[3:]}" if len(code) == 7 and code.isdigit() else code


def nta_record(row: dict) -> dict:
    number = _text(row.get("corporateNumber"))
    if len(number) != 13 or not number.isdigit():
        raise Skip("no corporate number")
    if _text(row.get("hihyoji")) == "1":
        raise Skip("excluded from the NTA's search at its request")
    name = _text(row.get("name"))
    if not name:
        raise Skip("no name")
    kind = _text(row.get("kind"))
    closed = _text(row.get("closeDate"))
    foreign = kind == "401"
    domestic = {k: v for k, v in {
        "addressLines": [line for line in [_text(row.get("streetNumber"))] if line],
        "city": _text(row.get("cityName")), "region": _text(row.get("prefectureName")), "postalCode": _postcode(row.get("postCode")),
        "country": "JP"}.items() if v}
    outside = _text(row.get("addressOutside"))
    english = _text(row.get("enName"))
    record = {
        "directoryId": mint("JP", RA, number),
        "legalName": name,
        "transliteratedNames": [english] if english and english != name else [],
        "legalForm": KINDS.get(kind) or (f"Kind {kind}" if kind else None),
        "legalJurisdiction": None if foreign else "JP",
        "registrationAuthority": "JP-NTA",
        "registrationNumber": number,
        "legalStatus": CLOSED.get(_text(row.get("closeCause")), "Closed") if closed else "Active",
        "incorporationDate": None,
        "dissolutionDate": closed or None,
        # A foreign company's legal address is its head office abroad; its office in Japan is where it
        # does business here.
        # A foreign company's legal address is its head office abroad. Its address in Japan is an office
        # here, not its headquarters, so it isn't given as either (accuracy before coverage).
        "legalAddress": ({"addressLines": [outside]} if outside else {}) if foreign else domestic,
        "recordMode": "mirror",
    }
    return record
