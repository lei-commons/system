"""Builds the day's snapshot from the current records of every source (format v2, docs/snapshots-v2.md).

A company present in more than one source becomes one record. Register values
win for register fields (Article 4.3); GLEIF supplies the LEI. Every record says
which sources it came from (Article 3.3).

Each record goes to a part named for its country and its exact sources, split into
2, 4, 8... parts by shard when the group is large. Against the previous day's state,
the build also writes the day's changes (added, changed and deleted records), and the
new state for tomorrow. Whether the day is published as a full snapshot or as changes
only is decided by ``plan`` (every ``full_every_days``) and by the size of the changes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from lc_core.snapshot_format import (CHANGES_KIND, ENTITY_KIND, RELATIONSHIP_CHANGES_KIND, RELATIONSHIP_GROUP,
                                     RELATIONSHIP_KIND, SCHEMA_VERSION, SHARDS, days_between, group_of, part_name,
                                     parts_for)

from lc_core.attribution import record_url

from lc_core.status import UNKNOWN as UNKNOWN_STATUS

from .merkle import StreamingRoot, leaf, root

def sources(path=None) -> list:
    """The built sources, in merge order (registers before GLEIF; earlier sources win on conflicting fields),
    from config/sources.yaml, with what every record needs to credit them: [{id (what records carry), prefix
    (store), name, publisher, licence, licenceUrl, datasetUrl, recordUrl (a template, or None), attribution,
    citation, relationships}]."""
    from lc_core.sources import merge_order

    out = []
    for p in merge_order(path):
        licence = p["licence"] or "unstated"
        citation = " ".join(part for part in (
            f"{p['name']}: {p['dataset']}.", f"{p['url']}." if p.get("url") else "",
            f"Licence: {licence}" + (f" ({p['terms_url']})." if p.get("terms_url") else "."),
            p["attribution"] or "", "Retrieved and processed by lei-commons (records merged and normalised).") if part)
        out.append({"id": p["records_as"], "prefix": p["store"], "name": p["dataset"], "publisher": p["name"],
                    "licence": licence, "licenceUrl": p.get("terms_url"), "datasetUrl": p.get("url"),
                    "recordUrl": p.get("record_url"), "attribution": p["attribution"] or "", "citation": citation,
                    "relationships": p.get("relationships")})
    return out


SCALARS = ["directoryId", "lei", "legalName", "legalForm", "legalJurisdiction", "registrationAuthority",
           "registrationNumber", "legalStatus", "statusCategory", "incorporationDate", "dissolutionDate", "recordMode",
           "registryUrl"]
NESTED = ["otherNames", "legalAddress", "headquartersAddress", "industryCodes", "labels"]
COLUMNS = SCALARS + NESTED + ["sources", "country"]
RELATIONSHIP_COLUMNS = ["relationshipId", "startNode", "endNode", "startId", "endId", "relationshipType",
                        "relationshipStatus", "percentage", "periodStart", "periodEnd"]
ENTITY_SCHEMA = pa.schema([(c, pa.string()) for c in COLUMNS])
RELATIONSHIP_SCHEMA = pa.schema([(c, pa.string()) for c in RELATIONSHIP_COLUMNS])
CHANGE_SCHEMA = pa.schema([("op", pa.string())] + [(c, pa.string()) for c in COLUMNS])
RELATIONSHIP_CHANGE_SCHEMA = pa.schema([("op", pa.string())] + [(c, pa.string()) for c in RELATIONSHIP_COLUMNS])
STATE_SCHEMA = pa.schema([("shard", pa.int16()), ("id", pa.string()), ("h", pa.binary(16))])
STATE_FILES = {ENTITY_KIND: "entities.parquet", RELATIONSHIP_KIND: "relationships.parquet"}
STATS_TOP = {"byCountry": 30, "bySources": 10, "byStatus": 15, "unmappedStatus": 50}


def merge(records_by_source: dict, order: list = None, statuses=None, unmapped: Counter = None) -> dict:
    """{source id: {directory id: record}} to {directory id: record}, in merge order (``order``: source ids).

    Each merged record gets ``statusCategory``: its legalStatus mapped by the map of the source that
    supplied that legalStatus (``statuses``, an lc_core.status.StatusMap; config/status.yaml by default).
    Words the map doesn't list are counted in ``unmapped`` as "<source>: <word>"."""
    from lc_core import status as status_config

    merged, status_from = {}, {}
    order = order or [s["id"] for s in sources()]
    statuses = statuses or status_config.default()
    unknown = set(records_by_source) - set(order)
    if unknown:  # a source the registry doesn't list must never be merged in an arbitrary order
        raise ValueError(f"sources not in the registry: {sorted(unknown)}")
    for source in ({"id": sid} for sid in order):
        for directory_id, record in records_by_source.get(source["id"], {}).items():
            if "legalStatus" in record and directory_id not in status_from:
                status_from[directory_id] = source["id"]  # the source whose legalStatus the record keeps
            current = merged.get(directory_id)
            if current is None:
                merged[directory_id] = {**record, "sources": [source["id"]]}
                continue
            for field, value in record.items():
                current.setdefault(field, value)  # the earlier, register source keeps its value
            current["sources"].append(source["id"])
    for directory_id, record in merged.items():
        sid = status_from.get(directory_id)
        record["statusCategory"], mapped = statuses.category(sid, record.get("legalStatus"))
        if not mapped and unmapped is not None:
            unmapped[f"{sid}: {record['legalStatus']}"] += 1
    return merged


