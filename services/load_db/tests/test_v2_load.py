"""Format v2 loads against a real Postgres (CI runs one and sets LC_TEST_PG).

Days are published through the real build-snapshot steps into in-memory buckets; after each,
load-db runs and the database must hold exactly that day's records. Then the ways an
incremental load must refuse: wrong counts, duplicates, impossible adds, broken chains."""

import json
import os
from dataclasses import replace
from datetime import date as Date
from datetime import datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("LC_TEST_PG")
pytestmark = pytest.mark.skipif(not DSN, reason="needs LC_TEST_PG (CI provides a Postgres service)")

from lc_core.publishing import load as load_settings  # noqa: E402
from lc_directory.load import LOAD_VERSION  # noqa: E402
from lc_publish.job import publish  # noqa: E402
from lc_publish.snapshot import CHANGE_SCHEMA, sha256_file  # noqa: E402
from lc_publish.testing import FakeBucket, World  # noqa: E402

from load_db.job import run  # noqa: E402

READER = "site_reader"
START = Date(2026, 11, 18)


def settings(**snap):
    base = load_settings()
    # These worlds are a few hundred records, so a handful of changes is a large share: the incremental
    # threshold is set for them here (the repository's 4% is for tens of millions of records).
    return replace(base, snapshots=replace(base.snapshots, **{"full_every_days": 3, "part_target_records": 300,
                                                              "change_file_max_rows": 40, "prune_grace_days": 0, **snap}),
                   load=replace(base.load, incremental_max_fraction=0.2))


class Env:
    def __init__(self, tmp, **snap):
        self.tmp, self.settings, self.day = tmp, settings(**snap), 0
        self.now = datetime(2026, 11, 18, 3, tzinfo=timezone.utc)
        self.snapshots, self.data = FakeBucket(lambda: self.now), FakeBucket(lambda: self.now)
        self.events = []
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS live CASCADE")
            conn.execute(f"DO $$ BEGIN CREATE ROLE {READER}; EXCEPTION WHEN duplicate_object THEN NULL; END $$")

    def publish(self, world):
        date = (START + timedelta(days=self.day)).isoformat()
        self.now = datetime.fromisoformat(date + "T03:00:00+00:00")
        work = self.tmp / f"w{self.day}"
        work.mkdir()
        m = publish(self.snapshots, self.data, date, world.readers(), world.relationship_reader(), self.settings, work,
                    lambda e: None, now=self.now)
        self.day += 1
        return m

    def load(self, **load_over):
        events = []
        try:
            outcome = run(bucket=self.snapshots, connect=lambda: psycopg.connect(DSN), readers=[READER], gate=lambda: None,
                          log=events.append, settings=replace(self.settings.load, **load_over))
        finally:
            self.events += events
        return outcome, events


def db_rows():
    with psycopg.connect(DSN) as conn:
        entities = conn.execute("SELECT directory_id, legal_name, legal_status, country, array_to_string(sources, ','), "
                                "lei FROM live.entities ORDER BY 1").fetchall()
        rels = conn.execute("SELECT relationship_id, start_id, end_id, relationship_status FROM live.relationships "
                            "ORDER BY 1").fetchall()
        meta = conn.execute("SELECT date, snapshot_root, records, relationships, stats, load_version, mode FROM live.meta").fetchone()
    return entities, rels, meta


def truth(world):
    rows = [json.loads(r) for r in world.truth()]
    return sorted((r["directoryId"], r["legalName"], r["legalStatus"], r["country"], r["sources"], r["lei"]) for r in rows)


def check(world, manifest):
    entities, rels, meta = db_rows()
    assert entities == truth(world)
    assert len(rels) == len(world.relationships) == manifest["relationships"]
    assert meta[0] == manifest["date"] and meta[1] == manifest["snapshotRoot"] and meta[2] == manifest["records"]
    return meta


