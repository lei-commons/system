"""Load test: does the serving database hold up at 30 million records?

Builds a synthetic directory inside the database, in its own ``bench_*``
schema. It repeats the real directory under new IDs, and every copy after the
first adds one word to each name. Real names, word frequencies and ownership
graphs are therefore kept, and the size is about N records. It indexes the
copy exactly as the daily load does, then runs every query the site makes, through
the site's own code and with the site's 5-second statement timeout: one query at a time,
then eight at once. It drops the schema at the end, even after a failure.

It takes the daily load's lock, so the two never overlap. It stops at once
unless the system is running. It reports only timings, counts and sizes, never
names or IDs, because the system repo's workflow logs are public.

Run it as an override of the load-db job: see .github/workflows/load-test.yml.
"""

from __future__ import annotations

import json
import math
import os
import random
import secrets
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from lc_core.control import SystemHalted, require_running

COMPONENT = "job:load-db"
LOCK = "SELECT pg_try_advisory_lock(hashtext('lc-load-db'))"
# One word per extra copy: common words, so the test includes the slow case,
# a word that matches millions of records.
SUFFIXES = ["NORTH", "GLOBAL", "HOLDINGS", "SERVICES", "EUROPE", "PACIFIC", "TRADING", "PARTNERS", "GROUP",
            "INTERNATIONAL", "CAPITAL", "SOLUTIONS", "VENTURES", "INVESTMENTS", "ASIA", "AMERICA", "WEST", "EAST",
            "SOUTH", "UNITED", "NEW", "FIRST", "PRIME", "ALPHA", "OMEGA", "NOVA", "ATLAS", "APEX", "ZENITH", "CREST"]
COMMON_WORDS = ["LIMITED", "HOLDINGS", "SERVICES", "GMBH", "FUND", "NORTH", "GLOBAL", "LTD"]


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def _mapped(column: str, copy: int) -> str:
    """SQL for an ID in a given copy: the real ID in copy 0, a stable new one after."""
    if copy == 0:
        return column
    return f"upper(substr(md5({column} || ':{copy}'), 1, 20))"


