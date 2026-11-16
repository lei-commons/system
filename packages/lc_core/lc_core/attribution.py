"""Crediting sources: where each record can be seen at its source, and the attribution its licence asks for.

Every record carries its source URL (registryUrl, filled by the snapshot), and the snapshot's manifest and
the database carry each source's licence, attribution and citation (owner requirement 2026-11-16). The
snapshot, the database API and the site's in-memory snapshot all build a record's credits with this.
"""

from __future__ import annotations

from urllib.parse import quote

FIELDS = ("id", "name", "publisher", "licence", "licenceUrl", "datasetUrl", "recordUrl", "attribution", "citation")


def record_url(record: dict, source: dict):
    """The source's page for this record if the registry gives a pattern and the record has its values, else the dataset."""
    template = (source or {}).get("recordUrl")
    if template:
        values = {k: record.get(k) for k in ("registrationNumber", "lei", "directoryId")}
        needed = [k for k in values if "{" + k + "}" in template]
        if needed and all(values[k] for k in needed):
            return template.format(**{k: quote(str(v or ""), safe="") for k, v in values.items()})
    return (source or {}).get("datasetUrl")


def credits(record: dict, sources_by_id: dict) -> list:
    """One entry per source the record came from, in its order: who published it, under what licence, the
    attribution to show, a citation, and the URL where this record (or its dataset) can be seen."""
    out = []
    ids = record.get("sources") or []
    for i, sid in enumerate(ids.split(",") if isinstance(ids, str) else ids):
        source = sources_by_id.get(sid)
        if not source:
            out.append({"id": sid, "name": sid, "url": None, "attribution": "", "citation": ""})
            continue
        url = record.get("registryUrl") if i == 0 and record.get("registryUrl") else record_url(record, source)
        out.append({k: source.get(k) for k in FIELDS} | {"url": url})
    return out
