"""The pilot's report: counts, rates and the precision of each kind of claim against its truth set.

- Websites found by discovery are scored on the with-website truth sample (the found address against the
  published one). Websites of the with-website set are the register's own, so they measure coverage (is the
  site in the archive, does it show evidence), not precision.
- Parent claims are scored against GLEIF's parents for LEI holders.
Only counts go to the public log; the report (data bucket, trials/web-claims/) adds a small sample of claims.
"""

from __future__ import annotations

from collections import Counter

from .candidates import WITH, WITHOUT


def _rate(n, d):
    return round(n / d, 4) if d else None


def _gate(correct: int, wrong: int, bar: float) -> dict:
    judged = correct + wrong
    precision = _rate(correct, judged)
    return {"judged": judged, "correct": correct, "wrong": wrong, "precision": precision, "bar": bar,
            "passes": precision is not None and precision >= bar}


def build(candidates: list, results: dict, s, usage_total: dict, meta: dict) -> dict:
    sets = Counter(c["set"] for c in candidates)
    done = [results[c["id"]] for c in candidates if c["id"] in results]
    by = {}
    for r in done:
        g = by.setdefault((r["set"], r["group"]), Counter())
        g["processed"] += 1
        g["inArchive"] += bool(r.get("inArchive"))
        g["evidence:" + (r.get("evidence") or "none")] += 1
        g["withLei"] += r.get("lei", False)
        g["tried"] += r.get("tried", 0)
    with_website = {grp: {"processed": c["processed"], "inArchive": c["inArchive"],
                          "inArchiveRate": _rate(c["inArchive"], c["processed"]),
                          "evidence": {k[9:]: v for k, v in c.items() if k.startswith("evidence:")},
                          "evidenceRate": _rate(c["processed"] - c["evidence:none"], c["processed"])}
                    for (st, grp), c in sorted(by.items()) if st == WITH}
    without_website = {grp: {"processed": c["processed"], "addressesTried": c["tried"],
                             "anyInArchive": c["inArchive"],
                             "found": c["processed"] - c["evidence:none"],
                             "yield": _rate(c["processed"] - c["evidence:none"], c["processed"]),
                             "evidence": {k[9:]: v for k, v in c.items() if k.startswith("evidence:")}}
                       for (st, grp), c in sorted(by.items()) if st == WITHOUT}
    truth = [r["discoveryTruth"] for r in done if r.get("discoveryTruth")]
    disc_by_kind = Counter()
    for t in truth:
        if t["found"]:
            disc_by_kind[(t["evidence"], t["correct"])] += 1
    discovery = {"sampled": len(truth), "found": sum(t["found"] for t in truth),
                 "byEvidence": {k: {"correct": disc_by_kind[(k, True)], "wrong": disc_by_kind[(k, False)]}
                                for k in sorted({k for k, _ in disc_by_kind})},
                 **{k: v for k, v in _gate(sum(t["correct"] is True for t in truth),
                                           sum(t["correct"] is False for t in truth), s.precision_bar).items()}}
    models = Counter()
    parents = Counter()
    for r in done:
        if r.get("status") != "evidence":
            continue
        models["records:" + str(r.get("models", "skipped"))] += 1
        if r.get("models") == "done":
            models[f"operatorSupported:{r.get('operatorSupported')}"] += 1
            models[f"operatorNumberAgrees:{r.get('operatorNumberAgrees')}"] += 1
        p = r.get("parent")
        if p:
            parents["extracted"] += 1
            parents[p["status"]] += 1
            if p["status"] == "accepted":
                parents["truth:" + p["truth"]] += 1
    parent_report = {"extracted": parents["extracted"], "accepted": parents["accepted"],
                     "checkerRejected": parents["checker-rejected"], "guardRejected": parents["guard-rejected"],
                     "truth": {k[6:]: v for k, v in parents.items() if k.startswith("truth:")},
                     **_gate(parents["truth:correct"], parents["truth:wrong"], s.precision_bar)}
    claims = [c for r in done for c in r.get("claims", [])]
    sample = sorted(claims, key=lambda c: (c["kind"] != "parent", c["directoryId"]))[:s.sample]
    return {
        **meta,
        "candidates": {WITH: sets[WITH], WITHOUT: sets[WITHOUT]},
        "processed": len(done),
        "remaining": len(candidates) - len(done),
        "finished": len(done) == len(candidates),
        "withWebsite": with_website,
        "withoutWebsite": without_website,
        "discoveryTruth": discovery,
        "models": dict(sorted(models.items())),
        "modelUsage": usage_total,
        "parents": parent_report,
        "claims": dict(Counter(c["kind"] for c in claims)),
        "gates": {"website-discovery": {k: discovery[k] for k in ("judged", "precision", "bar", "passes")},
                  "parent": {k: parent_report[k] for k in ("judged", "precision", "bar", "passes")}},
        "personalData": {"parentsRejectedByTheGuard": parents["guard-rejected"]},
        "sample": sample,
    }


def public(report: dict) -> dict:
    """The counts for the public log: no names, no addresses, no claims."""
    return {
        "event": "web-claims-report",
        "processed": report["processed"], "remaining": report["remaining"], "finished": report["finished"],
        "withWebsite": {g: {k: v[k] for k in ("processed", "inArchive", "evidenceRate")} for g, v in report["withWebsite"].items()},
        "withoutWebsite": {g: {k: v[k] for k in ("processed", "found", "yield")} for g, v in report["withoutWebsite"].items()},
        "discoveryTruth": {k: report["discoveryTruth"][k] for k in ("sampled", "found", "judged", "precision", "passes")},
        "parents": {k: report["parents"][k] for k in ("extracted", "accepted", "guardRejected", "judged", "precision", "passes")},
        "claims": report["claims"],
        "modelUsd": round(report["modelUsage"].get("usd", 0.0), 2),
    }
