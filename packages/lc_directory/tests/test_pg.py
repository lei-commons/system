"""Against a real Postgres: CI runs one as a service and sets LC_TEST_PG."""

import contextlib
import os

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("LC_TEST_PG")
pytestmark = pytest.mark.skipif(not DSN, reason="needs LC_TEST_PG (CI provides a Postgres service)")

ID = "LECOZZFZ6LZCOJBDKT73"
OTHER = "LECOZZ000000000000AA"
READER = "site_reader"


def records(status="Active"):
    gb = {ID: {"directoryId": ID, "legalName": "EXAMPLE 100% LIMITED", "legalJurisdiction": "GB", "registrationNumber": "01234567",
               "legalStatus": status, "legalAddress": {"country": "GB", "city": "LONDON"}, "industryCodes": ["62012"],
               "otherNames": ["OLD EXAMPLE LTD"]}}
    gleif = {ID: {"directoryId": ID, "lei": "5493001KJTIIGC8Y1R12", "legalName": "EXAMPLE LTD", "legalJurisdiction": "GB"},
             OTHER: {"directoryId": OTHER, "lei": "529900T8BM49AURSDO55", "legalName": "<script>alert(1)</script> INC",
                     "legalJurisdiction": "US-DE"}}
    for i in range(150):
        gb[f"LECOZZ0000000001{i:04d}"] = {"directoryId": f"LECOZZ0000000001{i:04d}", "legalName": f"MANY CO {i}",
                                         "legalJurisdiction": "GB"}
    return {"gb-coh": gb, "gleif": gleif}


RELS = {"REL1": {"directoryId": "REL1", "startNode": "529900T8BM49AURSDO55", "endNode": "5493001KJTIIGC8Y1R12",
                 "relationshipType": "IS_DIRECTLY_CONSOLIDATED_BY", "relationshipStatus": "ACTIVE", "percentage": 60.0},
        "REL2": {"directoryId": "REL2", "startNode": "5493001KJTIIGC8Y1R12", "endNode": "NOTINDIRECTORY000000",
                 "relationshipType": "IS_ULTIMATELY_CONSOLIDATED_BY", "relationshipStatus": "ACTIVE"}}


def build(tmp, recs, date="2026-11-07"):
    from lc_publish.shards import shard_of
    from lc_publish.snapshot import build_sharded

    readers = {sid: (lambda n, r=r: {k: v for k, v in r.items() if shard_of(k) == n}) for sid, r in recs.items()}
    m = build_sharded(readers, tmp, date, relationships=lambda n: {k: v for k, v in RELS.items() if shard_of(k) == n})
    files = [str(tmp / f["name"]) for f in m["files"] if f.get("kind") != "relationships"]
    return files, [str(tmp / "relationships.parquet")], {"date": m["date"], "snapshotRoot": m["snapshotRoot"]}