def build(conn, schema: str, target: int, max_gb: float, log=log) -> dict:
    """Create and fill the bench schema. Returns build timings and sizes."""
    from lc_directory.load import INDEXES, TABLES, vacuum
    from psycopg import sql

    s = sql.Identifier(schema)
    with conn.cursor() as cur:
        cur.execute("SELECT records, relationships FROM live.meta")
        live_records, live_relationships = cur.fetchone()
        cur.execute("SELECT pg_total_relation_size('live.entities') + pg_total_relation_size('live.relationships')")
        live_bytes = cur.fetchone()[0]
    copies = max(1, math.ceil(target / live_records))
    estimate_gb = live_bytes * target / live_records / 1e9
    log({"event": "bench-plan", "target": target, "liveRecords": live_records, "liveRelationships": live_relationships,
         "liveGB": round(live_bytes / 1e9, 2), "copies": copies, "estimateGB": round(estimate_gb, 1)})
    if estimate_gb > max_gb:
        raise RuntimeError(f"the test would need about {estimate_gb:.0f} GB, over the {max_gb:.0f} GB limit")

    timings = {}
    with conn.cursor() as cur:
        started = time.time()
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(s))
        # Unlogged: the copy is thrown away, so skip writing it to the WAL as well.
        # This saves disk and backup space. Queries run exactly as they do on logged tables.
        cur.execute(sql.SQL(TABLES.replace("CREATE TABLE", "CREATE UNLOGGED TABLE")).format(s=s))
        conn.commit()
        cols = ("lei, legal_name, legal_form, legal_jurisdiction, registration_authority, registration_number, legal_status, "
                "incorporation_date, dissolution_date, record_mode, registry_url, other_names, legal_address, "
                "headquarters_address, industry_codes, labels, sources, country, names_text, filter_terms")
        loaded = 0
        for copy in range(copies):
            take = min(live_records, target - loaded)
            if take <= 0:
                break
            word = SUFFIXES[(copy - 1) % len(SUFFIXES)] if copy else ""
            name = "legal_name" if copy == 0 else f"legal_name || ' {word}'"
            names_text = "names_text" if copy == 0 else f"'{word} ' || names_text"
            lei = "lei" if copy == 0 else "NULL"  # an LEI belongs to one record; copies are registry-only
            number = "registration_number" if copy == 0 else f"registration_number || '-{copy}'"
            cur.execute(sql.SQL(
                f"INSERT INTO {{s}}.entities (directory_id, {cols}) SELECT {_mapped('directory_id', copy)}, {lei}, {name}, "
                "legal_form, legal_jurisdiction, registration_authority, " + number + ", legal_status, incorporation_date, "
                "dissolution_date, record_mode, registry_url, other_names, legal_address, headquarters_address, "
                f"industry_codes, labels, sources, country, {names_text}, filter_terms FROM live.entities LIMIT %s").format(s=s), [take])
            loaded += cur.rowcount
            cur.execute(sql.SQL(
                "INSERT INTO {s}.relationships SELECT " + ("relationship_id" if copy == 0 else f"relationship_id || ':{copy}'") +
                f", start_node, end_node, {_mapped('start_id', copy)}, {_mapped('end_id', copy)}, relationship_type, "
                "relationship_status, percentage, period_start, period_end FROM live.relationships").format(s=s))
            conn.commit()
            log({"event": "bench-copy", "copy": copy, "records": loaded, "seconds": round(time.time() - started)})
        timings["fill"] = round(time.time() - started)

        for statement in INDEXES:
            t = time.time()
            cur.execute(sql.SQL(statement).format(s=s))
            conn.commit()
            label = statement.split(" ON ")[1].replace("{s}.", "")
            timings[f"index {label}"] = round(time.time() - t)
            log({"event": "bench-index", "index": label, "seconds": timings[f"index {label}"]})
        t = time.time()
        vacuum(conn, schema)
        timings["vacuum analyze"] = round(time.time() - t)
        cur.execute(sql.SQL("SELECT count(*) FROM {}.relationships").format(s))
        relationships = cur.fetchone()[0]
        cur.execute(sql.SQL("INSERT INTO {}.meta (date, snapshot_root, records, relationships, stats) "
                            "VALUES ('bench', 'bench', %s, %s, '{{}}')").format(s), [loaded, relationships])
        conn.commit()
        sizes = {}
        for table in ("entities", "relationships"):
            cur.execute("SELECT pg_table_size(%s), pg_indexes_size(%s)", [f"{schema}.{table}"] * 2)
            data, index = cur.fetchone()
            sizes[table] = {"tableGB": round(data / 1e9, 2), "indexGB": round(index / 1e9, 2)}
    return {"records": loaded, "relationships": relationships, "copies": copies, "seconds": timings, "sizes": sizes}