def test_a_full_load_then_incremental_days_including_across_full_snapshots(tmp_path):
    env, world = Env(tmp_path), World(n=400)
    m = env.publish(world)
    assert env.load()[0] == "loaded"
    assert check(world, m)[6] == "full"
    for _ in range(7):
        world.mutate()
        m = env.publish(world)
        outcome, events = env.load()
        loaded = [e for e in events if e["event"] == "loaded"][0]
        assert outcome == "loaded" and loaded["mode"] == "incremental" and loaded["days"] == 1, events
        assert check(world, m)[6] == "incremental"
    assert env.load()[0] == "already-loaded"
    with psycopg.connect(DSN) as conn:  # the site's role can still read, and stats came from the manifest
        conn.execute(f"SET ROLE {READER}")
        assert conn.execute("SELECT count(*) FROM live.entities").fetchone()[0] == m["records"]
        conn.execute("RESET ROLE")
        stats = conn.execute("SELECT stats FROM live.meta").fetchone()[0]
        by_country = dict(conn.execute("SELECT country, count(*) FROM live.entities GROUP BY 1").fetchall())
    assert {s["value"]: s["records"] for s in stats["byCountry"]} == by_country


def test_the_status_category_day_is_a_new_chain_and_a_full_reload(tmp_path):
    """The day statusCategory arrives: the snapshot starts a new chain (full, no base) and load-db, finding a
    database loaded by the previous load version, reloads in full; every row then has its category."""
    env, world = Env(tmp_path), World(n=300)
    first = env.publish(world)
    env.load()
    key = f"{first['date']}/manifest.json"
    old = json.loads(env.snapshots.objects[key]["data"])
    old.pop("columns")  # as every manifest before this change
    env.snapshots.objects[key]["data"] = json.dumps(old).encode()
    with psycopg.connect(DSN) as conn:
        conn.execute("UPDATE live.meta SET load_version = 5")
        conn.commit()
    world.mutate()
    m = env.publish(world)
    assert m["kind"] == "full" and m["base"] is None and not m["changes"]["files"]
    outcome, events = env.load()
    loaded = [e for e in events if e["event"] == "loaded"][0]
    assert outcome == "loaded" and loaded["mode"] == "full" and "another load version" in loaded["why"], events
    meta = check(world, m)
    assert meta[5] == LOAD_VERSION
    with psycopg.connect(DSN) as conn:
        rows = conn.execute("SELECT legal_status, status_category, filter_terms FROM live.entities").fetchall()
    assert rows and all(category and f"s={category}" in terms for _, category, terms in rows)
    assert {(word, category) for word, category, _ in rows} <= {("Active", "active"), ("ATIVA", "active"),
                                                                ("ACTIVE", "active"), (None, "unknown")}
    counted = {}
    for _, category, _ in rows:
        counted[category] = counted.get(category, 0) + 1
    assert {s["value"]: s["records"] for s in meta[4]["byStatusCategory"]} == counted and counted["active"]
    world.mutate()
    env.publish(world)
    assert [e for e in env.load()[1] if e["event"] == "loaded"][0]["mode"] == "incremental"


def test_several_days_behind_are_applied_in_order(tmp_path):
    env, world = Env(tmp_path), World(n=300)
    env.publish(world)
    env.load()
    for _ in range(4):
        world.mutate()
        m = env.publish(world)
    outcome, events = env.load()
    loaded = [e for e in events if e["event"] == "loaded"][0]
    assert loaded["mode"] == "incremental" and loaded["days"] == 4
    check(world, m)


@pytest.mark.parametrize("over,why", [({"incremental_max_days": 2}, "more than 2 days"), ({"incremental": False}, "off"),
                                      ({"incremental_max_fraction": 0.0001}, "too large")])
def test_limits_send_it_to_a_full_load(tmp_path, over, why):
    env, world = Env(tmp_path), World(n=300)
    env.publish(world)
    env.load()
    for _ in range(4):
        world.mutate()
        m = env.publish(world)
    outcome, events = env.load(**over)
    loaded = [e for e in events if e["event"] == "loaded"][0]
    assert loaded["mode"] == "full" and why in loaded["why"]
    check(world, m)


