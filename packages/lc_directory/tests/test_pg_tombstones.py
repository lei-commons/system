"""Tombstones in the database: an old ID answers with where it went; search and suggestions leave them out
unless asked. Against a real Postgres: CI runs one as a service and sets LC_TEST_PG."""

import contextlib
import os

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("LC_TEST_PG")
pytestmark = pytest.mark.skipif(not DSN, reason="needs LC_TEST_PG (CI provides a Postgres service)")

OLD, NEW = "LECOZZ00000000000O11", "LECOZZ00000000000N22"
LEI = "5493000OREGON0000001"


class Pool:
    @contextlib.contextmanager
    def connection(self):
        with psycopg.connect(DSN) as conn:
            yield conn


@pytest.fixture(scope="module")
def d(tmp_path_factory):
    from lc_core import tombstone
    from lc_core.publishing import load as settings
    from lc_directory.load import load
    from lc_directory.pg import Directory
    from lc_publish.snapshot import build as build_snapshot

    gleif = {"directoryId": OLD, "lei": LEI, "legalName": "PRECISION CASTPARTS CORP", "legalJurisdiction": "US-OR",
             "registrationAuthority": "RA000631", "registrationNumber": "057491-14", "legalAddress": {"country": "US"}}
    register = {"directoryId": NEW, "legalName": "PRECISION CASTPARTS CORP.", "legalStatus": "Active",
                "legalJurisdiction": "US-OR", "registrationNumber": "5749114"}
    records = {"us-or-sos": {NEW: register}, "gleif": {NEW: {**gleif, "directoryId": NEW}, OLD: tombstone.make(gleif, NEW)}}
    tmp = tmp_path_factory.mktemp("snap")
    m = build_snapshot(records, tmp, "2026-11-22", settings=settings().snapshots)
    files = [str(tmp / "parts" / f["name"]) for f in m["parts"] if f["kind"] == "entities"]
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS live CASCADE")
    with psycopg.connect(DSN) as conn:
        load(conn, files, [], {k: m[k] for k in ("date", "snapshotRoot", "records", "relationships", "stats")})
    return Directory(Pool())


def ids(found):
    return [r["directoryId"] for r in found["results"]]


def test_the_old_id_answers_with_where_it_went(d):
    grave = d.entity(OLD)
    assert grave["mergedInto"] == NEW and grave["statusCategory"] == "merged" and "lei" not in grave
    assert "mergedInto" not in d.entity(NEW) and d.entity(NEW)["lei"] == LEI


def test_search_and_suggestions_leave_tombstones_out_unless_asked(d):
    assert ids(d.search("precision castparts")) == [NEW]
    assert ids(d.search("precis")) == [NEW]  # the names-starting-with path too
    assert ids(d.search(LEI)) == [NEW] and ids(d.search(OLD)) == [OLD]
    assert ids(d.search("precision", status="merged")) == [OLD]
    assert [r["directoryId"] for r in d.suggest("precision")] == [NEW]


def test_dashboards_count_tombstones_as_merged(d):
    assert {r["value"]: r["records"] for r in d.stats()["byStatusCategory"]} == {"active": 1, "merged": 1}
