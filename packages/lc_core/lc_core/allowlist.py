"""Field allow-list (constitution Article 3), applied at the first pipeline step."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "fields.yaml"


@dataclass(frozen=True)
class AllowList:
    allowed: dict[str, frozenset[str]]
    personal: dict[str, frozenset[str]]
    forbidden: frozenset[str]

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PATH) -> "AllowList":
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        allowed, personal = {}, {}
        for kind in ("entity", "relationship", "person_role", "contributor"):
            items = cfg.get(kind, [])
            allowed[kind] = frozenset(i["name"] for i in items)
            personal[kind] = frozenset(i["name"] for i in items if i.get("personal"))
        forbidden = frozenset(f.split(".", 1)[1] for f in cfg.get("forbidden", []))
        clash = forbidden & allowed["person_role"]
        if clash:
            raise ValueError(f"forbidden fields also allowed: {sorted(clash)}")
        return cls(allowed, personal, forbidden)

    def filter(self, kind: str, record: dict) -> tuple[dict, list[str]]:
        """Keep allowed fields only. Returns (kept, dropped_field_names)."""
        if kind not in self.allowed:
            raise ValueError(f"unknown record kind {kind!r}")
        ok = self.allowed[kind]
        kept = {k: v for k, v in record.items() if k in ok}
        dropped = sorted(k for k in record if k not in ok)
        return kept, dropped
