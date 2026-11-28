"""Owner-supplied sources (docs/owner-supplied-sources.md): files only a person may download, which the owner
downloads by hand and keeps, cleaned, in the private repository lei-commons/data-downloads. Its copy workflow
writes each source's folder as one bundle to the data bucket:

    intake/<source>/<git tree id>.tar.gz   the cleaned CSVs and their manifest
    intake/<source>/current.json           {"key": <git tree id>, "object": "intake/<source>/<id>.tar.gz", "commit": ...}

The bundle is one complete publication, keyed by its tree id, so the usual runner processes it (lc_ingest.runner:
diff, volume guard, trial measures). The cycle never reaches GitHub, and nothing new goes through the gateway.
In production the bundle is deleted once processed; at every run, any bundle current.json no longer names is
deleted (``tidy``). current.json itself holds no records and stays, so the next run finds its key already done.

India (in-mca): the Ministry of Corporate Affairs' data, cleaned by data-downloads' tools/clean_mca.py. Every row
is a company or an LLP: no natural person is listed. Each identifier becomes one record, from the first of:
1. the master data (``mca/master/<date>/``, the latest snapshot): every company, LLP and foreign company on the
   register with its status today, in MCA's own words;
2. the monthly incorporation reports (January 2016 on): the first report that lists it. A report's status is the
   status at registration, so it isn't kept: the record is "Struck Off" when a strike-off report lists it, else its
   status is not stated;
3. the monthly strike-off reports (October 2023 on) alone, with the identifier and name.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tarfile
from collections import Counter

from lc_core.directory_id import mint

from .bulk import Profile, _clean
from .bulk_batch3 import _address
from .gleif import Skip

INTAKE = "intake"


def _bucket():
    from google.cloud import storage

    return storage.Client().bucket(os.environ["LC_DATA_BUCKET"])


def current(bucket, source: str):
    """The source's current.json, or None when nothing was ever copied."""
    blob = bucket.blob(f"{INTAKE}/{source}/current.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_bytes())


class IntakeEmpty(ValueError):
    """Nothing was ever copied for this source: data-downloads' copy workflow hasn't run for it."""


def locate_for(source: str, bucket=None):
    """Profile.locate: (key, url) of the bundle current.json names."""
    def locate(gateway):
        b = bucket or _bucket()
        pointer = current(b, source)
        if not pointer:
            raise IntakeEmpty(f"no {INTAKE}/{source}/current.json: nothing copied from data-downloads yet")
        if not re.fullmatch(r"[0-9a-f]{40}", pointer.get("key", "")) or \
                pointer.get("object") != f"{INTAKE}/{source}/{pointer['key']}.tar.gz":
            raise IntakeEmpty(f"{INTAKE}/{source}/current.json doesn't name a bundle of this source")
        return pointer["key"], f"gs://{b.name}/{pointer['object']}"
    return locate


def fetch_for(bucket=None):
    """Profile.fetch: copy the bundle from the data bucket into the open file (never through the gateway)."""
    def fetch(gateway, url, out):
        b = bucket or _bucket()
        name = url.split(f"gs://{b.name}/", 1)[1]
        b.blob(name).download_to_file(out)
        return out.tell()
    return fetch


def tidy(bucket, source: str, processed, production: bool, log) -> int:
    """Delete the bundles no longer needed: any current.json doesn't name, and, in production, the one it names
    once processed. A trial never deletes the current bundle: production reads it after promotion."""
    pointer = current(bucket, source) or {}
    deleted = 0
    for blob in bucket.list_blobs(prefix=f"{INTAKE}/{source}/"):
        if not blob.name.endswith(".tar.gz"):
            continue
        if blob.name != pointer.get("object") or (production and pointer.get("key") in processed):
            blob.delete()
            deleted += 1
    if deleted:
        log({"event": "intake-tidied", "source": source, "objects": deleted})
    return deleted


# ---- India: Ministry of Corporate Affairs ----

KINDS = {"companies.csv": "company", "llps.csv": "llp", "foreign-companies.csv": "foreign-company"}
MEMBER = re.compile(r"^mca/(?P<folder>incorporated|struck-off)/(?P<month>\d{4}-\d{2})/(?P<file>[a-z-]+\.csv)$")
MASTER = re.compile(r"^mca/master/(?P<day>\d{4}-\d{2}-\d{2})/(?P<file>[a-z-]+)-\d{2}\.csv$")
IDS = {"company": re.compile(r"[UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}"), "llp": re.compile(r"[A-Z]{3}-\d{4}"),
       "foreign-company": re.compile(r"F\d{5}")}
ID_COLUMN = {"company": "cin", "llp": "llpin", "foreign-company": "fcrn"}
PIN = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def _members(f, folder: str):
    """(month, kind, rows) for each of the bundle's CSVs under ``folder``, oldest month first."""
    f.seek(0)
    with tarfile.open(fileobj=f, mode="r:gz") as tar:
        found = []
        for member in tar:
            m = MEMBER.match(member.name)
            if m and member.isfile() and m["folder"] == folder and m["file"] in KINDS:
                found.append((m["month"], KINDS[m["file"]], member))
        for month, kind, member in sorted(found, key=lambda x: (x[0], x[1])):
            text = io.TextIOWrapper(tar.extractfile(member), encoding="utf-8", newline="")
            yield month, kind, csv.DictReader(text)


def _master(f):
    """(kind, rows) for each part of the bundle's latest master snapshot."""
    f.seek(0)
    with tarfile.open(fileobj=f, mode="r:gz") as tar:
        parts = [(m["day"], member.name, KINDS[m["file"] + ".csv"], member) for member in tar
                 if (m := MASTER.match(member.name)) and member.isfile() and m["file"] + ".csv" in KINDS]
        latest = max((p[0] for p in parts), default=None)
        for day, _, kind, member in sorted(p for p in parts if p[0] == latest):
            text = io.TextIOWrapper(tar.extractfile(member), encoding="utf-8", newline="")
            yield kind, csv.DictReader(text)


def _registration(cin: str):
    """A CIN's state, year and registration number: what stays when the CIN changes (a new main activity, company
    type or listing gives the company a new CIN)."""
    return (cin[6:8], cin[8:12], cin[15:21]) if len(cin) == 21 else None


def mca_read(f):
    """Rows for the mapper, one per company or LLP: the master snapshot's row when it lists the identifier, else
    the first report that lists it (a company is registered once), with the month a strike-off report gives.
    A report's CIN that the master lists under a changed CIN (one company in the master with the same state, year
    and registration number) is the same company, so it is left out."""
    seen, registered = set(), Counter()
    for kind, rows in _master(f):
        for row in rows:
            ident = row.get(ID_COLUMN[kind], "")
            if ident in seen:
                continue
            seen.add(ident)
            if kind == "company":
                registered[_registration(ident)] += 1
            yield {**row, "id": ident, "kind": kind, "master": True}

    def listed(ident: str, kind: str) -> bool:
        if ident in seen:
            return True
        key = _registration(ident) if kind == "company" else None
        return key is not None and registered[key] == 1  # Counter: an unknown key reads 0

    struck = {}
    for month, kind, rows in _members(f, "struck-off"):
        for row in rows:
            struck.setdefault(row.get(ID_COLUMN[kind], ""), (month, kind, row.get("name", "")))
    for month, kind, rows in _members(f, "incorporated"):
        for row in rows:
            ident = row.get(ID_COLUMN[kind], "")
            if listed(ident, kind):
                continue
            seen.add(ident)
            yield {**row, "id": ident, "kind": kind, "struckOff": struck.get(ident, ("",))[0]}
    for ident, (month, kind, name) in struck.items():
        if not listed(ident, kind):  # registered before 2016 and not in the master: from the strike-off report alone
            yield {"id": ident, "kind": kind, "name": name, "struckOff": month}


def _title(v: str) -> str:
    v = _clean(v)
    return v if not v.isupper() else v.title()


def mca_form(row: dict) -> str:
    kind = row.get("kind")
    if kind == "llp":
        return "Limited Liability Partnership"
    if kind == "foreign-company":
        office = _clean(row.get("office_type"))
        return "Foreign company" + (f" ({_title(office)})" if office else "")
    return " - ".join(p for p in (mca_class(row.get("class", "")), mca_category(row.get("category", ""))) if p) or "Company"


def mca_category(v: str) -> str:
    """MCA's reports spell a category many ways ("Company Limted by Shares", "Companies Limited by Shares", ...)."""
    v = _clean(v)
    up = v.upper().replace(" ", "")
    if "GUAR" in up:
        return "Company limited by guarantee"
    if "SHARE" in up:
        return "Company limited by shares"
    if "UNLIMITED" in up:
        return "Unlimited company"
    return v


def mca_class(v: str) -> str:
    """MCA spells a class several ways over the years ("Private(One Person Company)", "Private One Person", ...)."""
    v = _clean(v).upper()
    if "ONE PERSON" in v:
        return "One Person Company"
    if v.startswith("PRIVATE"):
        return "Private"
    if v.startswith("PUBLIC"):
        return "Public"
    return _title(v)


def mca_record(row: dict) -> dict:
    kind, ident, name = row.get("kind"), _clean(row.get("id")), _clean(row.get("name"))
    if kind not in IDS or not IDS[kind].fullmatch(ident) or not name:
        raise Skip("no identifier or name")
    address = _clean(row.get("address"))
    pin = _clean(row.get("pin"))
    pins = [pin] if PIN.fullmatch(pin) else PIN.findall(address)
    activity = _clean(row.get("activity_code"))
    if not activity and kind == "company":
        activity = ident[1:6]  # a CIN holds the company's NIC activity code
    return {
        "directoryId": mint("IN", "IN-MCA", ident),
        "legalName": name,
        "legalForm": mca_form(row),
        "legalJurisdiction": "IN",
        "registrationAuthority": "IN-MCA",
        "registrationNumber": ident,
        "legalStatus": (_clean(row.get("status")) or None) if row.get("master")
        else ("Struck Off" if row.get("struckOff") else None),
        "incorporationDate": _clean(row.get("registered")) or None,
        "industryCodes": [activity] if activity else [],
        "legalAddress": _address(addressLines=[address] if address else [], region=_title(row.get("state", "")),
                                 postalCode=pins[-1] if pins else "",
                                 country="IN") if address or row.get("state") else None,
        "recordMode": "mirror",
    }


INDIA_MCA = Profile(
    source="in-mca", job="ingest-in-mca", store="in-mca", filename="in-mca", extension=".tar.gz",
    file_url="", guard_names=True, ra="IN-MCA", read=mca_read, mapper=mca_record,
    locate=locate_for("in-mca"), fetch=fetch_for(), tidy=tidy)

OWNER_SUPPLIED = (INDIA_MCA,)
