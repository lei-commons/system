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
    # Licences marked to-be-verified: the owner verifies them before anything goes public.
    {"id": "br-rfb", "prefix": "br", "name": "Receita Federal do Brasil, dados abertos do CNPJ", "licence": "to-be-verified",
     "attribution": "Source: Receita Federal do Brasil, Cadastro Nacional da Pessoa Jurídica (open data)."},
    {"id": "us-ny-dos", "prefix": "us-ny", "name": "New York Department of State, active corporations (data.ny.gov)",
     "licence": "to-be-verified", "attribution": "Source: New York State Department of State, via data.ny.gov."},
    {"id": "sg-acra", "prefix": "sg", "name": "ACRA Information on Corporate Entities (data.gov.sg)",
     "licence": "SG-Open-Data-1.0", "attribution": "Contains information from ACRA Information on Corporate Entities accessed "
     "from data.gov.sg, made available under the Singapore Open Data Licence version 1.0."},
    {"id": "gleif", "prefix": "gleif", "name": "GLEIF LEI golden copy", "licence": "CC0-1.0", "attribution": ""},
]
SCALARS = ["directoryId", "lei", "legalName", "legalForm", "legalJurisdiction", "registrationAuthority",
           "registrationNumber", "legalStatus", "incorporationDate", "dissolutionDate", "recordMode", "registryUrl"]
NESTED = ["otherNames", "legalAddress", "headquartersAddress", "industryCodes", "labels"]
COLUMNS = SCALARS + NESTED + ["sources"]
RELATIONSHIP_FILE = "relationships.parquet"
RELATIONSHIP_COLUMNS = ["relationshipId", "startNode", "endNode", "startId", "endId", "relationshipType",
                        "relationshipStatus", "percentage", "periodStart", "periodEnd"]


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


# Registers spell the same status differently: GLEIF writes ACTIVE and INACTIVE,
# Companies House and the others write Active. One spelling everywhere downstream,
# so the database, search filters, facets and dashboards count them together
# (owner request 2026-11-11).
STATUS_SPELLINGS = {"ACTIVE": "Active", "INACTIVE": "Inactive"}


def normalise_status(value):
    return STATUS_SPELLINGS.get(value, value) if isinstance(value, str) else value


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


def _write_relationships(read, lei_to_id: dict, out: Path, shards: int) -> dict:
    """Write relationships.parquet, resolving each LEI to its directory ID where we hold it."""
    schema = pa.schema([(c, pa.string()) for c in RELATIONSHIP_COLUMNS])
    path = out / RELATIONSHIP_FILE
    leaves, count, resolved = [], 0, 0
    with pq.ParquetWriter(path, schema, compression="zstd") as writer:
        for n in range(shards):
            rows = []
            for key in sorted(shard := read(n)):
                rel = shard[key]
                row = {"relationshipId": key, "startId": lei_to_id.get(rel.get("startNode")),
                       "endId": lei_to_id.get(rel.get("endNode"))}
                for c in RELATIONSHIP_COLUMNS[1:]:
                    if c not in row:
                        value = rel.get(c)
                        row[c] = None if value is None else str(value)
                rows.append(row)
                leaves.append(leaf(row))
                resolved += bool(row["startId"] and row["endId"])
            if rows:
                writer.write_table(pa.table({c: [r[c] for r in rows] for c in RELATIONSHIP_COLUMNS}, schema=schema))
            count += len(rows)
    return {"name": RELATIONSHIP_FILE, "kind": "relationships", "records": count, "bothEndsInDirectory": resolved,
            "root": root(leaves), "sha256": _sha256(path)}


def build_sharded(readers: dict, out: Path, date: str, shards: int = 256, relationships=None, decision_log=None) -> dict:
    """Write one Parquet file per country and return the manifest, a shard at a time.

    ``readers`` maps a source ID to ``read(n)``, which returns that source's
    records in shard ``n``. A company has the same directory ID, and so the same
    shard, in every source, so merging shard by shard merges everything. Only
    one shard is in memory at once. Records are ordered by shard, then ID.
    """
    out.mkdir(parents=True, exist_ok=True)
    schema = pa.schema([(c, pa.string()) for c in COLUMNS])
    writers, leaves, per_country, lei_to_id = {}, defaultdict(list), defaultdict(int), {}
    counts = {s["id"]: 0 for s in SOURCES}
    total = shared = 0
    try:
        for n in range(shards):
            shard = {sid: readers[sid](n) for sid in readers}
            for sid, records in shard.items():
                counts[sid] += len(records)
            merged = merge(shard)
            by_country = defaultdict(list)
            for directory_id in sorted(merged):
                record = merged[directory_id]
                if record.get("legalStatus") in STATUS_SPELLINGS:  # before its Merkle leaf, so the root covers what is published
                    record["legalStatus"] = normalise_status(record["legalStatus"])
                by_country[country_of(record)].append(record)
                if record.get("lei"):
                    lei_to_id[record["lei"]] = directory_id
                shared += len(record["sources"]) > 1
            total += len(merged)
            for country, records in by_country.items():
                if country not in writers:
                    writers[country] = pq.ParquetWriter(out / f"{country}.parquet", schema, compression="zstd")
                rows = [_row(r) for r in records]
                writers[country].write_table(pa.table({c: [r[c] for r in rows] for c in COLUMNS}, schema=schema))
                leaves[country].extend(leaf(r) for r in records)
                per_country[country] += len(records)
    finally:
        for writer in writers.values():
            writer.close()

    files = []
    for country in sorted(writers):
        path = out / f"{country}.parquet"
        files.append({"name": path.name, "country": country, "records": per_country[country],
                      "root": root(leaves[country]), "sha256": _sha256(path)})
    if relationships is not None:
        files.append(_write_relationships(relationships, lei_to_id, out, shards))
    manifest = {
        "date": date,
        "schemaVersion": SCHEMA_VERSION,
        "records": total,
        "recordsPerSource": {k: v for k, v in counts.items() if v},  # sources with records only
        "inMoreThanOneSource": shared,
        "relationships": next((f["records"] for f in files if f.get("kind") == "relationships"), 0),
        "files": files,
        "snapshotRoot": root([bytes.fromhex(f["root"].split(":", 1)[1]) for f in files]),
        "sources": [{k: s[k] for k in ("id", "name", "licence", "attribution")} for s in SOURCES],
    }
    if decision_log:  # the seal of the decision log up to yesterday, anchored with the manifest
        manifest["decisionLog"] = decision_log
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    notice = ["lei-commons snapshot " + date, "",
              "An independent open directory. Not affiliated with GLEIF, and does not issue LEIs.", ""]
    notice += [f"{s['name']} ({s['licence']}). {s['attribution']}".strip() for s in SOURCES]
    (out / "NOTICE.txt").write_text("\n".join(notice) + "\n", encoding="utf-8")
    return manifest


def build(records_by_source: dict, out: Path, date: str) -> dict:
    """The same, from records already in memory: {source ID: {directory ID: record}}."""
    from .shards import shard_of

    by_shard = {sid: defaultdict(dict) for sid in records_by_source}
    for sid, records in records_by_source.items():
        for directory_id, record in records.items():
            by_shard[sid][shard_of(directory_id)][directory_id] = record
    return build_sharded({sid: (lambda n, b=by_shard[sid]: dict(b.get(n, {}))) for sid in by_shard}, out, date)
