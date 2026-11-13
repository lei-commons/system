"""The owner's edits to the sources registry: added, edited and removed sources, and suggestions for Claude.

The registry in git (config/sources.yaml) is the base. The owner's edits, made on
the site's Data sources tab (each with a fresh authenticator code), are kept next to
the control state as ``source-edits.json`` and laid over it for every page and
action. Jobs, the snapshot and approvals don't read edits: an edit can't stop or
change the daily cycle.

Some fields can't be edited from a form: a source's id, its approval (that has its
own actions) and its pipeline (job, store, records_as), because changing those could
break a running job. A built source (one with a pipeline) can't be removed: deny it
instead, so its records leave the snapshot, and remove it in code.
"""

from __future__ import annotations

import json
import re

EDITS_NAME = "source-edits.json"
HINT_MAX = 1000
TEXT_MAX = 500
SUMMARY_MAX = 1000
NAME_MAX = 120
MAX_HISTORY = 100
ACCESS = ("open", "owner-key", "web-discovery")
METHOD = ("bulk", "api", "crawl", "other")
CADENCE = ("daily", "weekly", "monthly", "quarterly", "yearly", "irregular")
RISK = ("low", "medium", "high", "unknown")
EDITABLE = ("name", "country", "url", "terms_url", "licence", "access", "method", "cadence")
ASSESSMENT_EDITABLE = ("risk", "commercial", "attribution", "summary", "verified")
ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
URL = re.compile(r"^https?://[^\s<>\"']{3,490}$")
COUNTRY = re.compile(r"^([A-Z]{2}|\*)$")


class Invalid(ValueError):
    """An edit that can't be accepted; nothing is written."""


def _text(value, limit, what, required=False):
    value = " ".join(str(value or "").split())
    if required and not value:
        raise Invalid(f"{what} is required")
    if len(value) > limit:
        raise Invalid(f"{what} is longer than {limit} characters")
    return value


def clean(fields: dict) -> dict:
    """Validate and normalise the editable fields of a source. Unknown fields are refused."""
    unknown = set(fields) - set(EDITABLE) - {"assessment"}
    if unknown:
        raise Invalid(f"these can't be edited here: {', '.join(sorted(unknown))}")
    out = {}
    if "name" in fields:
        out["name"] = _text(fields["name"], NAME_MAX, "name", required=True)
    if "country" in fields:
        country = _text(fields["country"], 10, "country", required=True).upper()
        if not COUNTRY.match(country):
            raise Invalid("country must be a two-letter code, or * for global")
        out["country"] = country
    for key in ("url", "terms_url"):
        if key in fields:
            value = _text(fields[key], TEXT_MAX, key)
            if value and not URL.match(value):
                raise Invalid(f"{key} must be an http or https address")
            out[key] = value
    if "licence" in fields:
        out["licence"] = _text(fields["licence"], 100, "licence")
    for key, allowed in (("access", ACCESS), ("method", METHOD), ("cadence", CADENCE)):
        if key in fields:
            value = _text(fields[key], 40, key)
            if value and value not in allowed:
                raise Invalid(f"{key} must be one of {', '.join(allowed)}")
            out[key] = value
    if "assessment" in fields:
        a = fields["assessment"] or {}
        if not isinstance(a, dict) or set(a) - set(ASSESSMENT_EDITABLE):
            raise Invalid("assessment takes risk, commercial, attribution, summary and verified")
        assessment = {}
        if "risk" in a:
            risk = _text(a["risk"], 10, "risk")
            if risk not in RISK:
                raise Invalid(f"risk must be one of {', '.join(RISK)}")
            assessment["risk"] = risk
        for key in ("commercial", "attribution"):
            if key in a:
                assessment[key] = _text(a[key], 200, key)
        if "summary" in a:
            assessment["summary"] = _text(a["summary"], SUMMARY_MAX, "summary")
        if "verified" in a:
            if not isinstance(a["verified"], bool):
                raise Invalid("verified must be true or false")
            assessment["verified"] = a["verified"]
        out["assessment"] = assessment
    return out


def clean_hint(text) -> str:
    """A suggestion for Claude: free text up to 1,000 characters, line breaks kept."""
    text = str(text or "").replace("\r\n", "\n").strip()
    if len(text) > HINT_MAX:
        raise Invalid(f"the suggestion is longer than {HINT_MAX} characters")
    return text


def slug(name: str, country: str) -> str:
    """An id from the name: the country, then whole words, at most 36 characters (room for a -2 suffix)."""
    prefix = country.lower() if COUNTRY.match(country) and country != "*" else "xx"
    out = prefix
    for word in re.sub(r"[^a-z0-9]+", " ", name.lower()).split():
        if len(out) + 1 + len(word) > 36:
            break
        out += "-" + word
    return out if out != prefix else f"{prefix}-source"


