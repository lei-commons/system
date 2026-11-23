"""One candidate, start to finish: find its site in Common Crawl, read the evidence pages from the archive, look
for hard match evidence, and (with evidence only) have the maker extract claims and the checker check them.

What comes out is a small result: counts, kinds, the site's address and claims about companies. Page text never
leaves this module.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from lc_core.fetch import FetchError

from . import commoncrawl, evidence, names, text
from .candidates import WITH


@dataclass
class Context:
    gateway: object
    crawls: list
    settings: object
    maker: object = None
    checker: object = None
    usage: object = None
    legal_forms: re.Pattern = field(default_factory=names.legal_form_pattern)
    allow_models: object = lambda: True     # False once the spend cap is reached
    lock: threading.Lock = field(default_factory=threading.Lock)
    today: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))


def _home_url(domain: str, captures_used: list) -> str:
    for c in captures_used:
        host = (urlsplit(c.url).hostname or "").lower()
        if names.registrable(host) == domain:
            return f"https://{host}/"
    return f"https://{domain}/"


def look_up(ctx: Context, domain: str) -> list:
    """Captures of ``domain`` in the newest crawl that has any."""
    for crawl in ctx.crawls:
        found = commoncrawl.lookup(ctx.gateway, crawl, domain, ctx.settings.index_limit)
        if found:
            return found
    return []


def examine(ctx: Context, record: dict, domain: str, captures: list) -> dict:
    """Read a site's evidence pages from the archive and find the strongest match evidence.

    Returns {"pages", "pageErrors", "evidence" (kind or None), "page" (the capture holding it), "texts"}; the
    texts are for this record's model calls only."""
    s = ctx.settings
    pages, errors, best, texts = 0, 0, None, []
    for capture in text.choose(captures, domain, s.max_pages):
        try:
            page = commoncrawl.read_page(ctx.gateway, capture, s.max_record_bytes)
        except (commoncrawl.ArchiveError, FetchError):
            errors += 1
            continue
        pages += 1
        page_text = text.html_text(page.html, page.charset, s.max_text_chars)
        texts.append(page_text)
        found = evidence.find(record, [page_text], domain)
        if found and (best is None or evidence.STRENGTH[found["kind"]] > evidence.STRENGTH[best[0]]):
            best = (found["kind"], page)
    return {"pages": pages, "pageErrors": errors, "evidence": best[0] if best else None,
            "page": best[1] if best else None, "texts": texts, "home": _home_url(domain, captures)}


def discover(ctx: Context, record: dict) -> dict:
    """Find a website for a record from its legal name: candidate addresses, the archive's index, evidence."""
    s = ctx.settings
    tried, in_archive = 0, 0
    for domain in names.candidate_domains(record["legalName"], record["country"], s.country_domains, s.everywhere,
                                          s.max_candidates):
        tried += 1
        captures = look_up(ctx, domain)
        if not captures:
            continue
        in_archive += 1
        seen = examine(ctx, record, domain, captures)
        if seen["evidence"]:
            return {"tried": tried, "inArchive": in_archive, "domain": domain, **seen}
    return {"tried": tried, "inArchive": in_archive, "domain": None, "evidence": None, "pages": 0, "texts": []}


def _same_number(a, b) -> bool:
    va = set(evidence.number_variants(a or "")) | {re.sub(r"[^0-9A-Z]", "", (a or "").upper()).lstrip("0")}
    vb = set(evidence.number_variants(b or "")) | {re.sub(r"[^0-9A-Z]", "", (b or "").upper()).lstrip("0")}
    return bool((va - {""}) & (vb - {""}))


def parent_truth(record: dict, parent: dict) -> str:
    """correct or wrong against GLEIF's parents, no-gleif-parent for an LEI holder GLEIF gives none, unknown
    for a record without an LEI."""
    if not record.get("lei"):
        return "unknown"
    truths = list((record.get("truthParents") or {}).values())
    if not truths:
        return "no-gleif-parent"
    for t in truths:
        if parent.get("lei") and t.get("lei") and parent["lei"].upper() == t["lei"].upper():
            return "correct"
        if parent.get("registrationNumber") and t.get("registrationNumber") and _same_number(parent["registrationNumber"], t["registrationNumber"]):
            return "correct"
        if t.get("legalName") and names.core_words(parent.get("legalName", "")) == names.core_words(t["legalName"]):
            return "correct"
    return "wrong"


