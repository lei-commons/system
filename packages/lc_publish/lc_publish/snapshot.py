"""Builds the snapshot from the current records of every source.

A company present in more than one source becomes one record. Register values
win for register fields (Article 4.3); GLEIF supplies the LEI. Every record says
which sources it came from (Article 3.3).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .merkle import leaf, root

SCHEMA_VERSION = 1
# Earlier sources win on conflicting fields: registers before GLEIF.
SOURCES = [
    {"id": "gb-coh", "prefix": "gb", "name": "Companies House basic company data", "licence": "OGL-UK-3.0",
     "attribution": "Contains public sector information licensed under the Open Government Licence v3.0."},
    {"id": "gleif", "prefix": "gleif", "name": "GLEIF LEI golden copy", "licence": "CC0-1.0", "attribution": ""},
]
SCALARS = ["directoryId", "lei", "legalName", "legalForm", "legalJurisdiction", "registrationAuthority",
           "registrationNumber", "legalStatus", "incorporationDate", "dissolutionDate", "recordMode", "registryUrl"]
NESTED = ["otherNames", "legalAddress", "headquartersAddress", "industryCodes", "labels"]
COLUMNS = SCALARS + NESTED + ["sources"]


def merge(records_by_source: dict) -> dict:
    """{source id: {directory id: record}} to {directory id: record}, in SOURCES order."""
    merged = {}
    for source in SOURCES:
        for directory_id, record in records_by_source.get(source["id"], {}).items():
            current = merged.get(directory_id)
            if current is None:
                merged[directory_id] = {**record, "sources": [source["id"]]}
                continue
            for field, value in record.items():
                current.setdefault(field, value)  # the earlier, register source keeps its value
            current["sources"].append(source["id"])
    return merged


def country_of(record: dict) -> str:
    country = (record.get("legalJurisdiction") or "")[:2] or (record.get("legalAddress") or {}).get("country", "")
    return country.upper() if len(country) == 2 and country.isalpha() else "XX"


def _row(record: dict) -> dict:
    row = {c: record.get(c) for c in SCALARS}
    for c in NESTED:
        value = record.get(c)
        row[c] = json.dumps(value, sort_keys=True, ensure_ascii=False) if value not in (None, [], {}) else None
    row["sources"] = ",".join(record["sources"])
    return row


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def build(records_by_source: dict, out: Path, date: str) -> dict:
    """Write one Parquet file per country and return the manifest."""
    out.mkdir(parents=True, exist_ok=True)
    merged = merge(records_by_source)
    by_country = defaultdict(list)
    for directory_id in sorted(merged):
        by_country[country_of(merged[directory_id])].append(merged[directory_id])

    files = []
    for country in sorted(by_country):
        records = by_country[country]
        path = out / f"{country}.parquet"
        table = pa.table({c: [_row(r)[c] for r in records] for c in COLUMNS},
                         schema=pa.schema([(c, pa.string()) for c in COLUMNS]))
        pq.write_table(table, path, compression="zstd")
        files.append({"name": path.name, "country": country, "records": len(records),
                      "root": root([leaf(r) for r in records]), "sha256": _sha256(path)})

    counts = {s["id"]: len(records_by_source.get(s["id"], {})) for s in SOURCES}
    manifest = {
        "date": date,
        "schemaVersion": SCHEMA_VERSION,
        "records": len(merged),
        "recordsPerSource": counts,
        "inMoreThanOneSource": sum(1 for r in merged.values() if len(r["sources"]) > 1),
        "files": files,
        "snapshotRoot": root([bytes.fromhex(f["root"].split(":", 1)[1]) for f in files]),
        "sources": [{k: s[k] for k in ("id", "name", "licence", "attribution")} for s in SOURCES],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    notice = ["lei-commons snapshot " + date, "",
              "An independent open directory. Not affiliated with GLEIF, and does not issue LEIs.", ""]
    notice += [f"{s['name']} ({s['licence']}). {s['attribution']}".strip() for s in SOURCES]
    (out / "NOTICE.txt").write_text("\n".join(notice) + "\n", encoding="utf-8")
    return manifest
