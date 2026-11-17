"""The snapshot format (schemaVersion 2), shared by build-snapshot, load-db, the site and the rebuild tool.

See docs/snapshots-v2.md. Each day has a manifest. A day either is a full snapshot (its parts
hold the whole state) or holds only changes against its ``base``, the previous day. Every day
names its ``anchor``, the full snapshot its state starts from. The parts of full snapshots are
content-addressed under ``parts/`` and shared across days.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date as Date

SCHEMA_VERSION = 2
SHARDS = 256
OPS = ("add", "change", "delete")
ENTITY_KIND, RELATIONSHIP_KIND = "entities", "relationships"
CHANGES_KIND, RELATIONSHIP_CHANGES_KIND = "changes", "relationship-changes"
RELATIONSHIP_GROUP = "relationships"
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PART = re.compile(r"^(?:(?P<country>[A-Z]{2})\.(?P<sources>[a-z0-9-]+(?:_[a-z0-9-]+)*)|relationships)"
                  r"\.p(?P<part>\d{1,3})of(?P<of>\d{1,3})\.parquet$")
OBJECT = re.compile(r"^parts/[0-9a-f]{64}\.parquet$")
CHANGE_FILE = re.compile(r"^(?:changes|relationship-changes)\.\d{3}\.parquet$")


class FormatError(ValueError):
    """A manifest or a chain of manifests isn't what the format promises."""


def shard_of(directory_id: str) -> int:
    """The same as lc_ingest.store.shard_of and lc_publish.shards.shard_of (a test checks)."""
    return hashlib.sha256(directory_id.encode()).digest()[0] % SHARDS


def group_of(country: str, sources) -> str:
    """The part group: country and the record's exact sources, e.g. ``GB.gb-coh_gleif``."""
    sources = [s for s in (sources.split(",") if isinstance(sources, str) else sources) if s]
    if not sources or not all(SOURCE_ID.match(s) for s in sources):
        raise FormatError(f"bad sources {sources!r}")
    if not re.fullmatch(r"[A-Z]{2}", country or ""):
        raise FormatError(f"bad country {country!r}")
    return f"{country}.{'_'.join(sources)}"


def part_name(group: str, part: int, of: int) -> str:
    if not (0 <= part < of <= SHARDS):
        raise FormatError(f"bad part {part} of {of}")
    name = f"{group}.p{part}of{of}.parquet"
    if not PART.match(name):
        raise FormatError(f"bad part name {name}")
    return name


def parts_for(count: int, target: int) -> int:
    """How many parts a group of ``count`` records gets: 1, 2, 4, ... up to SHARDS."""
    n = 1
    while n < SHARDS and count > n * target:
        n *= 2
    return n


def part_of(group: str, shard: int, layout: dict) -> str:
    of = layout.get(group, 1)
    return part_name(group, shard % of, of)


def is_v2(manifest: dict) -> bool:
    return isinstance(manifest, dict) and manifest.get("schemaVersion", 1) >= SCHEMA_VERSION


def check_manifest(m: dict) -> dict:
    """Light structural checks of a v2 manifest; raises FormatError."""
    if not is_v2(m):
        raise FormatError("not a v2 manifest")
    for key in ("date", "kind", "anchor", "snapshotRoot", "records", "relationships", "parts", "changes", "files"):
        if key not in m:
            raise FormatError(f"manifest lacks {key}")
    if m["kind"] not in ("full", "changes") or not DATE.match(m["date"]) or not DATE.match(m["anchor"]):
        raise FormatError("manifest kind or dates malformed")
    if m["kind"] == "full" and m["anchor"] != m["date"]:
        raise FormatError("a full snapshot is its own anchor")
    base = m.get("base")
    if m["kind"] == "changes" and not base:
        raise FormatError("a changes day needs a base")
    if base and (not DATE.match(base.get("date", "")) or base["date"] >= m["date"] or not base.get("snapshotRoot")):
        raise FormatError("manifest base malformed")
    if m["anchor"] > m["date"]:
        raise FormatError("anchor after the day")
    for p in m["parts"]:
        if not PART.match(p.get("name", "")):
            raise FormatError(f"bad part name {p.get('name')!r}")
        if m["kind"] == "full" and not OBJECT.match(p.get("object", "")):
            raise FormatError(f"full snapshot part without an object: {p['name']}")
    for f in m["files"]:
        obj = f.get("object", "")
        if not (OBJECT.match(obj) or obj == f"{m['date']}/{f.get('name')}"):
            raise FormatError(f"bad object for {f.get('name')!r}")
    return m


