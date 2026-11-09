"""Export a source's store to its ledger repository, and rebuild the store from the ledger.

A ledger directory (the root of ``ledger-gb``, or ``relationships/`` in ``ledger-gleif``) holds:

- ``baseline/NNN.jsonl.gz``: every record, in the store's 256 shards, as of one
  moment, written once. The full state isn't committed again each day, so the
  git history doesn't grow by the whole directory every day.
- ``changes/<name>.jsonl.gz``: each change file the store wrote after the
  baseline, in order: one line per changed field.
- ``LEDGER.json``: the baseline's store manifest, the change files in order
  with their hashes, and the store manifest they bring the ledger up to.

``rebuild_and_compare`` replays baseline plus changes, shard by shard, and checks
the result against the store. This is the "reproducible from the ledger" check.

The export takes no lock, because it runs after the daily cycle. It still
refuses a store caught mid-write. Ingestion writes the change file, then the
shards, then the manifest, so anything newer than the manifest is unfinished
work. A baseline with a shard newer than its manifest is refused, and change
files newer than the manifest wait for the next export.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .ledger import apply
from .store import SHARDS, decode_records, shard_of

LEDGER = "LEDGER.json"
# GitHub refuses files over 100 MB. A change file bigger than this is split into
# ordered parts of CHUNK_LINES lines each: a first UK part load is 9 million lines.
SPLIT_BYTES = 40 * 1024 * 1024
CHUNK_LINES = 1_000_000


class StoreBusy(Exception):
    """The store is mid-write; try again after the cycle."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _manifest(bucket, prefix):
    blob = bucket.get_blob(f"{prefix}/manifest.json")
    if blob is None:
        raise StoreBusy(f"{prefix} has no manifest yet")
    return blob, json.loads(blob.download_as_bytes())


def _print(record: dict) -> None:
    print(json.dumps(record, sort_keys=True), flush=True)


def export(bucket, prefix: str, ledger_dir: Path, log=_print) -> dict:
    """Bring ``ledger_dir`` up to date with the store under ``prefix``. Returns what was added."""
    ledger_dir.mkdir(parents=True, exist_ok=True)
    state_path = ledger_dir / LEDGER
    manifest_blob, manifest = _manifest(bucket, prefix)
    written_at = manifest_blob.updated
    added = {"baseline": 0, "changes": 0}

    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        shards = [b for b in bucket.list_blobs(prefix=f"{prefix}/records/") if b.name.endswith(".jsonl.gz")]
        late = [b.name for b in shards if b.updated > written_at]
        if late:
            raise StoreBusy(f"{len(late)} shards are newer than the manifest: an ingest is running")
        (ledger_dir / "baseline").mkdir(exist_ok=True)
        files = {}
        for blob in sorted(shards, key=lambda b: b.name):
            target = ledger_dir / "baseline" / blob.name.rsplit("/", 1)[-1]
            blob.download_to_filename(str(target))
            files[target.name] = _sha256(target)
        _, again = _manifest(bucket, prefix)
        if again != manifest:
            raise StoreBusy("the manifest changed while the baseline was read")
        state = {"source": prefix, "baseline": {"storeManifest": manifest, "storeWrittenAt": _iso(written_at),
                                                "files": files},
                 "changes": [], "upTo": {"storeManifest": manifest, "storeWrittenAt": _iso(written_at)}}
        added["baseline"] = len(files)

    since = state["upTo"]["storeWrittenAt"]
    fresh = sorted((b for b in bucket.list_blobs(prefix=f"{prefix}/changes/") if _iso(b.time_created) > since
                    and b.time_created <= written_at), key=lambda b: (b.time_created, b.name))
    (ledger_dir / "changes").mkdir(exist_ok=True)
    for blob in fresh:
        name = blob.name.rsplit("/", 1)[-1]
        target = ledger_dir / "changes" / name
        if target.exists() or (ledger_dir / "changes" / _part_name(name, 1)).exists():
            raise RuntimeError(f"{name} is already in the ledger with different contents")
        blob.download_to_filename(str(target))
        entry = {"name": name, "created": _iso(blob.time_created)}
        if target.stat().st_size > SPLIT_BYTES:
            entry["parts"] = [{"name": p.name, "sha256": _sha256(p)} for p in _split(target)]
            target.unlink()
        else:
            entry["sha256"] = _sha256(target)
        state["changes"].append(entry)
        added["changes"] += 1
    state["upTo"] = {"storeManifest": manifest, "storeWrittenAt": _iso(written_at)}
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    log({"event": "exported", "source": prefix, **added, "total_changes": len(state["changes"]),
         "records": manifest.get("records")})
    return added


def _part_name(name: str, n: int) -> str:
    stem = name[: -len(".jsonl.gz")] if name.endswith(".jsonl.gz") else name
    return f"{stem}.part{n:03d}.jsonl.gz"


