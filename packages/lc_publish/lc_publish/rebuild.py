"""Rebuild any day from its full snapshot and the changes since, and check it against its signed manifest.

    python -m lc_publish.rebuild --date 2026-11-20 --bucket lei-commons-prod-snapshots [--out DIR]
    python -m lc_publish.rebuild --date 2026-11-20 --dir ./mirror [--require-signature]

``--dir`` is a copy of the bucket's layout (latest.json, parts/, <date>/...). The tool:
1. walks the day's ``base`` links back to its full snapshot (the ``anchor``), checking that
   each day names the previous day's root;
2. checks each manifest's Sigstore signature where present (always, with --require-signature);
3. checks every file it reads against the SHA-256 in its manifest;
4. streams the full snapshot's parts and applies each day's changes in order. An ``add`` of a
   record that exists, a ``change`` or ``delete`` of one that doesn't, or changes out of order,
   stop it;
5. recomputes every part's record count and Merkle root, and the snapshot root, and compares
   them with the day's manifest. With --out it also writes the day's parts.
Memory stays small: everything is a stream ordered by (shard, id).
"""

from __future__ import annotations

import argparse
import heapq
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pyarrow.parquet as pq
from lc_core.snapshot_format import (CHANGES_KIND, ENTITY_KIND, RELATIONSHIP_CHANGES_KIND, RELATIONSHIP_GROUP,
                                     RELATIONSHIP_KIND, SHARDS, FormatError, from_anchor, group_of, part_name, shard_of)

from .merkle import StreamingRoot, leaf, root
from .snapshot import ENTITY_SCHEMA, RELATIONSHIP_SCHEMA, sha256_file

SIGNER = "https://github.com/lei-commons/releases/.github/workflows/snapshot.yml@refs/heads/main"
ISSUER = "https://token.actions.githubusercontent.com"
KEYS = {ENTITY_KIND: "directoryId", RELATIONSHIP_KIND: "relationshipId"}


class VerifyError(Exception):
    """The day can't be rebuilt, or what was rebuilt doesn't match its manifest."""


class DirStore:
    """A local copy of the snapshots bucket's layout."""

    def __init__(self, root_dir):
        self.root = Path(root_dir)

    def read_manifest(self, date):
        path = self.root / date / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def manifest_path(self, date):
        return self.root / date / "manifest.json"

    def signature_path(self, date):
        path = self.root / date / "manifest.json.sigstore.json"
        return path if path.exists() else None

    def fetch(self, obj):
        path = self.root / obj
        if not path.exists():
            raise VerifyError(f"{obj} is missing")
        return path

    def done(self, path):
        pass


class BucketStore:
    """The snapshots bucket itself; files are downloaded one at a time and removed after use."""

    def __init__(self, bucket, work=None):
        self.bucket, self.work = bucket, Path(work or tempfile.mkdtemp(prefix="rebuild-"))

    def _download(self, name, target):
        blob = self.bucket.blob(name)
        if not blob.exists():
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(target))
        return target

    def read_manifest(self, date):
        path = self._download(f"{date}/manifest.json", self.work / date / "manifest.json")
        return json.loads(path.read_text(encoding="utf-8")) if path else None

    def manifest_path(self, date):
        return self.work / date / "manifest.json"

    def signature_path(self, date):
        return self._download(f"{date}/manifest.json.sigstore.json", self.work / date / "manifest.json.sigstore.json")

    def fetch(self, obj):
        path = self._download(obj, self.work / "files" / obj.replace("/", "_"))
        if path is None:
            raise VerifyError(f"{obj} is missing")
        return path

    def done(self, path):
        Path(path).unlink(missing_ok=True)


