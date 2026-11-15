"""Loads a snapshot into Postgres: in full (blue/green), or incrementally (docs/snapshots-v2.md).

A full load builds a fresh schema, fills it with COPY (from the full snapshot's parts,
then each later day's changes), checks its counts, indexes it, and only then swaps it in
under the name ``live`` with one atomic rename. An incremental load applies each day's
changes to ``live`` in one transaction, checking that every delete and insert touches
exactly the rows expected and that the final counts match the manifest. Either way,
readers never see a half-loaded directory, and a failed load leaves the previous one serving.
"""

from __future__ import annotations

import json
import re
import secrets
import time

import pyarrow.parquet as pq
from psycopg import sql

LIVE = "live"
# Bump when the schema or indexes change, so the next run reloads the same snapshot.
# 5: snapshot format v2 (country from the parts, stats from the manifest, incremental loads).
LOAD_VERSION = 5
ENTITY_COLUMNS = [  # (parquet column, table column, kind)
    ("directoryId", "directory_id", "text"), ("lei", "lei", "text"), ("legalName", "legal_name", "text"),
    ("legalForm", "legal_form", "text"), ("legalJurisdiction", "legal_jurisdiction", "text"),
    ("registrationAuthority", "registration_authority", "text"), ("registrationNumber", "registration_number", "text"),
    ("legalStatus", "legal_status", "text"), ("incorporationDate", "incorporation_date", "text"),
    ("dissolutionDate", "dissolution_date", "text"), ("recordMode", "record_mode", "text"),
    ("registryUrl", "registry_url", "text"), ("otherNames", "other_names", "json"), ("legalAddress", "legal_address", "json"),
    ("headquartersAddress", "headquarters_address", "json"), ("industryCodes", "industry_codes", "json"),
    ("labels", "labels", "json"), ("sources", "sources", "list"),
]
RELATIONSHIP_COLUMNS = [("relationshipId", "relationship_id"), ("startNode", "start_node"), ("endNode", "end_node"),
                        ("startId", "start_id"), ("endId", "end_id"), ("relationshipType", "relationship_type"),
                        ("relationshipStatus", "relationship_status"), ("percentage", "percentage"),
                        ("periodStart", "period_start"), ("periodEnd", "period_end")]
COUNTRY = re.compile(r"([A-Z]{2})\.parquet$")

TABLES = """
CREATE TABLE {s}.entities (
  directory_id text PRIMARY KEY, lei text, legal_name text NOT NULL, legal_form text, legal_jurisdiction text,
  registration_authority text, registration_number text, legal_status text, incorporation_date text,
  dissolution_date text, record_mode text, registry_url text, other_names jsonb, legal_address jsonb,
  headquarters_address jsonb, industry_codes jsonb, labels jsonb, sources text[] NOT NULL, country text NOT NULL,
  names_text text NOT NULL, filter_terms text[] NOT NULL,
  -- 'simple' keeps digits and doesn't stem, so "3M" and "APPLE 12" stay searchable. The filter
  -- terms ride in the same index, so "gmbh" in GB is one index lookup, not a scan of every GmbH.
  search tsvector GENERATED ALWAYS AS (to_tsvector('simple', names_text) || array_to_tsvector(filter_terms)) STORED
);
CREATE TABLE {s}.relationships (
  relationship_id text PRIMARY KEY, start_node text, end_node text, start_id text, end_id text,
  relationship_type text, relationship_status text, percentage text, period_start text, period_end text
);
CREATE TABLE {s}.meta (date text, snapshot_root text, records bigint, relationships bigint, stats jsonb,
  loaded_at timestamptz DEFAULT now(), load_version int, mode text, full_loaded_at timestamptz DEFAULT now());
"""
INDEXES = [
    "CREATE INDEX ON {s}.entities USING gin (search)",
    # Byte order, so a prefix search walks the index in order and stops after a few rows.
    'CREATE INDEX ON {s}.entities ((lower(legal_name) COLLATE "C"))',
    "CREATE INDEX ON {s}.entities (lei)",
    "CREATE INDEX ON {s}.entities (registration_number)",
    # In display order, so the first page of a group with 100,000 members reads only that page.
    "CREATE INDEX ON {s}.relationships (start_id, relationship_type, relationship_id)",
    "CREATE INDEX ON {s}.relationships (end_id, relationship_type, relationship_id)",
]


FILTER_KINDS = {"country": "c", "status": "s", "source": "src"}


