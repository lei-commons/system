"""The DeDi files (docs/dedi.md), served from the snapshots bucket, and their status for the System page.

Paths, and nothing else:
  /.well-known/dedi.index.json            -> dedi/dedi.index.json (the manifest; RFC 8615)
  /dedi/dedi.<registry>.json              -> dedi/dedi.<registry>.json
  /dedi/schemas/<name>.v<N>.json          -> dedi/schemas/<name>.v<N>.json
  /data/parts/<sha256>.parquet            -> parts/<sha256>.parquet (content-addressed, so permanent)
  /data/<date>/manifest.json              -> <date>/manifest.json, and its .sigstore.json, and NOTICE.txt

Every path is matched against a pattern before the bucket is touched. Like the rest of the site this is
behind the owner's login until the owner approves going public; the files are served as DeDi asks
(application/json, CORS *, a cache lifetime never beyond next_update) so that going public changes
only who may fetch them.
"""

from __future__ import annotations

import json
import re
import time

WELL_KNOWN = "/.well-known/dedi.index.json"
REGISTRY = re.compile(r"^/dedi/(dedi\.[a-z0-9][a-z0-9-]{0,40}\.json)$")
SCHEMA = re.compile(r"^/dedi/(schemas/[a-z0-9][a-z0-9-]{0,40}\.v\d{1,3}\.json)$")
PART = re.compile(r"^/data/(parts/[0-9a-f]{64}\.parquet)$")
DAY_FILE = re.compile(r"^/data/(\d{4}-\d{2}-\d{2}/(?:manifest\.json|manifest\.json\.sigstore\.json|NOTICE\.txt))$")
LABELS = {"dedi.data-sources.json": "Data sources", "dedi.countries.json": "Countries",
          "dedi.snapshots.json": "Snapshots", "dedi.digests.json": "Digests"}
MAX_AGE = 300  # seconds; far below any file's next_update


def object_for(path: str):
    """The bucket object a request path may read, or None."""
    if path == WELL_KNOWN:
        return "dedi/dedi.index.json"
    for pattern, prefix in ((REGISTRY, "dedi/"), (SCHEMA, "dedi/"), (PART, ""), (DAY_FILE, "")):
        m = pattern.match(path or "")
        if m and ".." not in path:
            return prefix + m.group(1)
    return None


def headers_for(obj: str) -> dict:
    out = {"Access-Control-Allow-Origin": "*", "Cache-Control": f"public, max-age={MAX_AGE}"}
    if obj.endswith(".json"):
        out["Content-Type"] = "application/json"
    elif obj.endswith(".parquet"):
        out["Content-Type"] = "application/vnd.apache.parquet"
        out["Cache-Control"] = "public, max-age=31536000, immutable"  # named by its SHA-256: never changes
    else:
        out["Content-Type"] = "text/plain; charset=utf-8"
    return out


class DediFiles:
    def __init__(self, bucket, ttl: float = 60.0, clock=time.monotonic):
        self._bucket, self._ttl, self._clock, self._cache = bucket, ttl, clock, {}

    def blob(self, path: str):
        obj = object_for(path)
        return (obj, self._bucket.get_blob(obj)) if obj else (None, None)

    def _json(self, name):
        blob = self._bucket.get_blob(name)
        if blob is None:
            return None
        try:
            return json.loads(blob.download_as_bytes())
        except ValueError:
            return {"ok": False, "error": f"{name} is unreadable"}

    def status(self) -> dict:
        """For the System page: the published files with links, the last publication and the last check."""
        hit = self._cache.get("status")
        if hit and self._clock() - hit[0] < self._ttl:
            return hit[1]
        value = self._status()
        self._cache["status"] = (self._clock(), value)
        return value

    def _status(self) -> dict:
        published, checked = self._json("dedi/status.json"), self._json("dedi/check.json")
        manifest = self._json("dedi/dedi.index.json")
        files = [{"label": "Index (manifest)", "path": WELL_KNOWN, "kind": "manifest",
                  "nextUpdate": (manifest or {}).get("next_update"), "updatedAt": (manifest or {}).get("updated_at"),
                  "keys": [k.get("kid") for k in (manifest or {}).get("keys", [])], "present": manifest is not None}]
        for name, info in sorted(((published or {}).get("files") or {}).items()):
            files.append({"label": LABELS.get(f"dedi.{name}.json", name), "path": f"/dedi/dedi.{name}.json",
                          "kind": "registry", "records": info.get("records"), "updatedAt": info.get("updatedAt"),
                          "nextUpdate": info.get("nextUpdate"), "bytes": info.get("bytes"), "digest": info.get("digest"),
                          "builtFrom": info.get("builtFrom"), "present": True})
        if manifest is not None:
            files.append({"label": "Digests", "path": "/dedi/dedi.digests.json", "kind": "digests", "present": True})
        state = ("unpublished" if not published and manifest is None
                 else "failing" if (checked and not checked.get("ok")) or (published and not published.get("ok"))
                 else "ok")
        return {"state": state, "files": files, "published": published, "check": checked,
                "domain": (manifest or {}).get("domain")}
