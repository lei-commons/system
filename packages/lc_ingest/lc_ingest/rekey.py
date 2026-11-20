"""Re-key the stored GLEIF records when the identity rules change (owner decision 2026-11-20).

config/sources.yaml ``gleif_identity`` (lc_core.gleif_identity) says how GLEIF's registration authority
and number become a directory ID. A day's file only holds the LEIs that changed, so new rules would reach
the rest of the store only when each LEI next changes. This pass applies them to every stored record,
once per set of rules (its fingerprint), and is safe to run again: a finished pass leaves a marker, and an
unfinished one finds its own work done and does only the rest.

For each stored record whose ID the rules now give differently:
- it moves to its new ID. If another LEI already holds that ID, the two are kept apart as on any day
  (Article 4.2): this one gets its own LEI-based ID and a ``possible-duplicate:<ID>`` label;
- its old ID keeps a tombstone (lc_core.tombstone): the name and registration it was published with and
  the label ``merged:<new ID>``. Nothing published disappears;
- if the new ID already holds this LEI (the day's file got there first), that record stays as it is.

Three passes over the shards, one shard in memory at a time: find the records that move, read the shards
they move to (to keep collisions apart), then write. Ledger change lines go to changes/rekey-<fingerprint>,
and the store's record count is recounted. A share of moves above the volume guard holds the pass unless
the owner approved it (config/sources.yaml volume_guard.mapping_upgrades, publication "rekey <fingerprint>").
The run logs one decision, ``gleif-rekeyed``, with counts per authority.
"""

from __future__ import annotations

import gzip
import json
import os
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from lc_core import gleif_identity, guard, tombstone
from lc_core.control import SystemHalted

from .gleif import lei_id, record_country, stored_id
from .ledger import VolumeGuardTripped, diff, volume_guard
from .store import SHARDS, encode_records, shard_of

SOURCE = "gleif"
DUPLICATE = "possible-duplicate:"


def marker_name(fingerprint: str) -> str:
    return f"rekey-{fingerprint}"


def publish_key(fingerprint: str) -> str:
    """The publication key the volume guard and mapping_upgrades know this pass by."""
    return f"rekey {fingerprint}"


def _intended(record: dict) -> str:
    """The register-based ID a stored record stands for: its own, or the one it was kept apart from."""
    for label in record.get("labels") or []:
        if label.startswith(DUPLICATE):
            return label[len(DUPLICATE):]
    return record["directoryId"]


@dataclass
class _Move:
    record: dict  # as stored
    target: str  # the register-based ID the rules now give
    final: str = None  # where it ends up: target, or its LEI-based ID when another LEI holds target
    there_already: bool = False  # the final ID already holds this LEI's record

    @property
    def old(self) -> str:
        return self.record["directoryId"]

    @property
    def lei(self) -> str:
        return self.record["lei"]


@dataclass
class _Plan:
    tombstones: dict = field(default_factory=lambda: defaultdict(dict))  # shard -> {old id: new id}
    records: dict = field(default_factory=lambda: defaultdict(dict))  # shard -> {id: record to store}


def _find(store, identity, shards: int):
    """Pass 1: every stored record the rules now key differently, and the store's record count."""
    moves, total = [], 0
    for n in range(shards):
        for record in store.read_shard(n).values():
            total += 1
            if not record.get("lei") or tombstone.is_tombstone(record):
                continue
            target = stored_id(record, identity)
            if target != _intended(record):
                moves.append(_Move(record, target))
    return moves, total


def _resolve(store, moves: list, counts: Counter) -> None:
    """Pass 2: where each record lands. A record leaving an ID doesn't hold it; the first LEI (in order) to
    claim a free ID gets it; any other LEI is kept apart under its own LEI-based ID, flagged."""
    leaving = {m.old for m in moves}
    owners = {}
    by_shard = defaultdict(set)
    for m in moves:
        by_shard[shard_of(m.target)].add(m.target)
    for n in sorted(by_shard):
        current = store.read_shard(n)
        for target in by_shard[n]:
            held = current.get(target)
            if held and held.get("lei") and target not in leaving and not tombstone.is_tombstone(held):
                owners[target] = (held["lei"], True)
    landed = set()
    for m in sorted(moves, key=lambda m: (m.target, m.lei, m.old)):
        owner, stored = owners.setdefault(m.target, (m.lei, False))
        if owner == m.lei:
            m.final, m.there_already = m.target, stored
        else:
            m.final = lei_id(record_country(m.record), m.lei)
            counts["kept apart: shared registration"] += 1
        if m.final in landed:  # a second stored copy of one LEI going the same way: the first one lands
            m.there_already = True
        landed.add(m.final)


