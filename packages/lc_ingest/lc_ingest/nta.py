"""Japan's National Tax Agency corporate numbers (source jp-nta), from the monthly nationwide full file and
its daily difference files.

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
import html as html_lib
import io
import re
import unicodedata
import zipfile
from datetime import date

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


# ---- The daily difference files (差分) ----
#
# Same 30 columns. A row is a corporation's state after that day's change, and its process code (処理区分)
# says what changed. A file can hold several rows for one corporation; only the latest (最新履歴 = 1) is its
# current state. Every change is an upsert through nta_record, so a closure (21, 71) becomes "Closed: ..."
# from the row's close date and cause, and a reopening (22, 72) "Active" again. A deletion (99), where
# the NTA withdrew the number, never deletes: the stored record is labelled absent from the source, as
# records missing from a whole publication are elsewhere (Article 4: never delete on inference).

PROCESS = {"01": "new", "11": "name changed", "12": "address in Japan changed", "13": "address abroad changed",
           "21": "registration closed", "22": "closure cancelled", "71": "absorbed in a merger",
           "72": "merger annulled", "81": "trade-name registration erased", "99": "deleted"}
WITHDRAWN = {"99"}
ABSENT = "absent-from-source:jp-nta"
FULL_KEY = "zenkoku-unicode"  # the monthly full file's publish key: "<YYYY-MM-DD> zenkoku-unicode"

DATES = (
    (ERA, lambda m: (2018 + int(m.group(1)), int(m.group(2)), int(m.group(3)))),
    (re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"), None),
    (re.compile(r"(?<!\d)(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})(?!\d)"), None),
    (re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)"), None),
)
HEADING = re.compile(r"<(h[1-6])\b[^>]*>(.*?)</\1\s*>", re.S | re.I)


def nta_diff_record(row: dict) -> dict:
    """A daily-diff row: the corporation's latest state, labelled if the NTA deleted its number."""
    if _text(row.get("latest")) != "1":
        raise Skip("an earlier state in the same file")
    process = _text(row.get("process"))
    if process not in PROCESS:
        raise Skip(f"unknown process code {process or '(none)'}")
    record = nta_record(row)
    if process in WITHDRAWN:
        record["labels"] = [ABSENT]
    return record


def keep_known_withdrawn(n, records, current, counts):
    """The runner's ``prepare`` for diffs: a deletion only labels a record we hold; it never adds one."""
    for record in records:
        if ABSENT in (record.get("labels") or []) and record["directoryId"] not in current:
            counts["skipped: deleted, never held"] += 1
            continue
        yield record


def _plain(fragment: str) -> str:
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _squash(text: str) -> str:
    """Without any whitespace, as the browser compares text (sources.FIND_DAILY_FILE)."""
    return "".join(text.split())


def _dates(text: str) -> dict:
    """{YYYY-MM-DD: the text it was written as} for every valid date in ``text``."""
    found = {}
    for rx, parts in DATES:
        for m in rx.finditer(text):
            y, mo, d = parts(m) if parts else (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            try:
                day = date(y, mo, d).isoformat()
            except ValueError:
                continue
            found.setdefault(day, m.group(0))
    return found


def daily_files(html: str, section: str) -> list:
    """The daily files listed under the page's ``section`` heading, oldest first: [{day, number, label}].

    A row counts when it has a download link (doDownload(n)) and exactly one date; ``label`` is that date
    as the page writes it, without spaces, which the browser looks for to click the same row."""
    heads = list(HEADING.finditer(html))
    index = next((i for i, h in enumerate(heads) if _squash(section) in _squash(_plain(h.group(2)))), None)
    if index is None:
        raise ValueError(f"the daily-diff page has no {section} heading")
    body = html[heads[index].end(): heads[index + 1].start() if index + 1 < len(heads) else len(html)]
    rows = {}
    for chunk in re.split(r"<tr\b", body, flags=re.I)[1:]:
        numbers = re.findall(r"doDownload\((\d+)\)", chunk)
        dates = _dates(_plain(chunk))
        if numbers and len(dates) == 1:
            (day, label), = dates.items()
            rows.setdefault(day, {"day": day, "number": numbers[0], "label": _squash(label)})
    if not rows:
        raise ValueError(f"no dated daily files under {section} on the daily-diff page")
    return [rows[d] for d in sorted(rows)]


def latest_full(processed) -> str | None:
    """The date (YYYY-MM-DD) of the newest monthly full file processed, or None."""
    days = [k.split(" ", 1)[0] for k in processed if k.endswith(" " + FULL_KEY)]
    return max(days) if days else None


def diff_key(day: str, base: str) -> str:
    """A diff's publish key names the full file it is applied on, so a new full file applies it again."""
    return f"{day} diff-unicode on {base}"
