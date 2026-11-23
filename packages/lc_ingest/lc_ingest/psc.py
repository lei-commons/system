"""UK corporate owners: the entity rows of Companies House's register of people with significant control (PSC).

Web-asserted facts pilot, step 2 (owner decision 2026-11-23: "UK corporate owners (entity rows only) as a new trial
source"). Each row of the PSC snapshot says who controls a UK company. Only rows whose controller is an entity
(a company, a legal person, an overseas entity) are read; rows about individuals, super-secure persons, statements
and exemptions are skipped unread, so no person's name, address or birth date is ever mapped (Articles 1-3).

A row becomes one relationship: the company (start) IS_SIGNIFICANTLY_CONTROLLED_BY the controller (end), with the
band of shares or votes held, when the control was notified and when it ceased. A controller registered at
Companies House is named by its company number, so it links to a record we hold; any other controller keeps its
register's country and number. No names are kept at all: identifiers, dates and the band only.

The snapshot is published daily as one large ZIP and as numbered parts of it. A trial measures part 1 (a
sample of the whole, a few hundred megabytes at most); the report says how many parts there are.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import re
import zipfile
from collections import Counter
from datetime import date

from .bulk import Profile
from .pipeline import Skip

PAGE = "https://download.companieshouse.gov.uk/en_pscdata.html"
BASE = "https://download.companieshouse.gov.uk/"
PART = re.compile(r'href="([^"]*?psc-snapshot-(\d{4}-\d{2}-\d{2})_(\d+)of(\d+)\.zip)"', re.IGNORECASE)
WHOLE = re.compile(r'href="([^"]*?psc-snapshot-(\d{4}-\d{2}-\d{2})\.zip)"', re.IGNORECASE)

RELATIONSHIP = "IS_SIGNIFICANTLY_CONTROLLED_BY"
ENTITY_KINDS = ("corporate-entity-person-with-significant-control", "legal-person-person-with-significant-control",
                "corporate-entity-beneficial-owner", "legal-person-beneficial-owner")
UK_PLACES = ("companies house", "registrar of companies", "england", "wales", "scotland", "northern ireland",
             "united kingdom", "great britain", "uk")
CH_NUMBER = re.compile(r"^(?:\d{8}|[A-Z]{2}\d{6})$")
BAND = re.compile(r"(?:ownership-of-shares|voting-rights)-(\d+)-to-(\d+)-percent")


def locate(gateway):
    """The newest snapshot's part 1 (or, if no parts are listed, the whole file): (key, url)."""
    page = page_text(gateway, PAGE)
    parts = sorted({(m.group(2), int(m.group(3)), int(m.group(4)), m.group(1)) for m in PART.finditer(page)},
                   reverse=True)
    if parts:
        day = parts[0][0]
        first = min((p for p in parts if p[0] == day), key=lambda p: p[1])
        return f"{day} part {first[1]} of {first[2]}", _absolute(first[3])
    whole = sorted({(m.group(2), m.group(1)) for m in WHOLE.finditer(page)}, reverse=True)
    if whole:
        return f"{whole[0][0]} whole", _absolute(whole[0][1])
    raise ValueError("the PSC download page lists no psc-snapshot file")


def page_text(gateway, url: str, limit: int = 2 << 20) -> str:
    """The download page's HTML (at most ``limit`` bytes), through the gateway."""
    conn, resp = gateway.open(url, headers={"Accept": "text/html"})
    try:
        return resp.read(limit).decode("utf-8", errors="replace")
    finally:
        conn.close()


def _absolute(href: str) -> str:
    if href.startswith("https://download.companieshouse.gov.uk/"):
        return href
    if "://" in href or href.startswith("//"):
        raise ValueError("a PSC link points away from download.companieshouse.gov.uk")
    return BASE + href.lstrip("/")


def read(binary_file):
    """A ZIP holding one JSON-lines file: one dict per line, streamed."""
    with zipfile.ZipFile(binary_file) as z:
        members = [m for m in z.infolist() if not m.is_dir()]
        if len(members) != 1:
            raise ValueError(f"expected one file in the PSC ZIP, found {len(members)}")
        with z.open(members[0]) as raw:
            for line in io.TextIOWrapper(raw, encoding="utf-8"):
                line = line.strip()
                if line:
                    yield json.loads(line)


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def company_number(value) -> str:
    """A Companies House number as the register writes it: 8 characters, digits padded on the left."""
    number = _clean(value).upper().replace(" ", "")
    if number.isdigit():
        number = number.zfill(8)
    elif len(number) > 2 and number[:2].isalpha() and number[2:].isdigit():
        number = number[:2] + number[2:].zfill(6)
    return number


