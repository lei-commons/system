"""The pilot's candidates, from the published snapshot (the newest full snapshot, the latest day's anchor).

Two sets (config/web_claims.yaml): records with a published website, and records with none, a mix of
countries. Active records only, LEI holders first, then a stable pseudo-random order (the SHA-256 of the
directory ID), so a rerun picks the same records. The snapshot holds no people (they are dropped at ingestion).

For LEI holders, GLEIF's parent relationships (in the same snapshot) are the truth set the parent claims are
scored against.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile

WITH, WITHOUT = "with-website", "without-website"
DIRECT, ULTIMATE = "IS_DIRECTLY_CONSOLIDATED_BY", "IS_ULTIMATELY_CONSOLIDATED_BY"
COLUMNS = ["directoryId", "lei", "legalName", "registrationNumber", "statusCategory", "website", "country",
           "otherNames", "legalAddress"]
PART_COUNTRY = re.compile(r"^([A-Z]{2})\.")


class NotReady(Exception):
    """The snapshot can't give candidates yet (no snapshot, or its anchor predates the website column)."""


def _priority(row: dict) -> tuple:
    return (0 if row.get("lei") else 1, hashlib.sha256(row["directoryId"].encode()).hexdigest())


def _json(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def _other_names(value) -> list:
    out = []
    for item in _json(value) or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out[:3]


def _region(value) -> str:
    addr = _json(value)
    return str(addr.get("region") or "")[-2:].upper() if isinstance(addr, dict) else ""


def _keep(rows: list, limit: int) -> list:
    rows.sort(key=lambda r: r["_p"])
    del rows[limit:]
    return rows


def anchor_manifest(snapshots) -> dict:
    latest = snapshots.read_json("latest.json")
    if not latest:
        raise NotReady("no snapshot yet")
    manifest = snapshots.read_json(f"{latest['date']}/manifest.json")
    if not manifest or manifest.get("schemaVersion", 1) < 2:
        raise NotReady("the latest manifest isn't schema version 2")
    anchor = manifest if manifest.get("kind") == "full" else snapshots.read_json(f"{manifest['anchor']}/manifest.json")
    if not anchor:
        raise NotReady("the anchor's manifest is missing")
    if "website" not in ((anchor.get("columns") or {}).get("entities") or []):
        raise NotReady(f"the full snapshot of {anchor.get('date')} predates the website column")
    return anchor


def _country(entry: dict) -> str:
    if entry.get("country"):
        return entry["country"]
    m = PART_COUNTRY.match(entry.get("name", ""))
    return m.group(1) if m else ""


def _rows(snapshots, entry: dict, columns: list):
    """The rows of one part, read a batch at a time; the downloaded file is removed straight after."""
    import pyarrow.parquet as pq

    with tempfile.TemporaryDirectory(prefix="wc-") as work:
        local = os.path.join(work, "part.parquet")
        snapshots.download(entry["object"], local)
        pf = pq.ParquetFile(local)
        have = [c for c in columns if c in pf.schema_arrow.names]
        for batch in pf.iter_batches(columns=have, batch_size=65536):
            yield from batch.to_pylist()


def select(snapshots, s) -> dict:
    """{"date": anchor date, "records": [candidate, ...]} for the settings ``s``."""
    anchor = anchor_manifest(snapshots)
    entity_files = [f for f in anchor["files"] if f.get("kind") == "entities"]
    with_countries = set(s.with_website_countries)
    group_of = {c: g for g, cs in s.without_website_groups.items() for c in cs}
    per_group = s.without_website_count // max(1, len(s.without_website_groups))
    pools = {}  # (set, country) -> rows
    for entry in entity_files:
        country = _country(entry)
        if country not in with_countries and country not in group_of:
            continue
        for row in _rows(snapshots, entry, COLUMNS):
            if row.get("statusCategory") != s.status_category or not row.get("directoryId") or not row.get("legalName"):
                continue
            has_site = bool(row.get("website"))
            if has_site and country in with_countries:
                key, limit = (WITH, country), s.with_website_count
            elif not has_site and country in group_of:
                key, limit = (WITHOUT, country), per_group
            else:
                continue
            row["_p"] = _priority(row)
            pool = pools.setdefault(key, [])
            pool.append(row)
            if len(pool) > 2 * limit + 1000:
                _keep(pool, limit)
    chosen = []
    for country in s.with_website_countries:  # in order: the first country fills first
        room = s.with_website_count - len(chosen)
        chosen += [dict(r, set=WITH) for r in _keep(pools.get((WITH, country), []), room)] if room > 0 else []
    for group, countries in s.without_website_groups.items():
        lists = [_keep(pools.get((WITHOUT, c), []), per_group) for c in countries]
        taken, i = [], 0
        while len(taken) < per_group and any(i < len(lst) for lst in lists):
            taken += [lst[i] for lst in lists if i < len(lst)]
            i += 1
        chosen += [dict(r, set=WITHOUT, group=group) for r in taken[:per_group]]
    records = []
    for i, r in enumerate(chosen):
        records.append({
            "id": r["directoryId"], "set": r["set"], "group": r.get("group", r["country"]), "country": r["country"],
            "lei": r.get("lei") or None, "legalName": r["legalName"], "registrationNumber": r.get("registrationNumber"),
            "otherNames": _other_names(r.get("otherNames")), "region": _region(r.get("legalAddress")),
            "website": r.get("website") or None,
        })
    truth_ids = [rec["id"] for rec in records if rec["set"] == WITH]
    truth_ids.sort(key=lambda i: hashlib.sha256(("truth:" + i).encode()).hexdigest())
    sample = set(truth_ids[:s.discovery_truth_sample])
    for rec in records:
        rec["discoveryTruth"] = rec["id"] in sample
    parents(snapshots, anchor, records)
    return {"date": anchor["date"], "records": records}


def parents(snapshots, anchor: dict, records: list) -> None:
    """Adds GLEIF's direct and ultimate parents (legal name, register number, LEI) to LEI-holding candidates:
    the truth set for parent claims. Parents are LEI holders, so only GLEIF's parts are read for them."""
    by_lei = {r["lei"]: r for r in records if r.get("lei")}
    if not by_lei:
        return
    wanted = {}
    for entry in (f for f in anchor["files"] if f.get("kind") == "relationships"):
        for rel in _rows(snapshots, entry, ["startNode", "endNode", "relationshipType", "relationshipStatus"]):
            child = by_lei.get(rel.get("startNode"))
            if child is None or rel.get("relationshipType") not in (DIRECT, ULTIMATE):
                continue
            if (rel.get("relationshipStatus") or "ACTIVE").upper() != "ACTIVE":
                continue
            kind = "direct" if rel["relationshipType"] == DIRECT else "ultimate"
            child.setdefault("truthParents", {})[kind] = {"lei": rel.get("endNode")}
            wanted[rel.get("endNode")] = None
    if not wanted:
        return
    gleif_parts = [f for f in anchor["files"] if f.get("kind") == "entities" and "gleif" in f.get("name", "")]
    for entry in gleif_parts:
        for row in _rows(snapshots, entry, ["lei", "legalName", "registrationNumber", "country"]):
            if row.get("lei") in wanted:
                wanted[row["lei"]] = {"legalName": row.get("legalName"), "registrationNumber": row.get("registrationNumber"),
                                      "country": row.get("country")}
    for rec in records:
        for p in (rec.get("truthParents") or {}).values():
            p.update(wanted.get(p["lei"]) or {})