def filter_term(kind: str, value: str) -> str:
    """A filter as a search term. The '=' keeps it apart from words: the parser
    splits on it, so no word in a name or a query can ever produce one.

    Status and source keep their case. Registers spell the same status
    differently ("Active" at Companies House, "ACTIVE" at GLEIF), and the site
    filters on the exact value. A case-folded term would match both, and on
    most records a term narrows nothing (load test 6, 2026-11-08)."""
    value = (value or "none").strip()
    if kind == "country":
        value = value.lower()
    return f"{FILTER_KINDS[kind]}=" + re.sub(r"[^A-Za-z0-9-]+", "_", value)


def _filter_terms(country, status, sources):
    return [filter_term("country", country), filter_term("status", status)] + [filter_term("source", s) for s in sources]


def _names_text(name, other_names_json):
    others = json.loads(other_names_json) if other_names_json else []
    return " | ".join([name or ""] + [o for o in others if isinstance(o, str)])


class LoadMismatch(Exception):
    """The database doesn't hold what the manifest says it should; nothing is committed."""


ENTITY_TABLE_COLUMNS = [c for _, c, _ in ENTITY_COLUMNS] + ["country", "names_text", "filter_terms"]
RELATIONSHIP_TABLE_COLUMNS = [c for _, c in RELATIONSHIP_COLUMNS]


def _entity_rows(path, with_op=False):
    """Table rows from a parts file (or a changes file, with its op first). Country comes from the
    file's ``country`` column (v2), or from its name (v1 files are named after their country)."""
    match = COUNTRY.search(str(path))
    named = match.group(1) if match else "XX"
    for batch in pq.ParquetFile(path).iter_batches(batch_size=20_000):
        data = batch.to_pydict()
        has_country = "country" in data
        for i in range(batch.num_rows):
            row = []
            for src, _, kind in ENTITY_COLUMNS:
                value = data[src][i] if src in data else None
                if kind == "list":
                    value = [v for v in (value or "").split(",") if v]
                row.append(value)
            if with_op and data["op"][i] == "delete":
                yield ["delete"] + row + [None, None, None]
                continue
            sources = row[-1]  # sources is the last entity column
            country = (data["country"][i] if has_country else named) or "XX"
            status = data["legalStatus"][i] if "legalStatus" in data else None
            row += [country, _names_text(data["legalName"][i], data["otherNames"][i] if "otherNames" in data else None),
                    _filter_terms(country, status, sources)]
            yield ([data["op"][i]] + row) if with_op else row


def _relationship_rows(path, with_op=False):
    for batch in pq.ParquetFile(path).iter_batches(batch_size=50_000):
        data = batch.to_pydict()
        for i in range(batch.num_rows):
            row = [data[src][i] if src in data else None for src, _ in RELATIONSHIP_COLUMNS]
            yield ([data["op"][i]] + row) if with_op else row


def _copy_into(cur, table, columns, rows):
    copy = sql.SQL("COPY {} ({}) FROM STDIN").format(table, sql.SQL(", ").join(map(sql.Identifier, columns)))
    count = 0
    with cur.copy(copy) as out:
        for row in rows:
            out.write_row(row)
            count += 1
    return count


def _copy_entities(cur, schema, files):
    table = sql.SQL("{}.entities").format(sql.Identifier(schema))
    return _copy_into(cur, table, ENTITY_TABLE_COLUMNS, (row for path in files for row in _entity_rows(path)))


def _copy_relationships(cur, schema, files):
    table = sql.SQL("{}.relationships").format(sql.Identifier(schema))
    return _copy_into(cur, table, RELATIONSHIP_TABLE_COLUMNS, (row for path in files for row in _relationship_rows(path)))


STAGES = {
    "entities": ("lc_stage_entities", "directory_id", ENTITY_TABLE_COLUMNS, _entity_rows,
                 ("added", "changed", "deleted")),
    "relationships": ("lc_stage_relationships", "relationship_id", RELATIONSHIP_TABLE_COLUMNS, _relationship_rows,
                      ("relationshipsAdded", "relationshipsChanged", "relationshipsDeleted")),
}
STAGE_TYPES = {"sources": "text[]", "filter_terms": "text[]", "other_names": "jsonb", "legal_address": "jsonb",
               "headquarters_address": "jsonb", "industry_codes": "jsonb", "labels": "jsonb"}