def _samples(conn, schema: str, n: int, rng: random.Random) -> dict:
    """Inputs for the queries, drawn from the bench data itself. Never logged."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT directory_id, legal_name, registration_number FROM {schema}.entities TABLESAMPLE SYSTEM (0.05) "
                    "LIMIT %s", [n * 4])
        rows = cur.fetchall()
        cur.execute(f"SELECT end_id FROM {schema}.relationships WHERE end_id IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC LIMIT %s",
                    [max(3, n // 4)])
        hubs = [r[0] for r in cur.fetchall()]
        cur.execute(f"SELECT start_id FROM {schema}.relationships TABLESAMPLE SYSTEM (1) WHERE start_id IS NOT NULL LIMIT %s", [n])
        linked = [r[0] for r in cur.fetchall()]
    rng.shuffle(rows)
    rows = rows[:n]
    words = [max(name.split(), key=len) for _, name, _ in rows if name and name.split()]
    return {
        "ids": [r[0] for r in rows], "names": [r[1] for r in rows if r[1]], "words": words,
        "pairs": [" ".join(name.split()[:2]) for _, name, _ in rows if name and len(name.split()) >= 2],
        "numbers": [r[2] for r in rows if r[2] and r[2].isalnum() and len(r[2]) >= 6 and any(c.isdigit() for c in r[2])],
        "prefixes": [name[:k] for _, name, _ in rows if name for k in (2, 3, 5)],
        "hubs": hubs, "linked": linked,
    }


def cases(directory, samples: dict) -> list:
    """(category, callable) pairs covering every query the site makes."""
    d, s = directory, samples
    out = []
    out += [("search: exact name", lambda q=q: d.search(q)) for q in s["names"]]
    out += [("search: distinctive word", lambda q=q: d.search(q)) for q in s["words"]]
    out += [("search: two words", lambda q=q: d.search(q)) for q in s["pairs"]]
    out += [("search: common word", lambda q=q: d.search(q)) for q in COMMON_WORDS]
    out += [("search: common word, page 40", lambda q=q: d.search(q, page=40)) for q in COMMON_WORDS]
    out += [("search: common word + country", lambda q=q: d.search(q, country="GB")) for q in COMMON_WORDS]
    out += [("search: common word + status", lambda q=q: d.search(q, status="Active")) for q in COMMON_WORDS]
    out += [("search: ID or number", lambda q=q: d.search(q)) for q in s["ids"][:10] + s["numbers"][:10]]
    out += [("suggest", lambda q=q: d.suggest(q)) for q in s["prefixes"]]
    out += [("entity", lambda q=q: d.entity(q)) for q in s["ids"]]
    out += [("relationships: hub", lambda q=q: d.relationships(q)) for q in s["hubs"]]
    out += [("relationships: typical", lambda q=q: d.relationships(q)) for q in s["linked"]]
    for depth in (1, 2, 3):
        out += [(f"graph depth {depth}: hub", lambda q=q, k=depth: d.graph(q, depth=k)) for q in s["hubs"]]
        out += [(f"graph depth {depth}: typical", lambda q=q, k=depth: d.graph(q, depth=k)) for q in s["linked"]]
    out += [("stats", d.stats)]
    return out


PLAN_KEYS = ("Node Type", "Index Name", "Actual Total Time", "Actual Rows", "Actual Loops", "Shared Hit Blocks",
             "Shared Read Blocks", "Exact Heap Blocks", "Lossy Heap Blocks", "Rows Removed by Filter")


def _scrub(node) -> dict:
    """A plan node without its conditions or output columns: they could hold query text."""
    out = {k: node[k] for k in PLAN_KEYS if k in node}
    if node.get("Plans"):
        out["Plans"] = [_scrub(child) for child in node["Plans"]]
    return out


def plans(conn, schema: str) -> list:
    """Scrubbed EXPLAIN ANALYZE of common-word searches, with and without filters, as the site
    runs them and with the planner left to choose. The words are this module's constants, never data."""
    from lc_directory.pg import COUNT_CAP, SEARCH_SETTINGS, SUMMARY, text_match

    out = []
    for word in COMMON_WORDS[:4]:
        for label, filters in (("", {}), ("+country", {"country": "GB"}), ("+status", {"status": "Active"}),
                               ("+country+status", {"country": "DE", "status": "Active"})):
            match, params, row_filter, row_params = text_match(word, **filters)
            query = (f"SELECT {SUMMARY}, ts_rank(search, {match}) AS rank FROM {schema}.entities "
                     f"WHERE search @@ {match}{row_filter} LIMIT {COUNT_CAP + 1}")
            for path, settings in (("site", list(SEARCH_SETTINGS)), ("planner", [])):
                case = {"word": word + label, "path": path}
                try:
                    with conn.cursor() as cur:
                        cur.execute("SET LOCAL statement_timeout = 15000")
                        for setting in settings:
                            cur.execute(setting)
                        cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params * 2 + row_params)
                        plan = cur.fetchone()[0][0]
                    case.update({"ms": round(plan["Execution Time"]), "plan": _scrub(plan["Plan"])})
                except Exception as exc:
                    case["error"] = type(exc).__name__
                conn.rollback()
                out.append(case)
    return out


def _timed(fn):
    import psycopg

    t = time.perf_counter()
    try:
        fn()
        return (time.perf_counter() - t) * 1000, None
    except psycopg.errors.QueryCanceled:
        return (time.perf_counter() - t) * 1000, "timeout"
    except Exception as exc:  # recorded by type only, never the message (it may hold the query)
        return (time.perf_counter() - t) * 1000, type(exc).__name__


