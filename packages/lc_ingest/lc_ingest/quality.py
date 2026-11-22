"""How good a source's data is: measured on every record a trial run maps (owner, 2026-11-22).

A source on trial (config/sources.yaml pipeline stage) runs into its own store and stays out of the snapshot
until the owner promotes it. Each trial run measures what it mapped and reports it twice:
- the ``quality`` event, counts and register vocabulary only (status words, legal forms), because the
  system's logs are public: how many records, how full each field is, the status words and whether
  config/status.yaml maps them, the shapes of registration numbers, duplicates, implausible dates and names,
  anything that looks like contact details, and why rows were skipped;
- ``trial-report.json`` beside the store (private, for the admin page): the same, plus a small sample of
  records to read.

A source is ready for production when its records look right in the sample, the fields the register is known
for are filled, the status words are all mapped, and the anomaly counts are explained or near zero.
"""

from __future__ import annotations

import hashlib
import random
import re
from collections import Counter
from datetime import date

FIELDS = ("legalName", "otherNames", "legalForm", "legalJurisdiction", "registrationNumber", "legalStatus",
          "incorporationDate", "dissolutionDate", "industryCodes", "website", "lei")
ADDRESS_FIELDS = ("addressLines", "city", "region", "postalCode", "country")
TOP = 20
DISTINCT_CAP = 2000          # vocabularies (status words, forms) beyond this are counted as "(other)"
SAMPLE = 25
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[a-z]{2,}", re.I)
PHONE = re.compile(r"(?:\+|\b00)\d[\d ()-]{7,}\d|\b0\d{2,4}[ -]?\d{3}[ -]?\d{3,4}\b")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _shape(number: str) -> str:
    """A registration number's shape: digits as 9, letters as A, the rest kept ("12-3456" is 99-9999)."""
    return re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", number))[:24]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _bump(counter: Counter, key) -> None:
    key = str(key)
    if key in counter or len(counter) < DISTINCT_CAP:
        counter[key] += 1
    else:
        counter["(other)"] += 1


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


class Quality:
    """Feed it every mapped record (``add``); ``summary`` for the log, ``report`` for the store."""

    def __init__(self, source: str, statuses=None, today: date = None, seed: int = 0):
        self.source, self.statuses, self.today = source, statuses, today or date.today()
        self.records = 0
        self.fill, self.address = Counter(), Counter()
        self.status_words, self.forms, self.countries, self.shapes = Counter(), Counter(), Counter(), Counter()
        self.anomalies = Counter()
        self.unmapped = 0
        self._names, self._ids = set(), set()
        self._rng = random.Random(seed)
        self.sample = []

    def add(self, record: dict) -> None:
        self.records += 1
        n = self.records
        for field in FIELDS:
            if record.get(field) not in (None, "", [], {}):
                self.fill[field] += 1
        address = record.get("legalAddress") or {}
        for field in ADDRESS_FIELDS:
            if address.get(field):
                self.address[field] += 1
        self._names_and_ids(record, address)
        self._dates(record)
        word = record.get("legalStatus")
        _bump(self.status_words, word if word else "(none)")
        if self.statuses is not None and word and not self.statuses.category(self.source, word)[1]:
            self.unmapped += 1
        if record.get("legalForm"):
            _bump(self.forms, record["legalForm"])
        _bump(self.countries, address.get("country") or "(none)")
        if record.get("registrationNumber"):
            _bump(self.shapes, _shape(str(record["registrationNumber"])))
        if any(EMAIL.search(s) or PHONE.search(s) for s in _strings({k: v for k, v in record.items()
                                                                      if k not in ("registrationNumber", "directoryId",
                                                                                   "lei", "industryCodes")})):
            self.anomalies["contact-like value (email or phone)"] += 1
        # Reservoir sample: every record equally likely, the same records for the same input.
        if len(self.sample) < SAMPLE:
            self.sample.append(record)
        else:
            j = self._rng.randrange(n)
            if j < SAMPLE:
                self.sample[j] = record

    def _names_and_ids(self, record: dict, address: dict) -> None:
        name = " ".join(str(record.get("legalName") or "").split())
        if len(name) < 2:
            self.anomalies["name shorter than 2 characters"] += 1
        if len(name) > 250:
            self.anomalies["name longer than 250 characters"] += 1
        if name and not any(c.isalpha() for c in name):
            self.anomalies["name without letters"] += 1
        if re.search(r"https?://|www\.", name, re.I):
            self.anomalies["name holding a web address"] += 1
        key = hashlib.blake2b(f"{name.casefold()}|{address.get('country', '')}|{address.get('postalCode', '')}".encode(),
                              digest_size=8).digest()
        if key in self._names:
            self.anomalies["same name, country and postcode as an earlier record"] += 1
        else:
            self._names.add(key)
        directory_id = record.get("directoryId")
        if directory_id in self._ids:
            self.anomalies["directory ID repeated in the file"] += 1
        else:
            self._ids.add(directory_id)

    def _dates(self, record: dict) -> None:
        found = {}
        for field in ("incorporationDate", "dissolutionDate"):
            value = record.get(field)
            if not value:
                continue
            if not ISO_DATE.fullmatch(str(value)):
                self.anomalies[f"{field} not a date"] += 1
                continue
            try:
                day = date.fromisoformat(value)
            except ValueError:
                self.anomalies[f"{field} not a date"] += 1
                continue
            if day > self.today:
                self.anomalies[f"{field} in the future"] += 1
            if day.year < 1600:
                self.anomalies[f"{field} before 1600"] += 1
            found[field] = day
        if len(found) == 2 and found["dissolutionDate"] < found["incorporationDate"]:
            self.anomalies["dissolved before incorporated"] += 1

    def summary(self, counts=None) -> dict:
        """Counts and register vocabulary only: safe for the public log."""
        n = self.records
        skipped = {k.split(": ", 1)[1]: v for k, v in (counts or {}).items() if k.startswith("skipped: ")}
        return {
            "records": n,
            "skipped": skipped,
            "fill": {f: _pct(self.fill[f], n) for f in FIELDS if self.fill[f]},
            "addressFill": {f: _pct(self.address[f], n) for f in ADDRESS_FIELDS if self.address[f]},
            "statusWords": self.status_words.most_common(TOP),
            "statusUnmapped": self.unmapped,
            "legalForms": self.forms.most_common(TOP),
            "countries": self.countries.most_common(10),
            "numberShapes": self.shapes.most_common(5),
            "mainShapeShare": _pct(self.shapes.most_common(1)[0][1], sum(self.shapes.values())) if self.shapes else 0.0,
            "anomalies": dict(self.anomalies.most_common()),
        }

    def report(self, counts=None, publish: str = "") -> dict:
        """The summary and a sample of records, for the admin page (private)."""
        return {"source": self.source, "publish": publish, "at": self.today.isoformat(), **self.summary(counts),
                "sample": self.sample}
