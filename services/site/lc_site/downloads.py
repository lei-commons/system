"""The private /data page: the daily snapshots in the snapshots bucket, and their files.

A file can be downloaded only if that day's manifest lists it, or it is the
manifest, its signature or the notice itself. Dates and names are checked against
patterns first, and a v2 file's object (a shared part under parts/, or a file in the
day's folder) is checked against its pattern too, so no request can reach any other object. Files are streamed in chunks
through the site, behind the owner's login. The site has no permission to
sign URLs, and Cloud Run allows large responses only when they are chunked.
"""

from __future__ import annotations

import json
import re
import time

from lc_core.snapshot_format import CHANGE_FILE, OBJECT, is_v2

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
EXTRA = ("manifest.json", "manifest.json.sigstore.json", "NOTICE.txt")
MAX_SNAPSHOTS = 14
CHUNK = 1 << 20
TYPES = {".parquet": "application/vnd.apache.parquet", ".json": "application/json", ".txt": "text/plain; charset=utf-8"}


class SnapshotFiles:
    def __init__(self, bucket, ttl: float = 300.0, clock=time.monotonic):
        self._bucket, self._ttl, self._clock, self._cache = bucket, ttl, clock, {}

    def _cached(self, key, fn):
        hit = self._cache.get(key)
        if hit and self._clock() - hit[0] < self._ttl:
            return hit[1]
        value = fn()
        self._cache[key] = (self._clock(), value)
        return value

    def _manifest(self, date: str):
        blob = self._bucket.blob(f"{date}/manifest.json")
        return json.loads(blob.download_as_bytes()) if blob.exists() else None

    def snapshots(self) -> dict:
        return self._cached("list", self._list)

    def _list(self) -> dict:
        listing = self._bucket.list_blobs(delimiter="/")
        list(listing)  # the prefixes fill in as the listing is read
        dates = sorted((p.rstrip("/") for p in listing.prefixes if DATE.match(p.rstrip("/"))), reverse=True)
        out = []
        for date in dates[:MAX_SNAPSHOTS]:
            manifest = self._manifest(date)
            if not manifest:
                continue
            sizes = {b.name.split("/", 1)[1]: b.size for b in self._bucket.list_blobs(prefix=f"{date}/")}
            v2 = is_v2(manifest)
            files = [{"name": f["name"], "country": f.get("country"), "kind": f.get("kind", "entities"),
                      "records": f.get("records"), "sha256": f.get("sha256"),
                      "bytes": f.get("bytes") if v2 else sizes.get(f["name"])}
                     for f in manifest.get("files", [])]
            files += [{"name": n, "kind": "about", "bytes": sizes.get(n)} for n in EXTRA if n in sizes]
            entry = {"date": date, "records": manifest.get("records"), "relationships": manifest.get("relationships"),
                     "recordsPerSource": manifest.get("recordsPerSource"), "snapshotRoot": manifest.get("snapshotRoot"),
                     "decisionLog": manifest.get("decisionLog"), "files": files,
                     "bytes": sum(f["bytes"] or 0 for f in files), "kind": manifest.get("kind", "full") if v2 else "full"}
            if v2:
                entry.update(anchor=manifest["anchor"], base=(manifest.get("base") or {}).get("date"),
                             changes={k: manifest["changes"].get(k, 0) for k in ("added", "changed", "deleted",
                                                                                 "relationshipsAdded", "relationshipsChanged",
                                                                                 "relationshipsDeleted")})
            out.append(entry)
        return {"snapshots": out}

    def blob(self, date: str, name: str):
        """The object to stream, or None if the request isn't for a file that day's manifest lists."""
        if not DATE.match(date or "") or not NAME.match(name or "") or ".." in name:
            return None
        if name in EXTRA:
            return self._bucket.get_blob(f"{date}/{name}")
        manifest = self._cached(f"manifest:{date}", lambda: self._manifest(date))
        entry = next((f for f in (manifest or {}).get("files", []) if f.get("name") == name), None)
        if entry is None:
            return None
        if not is_v2(manifest):
            return self._bucket.get_blob(f"{date}/{name}")
        obj = entry.get("object", "")
        if not (OBJECT.match(obj) or (CHANGE_FILE.match(name) and obj == f"{date}/{name}")):
            return None
        return self._bucket.get_blob(obj)


def content_type(name: str) -> str:
    for suffix, kind in TYPES.items():
        if name.endswith(suffix):
            return kind
    return "application/octet-stream"
