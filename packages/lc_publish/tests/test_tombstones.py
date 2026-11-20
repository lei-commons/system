"""A re-keyed GLEIF record merges with its register record; its old ID stays as a tombstone row."""

import json

import pyarrow.parquet as pq
from lc_core import tombstone
from lc_core.directory_id import mint
from lc_core.publishing import load

from lc_publish.snapshot import STATE_FILES, StateReader, merge
from lc_publish.snapshot import build as build_v2

SETTINGS = load().snapshots
LEI, PARENT_LEI = "5493000OREGON0000001", "5493000PARENT0000009"
OLD, NEW = mint("US", "RA000631", "057491-14"), mint("US", "RA000631", "5749114")
PARENT = mint("US", "RA000599", "20201234567")
REGISTER = {"directoryId": NEW, "legalName": "PRECISION CASTPARTS CORP.", "legalStatus": "Active",
            "registrationAuthority": "US-OR-SOS", "registrationNumber": "5749114", "legalJurisdiction": "US-OR",
            "legalAddress": {"country": "US", "city": "PORTLAND"}}
GLEIF = {"directoryId": OLD, "lei": LEI, "legalName": "PRECISION CASTPARTS CORP", "legalStatus": "ACTIVE",
         "registrationAuthority": "RA000631", "registrationNumber": "057491-14", "legalJurisdiction": "US-OR",
         "legalAddress": {"country": "US", "city": "PORTLAND", "addressLines": ["4650 SW MACADAM AVE"]}}
GLEIF_PARENT = {"directoryId": PARENT, "lei": PARENT_LEI, "legalName": "PARENT CO", "legalJurisdiction": "US-CO",
                "legalAddress": {"country": "US"}}
RELS = {"R1": {"startNode": LEI, "endNode": PARENT_LEI, "relationshipType": "IS_DIRECTLY_CONSOLIDATED_BY",
               "relationshipStatus": "ACTIVE"}}


FILLER = {mint("US", "RA000631", str(100000 + i)): {"directoryId": mint("US", "RA000631", str(100000 + i)),
                                                    "legalName": f"OREGON CO {i}", "legalJurisdiction": "US-OR"}
          for i in range(20)}  # so the day's changes stay under full_when_changes_exceed


def before():
    return {"us-or-sos": {NEW: REGISTER, **FILLER}, "gleif": {OLD: GLEIF, PARENT: GLEIF_PARENT}}


def after():
    """As lc_ingest.rekey leaves the gleif store: the record under its new ID, a tombstone at the old one."""
    return {"us-or-sos": {NEW: REGISTER, **FILLER},
            "gleif": {NEW: {**GLEIF, "directoryId": NEW}, OLD: tombstone.make(GLEIF, NEW), PARENT: GLEIF_PARENT}}


def rows(out, m):
    found = {}
    for p in m["parts"]:
        if p["kind"] == "entities":
            for r in pq.read_table(out / "parts" / p["name"]).to_pylist():
                found[r["directoryId"]] = r
    return found


def test_the_moved_record_merges_and_the_tombstone_stands_alone():
    merged = merge(after())
    assert merged[NEW]["sources"] == ["us-or-sos", "gleif"] and merged[NEW]["lei"] == LEI
    assert merged[NEW]["legalName"] == "PRECISION CASTPARTS CORP." and merged[NEW]["statusCategory"] == "active"
    grave = merged[OLD]
    assert grave["sources"] == ["gleif"] and grave["statusCategory"] == "merged" and "lei" not in grave
    assert grave["labels"] == [f"merged:{NEW}"] and "legalStatus" not in grave


def test_a_tombstone_never_merges_and_a_live_record_keeps_its_id():
    s = after()
    s["us-or-sos"][OLD] = {**REGISTER, "directoryId": OLD, "registrationNumber": "5749114X"}
    merged = merge(s)
    assert merged[OLD]["sources"] == ["us-or-sos"] and not tombstone.is_tombstone(merged[OLD])
    assert merged[OLD]["statusCategory"] == "active"


def test_the_rekey_day_changes_two_rows_deletes_none_and_relationships_follow(tmp_path):
    day1, day2 = tmp_path / "d1", tmp_path / "d2"
    m1 = build_v2(before(), day1, "2026-11-21", settings=SETTINGS, relationships=RELS)
    r1 = rows(day1, m1)
    assert r1[OLD]["lei"] == LEI and r1[NEW]["sources"] == "us-or-sos"  # apart before
    rel1 = pq.read_table(next(day1 / "parts" / p["name"] for p in m1["parts"] if p["kind"] == "relationships")).to_pylist()
    assert rel1[0]["startId"] == OLD

    state = {kind: StateReader(day1 / "state" / name) for kind, name in STATE_FILES.items()}
    m2 = build_v2(after(), day2, "2026-11-22", settings=SETTINGS, previous=m1, state=state, relationships=RELS)
    assert m2["kind"] == "changes" and m2["records"] == m1["records"] == 23
    assert {k: m2["changes"][k] for k in ("added", "changed", "deleted", "relationshipsChanged")} == {
        "added": 0, "changed": 2, "deleted": 0, "relationshipsChanged": 1}
    changes = {r["directoryId"]: r for r in pq.read_table(day2 / "day" / "changes.000.parquet").to_pylist()}
    assert changes[NEW]["op"] == "change" and changes[NEW]["sources"] == "us-or-sos,gleif" and changes[NEW]["lei"] == LEI
    grave = changes[OLD]
    assert grave["op"] == "change" and grave["statusCategory"] == "merged" and grave["lei"] is None
    assert json.loads(grave["labels"]) == [f"merged:{NEW}"] and grave["country"] == "US"
    rel = pq.read_table(day2 / "day" / "relationship-changes.000.parquet").to_pylist()
    assert rel[0]["startId"] == NEW and rel[0]["endId"] == PARENT  # the LEI now resolves to the merged record
    by_category = {s["value"]: s["records"] for s in m2["stats"]["byStatusCategory"]}
    assert by_category["merged"] == 1
    assert m2["columns"] == m1["columns"]  # no new column: the day is not forced to a full snapshot