class Pool:
    @contextlib.contextmanager
    def connection(self):
        with psycopg.connect(DSN) as conn:
            yield conn


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    from lc_directory.load import load

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS live CASCADE")
        conn.execute(f"DO $$ BEGIN CREATE ROLE {READER}; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    files, rels, meta = build(tmp_path_factory.mktemp("snap"), records())
    with psycopg.connect(DSN) as conn:
        counts = load(conn, files, rels, meta, readers=[READER])
    return counts, meta


@pytest.fixture(scope="module")
def d(loaded):
    from lc_directory.pg import Directory

    return Directory(Pool())


def ids(found):
    return [r["directoryId"] for r in found["results"]]


def test_load_counts_and_meta(loaded, d):
    counts, meta = loaded
    assert counts == {"records": 152, "relationships": 2}
    assert d.count == 152 and d.relationship_count == 2 and d.meta["snapshotRoot"] == meta["snapshotRoot"]


def test_full_text_search_is_ranked_and_case_insensitive(d):
    assert ids(d.search("example")) == [ID]
    assert ids(d.search("old example")) == [ID]  # other names are searchable too


def test_exact_name_ranks_first(d):
    assert d.search("many co 1", limit=5)["results"][0]["legalName"] == "MANY CO 1"


def test_lookups_by_lei_id_and_number(d):
    for q in ("5493001KJTIIGC8Y1R12", ID, "01234567", ID.lower()):
        assert ids(d.search(q)) == [ID], q


def test_filters_and_facets(d):
    assert d.search("inc", country="us")["results"][0]["country"] == "US" and d.search("inc", country="GB")["total"] == 0
    assert {"value": "GB", "records": 150} in d.search("many")["facets"]["country"]
    assert ids(d.search("example", source="gleif")) == [ID] and d.search("many", source="gleif")["total"] == 0


def test_paging_and_caps(d):
    first, second = d.search("many", limit=100), d.search("many", limit=100, page=2)
    assert first["total"] == 150 and len(first["results"]) == 100 and len(second["results"]) == 50
    assert not set(ids(first)) & set(ids(second))
    assert len(d.search("many", limit=10_000)["results"]) == 100 and d.search("many", page=10_000)["results"] == []


def test_counts_are_capped_for_common_words(d, monkeypatch):
    import lc_directory.pg as pg

    monkeypatch.setattr(pg, "COUNT_CAP", 20)
    found = d.search("many")
    assert found["total"] == 20 and found["totalCapped"] is True


def test_injection_attempts_are_just_text(d):
    for q in ("' OR 1=1 --", "x'; DROP TABLE live.entities; --", "a & | ! ( :*"):
        d.search(q)
    assert d.search("example", country="X' OR '1'='1")["total"] == 0
    assert d.count == 152 and ids(d.search("example")) == [ID]


def test_suggest_by_prefix(d):
    names = [r["legalName"] for r in d.suggest("many co 1")]
    assert names[0] == "MANY CO 1" and all(n.startswith("MANY CO 1") for n in names)
    assert d.suggest("m") == [] and d.suggest("100%")== [] and d.suggest("_") == []


def test_entity_with_nested_fields(d):
    r = d.entity(ID)
    assert r["lei"] == "5493001KJTIIGC8Y1R12" and r["legalAddress"]["city"] == "LONDON" and r["industryCodes"] == ["62012"]
    assert r["sources"] == ["gb-coh", "gleif"] and "search" not in r and d.entity("NOPE") is None


def test_relationships_both_ways(d):
    rel = d.relationships(ID)
    assert rel["down"][0]["role"] == "direct subsidiary" and rel["down"][0]["otherName"].endswith("INC")
    assert rel["up"][0]["role"] == "ultimate parent" and rel["up"][0]["otherId"] is None


def test_graph_neighbourhood(d):
    g = d.graph(ID)
    nodes = {n["id"]: n for n in g["nodes"]}
    assert len(g["edges"]) == 2 and nodes[OTHER]["inDirectory"] and nodes["LEI:NOTINDIRECTORY000000"]["inDirectory"] is False
    assert d.graph(ID, per_node=1)["truncated"] and d.graph("NOPE") is None


def test_stats_are_precomputed(d):
    s = d.stats()
    assert s["records"] == 152 and s["byCountry"][0] == {"value": "GB", "records": 151}
    assert {r["value"] for r in s["relationshipTypes"]} == {"IS_DIRECTLY_CONSOLIDATED_BY", "IS_ULTIMATELY_CONSOLIDATED_BY"}


def test_reader_role_can_read(loaded):
    with psycopg.connect(DSN) as conn:
        conn.execute(f"SET ROLE {READER}")
        assert conn.execute("SELECT count(*) FROM live.entities").fetchone()[0] == 152


def test_reload_swaps_atomically_and_cleans_up(loaded, tmp_path):
    from lc_directory.load import already_loaded, load
    from lc_directory.pg import Directory

    _, meta = loaded
    with psycopg.connect(DSN) as conn:
        assert already_loaded(conn, meta["snapshotRoot"])
        files, rels, new_meta = build(tmp_path, records(status="Dissolved"), date="2026-11-08")
        load(conn, files, rels, new_meta, readers=[READER])
        schemas = {r[0] for r in conn.execute("SELECT nspname FROM pg_namespace").fetchall()}
    assert "live" in schemas and "live_old" not in schemas and not any(s.startswith("load_") for s in schemas)
    fresh = Directory(Pool())
    assert fresh.meta["date"] == "2026-11-08" and fresh.entity(ID)["legalStatus"] == "Dissolved"


def test_failed_load_leaves_the_live_directory_serving(loaded, tmp_path):
    from lc_directory.load import load
    from lc_directory.pg import Directory

    before = Directory(Pool()).meta["date"]
    files, rels, meta = build(tmp_path, records())
    with psycopg.connect(DSN) as conn:
        with pytest.raises(Exception):
            load(conn, files + ["/nonexistent/XX.parquet"], rels, meta, readers=[READER])
        conn.rollback()
    after = Directory(Pool())
    assert after.meta["date"] == before and after.count == 152


def plan(sql_text):
    with psycopg.connect(DSN) as conn:
        conn.execute("SET enable_seqscan = off")  # the tables are tiny here; ask what the plan would be at scale
        return "\n".join(r[0] for r in conn.execute("EXPLAIN " + sql_text))


def test_suggest_walks_the_name_index_without_sorting(loaded):
    p = plan("SELECT directory_id FROM live.entities WHERE lower(legal_name) COLLATE \"C\" LIKE 'many%' "
             "ORDER BY lower(legal_name) COLLATE \"C\" LIMIT 8")
    assert "Index" in p and "Sort" not in p, p


def test_relationship_pages_come_from_an_ordered_index(loaded):
    for side in ("start_id", "end_id"):
        p = plan(f"SELECT * FROM live.relationships WHERE {side} = 'X' ORDER BY relationship_type, relationship_id LIMIT 41")
        assert "Index" in p and "Sort" not in p, p


def test_capped_search_reports_its_cap_and_keeps_facets_within_it(d, monkeypatch):
    import lc_directory.pg as pg

    monkeypatch.setattr(pg, "COUNT_CAP", 5)
    found = d.search("many")
    assert found["cap"] == 5 and found["totalCapped"] and found["total"] == 5
    assert sum(f["records"] for f in found["facets"]["country"]) == 5


def test_a_new_load_version_reloads_the_same_snapshot(loaded):
    from lc_directory.load import LOAD_VERSION, already_loaded

    with psycopg.connect(DSN) as conn:
        root = conn.execute("SELECT snapshot_root FROM live.meta").fetchone()[0]
        assert already_loaded(conn, root)
        conn.execute("UPDATE live.meta SET load_version = %s", [LOAD_VERSION - 1])
        assert not already_loaded(conn, root)
        conn.rollback()