def country_of(record: dict) -> str:
    country = (record.get("legalJurisdiction") or "")[:2] or (record.get("legalAddress") or {}).get("country", "")
    return country.upper() if len(country) == 2 and country.isalpha() else "XX"


def entity_row(record: dict, by_id: dict = None) -> dict:
    """The published row: exactly the Parquet columns, all strings. A record's leaf is the hash of this.

    ``by_id`` (source id -> source, from ``sources()``) fills registryUrl, when the record has none, with
    where its first source shows it: every record carries a URL for its source."""
    row = {}
    for c in SCALARS:
        value = record.get(c)
        row[c] = None if value is None else str(value)
    if not row["registryUrl"] and by_id and record.get("sources"):
        row["registryUrl"] = record_url(record, by_id.get(record["sources"][0]) or {})
    # legalStatus is the register's own word, untouched (Article 4.3); statusCategory is set by merge
    # (config/status.yaml), and a record that never went through it has none to show: unknown.
    row["statusCategory"] = row["statusCategory"] or UNKNOWN_STATUS
    for c in NESTED:
        value = record.get(c)
        row[c] = json.dumps(value, sort_keys=True, ensure_ascii=False) if value not in (None, [], {}) else None
    row["sources"] = ",".join(record["sources"])
    row["country"] = country_of(record)
    return row


def relationship_row(key: str, rel: dict, lei_to_id: dict) -> dict:
    row = {"relationshipId": key, "startId": lei_to_id.get(rel.get("startNode")), "endId": lei_to_id.get(rel.get("endNode"))}
    for c in RELATIONSHIP_COLUMNS[1:]:
        if c not in row:
            value = rel.get(c)
            row[c] = None if value is None else str(value)
    return row


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _table(rows, schema):
    return pa.table({f.name: [r.get(f.name) for r in rows] for f in schema}, schema=schema)


def _top(counter: Counter, n=None) -> list:
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0] is None, kv[0] or ""))
    return [{"value": v, "records": c} for v, c in (ordered[:n] if n else ordered)]


class StateReader:
    """The previous day's state: (directory id -> 16-byte hash) for each shard, read a shard at a time.

    The state is written in shard order, so shards asked for in order are read in one pass over the
    file. A shard asked for out of order falls back to a filtered read of the whole file.
    """

    def __init__(self, path: Path):
        self.path = path
        pq.ParquetFile(path)  # unreadable: fail now, before anything is built from it
        import pyarrow.compute as pc

        column = pq.read_table(path, columns=["shard"]).column("shard")
        self._in_order = len(column) < 2 or pc.all(pc.greater_equal(column[1:], column[:-1])).as_py()
        self._batches = None  # the one pass: an iterator over record batches
        self._pending = None  # rows read past the shard last returned: (shards, ids, hashes)
        self._last = -1

    def _filtered(self, n: int) -> dict:
        table = pq.read_table(self.path, filters=[("shard", "=", n)], columns=["id", "h"])
        return dict(zip(table.column("id").to_pylist(), table.column("h").to_pylist()))

    def shard(self, n: int) -> dict:
        if not self._in_order or n <= self._last:
            return self._filtered(n)
        if self._batches is None:
            self._batches = pq.ParquetFile(self.path).iter_batches(columns=["shard", "id", "h"], batch_size=65_536)
        self._last = n
        out = {}
        while True:
            if self._pending is None:
                batch = next(self._batches, None)
                if batch is None:
                    return out
                self._pending = (batch.column(0).to_pylist(), batch.column(1).to_pylist(), batch.column(2).to_pylist())
            shards, ids, hashes = self._pending
            i = 0
            while i < len(shards) and shards[i] < n:
                i += 1
            j = i
            while j < len(shards) and shards[j] == n:
                j += 1
            out.update(zip(ids[i:j], hashes[i:j]))
            if j < len(shards):  # the batch goes past this shard: keep the rest for the next one
                self._pending = (shards[j:], ids[j:], hashes[j:])
                return out
            self._pending = None


