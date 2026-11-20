"""The ingestion volume guard's settings: the ``volume_guard`` section of config/sources.yaml.

A run that would change more than ``max_fraction`` of a store's existing
records is held, unless the store has fewer than ``minimum`` records (a first
load has no baseline, and small batches are too noisy to judge).

``mapping_upgrades`` lists one-time exceptions the owner approved: a source's
mapping gains a field, so one publication changes most of its records at once.
Each names the source, the one publication key it applies to, the reason, and
the owner's approval. That publication alone goes through past the guard, and
the run records a ``mapping-upgrade`` decision. Any other source or
publication still trips.

Every setting is required and checked: a missing, unknown or out-of-range value
raises ConfigError, so a job stops before it writes anything rather than guess.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "sources.yaml"
SETTINGS = {"max_fraction", "minimum", "mapping_upgrades"}
UPGRADE_FIELDS = ("source", "publish", "reason", "approved_by", "approved_on")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ConfigError(ValueError):
    """The volume guard's settings are missing or invalid."""


@dataclass(frozen=True)
class MappingUpgrade:
    source: str
    publish: str
    reason: str
    approved_by: str
    approved_on: str


@dataclass(frozen=True)
class GuardSettings:
    max_fraction: float
    minimum: int
    mapping_upgrades: tuple = ()

    def upgrade_for(self, source: str, publish: str) -> MappingUpgrade | None:
        """The owner-approved upgrade for exactly this source and publication, if there is one."""
        for upgrade in self.mapping_upgrades:
            if upgrade.source == source and upgrade.publish == publish:
                return upgrade
        return None


def _upgrade(entry, n: int, source_ids: set) -> MappingUpgrade:
    where = f"volume_guard.mapping_upgrades[{n}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{where}: must be a mapping")
    unknown = set(entry) - set(UPGRADE_FIELDS)
    if unknown:
        raise ConfigError(f"{where}: unknown settings {', '.join(sorted(unknown))}")
    values = {}
    for key in UPGRADE_FIELDS:
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{where}.{key}: missing")
        values[key] = " ".join(value.split())
    if values["source"] not in source_ids:
        raise ConfigError(f"{where}.source: {values['source']} is not a source in this file")
    if values["approved_by"] != "owner":
        raise ConfigError(f"{where}.approved_by: must be owner")
    if not DATE.match(values["approved_on"]):
        raise ConfigError(f"{where}.approved_on: must be a date, YYYY-MM-DD")
    return MappingUpgrade(**values)


def parse(data) -> GuardSettings:
    if not isinstance(data, dict):
        raise ConfigError("not a sources file")
    section = data.get("volume_guard")
    if not isinstance(section, dict):
        raise ConfigError("volume_guard: missing")
    unknown = set(section) - SETTINGS
    if unknown:
        raise ConfigError(f"volume_guard: unknown settings {', '.join(sorted(unknown))}")
    fraction, minimum = section.get("max_fraction"), section.get("minimum")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 < fraction <= 1:
        raise ConfigError("volume_guard.max_fraction: must be a number above 0 and at most 1")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 0 <= minimum <= 1_000_000:
        raise ConfigError("volume_guard.minimum: must be a whole number between 0 and 1000000")
    upgrades = section.get("mapping_upgrades")
    if upgrades is None:
        raise ConfigError("volume_guard.mapping_upgrades: missing (use [] for none)")
    if not isinstance(upgrades, list):
        raise ConfigError("volume_guard.mapping_upgrades: must be a list")
    source_ids = {s.get("id") for s in data.get("sources") or [] if isinstance(s, dict)}
    parsed = tuple(_upgrade(entry, n, source_ids) for n, entry in enumerate(upgrades))
    keys = [(u.source, u.publish) for u in parsed]
    if len(set(keys)) != len(keys):
        raise ConfigError("volume_guard.mapping_upgrades: a source and publication are listed twice")
    return GuardSettings(float(fraction), minimum, parsed)


def load(path=None) -> GuardSettings:
    """The settings from LC_SOURCES_CONFIG, or the repo's config/sources.yaml."""
    path = path or os.environ.get("LC_SOURCES_CONFIG") or DEFAULT_PATH
    return parse(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
