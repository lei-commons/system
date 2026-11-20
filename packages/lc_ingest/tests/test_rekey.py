"""The GLEIF re-key: aliases reach every stored record once, and every old ID keeps a tombstone."""

import gzip
import json

from lc_core import gleif_identity, tombstone
from lc_core.control import SystemHalted
from lc_core.directory_id import mint
from lc_core.guard import GuardSettings, MappingUpgrade
from lc_ingest.gleif import lei_id, to_record
from lc_ingest.rekey import marker_name, publish_key, rekey
from lc_ingest.store import decode_records, shard_of

IDENTITY = gleif_identity.load()
GUARD = GuardSettings(0.1, 100)


class MemStore:
    def __init__(self):
        self.manifest, self.shards, self.changes, self.markers = {"records": 0, "processed": []}, {}, {}, {}

    def read_manifest(self):
        return json.loads(json.dumps(self.manifest))

    def write_manifest(self, m):
        self.manifest = m

    def read_shard(self, n):
        return dict(self.shards.get(n, {}))

    def write_shard_file(self, n, path):
        with open(path, "rb") as f:
            self.shards[n] = decode_records(f.read())

    def write_changes_file(self, name, path):
        with gzip.open(path, "rt") as f:
            self.changes[name] = [json.loads(line) for line in f]

    def has_marker(self, name):
        return name in self.markers

    def write_marker(self, name, body):
        self.markers[name] = body

    def put(self, record):
        self.shards.setdefault(shard_of(record["directoryId"]), {})[record["directoryId"]] = record
        self.manifest["records"] += 1

    def get(self, directory_id):
        return self.shards.get(shard_of(directory_id), {}).get(directory_id)

    def all(self):
        return {k: v for shard in self.shards.values() for k, v in shard.items()}


def stored(lei, authority, number, country, name="EXAMPLE", **extra):
    """A GLEIF record as the store held it before gleif_identity: minted as GLEIF writes it."""
    record = {"directoryId": mint(country, authority, number), "lei": lei, "legalName": name, "registrationAuthority": authority,
              "registrationNumber": number, "legalJurisdiction": country, "legalStatus": "ACTIVE",
              "legalAddress": {"country": country, "city": "SOMEWHERE", "addressLines": ["1 Main Street"]}}
    return {**record, **extra}


OR = stored("5493000OREGON0000001", "RA000631", "057491-14", "US", "PRECISION CASTPARTS CORP.")
CA = stored("5493000CANADA0000002", "RA000072", "046311-6", "CA", "EXAMPLE CANADA INC.")
NO = stored("5493000NORWAY0000003", "RA000473", "967 675 040", "NO", "EKSEMPEL AS")
JP = stored("5493000JAPAN00000004", "RA000412", "0104-01-016070", "JP", "例株式会社")
GB = stored("5493001KJTIIGC8Y1R12", "RA000585", "01234567", "GB", "EXAMPLE LIMITED")
GB["directoryId"] = mint("GB", "GB-COH", "01234567")
NEW = {OR["lei"]: mint("US", "RA000631", "5749114"), CA["lei"]: mint("CA", "RA000072", "463116"),
       NO["lei"]: mint("NO", "RA000472", "967675040"), JP["lei"]: mint("JP", "RA001075", "3010401016070")}


def filler(n):
    return [stored(f"5493FILL{i:012d}", "RA000599", f"2020{i:07d}", "US") for i in range(n)]


def store_with(*records, fill=200):
    store = MemStore()
    for record in list(records) + filler(fill):
        store.put(dict(record))
    return store


def events():
    out = []
    return out, out.append


