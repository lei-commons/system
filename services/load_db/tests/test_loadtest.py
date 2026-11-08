import contextlib
import json
import os
import random

import pytest

from load_db.loadtest import _mapped, cases, summarise

DSN = os.environ.get("LC_TEST_PG")


def test_copy_zero_keeps_real_ids_and_later_copies_get_new_ones():
    assert _mapped("directory_id", 0) == "directory_id"
    assert "md5(directory_id || ':3')" in _mapped("directory_id", 3)


def test_summary_reports_percentiles_and_failures_by_type():
    results = [("a", float(ms), None) for ms in range(1, 101)] + [("b", 5000.0, "timeout"), ("b", 10.0, None)]
    out = summarise(results)
    assert out["a"] == {"n": 100, "p50": 50, "p95": 95, "max": 100}
    assert out["b"]["failures"] == {"timeout": 1}
    assert out["b"]["max"] == 5000


class FakeDirectory:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name, a, k))


def test_cases_cover_every_site_query():
    d = FakeDirectory()
    samples = {"ids": ["X" * 20], "names": ["A B"], "words": ["B"], "pairs": ["A B"], "numbers": ["123456"],
               "prefixes": ["AB"], "hubs": ["H" * 20], "linked": ["L" * 20]}
    for _, fn in cases(d, samples):
        fn()
    assert {name for name, _, _ in d.calls} == {"search", "suggest", "entity", "relationships", "graph", "stats"}
    assert {k.get("depth") for name, _, k in d.calls if name == "graph"} == {1, 2, 3}


@pytest.mark.skipif(not DSN, reason="needs LC_TEST_PG (CI provides a Postgres service)")
def test_build_and_benchmark_against_postgres():
    psycopg = pytest.importorskip("psycopg")
    from lc_directory.load import INDEXES, TABLES
    from lc_directory.pg import Directory
    from psycopg import sql

    from load_db.loadtest import _samples, benchmark, build

    with psycopg.connect(DSN, autocommit=True) as conn:
        for schema in ("live", "bench_test"):
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        conn.execute("CREATE SCHEMA live")
        conn.execute(sql.SQL(TABLES).format(s=sql.Identifier("live")))
        for i in range(50):
            conn.execute("INSERT INTO live.entities (directory_id, lei, legal_name, sources, country, names_text, registration_number, "
                         "filter_terms) VALUES (%s, %s, %s, '{gb-coh}', 'GB', %s, %s, '{c=gb,s=none,src=gb-coh}')",
                         [f"LECOZZ{i:014d}", f"LEI{i:017d}", f"ACME {i} LIMITED", f"ACME {i} LIMITED", f"SC{i:06d}"])
        for i in range(1, 50):
            conn.execute("INSERT INTO live.relationships VALUES (%s, %s, %s, %s, %s, 'IS_DIRECTLY_CONSOLIDATED_BY', 'ACTIVE')",
                         [f"R{i}", f"LEI{i:017d}", f"LEI{0:017d}", f"LECOZZ{i:014d}", f"LECOZZ{0:014d}"])
        for statement in INDEXES:
            conn.execute(sql.SQL(statement).format(s=sql.Identifier("live")))
        conn.execute("INSERT INTO live.meta (date, snapshot_root, records, relationships, stats) VALUES ('d', 'r', 50, 49, '{}')")

    events = []
    with psycopg.connect(DSN) as conn:
        built = build(conn, "bench_test", 120, max_gb=1, log=events.append)
        assert built["records"] == 120 and built["copies"] == 3
        assert built["relationships"] == 49 * 3
        with conn.cursor() as cur:
            # Each copy keeps its own graph: copy 1's subsidiaries point at copy 1's parent.
            cur.execute("SELECT count(DISTINCT end_id) FROM bench_test.relationships")
            assert cur.fetchone()[0] == 3
            cur.execute("SELECT count(*) FROM bench_test.entities WHERE lei IS NOT NULL")
            assert cur.fetchone()[0] == 50
        samples = _samples(conn, "bench_test", 10, random.Random(1))

    class Pool:
        @contextlib.contextmanager
        def connection(self):
            with psycopg.connect(DSN) as c:
                yield c

    directory = Directory(Pool(), schema="bench_test")
    assert directory.count == 120
    assert directory.search("ACME LIMITED NORTH")["results"]
    result = benchmark(directory, samples, seconds=1, threads=2)
    assert result["concurrent"]["n"] > 0
    assert not any(v.get("failures") for v in result["warm"].values())
    assert "ACME" not in json.dumps(result)  # timings only; names never reach the public logs
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA bench_test CASCADE")
        conn.execute("DROP SCHEMA live CASCADE")


def test_directory_rejects_a_bad_schema_name():
    pytest.importorskip("psycopg")
    from lc_directory.pg import Directory

    with pytest.raises(ValueError):
        Directory(object(), schema="live; DROP TABLE x")
