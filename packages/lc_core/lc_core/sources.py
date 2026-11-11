"""The sources registry (config/sources.yaml): which sources are built, and how.

The site's System page and the status views are drawn from the entries that
have a ``pipeline``, so a new source shows up once it is listed there.
"""

from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "sources.yaml"
REQUIRED = ("job", "store", "records_as", "label", "refresh")


def pipelines(path=DEFAULT_PATH) -> list:
    """The built sources, in file order: id, name, country, licence, cadence, status and the pipeline fields.

    >>> [p["job"] for p in pipelines()][:2]
    ['ingest-gleif', 'ingest-gb']
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    out = []
    for source in data.get("sources", []):
        pipeline = source.get("pipeline")
        if not pipeline:
            continue
        missing = [k for k in REQUIRED if not pipeline.get(k)]
        if missing:
            raise ValueError(f"source {source['id']}: pipeline is missing {missing}")
        out.append({"id": source["id"], "name": source.get("name"), "country": source.get("country"),
                    "licence": source.get("licence"), "cadence": source.get("cadence"), "status": source.get("status"),
                    **{k: pipeline[k] for k in REQUIRED}, "relationships": pipeline.get("relationships")})
    return out