def _plan(moves: list, counts: Counter) -> _Plan:
    plan = _Plan()
    for m in moves:
        authority = m.record.get("registrationAuthority") or "none"
        labels = [x for x in m.record.get("labels") or [] if not x.startswith(DUPLICATE)]
        if m.final != m.target:
            labels.append(DUPLICATE + m.target)
        moved = {**m.record, "directoryId": m.final}
        moved.pop("labels", None)
        if labels:
            moved["labels"] = sorted(set(labels))
        if m.final == m.old:  # kept apart where it already was: only its label changes
            plan.records[shard_of(m.old)][m.old] = moved
            counts[f"relabelled {authority}"] += 1
            continue
        plan.tombstones[shard_of(m.old)][m.old] = m.final
        counts[f"moved {authority}"] += 1
        if m.there_already:
            counts["there already"] += 1
        else:
            plan.records[shard_of(m.final)][m.final] = moved
    return plan


def rekey(store, *, identity=None, settings=None, gate=None, log=print, maker: str = "ingestion@gleif",
          shards: int = SHARDS) -> str:
    """Re-key ``store`` (the gleif entity store) under ``identity`` once. Returns done, already-done,
    halted or held."""
    identity = identity or gleif_identity.default()  # the rules the mapping uses
    fingerprint = identity.fingerprint
    if gate is not None:
        try:
            gate()
        except SystemHalted as exc:
            log({"event": "halted", "source": SOURCE, "reason": str(exc)})
            return "halted"
    settings = settings or guard.load()
    manifest = store.read_manifest()  # first: it decides whether the shards are current
    if store.has_marker(marker_name(fingerprint)):
        return "already-done"

    counts = Counter()
    moves, total = _find(store, identity, shards)
    key = publish_key(fingerprint)
    try:
        volume_guard(len(moves), total, settings)
    except VolumeGuardTripped as exc:
        upgrade = settings.upgrade_for(SOURCE, key)
        if upgrade is None:
            log({"event": "held", "source": SOURCE, "publish": key, "reason": str(exc)})
            return "held"
        log({"event": "mapping-upgrade", "source": SOURCE, "publish": key, "changed": len(moves), "total": total,
             "max_fraction": settings.max_fraction, "reason": upgrade.reason, "approved_by": upgrade.approved_by,
             "approved_on": upgrade.approved_on})
    _resolve(store, moves, counts)
    plan = _plan(moves, counts)

    source_id = f"src:{SOURCE}-{key.replace(' ', '-')}"
    added = tombstones = written = 0
    staged = {}
    with tempfile.TemporaryDirectory(prefix="rekey-") as work:
        changes_path = os.path.join(work, "changes.jsonl.gz")
        with gzip.open(changes_path, "wt", encoding="utf-8") as out:
            for n in sorted(set(plan.tombstones) | set(plan.records)):
                current = store.read_shard(n)
                updated = {}
                for old, into in plan.tombstones.get(n, {}).items():
                    if old in plan.records.get(n, {}):
                        continue  # a record arrives at this ID: it holds it, and no tombstone is needed
                    updated[old] = tombstone.make(current[old], into)
                    tombstones += 1
                for directory_id, record in plan.records.get(n, {}).items():
                    if directory_id not in current:
                        added += 1
                    updated[directory_id] = record
                for directory_id, record in updated.items():
                    found = diff(current.get(directory_id, {"directoryId": directory_id}), record, source_id, maker)
                    for change in found:
                        out.write(json.dumps(change, sort_keys=True) + "\n")
                    written += len(found)
                staged[n] = os.path.join(work, f"{n:03d}.jsonl.gz")
                with open(staged[n], "wb") as f:
                    f.write(encode_records({**current, **updated}))
        # As every ingestion run (lc_ingest.runner): the changes, then the shards, then the manifest.
        if written:
            store.write_changes_file(marker_name(fingerprint), changes_path)
        for n in sorted(staged):
            store.write_shard_file(n, staged[n])
    manifest["records"] = total + added
    store.write_manifest(manifest)
    by_authority = {k.split(" ", 1)[1]: v for k, v in sorted(counts.items()) if k.startswith("moved ")}
    relabelled = {k.split(" ", 1)[1]: v for k, v in sorted(counts.items()) if k.startswith("relabelled ")}
    summary = {"event": "gleif-rekeyed", "source": SOURCE, "rules": fingerprint, "moved": sum(by_authority.values()),
               "movedByAuthority": by_authority, "tombstones": tombstones, "added": added,
               "thereAlready": counts["there already"], "keptApart": counts["kept apart: shared registration"],
               "relabelled": sum(relabelled.values()), "changes": written, "total": manifest["records"]}
    store.write_marker(marker_name(fingerprint), summary)  # last: a pass cut short runs again and finishes
    log(summary)
    return "done"