def _controller(identification: dict) -> str:
    number = company_number(identification.get("registration_number"))
    if not number:
        raise Skip("controller has no registration number")
    place = " ".join(_clean(identification.get(k)).lower() for k in ("place_registered", "country_registered"))
    if any(p in place for p in UK_PLACES) and CH_NUMBER.match(number):
        return f"GB-COH:{number}"
    country = _clean(identification.get("country_registered") or identification.get("place_registered")).upper()
    return f"OTHER:{country or 'UNKNOWN'}:{number}"


def band(natures) -> str | None:
    """The band of shares (or, failing that, votes) held, e.g. "75-100"; None when control is by other means."""
    found = {}
    for nature in natures or []:
        m = BAND.search(nature)
        if m:
            found.setdefault("shares" if nature.startswith("ownership") else "votes", f"{m.group(1)}-{m.group(2)}")
    return found.get("shares") or found.get("votes")


def relationship(row: dict) -> dict:
    data = row.get("data") or {}
    kind = data.get("kind") or ""
    if kind not in ENTITY_KINDS:
        raise Skip("individual" if kind.startswith("individual") else "not an entity controller")
    child = company_number(row.get("company_number"))
    if not CH_NUMBER.match(child):
        raise Skip("company number malformed")
    end = _controller(data.get("identification") or {})
    start = f"GB-COH:{child}"
    notified, ceased = _clean(data.get("notified_on"))[:10] or None, _clean(data.get("ceased_on"))[:10] or None
    key = hashlib.sha256(f"{start}|{end}|{notified}".encode()).hexdigest()[:20].upper()
    return {"directoryId": f"PSC{key}", "startNode": start, "endNode": end, "relationshipType": RELATIONSHIP,
            "relationshipStatus": "INACTIVE" if ceased else "ACTIVE", "percentage": band(data.get("natures_of_control")),
            "periodStart": notified, "periodEnd": ceased}


SAMPLE = 25


class RelationshipQuality:
    """The trial measure for a relationship source (lc_ingest.quality.Quality's interface). Counts and
    identifiers only: these records carry no names."""

    FIELDS = ("percentage", "periodStart", "periodEnd")

    def __init__(self, source: str, statuses=None, today: date = None, seed: int = 0):
        self.source, self.today = source, today or date.today()
        self.records = 0
        self.fill, self.status, self.registers, self.countries, self.bands = (Counter() for _ in range(5))
        self.anomalies = Counter()
        self._pairs = set()
        self._rng = random.Random(seed)
        self.sample = []

    def add(self, record: dict) -> None:
        self.records += 1
        for field in self.FIELDS:
            if record.get(field):
                self.fill[field] += 1
        self.status[record.get("relationshipStatus") or "(none)"] += 1
        end = record.get("endNode") or ""
        register = end.split(":", 1)[0]
        self.registers["Companies House (links to a UK record)" if register == "GB-COH" else "another register"] += 1
        if register == "OTHER":
            self.countries[end.split(":")[1] if end.count(":") >= 2 else "(none)"] += 1
        self.bands[record.get("percentage") or "(other control)"] += 1
        if record.get("startNode") == end:
            self.anomalies["company controls itself"] += 1
        pair = (record.get("startNode"), end, record.get("relationshipStatus"))
        if record.get("relationshipStatus") == "ACTIVE":
            if pair in self._pairs:
                self.anomalies["same active link twice"] += 1
            self._pairs.add(pair)
        start, finish = record.get("periodStart"), record.get("periodEnd")
        if start and start > self.today.isoformat():
            self.anomalies["notified in the future"] += 1
        if start and finish and finish < start:
            self.anomalies["ceased before notified"] += 1
        if len(self.sample) < SAMPLE:
            self.sample.append(record)
        else:
            j = self._rng.randrange(self.records)
            if j < SAMPLE:
                self.sample[j] = record

    def summary(self, counts=None) -> dict:
        n = self.records
        pct = (lambda part: round(100.0 * part / n, 1) if n else 0.0)
        skipped = {k.split(": ", 1)[1]: v for k, v in (counts or {}).items() if k.startswith("skipped: ")}
        return {
            "kind": "relationship",
            "records": n,
            "skipped": skipped,
            "fill": {f: pct(self.fill[f]) for f in self.FIELDS if self.fill[f]},
            # The Trials tab shows these under its usual headings: status, legal forms (here: the controller's
            # register) and countries (here: controllers registered outside the UK).
            "statusWords": self.status.most_common(),
            "statusUnmapped": 0,
            "legalForms": self.registers.most_common(),
            "countries": self.countries.most_common(10),
            "bands": self.bands.most_common(),
            "anomalies": dict(self.anomalies.most_common()),
        }

    def report(self, counts=None, publish: str = "") -> dict:
        return {"source": self.source, "publish": publish, "at": self.today.isoformat(), **self.summary(counts),
                "sample": self.sample}


UK_PSC = Profile(
    source="gb-coh-psc", job="ingest-gb-psc", store="gb-coh-psc", filename="gb-coh-psc", extension=".zip",
    file_url=PAGE, ra="RA000585", read=read, mapper=relationship, locate=locate, kind="relationship")