def summarise(results) -> dict:
    """{category: {n, p50, p95, max (ms), failures}} from (category, ms, error) triples."""
    by = {}
    for category, ms, error in results:
        entry = by.setdefault(category, {"times": [], "failures": {}})
        entry["times"].append(ms)
        if error:
            entry["failures"][error] = entry["failures"].get(error, 0) + 1
    out = {}
    for category, entry in by.items():
        times = sorted(entry["times"])
        p95 = times[min(len(times) - 1, math.ceil(0.95 * len(times)) - 1)]
        out[category] = {"n": len(times), "p50": round(statistics.median(times)), "p95": round(p95), "max": round(times[-1]),
                         **({"failures": entry["failures"]} if entry["failures"] else {})}
    return out


def benchmark(directory, samples, seconds: float, threads: int = 8, rng=None) -> dict:
    rng = rng or random.Random(7)
    all_cases = cases(directory, samples)
    cold = [(c, *_timed(fn)) for c, fn in all_cases]
    warm = [(c, *_timed(fn)) for c, fn in all_cases]

    # Mixed load at the site's pool size: mostly searches and entity pages, like a person browsing.
    weights = {"search": 5, "suggest": 6, "entity": 4, "relationships": 2, "graph": 1, "stats": 1}
    pool = [(c, fn) for c, fn in all_cases for _ in range(weights.get(c.split(":")[0].split(" ")[0], 1))]
    results, lock, stop = [], threading.Lock(), time.time() + seconds

    def worker(seed):
        r = random.Random(seed)
        while time.time() < stop:
            c, fn = r.choice(pool)
            ms, error = _timed(fn)
            with lock:
                results.append(("mixed", ms, error))

    with ThreadPoolExecutor(threads) as ex:
        list(ex.map(worker, range(threads)))
    mixed = summarise(results).get("mixed", {})
    mixed["perSecond"] = round(len(results) / seconds, 1)
    mixed["threads"] = threads
    return {"cold": summarise(cold), "warm": summarise(warm), "concurrent": mixed}


def main() -> int:
    import psycopg
    from google.cloud import secretmanager
    from lc_directory.pg import Directory
    from psycopg_pool import ConnectionPool

    target = int(os.environ.get("LC_BENCH_RECORDS", "30000000"))
    max_gb = float(os.environ.get("LC_BENCH_MAX_GB", "60"))
    seconds = float(os.environ.get("LC_BENCH_SECONDS", "120"))
    keep = os.environ.get("LC_BENCH_KEEP") == "1"
    try:
        require_running(COMPONENT)
    except SystemHalted as exc:
        log({"event": "halted", "reason": str(exc)})
        return 0
    project = os.environ["GCP_PROJECT_ID"]
    password = secretmanager.SecretManagerServiceClient().access_secret_version(
        name=f"projects/{project}/secrets/{os.environ.get('LC_DB_SECRET', 'db-admin-password')}/versions/latest"
    ).payload.data.decode()
    conninfo = (f"host={os.environ['LC_DB_HOST']} dbname={os.environ.get('LC_DB_NAME', 'lei_commons')} "
                f"user={os.environ.get('LC_DB_ADMIN', 'lc_admin')} password={password} sslmode=require")
    schema = f"bench_{secrets.token_hex(4)}"
    with psycopg.connect(conninfo) as conn:
        if not conn.execute(LOCK).fetchone()[0]:
            log({"event": "busy", "reason": "the daily load is running"})
            return 0
        conn.commit()
        with conn.cursor() as cur:  # leftovers from a test that died part-way
            cur.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'bench\\_%%'")
            for (stale,) in cur.fetchall():
                cur.execute(f'DROP SCHEMA "{stale}" CASCADE')
        conn.commit()
        try:
            started = time.time()
            built = build(conn, schema, target, max_gb)
            log({"event": "bench-built", **built})
            samples = _samples(conn, schema, 40, random.Random(11))
            # The site's own settings: its 5-second statement timeout and a pool of eight.
            with ConnectionPool(conninfo + " options='-c statement_timeout=5000'", min_size=1, max_size=8, open=True) as pool:
                bench = benchmark(Directory(pool, schema=schema), samples, seconds)
            for case in plans(conn, schema):
                log({"event": "bench-plan-detail", **case})
            log({"event": "loadtest-summary", "records": built["records"], "relationships": built["relationships"],
                 "build": built, "queries": bench, "totalSeconds": round(time.time() - started)})
        finally:
            conn.rollback()
            if not keep:
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                conn.commit()
                log({"event": "bench-dropped"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