class _ChangeWriter:
    """Writes changes.000.parquet, changes.001.parquet, ... with at most ``max_rows`` rows each."""

    def __init__(self, directory: Path, stem: str, kind: str, schema, max_rows: int):
        self.directory, self.stem, self.kind, self.schema, self.max_rows = directory, stem, kind, schema, max_rows
        self.files, self._writer, self._rows, self._root = [], None, 0, None

    def _open(self):
        name = f"{self.stem}.{len(self.files):03d}.parquet"
        self._writer = pq.ParquetWriter(self.directory / name, self.schema, compression="zstd")
        self.files.append({"name": name, "kind": self.kind, "records": 0})
        self._rows, self._root = 0, StreamingRoot()

    def _close(self):
        if self._writer:
            self._writer.close()
            entry = self.files[-1]
            entry.update(records=self._rows, root=self._root.hexdigest())
            self._writer = None

    def write(self, rows: list):
        while rows:
            if self._writer is None or self._rows >= self.max_rows:
                self._close()
                self._open()
            take = rows[:self.max_rows - self._rows]
            rows = rows[len(take):]
            self._writer.write_table(_table(take, self.schema))
            for r in take:
                self._root.add(leaf(r))
            self._rows += len(take)

    def finish(self) -> list:
        self._close()
        for entry in self.files:
            path = self.directory / entry["name"]
            entry.update(sha256=sha256_file(path), bytes=path.stat().st_size)
        return self.files


class _Parts:
    """The parts of one kind (entities or relationships): a writer, a streaming root and a count per part."""

    def __init__(self, directory: Path, schema):
        self.directory, self.schema = directory, schema
        self.writers, self.roots, self.records, self.info = {}, {}, Counter(), {}

    def add(self, name: str, rows: list, leaves: list, info: dict):
        if name not in self.writers:
            self.writers[name] = pq.ParquetWriter(self.directory / name, self.schema, compression="zstd")
            self.roots[name] = StreamingRoot()
            self.info[name] = info
        self.writers[name].write_table(_table(rows, self.schema))
        for h in leaves:
            self.roots[name].add(h)
        self.records[name] += len(rows)

    def close(self):
        for writer in self.writers.values():
            writer.close()

    def entries(self) -> list:
        return [{"name": name, **self.info[name], "records": self.records[name], "root": self.roots[name].hexdigest()}
                for name in sorted(self.writers)]


def plan(date: str, previous: dict | None, settings, force_full: str = None, shards: int = SHARDS) -> dict:
    """Decide the day: full or changes, its base and the part layout. ``previous`` is the last published v2 manifest."""
    target = settings.part_target_records
    if previous is None:
        return {"kind": "full", "why": force_full or "no previous state", "base": None, "layout": {}}
    if (previous.get("layout") or {}).get("shards", SHARDS) != shards:
        force_full = force_full or "the shard count changed"
    base = {"date": previous["date"], "snapshotRoot": previous["snapshotRoot"]}
    since = days_between(previous["anchor"], date)
    layout = dict((previous.get("layout") or {}).get("groups") or {})
    if force_full or since >= settings.full_every_days:
        counts = Counter()
        for p in previous.get("parts", []):
            counts[p["group"]] += p["records"]
        return {"kind": "full", "why": force_full or f"{since} days since the last full snapshot", "base": base,
                "layout": {g: parts_for(c, target) for g, c in sorted(counts.items())}}
    return {"kind": "changes", "why": f"{since} days since the last full snapshot", "base": base, "layout": layout}