def _tamper(env, m, edit, fix_sha=True):
    """Rewrite the day's first change file with ``edit`` (and, as an attacker would, its SHA-256)."""
    name = f"{m['date']}/changes.000.parquet"
    path = env.tmp / "c.parquet"
    path.write_bytes(env.snapshots.objects[name]["data"])
    rows = edit(pq.read_table(path).to_pylist())
    pq.write_table(pa.table({f.name: [r.get(f.name) for r in rows] for f in CHANGE_SCHEMA}, schema=CHANGE_SCHEMA), path)
    env.snapshots.objects[name]["data"] = path.read_bytes()
    if fix_sha:
        next(f for f in m["files"] if f["name"] == "changes.000.parquet")["sha256"] = sha256_file(path)
        env.snapshots.objects[f"{m['date']}/manifest.json"]["data"] = json.dumps(m).encode()


def _day_after_a_load(tmp_path):
    env, world = Env(tmp_path), World(n=300)
    env.publish(world)
    env.load()
    world.mutate()
    return env, world, env.publish(world)


@pytest.mark.parametrize("edit", [
    lambda rows: rows[1:],                                                    # a change missing from the file
    lambda rows: rows + rows[:1],                                             # a change twice
    lambda rows: [{**r, "op": "add"} if r["op"] == "change" else r for r in rows],  # counts no longer match
])
def test_a_forged_change_file_is_refused_and_nothing_changes(tmp_path, edit):
    """Its SHA-256 forged to match: the counts still catch it. A full load needs the same day's changes,
    so it fails too, loudly, and the database keeps serving the day before."""
    env, world, m = _day_after_a_load(tmp_path)
    _tamper(env, m, edit)
    before = db_rows()
    with pytest.raises(Exception, match="manifest"):
        env.load()
    failed = [e for e in env.events if e["event"] == "incremental-failed"]
    assert failed and failed[0]["error"] == "LoadMismatch" and db_rows() == before


def test_a_damaged_file_is_caught_by_its_sha256(tmp_path):
    env, world, m = _day_after_a_load(tmp_path)
    _tamper(env, m, lambda rows: rows, fix_sha=False)  # rewritten, so its bytes differ
    before = db_rows()
    with pytest.raises(Exception, match="SHA-256"):
        env.load()
    assert db_rows() == before and [e for e in env.events if e["event"] == "incremental-failed"][0]["error"] == "DamagedFile"


def test_an_add_of_an_existing_record_is_refused_by_the_database(tmp_path):
    env, world, m = _day_after_a_load(tmp_path)
    forged = [0]

    def edit(rows):  # a changed record passed off as new, with the counts forged to match
        out = [{**r, "op": "add"} if r["op"] == "change" else r for r in rows]
        forged[0] = sum(1 for r in rows if r["op"] == "change")
        return out
    _tamper(env, m, edit)
    m["changes"] = {**m["changes"], "added": m["changes"]["added"] + forged[0], "changed": m["changes"]["changed"] - forged[0]}
    env.snapshots.objects[f"{m['date']}/manifest.json"]["data"] = json.dumps(m).encode()
    before = db_rows()
    with pytest.raises(Exception):
        env.load(fallback_to_full=False)
    assert [e for e in env.events if e["event"] == "incremental-failed"][0]["error"] in ("UniqueViolation", "LoadMismatch")
    assert db_rows() == before


