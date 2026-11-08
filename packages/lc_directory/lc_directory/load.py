"""Loads a snapshot into Postgres, blue/green.

Each load builds a fresh schema, fills it with COPY, indexes it, records its
statistics, and only then swaps it in under the name ``live`` with one atomic
rename. Readers never see a half-loaded directory, and a failed load leaves
the previous one serving.
"""

from __future__ import annotations

import json
import re
import secrets

import pyarrow.parquet as pq
from psycopg import sql

LIVE = "live"
# Bump when the schema or indexes change, so the next run reloads the same snapshot.
LOAD_VERSION = 4
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
  loaded_at timestamptz DEFAULT now(), load_version int);
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


def _copy_entities(cur, schema, files):
    cols = [c for _, c, _ in ENTITY_COLUMNS] + ["country", "names_text", "filter_terms"]
    copy = sql.SQL("COPY {}.entities ({}) FROM STDIN").format(sql.Identifier(schema), sql.SQL(", ").join(map(sql.Identifier, cols)))
    count = 0
    with cur.copy(copy) as out:
        for path in files:
            match = COUNTRY.search(str(path))
            country = match.group(1) if match else "XX"
            for batch in pq.ParquetFile(path).iter_batches(batch_size=20_000):
                data = batch.to_pydict()
                for i in range(batch.num_rows):
                    row = []
                    for src, _, kind in ENTITY_COLUMNS:
                        value = data[src][i] if src in data else None
                        if kind == "list":
                            value = [v for v in (value or "").split(",") if v]
                        row.append(value)
                    sources = row[-1]  # sources is the last entity column
                    status = data["legalStatus"][i] if "legalStatus" in data else None
                    row += [country, _names_text(data["legalName"][i], data["otherNames"][i] if "otherNames" in data else None),
                            _filter_terms(country, status, sources)]
                    out.write_row(row)
                    count += 1
    return count


def _copy_relationships(cur, schema, files):
    cols = [c for _, c in RELATIONSHIP_COLUMNS]
    copy = sql.SQL("COPY {}.relationships ({}) FROM STDIN").format(sql.Identifier(schema), sql.SQL(", ").join(map(sql.Identifier, cols)))
    count = 0
    with cur.copy(copy) as out:
        for path in files:
            for batch in pq.ParquetFile(path).iter_batches(batch_size=50_000):
                data = batch.to_pydict()
                for i in range(batch.num_rows):
                    out.write_row([data[src][i] for src, _ in RELATIONSHIP_COLUMNS])
                    count += 1
    return count


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


def load(conn, entity_files, relationship_files, meta: dict, readers=()) -> dict:
    """Load a snapshot and swap it in. ``readers`` are database roles granted read access."""
    schema = f"load_{secrets.token_hex(4)}"
    s = sql.Identifier(schema)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(s))
        cur.execute(sql.SQL(TABLES).format(s=s))
        records = _copy_entities(cur, schema, entity_files)
        relationships = _copy_relationships(cur, schema, relationship_files)
        for statement in INDEXES:
            cur.execute(sql.SQL(statement).format(s=s))
    conn.commit()
    vacuum(conn, schema)
    with conn.cursor() as cur:
        stats = _stats(cur, schema)
        cur.execute(sql.SQL("INSERT INTO {}.meta (date, snapshot_root, records, relationships, stats, load_version) "
                            "VALUES (%s, %s, %s, %s, %s, %s)")
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
    return {"records": records, "relationships": relationships}
