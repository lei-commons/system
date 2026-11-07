"""Copy a built snapshot to the private snapshots bucket and point latest.json at it.

    python -m lc_publish.upload --bucket lei-commons-prod-snapshots --dir out

The bucket enforces public access prevention, so nothing copied here can be
made public by mistake.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def upload(bucket, directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    prefix = manifest["date"]
    for path in sorted(directory.iterdir()):
        if path.is_file():
            bucket.blob(f"{prefix}/{path.name}").upload_from_filename(str(path))
    latest = {"date": manifest["date"], "prefix": prefix, "snapshotRoot": manifest["snapshotRoot"],
              "files": [f["name"] for f in manifest["files"]]}
    # Written last, so readers never see a pointer to a half-copied snapshot.
    bucket.blob("latest.json").upload_from_string(json.dumps(latest, indent=2), content_type="application/json")
    return latest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--dir", required=True)
    a = ap.parse_args(argv)
    from google.cloud import storage

    print(json.dumps(upload(storage.Client().bucket(a.bucket), Path(a.dir))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
