import json

import pyarrow.parquet as pq
import pytest

from lc_publish.merkle import leaf, root
from lc_publish.snapshot import build, country_of, merge

GB_ID = "LECOZZFZ6LZCOJBDKT73"
CH = {"directoryId": GB_ID, "legalName": "EXAMPLE LIMITED", "legalStatus": "Active", "legalJurisdiction": "GB",
      "registrationAuthority": "GB-COH", "registrationNumber": "01234567",
      "legalAddress": {"country": "GB", "city": "LONDON"}, "industryCodes": ["62012"]}
GLEIF_SAME = {"directoryId": GB_ID, "lei": "5493001KJTIIGC8Y1R12", "legalName": "EXAMPLE LTD", "legalStatus": "ACTIVE",
              "legalJurisdiction": "GB", "registrationAuthority": "RA000585", "registrationNumber": "01234567"}
GLEIF_US = {"directoryId": "LECOZZ000000000000XX", "lei": "529900T8BM49AURSDO55", "legalName": "US CO INC",
            "legalJurisdiction": "US-DE", "legalAddress": {"country": "US"}}


def sources():
    return {"gb-coh": {GB_ID: CH}, "gleif": {GB_ID: GLEIF_SAME, GLEIF_US["directoryId"]: GLEIF_US}}


def test_register_values_win_and_gleif_adds_the_lei():
    m = merge(sources())[GB_ID]
    assert m["legalName"] == "EXAMPLE LIMITED" and m["legalStatus"] == "Active" and m["registrationAuthority"] == "GB-COH"
    assert m["lei"] == "5493001KJTIIGC8Y1R12" and m["sources"] == ["gb-coh", "gleif"]


def test_merge_leaves_the_inputs_alone():
    s = sources()
    merge(s)
    assert "sources" not in s["gb-coh"][GB_ID] and "lei" not in s["gb-coh"][GB_ID]


@pytest.mark.parametrize("record,country", [({"legalJurisdiction": "US-DE"}, "US"), ({"legalJurisdiction": "GB"}, "GB"),
                                           ({"legalAddress": {"country": "FR"}}, "FR"), ({}, "XX"),
                                           ({"legalAddress": {"country": "UNITED STATES"}}, "XX")])
def test_country_of(record, country):
    assert country_of(record) == country


def test_build_writes_one_file_per_country_and_a_manifest(tmp_path):
    m = build(sources(), tmp_path, "2026-11-07")
    assert m["records"] == 2 and m["inMoreThanOneSource"] == 1 and m["recordsPerSource"] == {"gb-coh": 1, "gleif": 2}
    assert [f["name"] for f in m["files"]] == ["GB.parquet", "US.parquet"]
    assert json.loads((tmp_path / "manifest.json").read_text()) == m
    notice = (tmp_path / "NOTICE.txt").read_text()
    assert "Open Government Licence v3.0" in notice and "not affiliated with gleif" in notice.lower()


def test_parquet_round_trips(tmp_path):
    build(sources(), tmp_path, "2026-11-07")
    (row,) = pq.read_table(tmp_path / "GB.parquet").to_pylist()
    assert row["lei"] == "5493001KJTIIGC8Y1R12" and json.loads(row["industryCodes"]) == ["62012"]
    assert row["sources"] == "gb-coh,gleif" and json.loads(row["legalAddress"]) == {"city": "LONDON", "country": "GB"}


def test_same_input_same_roots(tmp_path):
    a = build(sources(), tmp_path / "a", "2026-11-07")
    b = build(sources(), tmp_path / "b", "2026-11-07")
    assert a["snapshotRoot"] == b["snapshotRoot"] and [f["root"] for f in a["files"]] == [f["root"] for f in b["files"]]


def test_any_change_changes_the_root(tmp_path):
    a = build(sources(), tmp_path / "a", "2026-11-07")
    s = sources()
    s["gb-coh"][GB_ID] = {**CH, "legalStatus": "Dissolved"}
    assert build(s, tmp_path / "b", "2026-11-07")["snapshotRoot"] != a["snapshotRoot"]