def _split(path: Path) -> list:
    """Split a gzipped change file into ordered gzipped parts of CHUNK_LINES lines."""
    parts, out, lines = [], None, 0
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if out is None or lines >= CHUNK_LINES:
                if out:
                    out.close()
                parts.append(path.parent / _part_name(path.name, len(parts) + 1))
                out, lines = gzip.open(parts[-1], "wt", encoding="utf-8"), 0
            out.write(line)
            lines += 1
    if out:
        out.close()
    return parts


def _files(entry: dict) -> list:
    """(name, sha256) of an entry's files, in order: one file, or its parts."""
    if "parts" in entry:
        return [(p["name"], p["sha256"]) for p in entry["parts"]]
    return [(entry["name"], entry["sha256"])]


def _split_changes(ledger_dir: Path, state: dict, work: Path) -> None:
    """Every change, appended to its shard's file in ledger order, so each shard replays alone."""
    outs = {}
    try:
        for name, digest in (f for entry in state["changes"] for f in _files(entry)):
            path = ledger_dir / "changes" / name
            if _sha256(path) != digest:
                raise ValueError(f"{name} doesn't match its hash in {LEDGER}")
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        n = shard_of(json.loads(line)["directoryId"])
                        if n not in outs:
                            outs[n] = (work / f"{n:03d}.jsonl").open("w", encoding="utf-8")
                        outs[n].write(line if line.endswith("\n") else line + "\n")
    finally:
        for out in outs.values():
            out.close()


def rebuild_and_compare(bucket, prefix: str, ledger_dir: Path, work: Path, log=_print) -> dict:
    """Replay the ledger shard by shard and compare each shard with the store's.

    The comparison is against the store as of ``upTo``. If the store has moved
    on since the export, the check is skipped rather than reported as a mismatch.
    """
    state = json.loads((ledger_dir / LEDGER).read_text())
    _, manifest = _manifest(bucket, prefix)
    if manifest != state["upTo"]["storeManifest"]:
        log({"event": "rebuild-skipped", "source": prefix, "reason": "the store moved on since the export"})
        return {"checked": 0, "skipped": True}
    for name, digest in state["baseline"]["files"].items():
        if _sha256(ledger_dir / "baseline" / name) != digest:
            raise ValueError(f"baseline/{name} doesn't match its hash in {LEDGER}")
    work.mkdir(parents=True, exist_ok=True)
    _split_changes(ledger_dir, state, work)
    mismatched, records = [], 0
    for n in range(SHARDS):
        base = ledger_dir / "baseline" / f"{n:03d}.jsonl.gz"
        shard = decode_records(base.read_bytes()) if base.exists() else {}
        changes_path = work / f"{n:03d}.jsonl"
        if changes_path.exists():
            by_record = {}
            with changes_path.open(encoding="utf-8") as f:
                for line in f:
                    change = json.loads(line)
                    by_record.setdefault(change["directoryId"], []).append(change)
            for directory_id, changes in by_record.items():
                current = shard.get(directory_id, {})
                rebuilt = apply(current, changes)
                rebuilt["directoryId"] = directory_id
                shard[directory_id] = rebuilt
            changes_path.unlink()
        blob = bucket.get_blob(f"{prefix}/records/{n:03d}.jsonl.gz")
        stored = decode_records(blob.download_as_bytes()) if blob else {}
        records += len(stored)
        if shard != stored:
            mismatched.append(n)
    result = {"event": "rebuilt", "source": prefix, "shards": SHARDS, "records": records, "mismatchedShards": len(mismatched),
              "firstMismatch": mismatched[0] if mismatched else None}
    log(result)
    if mismatched:
        raise ValueError(f"{len(mismatched)} shards don't rebuild from the ledger, first {mismatched[0]:03d}")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lc_ingest.export")
    parser.add_argument("command", choices=["export", "verify"])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--dir", required=True, type=Path)
    parser.add_argument("--work", type=Path, default=Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "ledger-work")
    parser.add_argument("--decisions-bucket", help="also record the decisions in this decision log bucket")
    parser.add_argument("--actor", default="app:lei-commons-maker")
    args = parser.parse_args(argv)
    from google.cloud import storage
    from lc_core.decisions import Recorder

    client = storage.Client()
    bucket = client.bucket(args.bucket)
    trigger = os.environ.get("GITHUB_EVENT_NAME", "")
    record = Recorder(args.actor, _print, run_id=os.environ.get("GITHUB_RUN_ID", ""), trigger=trigger)
    try:
        if args.command == "export":
            export(bucket, args.prefix, args.dir, log=record)
        else:
            rebuild_and_compare(bucket, args.prefix, args.dir, args.work / args.prefix, log=record)
        return 0
    except StoreBusy as exc:
        record({"event": "busy", "source": args.prefix, "reason": str(exc)})
        return 3
    except Exception as exc:
        record({"event": "failed", "source": args.prefix, "error": type(exc).__name__})
        raise
    finally:
        if args.decisions_bucket:
            record.write(client.bucket(args.decisions_bucket))


if __name__ == "__main__":
    sys.exit(main())