def _read_ahead(readers: dict, shards: int, depth: int):
    """Yield (n, {source id: records}) for each shard in order, reading up to ``depth`` shards ahead.

    Reading a shard is mostly waiting for Cloud Storage, so the next shards download in threads
    while this one is built. depth 0 reads each shard only when it is needed.
    """
    if depth <= 0:
        for n in range(shards):
            yield n, {sid: readers[sid](n) for sid in readers}
        return
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(32, (depth + 1) * max(1, len(readers)))) as pool:
        queued, upcoming = deque(), iter(range(shards))

        def queue_next():
            n = next(upcoming, None)
            if n is not None:
                queued.append((n, {sid: pool.submit(readers[sid], n) for sid in readers}))

        for _ in range(depth + 1):
            queue_next()
        while queued:
            n, pending = queued.popleft()
            shard = {sid: f.result() for sid, f in pending.items()}
            queue_next()
            yield n, shard


def build_sharded(readers: dict, out: Path, date: str, *, settings, previous: dict = None, state: dict = None,
                  relationships=None, decision_log=None, shards: int = SHARDS, force_full: str = None,
                  timings: dict = None) -> dict:
    """Build the day into ``out`` and return its manifest.

    ``readers`` maps a source id to ``read(n)``, its records in shard n. ``previous`` is the last
    published v2 manifest and ``state`` its {kind: StateReader}; with neither, the day is a full
    snapshot with no base. ``out`` gets: parts/ (every part), day/ (the change files),
    state/ (tomorrow's state), manifest.json and NOTICE.txt.
    """
    if (previous is None) != (state is None):
        raise ValueError("previous and state go together")
    decided = plan(date, previous, settings, force_full, shards)
    layout = decided["layout"]
    for sub in ("parts", "day", "state"):
        shutil.rmtree(out / sub, ignore_errors=True)
        (out / sub).mkdir(parents=True, exist_ok=True)
    parts = {ENTITY_KIND: _Parts(out / "parts", ENTITY_SCHEMA), RELATIONSHIP_KIND: _Parts(out / "parts", RELATIONSHIP_SCHEMA)}
    changes = {ENTITY_KIND: _ChangeWriter(out / "day", "changes", CHANGES_KIND, CHANGE_SCHEMA, settings.change_file_max_rows),
               RELATIONSHIP_KIND: _ChangeWriter(out / "day", "relationship-changes", RELATIONSHIP_CHANGES_KIND,
                                                RELATIONSHIP_CHANGE_SCHEMA, settings.change_file_max_rows)}
    state_writers = {kind: pq.ParquetWriter(out / "state" / name, STATE_SCHEMA, compression="zstd")
                     for kind, name in STATE_FILES.items()}
    counted = Counter()
    registered = sources()
    order = [s["id"] for s in registered]
    by_id = {s["id"]: s for s in registered}
    per_source = {s["id"]: 0 for s in registered}
    stats = {"byCountry": Counter(), "bySources": Counter(), "byStatus": Counter(), "byStatusCategory": Counter(),
             "unmappedStatus": Counter(), "relationshipTypes": Counter()}
    lei_to_id = {}
    total = shared = resolved = 0

    def diff(kind, n, rows, key, leaves):
        """This shard's changes against the previous state, and its new state."""
        old = state[kind].shard(n) if state else None
        out_rows, ids, hashes = [], [], []
        for row, h in zip(rows, leaves):
            rid = row[key]
            ids.append(rid)
            hashes.append(h[:16])
            if old is None:
                continue
            before = old.pop(rid, None)
            if before is None:
                out_rows.append({"op": "add", **row})
                counted[(kind, "add")] += 1
            elif before != h[:16]:
                out_rows.append({"op": "change", **row})
                counted[(kind, "change")] += 1
        for rid in (old or {}):
            out_rows.append({"op": "delete", key: rid})
            counted[(kind, "delete")] += 1
        if out_rows:
            out_rows.sort(key=lambda r: r[key])
            changes[kind].write(out_rows)
        if ids:
            state_writers[kind].write_table(pa.table({"shard": [n] * len(ids), "id": ids, "h": hashes}, schema=STATE_SCHEMA))

    import time

    clock = time.monotonic
    spent = Counter()  # seconds per stage, for the job's timings event (never in the manifest)
    depth = getattr(settings, "read_ahead_shards", 0)
    try:
        shards_in = _read_ahead(readers, shards, depth)
        while True:
            mark = clock()
            got = next(shards_in, None)
            spent["read_s"] += clock() - mark
            if got is None:
                break
            n, shard = got
            mark = clock()
            for sid, records in shard.items():
                per_source[sid] += len(records)
            merged = merge(shard, order, unmapped=stats["unmappedStatus"])
            rows = [entity_row(merged[d], by_id) for d in sorted(merged)]
            del shard, merged
            spent["rows_s"] += clock() - mark
            mark = clock()
            leaves = [leaf(r) for r in rows]
            spent["leaf_s"] += clock() - mark
            mark = clock()
            part_rows, part_leaves, part_info = defaultdict(list), defaultdict(list), {}
            for row, h in zip(rows, leaves):
                if row["lei"]:
                    lei_to_id[row["lei"]] = row["directoryId"]
                shared += "," in row["sources"]
                group = group_of(row["country"], row["sources"])
                of = layout.get(group, 1)
                name = part_name(group, n % of, of)
                part_rows[name].append(row)
                part_leaves[name].append(h)
                part_info.setdefault(name, {"kind": ENTITY_KIND, "group": group, "country": row["country"],
                                            "sources": row["sources"].split(","), "part": n % of, "of": of})
                stats["byCountry"][row["country"]] += 1
                stats["bySources"][row["sources"]] += 1
                stats["byStatus"][row["legalStatus"]] += 1
                stats["byStatusCategory"][row["statusCategory"]] += 1
            for name in sorted(part_rows):
                parts[ENTITY_KIND].add(name, part_rows[name], part_leaves[name], part_info[name])
            total += len(rows)
            spent["parts_s"] += clock() - mark
            mark = clock()
            diff(ENTITY_KIND, n, rows, "directoryId", leaves)
            spent["diff_s"] += clock() - mark
        rel_total = 0
        mark = clock()
        if relationships is not None:
            of = layout.get(RELATIONSHIP_GROUP, 1)
            for n, got in _read_ahead({"r": relationships}, shards, depth):
                records = got["r"]
                rows = [relationship_row(k, records[k], lei_to_id) for k in sorted(records)]
                leaves = [leaf(r) for r in rows]
                if rows:
                    parts[RELATIONSHIP_KIND].add(part_name(RELATIONSHIP_GROUP, n % of, of), rows, leaves,
                                                 {"kind": RELATIONSHIP_KIND, "group": RELATIONSHIP_GROUP, "part": n % of, "of": of})
                for r in rows:
                    resolved += bool(r["startId"] and r["endId"])
                    stats["relationshipTypes"][r["relationshipType"]] += 1
                rel_total += len(rows)
                diff(RELATIONSHIP_KIND, n, rows, "relationshipId", leaves)
        elif state:  # no relationships today: every one from yesterday is deleted
            for n in range(shards):
                diff(RELATIONSHIP_KIND, n, [], "relationshipId", [])
        spent["relationships_s"] += clock() - mark
    finally:
        for p in parts.values():
            p.close()
        for w in state_writers.values():
            w.close()
    mark = clock()
    change_files = changes[ENTITY_KIND].finish() + changes[RELATIONSHIP_KIND].finish()
    part_entries = parts[ENTITY_KIND].entries() + parts[RELATIONSHIP_KIND].entries()
    change_total = sum(counted.values())
    kind, why = decided["kind"], decided["why"]
    if kind == "changes" and change_total > settings.full_when_changes_exceed * max(1, total + rel_total):
        kind, why = "full", f"{change_total} changes exceed {settings.full_when_changes_exceed:.0%} of the records"
    files = []
    if kind == "full":
        for p in part_entries:
            path = out / "parts" / p["name"]
            digest = sha256_file(path)
            p.update(sha256=digest, bytes=path.stat().st_size, object=f"parts/{digest.split(':', 1)[1]}.parquet")
            files.append({k: p[k] for k in ("name", "kind", "records", "root", "sha256", "bytes", "object")}
                         | ({"country": p["country"]} if "country" in p else {}))
    for c in change_files:
        c["object"] = f"{date}/{c['name']}"
        files.append(dict(c))
    if kind != "full":  # only the changes are published today; the parts' roots stay in the manifest
        for p in part_entries:
            (out / "parts" / p["name"]).unlink()
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "date": date,
        "kind": kind,
        "why": why,
        "base": decided["base"],
        "anchor": date if kind == "full" else previous["anchor"],
        "records": total,
        "recordsPerSource": {k: v for k, v in per_source.items() if v},
        "inMoreThanOneSource": shared,
        "relationships": rel_total,
        "relationshipsBothEndsInDirectory": resolved,
        "snapshotRoot": root([bytes.fromhex(p["root"].split(":", 1)[1]) for p in part_entries]),
        "parts": part_entries,
        "changes": {"files": [c["name"] for c in change_files], "added": counted[(ENTITY_KIND, "add")],
                    "changed": counted[(ENTITY_KIND, "change")], "deleted": counted[(ENTITY_KIND, "delete")],
                    "relationshipsAdded": counted[(RELATIONSHIP_KIND, "add")],
                    "relationshipsChanged": counted[(RELATIONSHIP_KIND, "change")],
                    "relationshipsDeleted": counted[(RELATIONSHIP_KIND, "delete")]},
        "files": files,
        "layout": {"targetRecords": settings.part_target_records, "shards": shards,
                   "groups": {p["group"]: p["of"] for p in part_entries}},
        "stats": {k: _top(v, STATS_TOP.get(k)) for k, v in stats.items()},
        "columns": {ENTITY_KIND: COLUMNS, RELATIONSHIP_KIND: RELATIONSHIP_COLUMNS},
        "settings": {"fullEveryDays": settings.full_every_days, "fullWhenChangesExceed": settings.full_when_changes_exceed},
        "sources": [{k: s[k] for k in ("id", "name", "publisher", "licence", "licenceUrl", "datasetUrl", "recordUrl",
                                        "attribution", "citation")} for s in registered if per_source[s["id"]]],
    }
    if decision_log:  # the seal of the decision log up to yesterday, anchored with the manifest
        manifest["decisionLog"] = decision_log
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    notice = ["lei-commons snapshot " + date, "",
              "An independent open directory. Not affiliated with GLEIF, and does not issue LEIs.", "",
              ("A full snapshot." if kind == "full" else
               f"Changes since {decided['base']['date']}. The whole state is the full snapshot of {manifest['anchor']} "
               "plus each day's changes since; python -m lc_publish.rebuild rebuilds and checks it."), ""]
    notice += [f"{s['name']} ({s['licence']}). {s['attribution']}".strip() for s in registered if per_source[s["id"]]]
    (out / "NOTICE.txt").write_text("\n".join(notice) + "\n", encoding="utf-8")
    (out / "state" / "state.json").write_text(json.dumps(
        {"date": date, "snapshotRoot": manifest["snapshotRoot"], "schemaVersion": SCHEMA_VERSION,
         "files": {name: sha256_file(out / "state" / name) for name in STATE_FILES.values()}}, indent=2) + "\n")
    spent["finish_s"] += clock() - mark
    if timings is not None:
        timings.update({k: round(v) for k, v in spent.items()})
    return manifest


def build(records_by_source: dict, out: Path, date: str, *, settings, previous=None, state=None, relationships=None,
          shards: int = SHARDS) -> dict:
    """The same, from records already in memory: {source ID: {directory ID: record}}."""
    from .shards import shard_of

    by_shard = {sid: defaultdict(dict) for sid in records_by_source}
    for sid, records in records_by_source.items():
        for directory_id, record in records.items():
            by_shard[sid][shard_of(directory_id) % shards][directory_id] = record
    rel = None
    if relationships is not None:
        rel_shards = defaultdict(dict)
        for key, value in relationships.items():
            rel_shards[shard_of(key) % shards][key] = value
        rel = lambda n: dict(rel_shards.get(n, {}))  # noqa: E731
    return build_sharded({sid: (lambda n, b=by_shard[sid]: dict(b.get(n, {}))) for sid in by_shard}, out, date,
                         settings=settings, previous=previous, state=state, relationships=rel, shards=shards)