def check_signature(store, date, required, identity=SIGNER, issuer=ISSUER) -> str:
    """'verified', 'unsigned' (allowed unless required) or raises VerifyError."""
    bundle = store.signature_path(date)
    if bundle is None:
        if required:
            raise VerifyError(f"{date}'s manifest has no signature")
        return "unsigned"
    result = subprocess.run([sys.executable, "-m", "sigstore", "verify", "identity", "--bundle", str(bundle),
                             "--cert-identity", identity, "--cert-oidc-issuer", issuer, str(store.manifest_path(date))],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise VerifyError(f"{date}'s manifest signature doesn't verify: {(result.stderr or result.stdout).strip()[:300]}")
    return "verified"


def _rows(store, entry, kinds):
    """The rows of one file, after checking its SHA-256 against the manifest entry."""
    path = store.fetch(entry["object"])
    try:
        if sha256_file(path) != entry["sha256"]:
            raise VerifyError(f"{entry['name']} doesn't match its SHA-256")
        for batch in pq.ParquetFile(path).iter_batches(batch_size=50_000):
            yield from batch.to_pylist()
    finally:
        store.done(path)


def _keyed(rows, key, shards):
    for row in rows:
        rid = row[key]
        yield (shard_of(rid) % shards, rid), row


def apply_changes(base, changes, key):
    """Merge a stream of ((shard, id), row) with a stream of changes in the same order."""
    base = iter(base)
    current = next(base, None)
    last = None
    for k, change in changes:
        if last is not None and k <= last:
            raise VerifyError(f"changes out of order at {k[1]}")
        last = k
        while current is not None and current[0] < k:
            yield current
            current = next(base, None)
        exists = current is not None and current[0] == k
        op = change.get("op")
        row = {c: v for c, v in change.items() if c != "op"}
        if op == "add":
            if exists:
                raise VerifyError(f"add of {k[1]}, which already exists")
            yield k, row
        elif op == "change":
            if not exists:
                raise VerifyError(f"change of {k[1]}, which doesn't exist")
            yield k, row
            current = next(base, None)
        elif op == "delete":
            if not exists:
                raise VerifyError(f"delete of {k[1]}, which doesn't exist")
            current = next(base, None)
        else:
            raise VerifyError(f"unknown change {op!r} for {k[1]}")
    while current is not None:
        yield current
        current = next(base, None)


def _ordered(stream):
    last = None
    for k, row in stream:
        if last is not None and k <= last:
            raise VerifyError(f"records out of order at {k[1]}")
        last = k
        yield k, row


def state_stream(store, chain, kind):
    """The day's records of one kind, in (shard, id) order: the anchor's parts plus each day's changes."""
    anchor, steps = chain[0], chain[1:]
    key = KEYS[kind]
    shards = (anchor.get("layout") or {}).get("shards", SHARDS)
    part_entries = [f for f in anchor["files"] if f["kind"] == kind]
    stream = _ordered(heapq.merge(*(_keyed(_rows(store, f, kind), key, shards) for f in part_entries), key=lambda kr: kr[0]))
    change_kind = CHANGES_KIND if kind == ENTITY_KIND else RELATIONSHIP_CHANGES_KIND
    for step in steps:
        if (step.get("layout") or {}).get("shards", SHARDS) != shards:
            raise VerifyError(f"{step['date']} uses another shard count than its anchor")
        files = [f for f in step["files"] if f["kind"] == change_kind]
        changes = (kr for f in files for kr in _keyed(_rows(store, f, change_kind), key, shards))
        stream = apply_changes(stream, changes, key)
    return stream


def rebuild(store, date, out=None, require_signature=False, log=print) -> dict:
    """Rebuild ``date`` and check it. Returns a report; raises VerifyError (or FormatError) if anything doesn't match."""
    manifest = store.read_manifest(date)
    if manifest is None:
        raise VerifyError(f"no manifest for {date}")
    chain = from_anchor(store.read_manifest, manifest)
    signatures = {m["date"]: check_signature(store, m["date"], require_signature) for m in chain}
    layout = manifest.get("layout") or {}
    groups = layout.get("groups") or {}
    roots, counts, writers = {}, {}, {}
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
    try:
        for kind, schema in ((ENTITY_KIND, ENTITY_SCHEMA), (RELATIONSHIP_KIND, RELATIONSHIP_SCHEMA)):
            batch, batch_part = [], None
            for (shard, _), row in state_stream(store, chain, kind):
                if kind == ENTITY_KIND:
                    try:
                        group = group_of(row["country"], row["sources"])
                    except FormatError as exc:
                        raise VerifyError(f"record {row.get('directoryId')}: {exc}") from exc
                else:
                    group = RELATIONSHIP_GROUP
                if group not in groups:
                    raise VerifyError(f"{date}'s layout has no group {group}")
                name = part_name(group, shard % groups[group], groups[group])
                roots.setdefault(name, StreamingRoot()).add(leaf(row))
                counts[name] = counts.get(name, 0) + 1
                if out:
                    if batch_part not in (None, name) or len(batch) >= 20_000:
                        _write(writers, out, batch_part, batch, schema)
                        batch = []
                    batch_part = name
                    batch.append(row)
            if out and batch:
                _write(writers, out, batch_part, batch, schema)
    finally:
        for writer in writers.values():
            writer.close()
    expected = {p["name"]: p for p in manifest["parts"]}
    problems = []
    for name in sorted(set(expected) | set(roots)):
        if name not in roots:
            problems.append(f"{name}: in the manifest, not rebuilt")
        elif name not in expected:
            problems.append(f"{name}: rebuilt, not in the manifest")
        elif counts[name] != expected[name]["records"] or roots[name].hexdigest() != expected[name]["root"]:
            problems.append(f"{name}: {counts[name]} records, root {roots[name].hexdigest()[:19]}; the manifest says "
                            f"{expected[name]['records']}, {expected[name]['root'][:19]}")
    snapshot_root = root([roots[n].digest() for n in sorted(roots)])
    if snapshot_root != manifest["snapshotRoot"]:
        problems.append(f"snapshot root {snapshot_root} isn't the manifest's {manifest['snapshotRoot']}")
    report = {"date": date, "anchor": chain[0]["date"], "days": [m["date"] for m in chain], "parts": len(roots),
              "records": sum(c for n, c in counts.items() if not n.startswith(RELATIONSHIP_GROUP)),
              "relationships": sum(c for n, c in counts.items() if n.startswith(RELATIONSHIP_GROUP)),
              "snapshotRoot": snapshot_root, "signatures": signatures, "ok": not problems, "problems": problems[:20]}
    log(json.dumps(report, indent=2))
    if problems:
        raise VerifyError(f"{date} doesn't match its manifest: {problems[0]}")
    return report


def _write(writers, out, name, rows, schema):
    import pyarrow as pa

    if not rows:
        return
    if name not in writers:
        writers[name] = pq.ParquetWriter(Path(out) / name, schema, compression="zstd")
    writers[name].write_table(pa.table({f.name: [r.get(f.name) for r in rows] for f in schema}, schema=schema))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--date", required=True)
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--bucket")
    where.add_argument("--dir")
    ap.add_argument("--out")
    ap.add_argument("--require-signature", action="store_true")
    a = ap.parse_args(argv)
    if a.bucket:
        from google.cloud import storage

        work = tempfile.mkdtemp(prefix="rebuild-")
        store = BucketStore(storage.Client().bucket(a.bucket), work)
    else:
        work, store = None, DirStore(a.dir)
    try:
        rebuild(store, a.date, a.out, a.require_signature)
    except (VerifyError, FormatError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