def merged(base: list, edits: dict) -> list:
    """The registry the owner sees and acts on: base sources with edits applied, added ones, minus removed ones."""
    changes = (edits or {}).get("sources") or {}
    out = []
    for source in base:
        change = changes.get(source["id"]) or {}
        if change.get("removed") and not source.get("pipeline"):
            continue
        out.append(_apply(source, change))
    for sid, change in changes.items():
        if change.get("added") and not change.get("removed") and all(s["id"] != sid for s in base):
            out.append(_apply({"id": sid, "approval": "pending", "status": "active"}, change))
    return out


def _apply(source: dict, change: dict) -> dict:
    result = json.loads(json.dumps(source))
    for key in EDITABLE:
        if key in (change.get("fields") or {}):
            result[key] = change["fields"][key]
    if "assessment" in (change.get("fields") or {}):
        result["assessment"] = {**(result.get("assessment") or {}), **change["fields"]["assessment"]}
    result["owner"] = {k: change.get(k) for k in ("added", "edited_by", "edited_at", "hint", "hint_by", "hint_at")
                       if change.get(k) not in (None, "")}
    return result


def _history(edits, entry):
    history = (edits.get("history") or [])[-(MAX_HISTORY - 1):]
    history.append(entry)
    return history


def edit(edits: dict, base: list, source_id: str, fields: dict, actor: str, now: str) -> tuple:
    """(new edits, changed field names). Raises Invalid."""
    current = {s["id"]: s for s in merged(base, edits)}
    if source_id not in current:
        raise Invalid(f"there is no source {source_id}")
    fields = clean(fields)
    before = current[source_id]
    changed = [k for k in fields if (before.get(k) if k != "assessment" else
                                     {x: (before.get("assessment") or {}).get(x) for x in fields["assessment"]}) != fields[k]]
    if not changed:
        raise Invalid("nothing changed")
    new = json.loads(json.dumps(edits or {}))
    entry = new.setdefault("sources", {}).setdefault(source_id, {})
    stored = entry.setdefault("fields", {})
    for key in changed:
        if key == "assessment":
            stored["assessment"] = {**(stored.get("assessment") or {}), **fields["assessment"]}
        else:
            stored[key] = fields[key]
    entry.update(edited_by=actor, edited_at=now)
    new["history"] = _history(new, {"at": now, "by": actor, "source": source_id, "change": "edit",
                                    "before": {k: before.get(k) for k in changed}, "after": {k: fields[k] for k in changed}})
    return new, changed


def add(edits: dict, base: list, fields: dict, actor: str, now: str) -> tuple:
    """(new edits, new id). A new source starts pending. Raises Invalid."""
    fields = clean(fields)
    if not fields.get("name") or not fields.get("country"):
        raise Invalid("a new source needs at least a name and a country")
    existing = {s["id"] for s in merged(base, edits)} | {s["id"] for s in base}
    sid, n = slug(fields["name"], fields["country"]), 2
    while sid in existing:
        sid, n = f"{slug(fields['name'], fields['country'])}-{n}", n + 1
    if not ID.match(sid):
        raise Invalid("couldn't make an id from that name")
    new = json.loads(json.dumps(edits or {}))
    new.setdefault("sources", {})[sid] = {"added": True, "fields": fields, "edited_by": actor, "edited_at": now}
    new["history"] = _history(new, {"at": now, "by": actor, "source": sid, "change": "add", "after": fields})
    return new, sid


def remove(edits: dict, base: list, source_id: str, actor: str, now: str) -> dict:
    current = {s["id"]: s for s in merged(base, edits)}
    if source_id not in current:
        raise Invalid(f"there is no source {source_id}")
    if current[source_id].get("pipeline"):
        raise Invalid("a source in use can't be removed here: deny it, so its records leave the snapshot")
    new = json.loads(json.dumps(edits or {}))
    new.setdefault("sources", {}).setdefault(source_id, {}).update(removed=True, edited_by=actor, edited_at=now)
    new["history"] = _history(new, {"at": now, "by": actor, "source": source_id, "change": "remove"})
    return new


def hint(edits: dict, base: list, source_id: str, text, actor: str, now: str) -> dict:
    current = {s["id"] for s in merged(base, edits)}
    if source_id not in current:
        raise Invalid(f"there is no source {source_id}")
    text = clean_hint(text)
    new = json.loads(json.dumps(edits or {}))
    entry = new.setdefault("sources", {}).setdefault(source_id, {})
    entry.update(hint=text, hint_by=actor, hint_at=now)
    new["history"] = _history(new, {"at": now, "by": actor, "source": source_id, "change": "hint", "after": {"hint": text}})
    return new