def _apply(cur, schema, kind, files, expected: dict, day: str) -> dict:
    """Apply one day's changes of one kind to ``schema``'s table, checking every count. Returns the counts."""
    stage, key, columns, rows, names = STAGES[kind]
    cur.execute(sql.SQL("CREATE TEMP TABLE IF NOT EXISTS {} (op text, {}) ON COMMIT DROP").format(
        sql.Identifier(stage), sql.SQL(", ").join(sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(STAGE_TYPES.get(c, "text")))
                                                  for c in columns)))
    cur.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(stage)))
    _copy_into(cur, sql.Identifier(stage), ["op"] + columns, (r for path in files for r in rows(path, with_op=True)))
    cur.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(stage)))  # a small table: the planner then uses the key's index
    cur.execute(sql.SQL("SELECT op, count(*), count(DISTINCT {}) FROM {} GROUP BY op").format(sql.Identifier(key), sql.Identifier(stage)))
    staged = {op: (n, distinct) for op, n, distinct in cur.fetchall()}
    unknown = set(staged) - {"add", "change", "delete"}
    if unknown:
        raise LoadMismatch(f"{day}: unknown changes {sorted(unknown)}")
    got = {op: staged.get(op, (0, 0))[0] for op in ("add", "change", "delete")}
    want = {op: int(expected.get(name, 0)) for op, name in zip(("add", "change", "delete"), names)}
    if got != want:
        raise LoadMismatch(f"{day}: {kind} changes in the files {got} aren't the manifest's {want}")
    cur.execute(sql.SQL("SELECT count(DISTINCT {}) FROM {}").format(sql.Identifier(key), sql.Identifier(stage)))
    if cur.fetchone()[0] != sum(got.values()):
        raise LoadMismatch(f"{day}: a {kind} record changes twice in one day")
    target = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(kind))
    cur.execute(sql.SQL("DELETE FROM {t} USING {st} WHERE {st}.op IN ('change', 'delete') AND {t}.{k} = {st}.{k}").format(
        t=target, st=sql.Identifier(stage), k=sql.Identifier(key)))
    if cur.rowcount != got["change"] + got["delete"]:
        raise LoadMismatch(f"{day}: {cur.rowcount} {kind} rows removed, expected {got['change'] + got['delete']}")
    cols = sql.SQL(", ").join(map(sql.Identifier, columns))
    cur.execute(sql.SQL("INSERT INTO {t} ({c}) SELECT {c} FROM {st} WHERE op IN ('add', 'change')").format(
        t=target, c=cols, st=sql.Identifier(stage)))
    if cur.rowcount != got["add"] + got["change"]:
        raise LoadMismatch(f"{day}: {cur.rowcount} {kind} rows inserted, expected {got['add'] + got['change']}")
    return got


def apply_steps(cur, schema, steps) -> dict:
    """Apply each day's changes in order. ``steps`` yields (date, entity files, relationship files, manifest changes)."""
    totals = {"days": 0, "added": 0, "changed": 0, "deleted": 0}
    for day, entity_files, relationship_files, expected in steps:
        got = _apply(cur, schema, "entities", entity_files, expected, day)
        _apply(cur, schema, "relationships", relationship_files, expected, day)
        totals["days"] += 1
        for op, name in (("add", "added"), ("change", "changed"), ("delete", "deleted")):
            totals[name] += got[op]
    return totals


def _check_counts(cur, schema, meta):
    for table, key in (("entities", "records"), ("relationships", "relationships")):
        if not isinstance(meta.get(key), int) or isinstance(meta.get(key), bool):
            continue  # only a manifest's counts are checked
        cur.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table)))
        found = cur.fetchone()[0]
        if found != meta[key]:
            raise LoadMismatch(f"{found} {table} after loading, but the manifest says {meta[key]}")


