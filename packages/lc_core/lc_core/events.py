"""Decision log: hash-chained, human-readable record of every action.

Each event records who acted (component or agent ID), when, what triggered it,
what it did, what it decided and why. Events are written one per line to a
daily JSONL file. Each event carries the hash of the previous one, so removing
or editing any line breaks the chain. The day's final hash goes into the
snapshot manifest, which is anchored in Sigstore Rekor.

The one-line ``summary`` is always built from a fixed template, never written by
a model, so it cannot drift from the structured fields.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

GENESIS = "0" * 64

DECISIONS = {
    "approved", "rejected", "held", "merged", "kept-separate", "published",
    "skipped", "paused", "stopped", "resumed", "failed", "proposed", "done",
}

SUMMARY_TEMPLATE = "{actor} {action} {subject}: {decision} — {reason}"


def canonical(obj: dict) -> bytes:
    """Canonical JSON bytes: sorted keys, no spaces, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Event:
    actor: str            # e.g. "agent:resolver-gb", "job:ingest-gleif", "app:lei-commons-checker"
    action: str           # verb, e.g. "reviewed", "ingested", "merged"
    subject: str          # what was acted on, e.g. "PR ledger-gb#412", "LECOZZFZ6LZCOJBDKT73"
    decision: str         # one of DECISIONS
    reason: str           # plain-language reason, one sentence
    trigger: str = ""     # what caused it, e.g. "schedule:daily-cycle", "pr:opened"
    run_id: str = ""      # workflow/job execution ID
    refs: dict = field(default_factory=dict)  # links: PR URL, model, evidence hashes
    ts: str = field(default_factory=utcnow)
    prev: str = GENESIS
    hash: str = ""
    summary: str = ""

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise ValueError(f"unknown decision {self.decision!r}")
        for name in ("actor", "action", "subject", "reason"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if "\n" in self.reason:
            raise ValueError("reason must be one line")

    def body(self) -> dict:
        d = asdict(self)
        d.pop("hash")
        d.pop("summary")
        return d

    def seal(self, prev: str) -> "Event":
        self.prev = prev
        self.summary = SUMMARY_TEMPLATE.format(
            actor=self.actor, action=self.action, subject=self.subject,
            decision=self.decision, reason=self.reason,
        )
        self.hash = hashlib.sha256(canonical(self.body())).hexdigest()
        return self

    def to_line(self) -> str:
        return canonical(asdict(self)).decode("utf-8")


class DecisionLog:
    """Append-only JSONL decision log for one day."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._head = self._read_head()

    def _read_head(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)["hash"]
        return last

    @property
    def head(self) -> str:
        return self._head

    def append(self, event: Event) -> Event:
        event.seal(self._head)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(event.to_line() + "\n")
        self._head = event.hash
        return event


def verify(lines: Iterable[str], start: str = GENESIS) -> tuple[bool, int, str]:
    """Verify a chain. Returns (ok, events_checked, head_or_error)."""
    prev = start
    n = 0
    for raw in lines:
        if not raw.strip():
            continue
        d = json.loads(raw)
        h = d.pop("hash")
        summary = d.pop("summary")
        if d["prev"] != prev:
            return False, n, f"event {n}: prev mismatch"
        if hashlib.sha256(canonical(d)).hexdigest() != h:
            return False, n, f"event {n}: hash mismatch"
        expected = SUMMARY_TEMPLATE.format(**{k: d[k] for k in ("actor", "action", "subject", "decision", "reason")})
        if summary != expected:
            return False, n, f"event {n}: summary does not match template"
        prev = h
        n += 1
    return True, n, prev