def test_merkle_root_is_order_sensitive_and_handles_odd_and_empty():
    leaves = [leaf({"a": i}) for i in range(3)]
    assert root(leaves) != root(list(reversed(leaves)))
    assert root([]).startswith("sha256:") and root(leaves[:1]) != root([])


def test_nothing_outside_the_columns_is_published(tmp_path):
    s = sources()
    s["gb-coh"][GB_ID] = {**CH, "careOf": "MR PERSON"}
    build(s, tmp_path, "2026-11-07")
    assert "MR PERSON" not in json.dumps(pq.read_table(tmp_path / "GB.parquet").to_pylist())


def test_shard_function_matches_ingestion():
    import hashlib

    from lc_publish.shards import shard_of

    for i in range(500):
        directory_id = f"LECOZZ{i:012d}XX"
        assert shard_of(directory_id) == hashlib.sha256(directory_id.encode()).digest()[0] % 256


def test_sharded_build_merges_across_sources(tmp_path):
    from lc_publish.shards import shard_of
    from lc_publish.snapshot import build_sharded

    s = sources()
    readers = {sid: (lambda n, recs=recs: {k: v for k, v in recs.items() if shard_of(k) == n}) for sid, recs in s.items()}
    m = build_sharded(readers, tmp_path / "a", "2026-11-07")
    assert m["records"] == 2 and m["inMoreThanOneSource"] == 1 and m == build(s, tmp_path / "b", "2026-11-07")


def test_many_records_split_across_shards_and_countries(tmp_path):
    many = {f"LECOZZ{i:012d}XX": {"directoryId": f"LECOZZ{i:012d}XX", "legalName": f"CO {i}",
                                   "legalJurisdiction": "GB" if i % 3 else "US"} for i in range(3000)}
    m = build({"gb-coh": many}, tmp_path, "2026-11-07")
    assert m["records"] == 3000 and {f["country"]: f["records"] for f in m["files"]} == {"GB": 2000, "US": 1000}
    assert pq.read_table(tmp_path / "GB.parquet").num_rows == 2000


def test_relationships_are_published_with_resolved_ends(tmp_path):
    from lc_publish.shards import shard_of
    from lc_publish.snapshot import build_sharded

    rels = {"REL1": {"directoryId": "REL1", "startNode": "529900T8BM49AURSDO55", "endNode": "5493001KJTIIGC8Y1R12",
                     "relationshipType": "IS_DIRECTLY_CONSOLIDATED_BY", "relationshipStatus": "ACTIVE", "percentage": 75.5},
            "REL2": {"directoryId": "REL2", "startNode": "529900T8BM49AURSDO55", "endNode": "UNKNOWNLEI0000000000",
                     "relationshipType": "IS_ULTIMATELY_CONSOLIDATED_BY"}}
    s = sources()
    readers = {sid: (lambda n, recs=recs: {k: v for k, v in recs.items() if shard_of(k) == n}) for sid, recs in s.items()}
    m = build_sharded(readers, tmp_path, "2026-11-07",
                      relationships=lambda n: {k: v for k, v in rels.items() if shard_of(k) == n})
    rel_file = [f for f in m["files"] if f.get("kind") == "relationships"][0]
    assert m["relationships"] == 2 and rel_file["bothEndsInDirectory"] == 1
    rows = {r["relationshipId"]: r for r in pq.read_table(tmp_path / "relationships.parquet").to_pylist()}
    assert rows["REL1"]["startId"] == "LECOZZ000000000000XX" and rows["REL1"]["endId"] == GB_ID
    assert rows["REL1"]["percentage"] == "75.5" and rows["REL2"]["endId"] is None


def test_relationships_change_the_snapshot_root(tmp_path):
    from lc_publish.snapshot import build_sharded

    s = sources()
    readers = {sid: (lambda n, recs=recs: dict(recs) if n == 0 else {}) for sid, recs in s.items()}
    without = build_sharded(readers, tmp_path / "a", "2026-11-07", shards=1)
    rel = {"REL1": {"directoryId": "REL1", "startNode": "A" * 20, "endNode": "B" * 20, "relationshipType": "X"}}
    withrel = build_sharded(readers, tmp_path / "b", "2026-11-07", shards=1, relationships=lambda n: dict(rel))
    assert without["snapshotRoot"] != withrel["snapshotRoot"]