class TooFar(FormatError):
    """The walk passed its limit before reaching where it was going."""


def back_to(read_manifest, manifest: dict, stop, limit: int = None) -> list:
    """Walk ``base`` links from ``manifest`` until ``stop(m)`` is true; return the walk, oldest first.

    ``read_manifest(date)`` returns that day's manifest or None. Raises FormatError if the
    chain breaks before ``stop`` is reached (a missing day, a root that doesn't match), and
    TooFar if more than ``limit`` steps back would be needed.
    """
    walk = [check_manifest(manifest)]
    seen = {manifest["date"]}
    while not stop(walk[-1]):
        if limit is not None and len(walk) > limit:
            raise TooFar(f"more than {limit} days back")
        base = walk[-1].get("base")
        if not base:
            raise FormatError(f"{walk[-1]['date']} has no base")
        previous = read_manifest(base["date"])
        if previous is None:
            raise FormatError(f"manifest for {base['date']} is missing")
        check_manifest(previous)
        if previous["snapshotRoot"] != base["snapshotRoot"]:
            raise FormatError(f"{base['date']}'s root doesn't match what {walk[-1]['date']} names as its base")
        if previous["date"] in seen:
            raise FormatError("the chain loops")
        seen.add(previous["date"])
        walk.append(previous)
    return list(reversed(walk))


def from_anchor(read_manifest, manifest: dict) -> list:
    """[anchor full snapshot, the changes days after it ..., manifest]."""
    walk = back_to(read_manifest, manifest, lambda m: m["kind"] == "full" and m["date"] == manifest["anchor"]
                   or m["date"] < manifest["anchor"])
    if walk[0]["kind"] != "full" or walk[0]["date"] != manifest["anchor"]:
        raise FormatError(f"{manifest['date']}'s anchor {manifest['anchor']} isn't in its chain")
    return walk


def change_count(m: dict) -> int:
    c = m.get("changes") or {}
    return sum(int(c.get(k, 0)) for k in ("added", "changed", "deleted", "relationshipsAdded", "relationshipsChanged",
                                          "relationshipsDeleted"))


def plan_load(read_manifest, latest: dict, live: dict | None, *, incremental: bool, max_days: int,
              max_fraction: float, load_version: int) -> dict:
    """What load-db should do to bring the database from ``live`` to ``latest``.

    ``live`` is {date, snapshotRoot, loadVersion} from the database, or None.
    Returns {"mode": "already-loaded"} or {"mode": "incremental", "steps": [manifests]}
    or {"mode": "full", "anchor": manifest, "steps": [manifests after the anchor], "why": str}.
    """
    check_manifest(latest)
    if live and live.get("snapshotRoot") == latest["snapshotRoot"] and live.get("loadVersion") == load_version:
        return {"mode": "already-loaded"}
    why = "incremental loading is off"
    if incremental:
        why = "the database holds no snapshot"
        if live and live.get("loadVersion") != load_version:
            why = "the database was loaded by another load version"
        elif live and live.get("date") and DATE.match(live["date"]) and live["date"] < latest["date"]:
            try:
                walk = back_to(read_manifest, latest, lambda m: m["date"] <= live["date"], limit=max_days)
            except TooFar:
                walk = None
                why = f"the database is more than {max_days} days of changes behind"
            except FormatError as exc:
                walk = None
                why = f"no chain of changes back to the database's day ({exc})"
            if walk is not None:
                head = walk[0]
                steps = walk[1:]
                if head["date"] != live["date"] or head["snapshotRoot"] != live.get("snapshotRoot"):
                    why = "the database's day isn't in the chain of changes"
                elif sum(change_count(s) for s in steps) > max_fraction * max(1, latest["records"]):
                    why = "the changes are too large for an incremental load"
                else:
                    return {"mode": "incremental", "steps": steps}
        elif live:
            why = "the database's day isn't before the latest"
    chain = from_anchor(read_manifest, latest)
    return {"mode": "full", "anchor": chain[0], "steps": chain[1:], "why": why}


def days_between(a: str, b: str) -> int:
    return (Date.fromisoformat(b) - Date.fromisoformat(a)).days
