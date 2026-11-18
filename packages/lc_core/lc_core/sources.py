"""The sources registry (config/sources.yaml): which sources are built, and how.

Everything that lists sources reads it: the site's pages and its source names, the
snapshot (which sources, in what merge order, with what licence and attribution),
the daily cycle's steps (Terraform), and the status and run-job workflows. So a new
source needs only its entry here.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "sources.yaml"
REQUIRED = ("job", "store", "records_as", "label", "refresh", "dataset", "attribution")
ROLES = ("register", "lei")  # registers' values win in a merge; LEI sources (GLEIF) come last


def _path(path):
    return path or os.environ.get("LC_SOURCES_CONFIG") or DEFAULT_PATH


def pipelines(path=None) -> list:
    """The built sources, in file order: id, name, country, licence, cadence, status and the pipeline fields.

    >>> [p["job"] for p in pipelines()][:2]
    ['ingest-gleif', 'ingest-gb']
    """
    data = yaml.safe_load(Path(_path(path)).read_text(encoding="utf-8"))
    out, seen = [], {}
    for source in data.get("sources", []):
        pipeline = source.get("pipeline")
        if not pipeline:
            continue
        missing = [k for k in REQUIRED if k != "attribution" and not pipeline.get(k)]
        if missing or "attribution" not in pipeline:
            raise ValueError(f"source {source['id']}: pipeline is missing {missing or ['attribution']}")
        role = pipeline.get("role", "register")
        if role not in ROLES:
            raise ValueError(f"source {source['id']}: role must be one of {ROLES}")
        for key in ("job", "store", "records_as"):
            if pipeline[key] in seen.get(key, set()):
                raise ValueError(f"source {source['id']}: another source already uses {key} {pipeline[key]}")
            seen.setdefault(key, set()).add(pipeline[key])
        record_url = pipeline.get("record_url")
        if record_url and not record_url.startswith("https://"):
            raise ValueError(f"source {source['id']}: record_url must be an https address")
        out.append({"id": source["id"], "name": source.get("name"), "country": source.get("country"),
                    "licence": source.get("licence"), "cadence": source.get("cadence"), "status": source.get("status"),
                    "url": source.get("url"), "terms_url": source.get("terms_url"), "record_url": record_url,
                    **{k: pipeline[k] for k in REQUIRED}, "relationships": pipeline.get("relationships"), "role": role})
    return out


def merge_order(path=None) -> list:
    """The built sources in merge order: registers (file order), then LEI sources."""
    built = pipelines(path)
    return [p for p in built if p["role"] == "register"] + [p for p in built if p["role"] == "lei"]


def jobs(path=None) -> list:
    """The ingestion jobs of the daily cycle, in file order (build-snapshot and load-db follow them)."""
    return [p["job"] for p in pipelines(path)]


def names(path=None) -> dict:
    """How each source is shown: {id records carry: label}."""
    return {p["records_as"]: p["label"] for p in pipelines(path)}


def browser_fallbacks(path=None, egress_path=None, approved_only: bool = True) -> dict:
    """Sources with a browser fallback (pipeline.browser_fallback) whose every host the owner approved as
    a browser host for that source (config/egress-allowlist.yaml browser_hosts): {regular job: {source,
    store, page, link, hosts, job}}, ``job`` being the fallback's own job (<regular job>-browser).
    A fallback whose hosts aren't all approved is left out: it neither runs nor joins the cycle."""
    data = yaml.safe_load(Path(_path(path)).read_text(encoding="utf-8"))
    egress = Path(egress_path or os.environ.get("LC_EGRESS_CONFIG") or Path(_path(path)).parent / "egress-allowlist.yaml")
    approved = {}
    if egress.exists():
        for entry in (yaml.safe_load(egress.read_text(encoding="utf-8")) or {}).get("browser_hosts") or []:
            if isinstance(entry, dict) and entry.get("approved") and entry.get("host") and entry.get("source"):
                approved.setdefault(str(entry["source"]), set()).add(str(entry["host"]).lower())
    out = {}
    for source in data.get("sources", []):
        pipeline, fallback = source.get("pipeline") or {}, (source.get("pipeline") or {}).get("browser_fallback")
        if not fallback:
            continue
        page, hosts = str(fallback.get("page", "")), [str(h).lower() for h in fallback.get("hosts") or []]
        if not page.startswith("https://") or not hosts:
            raise ValueError(f"source {source['id']}: browser_fallback needs an https page and its hosts")
        if page.split("/")[2].lower() not in hosts:
            raise ValueError(f"source {source['id']}: the fallback page's host must be among its hosts")
        if not approved_only or set(hosts) <= approved.get(source["id"], set()):
            out[pipeline["job"]] = {"source": source["id"], "store": pipeline["store"], "page": page,
                                    "link": fallback.get("link"), "hosts": hosts, "job": f"{pipeline['job']}-browser"}
    return out