def test_the_gleif_mapping_now_gives_the_registers_ids():
    def raw(authority, number, country):
        return {"attributes": {"lei": "5493000TEST000000001", "entity": {
            "legalName": {"name": "X"}, "legalAddress": {"country": country}, "registeredAt": {"id": authority},
            "registeredAs": number}}}

    from lc_ingest.bulk import ca_record
    from lc_ingest.socrata import or_record

    oregon = or_record({"registry_number": "5749114", "entity_name": "PRECISION CASTPARTS CORP.",
                        "entity_kind": "FOREIGN BUSINESS CORPORATION"})
    assert to_record(raw("RA000631", "057491-14", "US"))["directoryId"] == oregon["directoryId"]
    canada = ca_record({"Corporation number": "463116", "Corporate name - form 1": "EXAMPLE CANADA INC."})
    assert to_record(raw("RA000072", "046311-6", "CA"))["directoryId"] == canada["directoryId"]
    assert to_record(raw("RA000473", "967 675 040", "NO"))["directoryId"] == mint("NO", "RA000472", "967675040")
    assert to_record(raw("RA000412", "0104-01-016070", "JP"))["directoryId"] == mint("JP", "RA001075", "3010401016070")
    assert to_record(raw("RA000585", "01234567", "FR"))["directoryId"] == GB["directoryId"]  # unchanged


def test_a_rekey_moves_records_and_leaves_tombstones():
    store = store_with(OR, CA, NO, JP, GB)
    log, note = events()
    assert rekey(store, identity=IDENTITY, settings=GUARD, log=note) == "done"
    for old in (OR, CA, NO, JP):
        new = NEW[old["lei"]]
        moved, grave = store.get(new), store.get(old["directoryId"])
        assert moved == {**old, "directoryId": new}  # every field kept, under its new ID
        assert grave == {"directoryId": old["directoryId"], "legalName": old["legalName"],
                         "legalJurisdiction": old["legalJurisdiction"], "registrationAuthority": old["registrationAuthority"],
                         "registrationNumber": old["registrationNumber"], "legalAddress": {"country": old["legalJurisdiction"]},
                         "labels": [f"merged:{new}"]}
        assert tombstone.merged_into(grave) == new and "lei" not in grave
    assert store.get(GB["directoryId"]) == GB  # an unchanged mapping: untouched
    [summary] = [e for e in log if e["event"] == "gleif-rekeyed"]
    assert summary["movedByAuthority"] == {"RA000072": 1, "RA000412": 1, "RA000473": 1, "RA000631": 1}
    assert {k: summary[k] for k in ("moved", "tombstones", "added", "thereAlready", "keptApart", "total")} == {
        "moved": 4, "tombstones": 4, "added": 4, "thereAlready": 0, "keptApart": 0, "total": 209}
    assert summary["rules"] == IDENTITY.fingerprint and store.markers[marker_name(IDENTITY.fingerprint)] == summary
    assert store.manifest["records"] == len(store.all()) == 209
    changes = store.changes[marker_name(IDENTITY.fingerprint)]
    assert {c["directoryId"] for c in changes} == {OR["directoryId"], CA["directoryId"], NO["directoryId"], JP["directoryId"],
                                                  *NEW.values()}
    assert all(c["maker"] == "ingestion@gleif" and c["sourceIds"] == [f"src:gleif-rekey-{IDENTITY.fingerprint}"]
               for c in changes)


def test_a_second_run_does_nothing_and_a_cut_short_run_finishes():
    store = store_with(OR, CA, NO, JP)
    rekey(store, identity=IDENTITY, settings=GUARD, log=lambda e: None)
    before = json.dumps(store.all(), sort_keys=True)
    log, note = events()
    assert rekey(store, identity=IDENTITY, settings=GUARD, log=note) == "already-done" and log == []
    store.markers.clear()  # as if the run stopped after its shards, before its marker
    assert rekey(store, identity=IDENTITY, settings=GUARD, log=note) == "done"
    assert json.dumps(store.all(), sort_keys=True) == before
    assert log[-1]["moved"] == 0 and log[-1]["total"] == len(store.all())


def test_a_record_already_at_its_new_id_stays_and_the_old_one_becomes_a_tombstone():
    store = store_with(OR)
    fresher = {**OR, "directoryId": NEW[OR["lei"]], "legalName": "PRECISION CASTPARTS CORP. (NEWER)"}
    store.put(fresher)  # the day's file, mapped with the new rules, got there first
    log, note = events()
    rekey(store, identity=IDENTITY, settings=GUARD, log=note)
    assert store.get(NEW[OR["lei"]]) == fresher and tombstone.merged_into(store.get(OR["directoryId"])) == NEW[OR["lei"]]
    assert log[-1]["thereAlready"] == 1 and log[-1]["added"] == 0


