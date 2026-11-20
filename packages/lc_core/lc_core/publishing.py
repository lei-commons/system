"""The publishing settings: config/publishing.yaml (docs/snapshots-v2.md).

Read by build-snapshot (full every x days, parts, changes, retention), load-db
(incremental loads) and Terraform (old versions' lifecycle). Every setting is
required and checked: a missing, unknown or out-of-range value raises
ConfigError, so a job stops before it writes anything rather than guess.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


class ConfigError(ValueError):
    """The publishing settings are missing or invalid."""


# name: (type, minimum, maximum)
SNAPSHOT_SETTINGS = {
    "full_every_days": (int, 1, 366),
    "retain_days": (int, 1, 3660),
    "noncurrent_retain_days": (int, 1, 365),
    "part_target_records": (int, 10_000, 50_000_000),
    "change_file_max_rows": (int, 1_000, 50_000_000),
    "full_when_changes_exceed": (float, 0.0, 1.0),
    "state_kept": (int, 2, 30),
    "prune_grace_days": (int, 0, 30),
    "prune_dry_run": (bool, None, None),
    "read_ahead_shards": (int, 0, 8),
}
STORAGE_SETTINGS = {
    "data_noncurrent_retain_days": (int, 1, 3660),
}
LOAD_SETTINGS = {
    "incremental": (bool, None, None),
    "incremental_max_days": (int, 1, 366),
    "incremental_max_fraction": (float, 0.0, 1.0),
    "fallback_to_full": (bool, None, None),
    "vacuum_after_incremental": (bool, None, None),
}


@dataclass(frozen=True)
class SnapshotSettings:
    full_every_days: int
    retain_days: int
    noncurrent_retain_days: int
    part_target_records: int
    change_file_max_rows: int
    full_when_changes_exceed: float
    state_kept: int
    prune_grace_days: int
    prune_dry_run: bool
    read_ahead_shards: int = 2


@dataclass(frozen=True)
class LoadSettings:
    incremental: bool
    incremental_max_days: int
    incremental_max_fraction: float
    fallback_to_full: bool
    vacuum_after_incremental: bool


@dataclass(frozen=True)
class StorageSettings:
    data_noncurrent_retain_days: int


@dataclass(frozen=True)
class Publishing:
    snapshots: SnapshotSettings
    load: LoadSettings
    storage: StorageSettings

    def as_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


def _section(data, name, spec):
    section = data.get(name)
    if not isinstance(section, dict):
        raise ConfigError(f"{name}: missing")
    unknown = set(section) - set(spec)
    if unknown:
        raise ConfigError(f"{name}: unknown settings {', '.join(sorted(unknown))}")
    out = {}
    for key, (kind, low, high) in spec.items():
        if key not in section:
            raise ConfigError(f"{name}.{key}: missing")
        value = section[key]
        if kind is bool:
            if not isinstance(value, bool):
                raise ConfigError(f"{name}.{key}: must be true or false")
        elif kind is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{name}.{key}: must be a whole number")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"{name}.{key}: must be a number")
            value = float(value)
        if low is not None and not (low <= value <= high):
            raise ConfigError(f"{name}.{key}: must be between {low} and {high}")
        out[key] = value
    return out


def parse(data) -> Publishing:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ConfigError("version: must be 1")
    unknown = set(data) - {"version", "snapshots", "load", "storage"}
    if unknown:
        raise ConfigError(f"unknown sections {', '.join(sorted(unknown))}")
    return Publishing(SnapshotSettings(**_section(data, "snapshots", SNAPSHOT_SETTINGS)),
                      LoadSettings(**_section(data, "load", LOAD_SETTINGS)),
                      StorageSettings(**_section(data, "storage", STORAGE_SETTINGS)))


def default_path() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "config" / "publishing.yaml"
        if candidate.exists():
            return candidate
    raise ConfigError("config/publishing.yaml not found; set LC_PUBLISHING_CONFIG")


def load(path=None) -> Publishing:
    path = Path(path or os.environ.get("LC_PUBLISHING_CONFIG") or default_path())
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"can't read {path}: {type(exc).__name__}") from exc
    return parse(data)