def test_a_drifted_database_is_rolled_back_and_reloaded_in_full(tmp_path):
    """The case the fallback is for: the database no longer matches the day it says it holds."""
    env, world, m = _day_after_a_load(tmp_path)
    with psycopg.connect(DSN) as conn:
        conn.execute("INSERT INTO live.entities (directory_id, legal_name, sources, country, names_text, filter_terms) "
                     "VALUES ('LECOZZSTRAY0000000XX', 'STRAY', '{gb-coh}', 'GB', 'STRAY', '{}')")
    outcome, events = env.load()
    assert [e for e in events if e["event"] == "incremental-failed"][0]["error"] == "LoadMismatch"
    loaded = [e for e in events if e["event"] == "loaded"][0]
    assert outcome == "loaded" and loaded["mode"] == "full" and "failed a check" in loaded["why"]
    check(world, m)
    with pytest.raises(Exception):  # the same failure without the fallback raises instead
        with psycopg.connect(DSN) as conn:
            conn.execute("INSERT INTO live.entities (directory_id, legal_name, sources, country, names_text, filter_terms) "
                         "VALUES ('LECOZZSTRAY0000000XX', 'STRAY', '{gb-coh}', 'GB', 'STRAY', '{}')")
            conn.commit()
        world.mutate()
        env.publish(world)
        env.load(fallback_to_full=False)


def test_a_database_from_another_load_version_is_reloaded_in_full(tmp_path):
    env, world = Env(tmp_path), World(n=300)
    env.publish(world)
    env.load()
    with psycopg.connect(DSN) as conn:
        conn.execute("UPDATE live.meta SET load_version = 4")
    world.mutate()
    m = env.publish(world)
    outcome, events = env.load()
    loaded = [e for e in events if e["event"] == "loaded"][0]
    assert loaded["mode"] == "full" and "another load version" in loaded["why"]
    check(world, m)


def test_old_format_snapshots_still_load(tmp_path):
    env = Env(tmp_path)
    path = tmp_path / "GB.parquet"
    cols = ["directoryId", "lei", "legalName", "legalStatus", "otherNames", "sources"]
    pq.write_table(pa.table({c: ["LECOZZ000000000000AA" if c == "directoryId" else ("X LTD" if c == "legalName" else
                                 ("gb-coh" if c == "sources" else None))] for c in cols}), path)
    env.snapshots.objects["2026-11-10/GB.parquet"] = {"data": path.read_bytes(), "created": env.now}
    env.snapshots.objects["latest.json"] = {"data": json.dumps({"date": "2026-11-10", "prefix": "2026-11-10",
                                                                "snapshotRoot": "sha256:v1", "files": ["GB.parquet"],
                                                                "relationships": []}).encode(), "created": env.now}
    outcome, events = env.load()
    entities, _, meta = db_rows()
    assert outcome == "loaded" and entities == [("LECOZZ000000000000AA", "X LTD", None, "GB", "gb-coh", None)]
    assert meta[6] == "full" and meta[5] == LOAD_VERSION


def test_the_database_credits_every_record_through_its_sources(tmp_path):
    import contextlib

    from lc_directory.pg import Directory

    env, world = Env(tmp_path), World(n=200)
    m = env.publish(world)
    env.load()
    world.mutate()
    m2 = env.publish(world)
    assert env.load()[1][-1]["mode"] == "incremental"
    with psycopg.connect(DSN) as conn:
        rows = conn.execute("SELECT id, attribution, citation, licence FROM live.sources ORDER BY id").fetchall()
        missing = conn.execute("SELECT count(*) FROM live.entities WHERE registry_url IS NULL OR registry_url = ''").fetchone()[0]
        orphans = conn.execute("SELECT count(*) FROM live.entities e WHERE NOT EXISTS (SELECT 1 FROM live.sources s "
                               "WHERE s.id = ANY(e.sources))").fetchone()[0]
    assert {r[0] for r in rows} == {s["id"] for s in m2["sources"]} and all(r[2] for r in rows)
    assert missing == 0 and orphans == 0  # every record has a source URL and a credited source

    class Pool:
        @contextlib.contextmanager
        def connection(self):
            with psycopg.connect(DSN) as conn:
                yield conn

    some = next(iter(world.sources["gb-coh"]))
    record = Directory(Pool()).entity(some)
    assert record["credits"][0]["id"] == "gb-coh" and record["credits"][0]["url"].endswith("/company/" + record["registrationNumber"]) \
        if record.get("registrationNumber") else record["credits"][0]["url"]
    assert "Open Government Licence" in record["credits"][0]["attribution"]