def _entity_like(parent: dict) -> bool:
    """The guard against a person slipping in as a 'parent': a legal-form word, a register number or an LEI,
    and nothing that looks like contact details."""
    name = parent.get("legalName") or ""
    if not name or text.EMAIL.search(name) or text.PHONE.search(name) or len(name) > 200:
        return False
    return names.has_legal_form(name) or bool(parent.get("registrationNumber")) or bool(parent.get("lei"))


def claims_from_models(ctx: Context, record: dict, domain: str, seen: dict) -> dict:
    """Maker then checker, on the minimised lines. Returns the model results (no text)."""
    excerpts = text.minimise(seen["texts"], ctx.legal_forms, ctx.settings.max_model_chars)
    if not excerpts:
        return {"models": "no-lines"}
    made = ctx.maker.extract(record, domain, excerpts, ctx.usage)
    operator = made.get("operator") or {}
    parent = made.get("parent")
    claims = {"operator": operator, "parent": parent}
    checked = ctx.checker.check(record, domain, excerpts, claims, ctx.usage)
    out = {"models": "done", "operatorSupported": checked.get("operatorSupported"),
           "operatorNumberAgrees": (any(_same_number(n, record.get("registrationNumber")) for n in operator.get("registrationNumbers") or [])
                                    if record.get("registrationNumber") and operator.get("registrationNumbers") else None)}
    if isinstance(parent, dict):
        if not _entity_like(parent):
            out["parent"] = {"status": "guard-rejected"}
        else:
            supported = checked.get("parentSupported") is True
            out["parent"] = {"status": "accepted" if supported else "checker-rejected",
                             "truth": parent_truth(record, parent),
                             "value": {k: parent.get(k) for k in ("legalName", "registrationNumber", "lei", "country", "relation")}}
    return out


def _claim(ctx: Context, record: dict, kind: str, value, seen: dict, by: str, checked_by) -> dict:
    page = seen["page"]
    return {"directoryId": record["id"], "kind": kind, "value": value, "tier": "own-site",
            "sourceUrl": page.capture.url, "capturedAt": page.capture.captured_at, "contentSha256": page.sha256,
            "matchEvidence": seen["evidence"], "extractedBy": by, "checkedBy": checked_by, "assertedOn": ctx.today}


def run_models(ctx: Context) -> bool:
    return ctx.maker is not None and ctx.checker is not None and ctx.allow_models()


def process(ctx: Context, record: dict) -> dict:
    """The result for one candidate: {"id", "set", "group", "country", "lei", "status", ..., "claims": [...]}.
    Raises models.ModelError (the record stays pending) or the gateway's errors (likewise)."""
    result = {"id": record["id"], "set": record["set"], "group": record["group"], "country": record["country"],
              "lei": bool(record.get("lei")), "claims": []}
    if record["set"] == WITH:
        domain = names.ascii_host(urlsplit(record["website"]).hostname or "")
        captures = look_up(ctx, domain) if domain else []
        result["inArchive"] = bool(captures)
        seen = examine(ctx, record, domain, captures) if captures else {"evidence": None, "pages": 0, "texts": []}
        if record.get("discoveryTruth"):
            found = discover(ctx, record)
            result["discoveryTruth"] = {"tried": found["tried"], "inArchive": found["inArchive"],
                                        "found": bool(found["domain"]), "evidence": found["evidence"],
                                        "correct": found["domain"] == domain if found["domain"] else None}
    else:
        seen = discover(ctx, record)
        domain = seen.get("domain")
        result.update(tried=seen["tried"], inArchive=seen["inArchive"] > 0)
    result.update(pages=seen.get("pages", 0), evidence=seen.get("evidence"), domain=domain if seen.get("evidence") else None)
    if not seen.get("evidence"):
        result["status"] = "no-capture" if not result["inArchive"] else "no-evidence"
        return result
    result["status"] = "evidence"
    models = {"models": "skipped"}
    if run_models(ctx):
        models = claims_from_models(ctx, record, domain, seen)
    result.update(models)
    checker_id = ctx.settings.checker.model_id if models.get("models") == "done" else None
    result["claims"].append(_claim(ctx, record, "website", seen["home"], seen, "rules", checker_id))
    parent = models.get("parent") or {}
    if parent.get("status") == "accepted":
        result["claims"].append(_claim(ctx, record, "parent", parent["value"], seen, ctx.settings.maker.model_id, checker_id))
    return result