def live_meta(conn):
    """{date, snapshotRoot, loadVersion} of what the database serves, or None."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = 'meta'", [LIVE])
        if not cur.fetchone():
            return None
        cur.execute(sql.SQL("SELECT date, snapshot_root, (to_jsonb(m) ->> 'load_version')::int FROM {}.meta m")
                    .format(sql.Identifier(LIVE)))
        row = cur.fetchone()
    return {"date": row[0], "snapshotRoot": row[1], "loadVersion": row[2]} if row else None


def load_incremental(conn, steps, meta: dict, vacuum_after=True) -> dict:
    """Apply the days' changes to the live directory in one transaction, then check and commit.

    Raises LoadMismatch (or a database error) with nothing committed."""
    timer = time.monotonic()
    try:
        with conn.cursor() as cur:
            totals = apply_steps(cur, LIVE, steps)
            applied = time.monotonic()
            _check_counts(cur, LIVE, meta)
            cur.execute(sql.SQL("UPDATE {}.meta SET date = %s, snapshot_root = %s, records = %s, relationships = %s, "
                                "stats = %s, load_version = %s, loaded_at = now(), mode = 'incremental'")
                        .format(sql.Identifier(LIVE)),
                        [meta["date"], meta["snapshotRoot"], meta["records"], meta["relationships"],
                         json.dumps(meta["stats"]), LOAD_VERSION])
            if cur.rowcount != 1:
                raise LoadMismatch("the live directory has no meta row")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    seconds = {"apply_s": round(applied - timer), "check_s": round(time.monotonic() - applied)}
    if vacuum_after:
        started = time.monotonic()
        vacuum(conn, LIVE)
        seconds["vacuum_s"] = round(time.monotonic() - started)
    return {"records": meta["records"], "relationships": meta["relationships"], **totals, **seconds}


def _stats(cur, schema):
    def top(expr, n):
        cur.execute(sql.SQL("SELECT {e} AS value, count(*) FROM {s}.entities GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT %s")
                    .format(e=sql.SQL(expr), s=sql.Identifier(schema)), [n])
        return [{"value": v, "records": n} for v, n in cur.fetchall()]

    cur.execute(sql.SQL("SELECT relationship_type, count(*) FROM {}.relationships GROUP BY 1 ORDER BY 2 DESC").format(sql.Identifier(schema)))
    rel = [{"value": v, "records": n} for v, n in cur.fetchall()]
    return {"byCountry": top("country", 30), "bySources": top("array_to_string(sources, ',')", 10),
            "byStatus": top("legal_status", 15), "relationshipTypes": rel}


def vacuum(conn, schema: str):
    """VACUUM ANALYZE, not just ANALYZE: it also sets the visibility map, so counts and
    lookups can be answered from the indexes without reading the table."""
    conn.commit()
    conn.autocommit = True
    try:
        for table in ("entities", "relationships"):
            conn.execute(sql.SQL("VACUUM (ANALYZE) {}.{}").format(sql.Identifier(schema), sql.Identifier(table)))
    finally:
        conn.autocommit = False


def already_loaded(conn, snapshot_root: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = 'meta'", [LIVE])
        if not cur.fetchone():
            return False
        # to_jsonb, so a meta table from before load_version existed reads as an older version
        cur.execute(sql.SQL("SELECT snapshot_root, (to_jsonb(m) ->> 'load_version')::int FROM {}.meta m").format(sql.Identifier(LIVE)))
        row = cur.fetchone()
        return bool(row) and row[0] == snapshot_root and row[1] == LOAD_VERSION


def load(conn, entity_files, relationship_files, meta: dict, readers=(), steps=()) -> dict:
    """Load a snapshot and swap it in. ``readers`` are database roles granted read access.

    ``steps`` are later days' changes to apply before the swap (a v2 full snapshot plus its
    changes). With ``meta["records"]`` the counts are checked before the swap, and with
    ``meta["stats"]`` the dashboards' counts come from the manifest instead of a recount."""
    schema = f"load_{secrets.token_hex(4)}"
    s = sql.Identifier(schema)
    timer, seconds = time.monotonic(), {}

    def lap(name):  # how long each phase took, to know what to speed up as the directory grows
        nonlocal timer
        now = time.monotonic()
        seconds[f"{name}_s"] = round(now - timer)
        timer = now

    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(s))
        cur.execute(sql.SQL(TABLES).format(s=s))
        records = _copy_entities(cur, schema, entity_files)  # files may be fetched as they are read
        lap("copy_entities")
        relationships = _copy_relationships(cur, schema, relationship_files)
        lap("copy_relationships")
        applied = apply_steps(cur, schema, steps)
        if applied["days"]:
            records = records + applied["added"] - applied["deleted"]
            lap("apply_changes")
        _check_counts(cur, schema, meta)
        if isinstance(meta.get("records"), int) and isinstance(meta.get("relationships"), int):
            records, relationships = meta["records"], meta["relationships"]
        for statement in INDEXES:
            cur.execute(sql.SQL(statement).format(s=s))
        lap("indexes")
    conn.commit()
    vacuum(conn, schema)
    lap("vacuum")
    with conn.cursor() as cur:
        stats = meta.get("stats") or _stats(cur, schema)
        lap("stats")
        cur.execute(sql.SQL("INSERT INTO {}.meta (date, snapshot_root, records, relationships, stats, load_version, mode) "
                            "VALUES (%s, %s, %s, %s, %s, %s, 'full')")
                    .format(s), [meta["date"], meta["snapshotRoot"], records, relationships, json.dumps(stats), LOAD_VERSION])
        for reader in readers:
            role = sql.Identifier(reader)
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(s, role))
            cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(s, role))
    conn.commit()

    # The swap: one transaction, so readers see the old directory or the new one, never neither.
    with conn.cursor() as cur:
        cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(f"{LIVE}_old")))
        cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", [LIVE])
        if cur.fetchone():
            cur.execute(sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(sql.Identifier(LIVE), sql.Identifier(f"{LIVE}_old")))
        cur.execute(sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(s, sql.Identifier(LIVE)))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(f"{LIVE}_old")))
        cur.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'load\\_%%'")
        for (stale,) in cur.fetchall():  # leftovers from loads that failed part-way
            cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(stale)))
    conn.commit()
    lap("swap")
    return {"records": records, "relationships": relationships, "days": applied["days"], **seconds}