def test_another_lei_on_the_new_id_is_kept_apart_and_flagged():
    store = store_with(NO)
    holder = stored("5493000NORWAYHOLDER9", "RA000472", "967675040", "NO", "EKSEMPEL HOLDING AS")
    store.put(holder)
    log, note = events()
    rekey(store, identity=IDENTITY, settings=GUARD, log=note)
    target = NEW[NO["lei"]]
    assert store.get(target) == holder  # the LEI that held the ID keeps it
    apart = lei_id("NO", NO["lei"])
    assert store.get(apart)["labels"] == [f"possible-duplicate:{target}"] and store.get(apart)["lei"] == NO["lei"]
    assert tombstone.merged_into(store.get(NO["directoryId"])) == apart
    assert log[-1]["keptApart"] == 1


def test_a_record_kept_apart_follows_the_id_it_was_kept_apart_from():
    shared = mint("JP", "RA000412", "0104-01-016070")
    other = {**JP, "lei": "5493000JAPANOTHER005", "directoryId": lei_id("JP", "5493000JAPANOTHER005"),
             "labels": [f"possible-duplicate:{shared}"]}
    store = store_with(JP, other)
    rekey(store, identity=IDENTITY, settings=GUARD, log=lambda e: None)
    target = NEW[JP["lei"]]
    assert store.get(target)["lei"] == JP["lei"]
    assert store.get(other["directoryId"])["labels"] == [f"possible-duplicate:{target}"]  # same ID, label updated


def test_too_many_moves_are_held_unless_the_owner_approved_them():
    store = store_with(OR, CA, NO, JP, fill=20)  # 4 of 24 records would move: over a tenth
    small = GuardSettings(0.1, 10)
    log, note = events()
    assert rekey(store, identity=IDENTITY, settings=small, log=note) == "held"
    assert log == [{"event": "held", "source": "gleif", "publish": publish_key(IDENTITY.fingerprint),
                    "reason": "4 of 24 records changed"}]
    assert not store.markers and store.get(OR["directoryId"]) == OR
    approved = GuardSettings(0.1, 10, (MappingUpgrade("gleif", publish_key(IDENTITY.fingerprint), "aliases", "owner",
                                                      "2026-11-20"),))
    assert rekey(store, identity=IDENTITY, settings=approved, log=note) == "done"
    assert log[1]["event"] == "mapping-upgrade" and log[1]["changed"] == 4 and log[-1]["event"] == "gleif-rekeyed"


def test_nothing_moves_while_the_system_is_halted():
    def halted():
        raise SystemHalted("paused by the owner")

    store = store_with(OR)
    log, note = events()
    assert rekey(store, identity=IDENTITY, settings=GUARD, gate=halted, log=note) == "halted"
    assert log[0]["event"] == "halted" and not store.markers and store.get(OR["directoryId"]) == OR


def test_the_ledger_lines_rebuild_the_rekeyed_store():
    from lc_ingest.ledger import apply

    store = store_with(OR, CA, NO, JP, fill=20)
    before = store.all()
    rekey(store, identity=IDENTITY, settings=GUARD, log=lambda e: None)
    rebuilt = dict(before)
    by_record = {}
    for change in store.changes[marker_name(IDENTITY.fingerprint)]:
        by_record.setdefault(change["directoryId"], []).append(change)
    for directory_id, changes in by_record.items():
        rebuilt[directory_id] = {**apply(rebuilt.get(directory_id, {}), changes), "directoryId": directory_id}
    assert rebuilt == store.all()


def test_the_rekey_is_a_decision_in_the_log():
    from lc_core.decisions import _event

    store = store_with(OR, CA, NO, JP)
    log, note = events()
    rekey(store, identity=IDENTITY, settings=GUARD, log=note)
    event = _event("job:ingest-gleif", log[-1])
    assert event.decision == "done" and event.subject == f"gleif rules {IDENTITY.fingerprint}"
    assert "4 records moved" in event.reason and "RA000412 1" in event.reason and "4 old IDs kept" in event.reason
    assert event.refs["moved_RA000631"] == 1 and event.refs["tombstones"] == 4
