"""The harmonised status category (owner decision 2026-11-20; config/status.yaml).

Every register words a company's status its own way ("Active", "ACTIVE", "Good Standing",
"Normal", "Live Company"...). The record keeps that word in ``legalStatus``, untouched
(Article 4.3). Its ``statusCategory`` is one of the vocabulary in config/labels.yaml
``legal_status``, mapped per source in config/status.yaml, so filters and dashboards can
count like with like.

The map is checked when read: a category not in the vocabulary, or a word mapped to two
categories, raises StatusConfigError, so build-snapshot stops before it writes anything.
A word the map doesn't list gets ``unknown`` and is reported as unmapped, to be added.

>>> m = StatusMap({"gleif": {"active": ["ACTIVE"]}, "jp-nta": {"inactive": ["Closed:*"]}}, ["active", "inactive", "unknown"])
>>> m.category("gleif", " active "), m.category("jp-nta", "Closed: by merger"), m.category("gleif", "LAPSED")
(('active', True), ('inactive', True), ('unknown', False))
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parents[3] / "config"
UNKNOWN = "unknown"


class StatusConfigError(ValueError):
    """config/status.yaml or the labels' legal_status list is missing or invalid."""


def _key(word) -> str:
    return " ".join(str(word).split()).casefold()


def categories(labels_path=None) -> list:
    """The vocabulary: config/labels.yaml ``legal_status``."""
    path = Path(labels_path or os.environ.get("LC_LABELS_CONFIG") or CONFIG / "labels.yaml")
    values = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("legal_status")
    if not isinstance(values, list) or not values or not all(isinstance(v, str) and v for v in values):
        raise StatusConfigError(f"{path}: legal_status must be a list of words")
    if UNKNOWN not in values:
        raise StatusConfigError(f"{path}: legal_status must include {UNKNOWN}")
    return values


class StatusMap:
    """``maps``: {source id records carry: {category: [register word, ...]}}; ``vocabulary``: the categories."""

    def __init__(self, maps: dict, vocabulary):
        self.vocabulary = list(vocabulary)
        self._exact, self._prefixes, self._seen = {}, {}, {}
        for source, by_category in (maps or {}).items():
            if not isinstance(by_category, dict):
                raise StatusConfigError(f"{source}: expected {{category: [words]}}")
            exact, prefixes = {}, {}
            for category, words in by_category.items():
                if category not in self.vocabulary:
                    raise StatusConfigError(f"{source}: {category!r} isn't in labels.yaml legal_status {self.vocabulary}")
                if not isinstance(words, list) or not words:
                    raise StatusConfigError(f"{source}: {category} needs a list of words")
                for word in words:
                    if not isinstance(word, str) or not _key(word.rstrip("*")):
                        raise StatusConfigError(f"{source}: {category} has an empty or non-text word {word!r}")
                    target, key = (prefixes, _key(word[:-1])) if word.endswith("*") else (exact, _key(word))
                    if target.get(key, category) != category:
                        raise StatusConfigError(f"{source}: {word!r} is mapped to both {target[key]} and {category}")
                    target[key] = category
            self._exact[source] = exact
            # Longest prefix first, so "Inactive - Amalgamated*" wins over "Inactive*".
            self._prefixes[source] = sorted(prefixes.items(), key=lambda kv: -len(kv[0]))

    def category(self, source, word) -> tuple:
        """(category, mapped): ``mapped`` is False for a word the map doesn't list (it gets unknown)."""
        try:  # a register uses a handful of words over millions of records: look each up once
            return self._seen[(source, word)]
        except KeyError:
            pass
        except TypeError:  # not hashable: never a register's word, but answer all the same
            return self._category(source, word)
        if len(self._seen) < 100_000:
            self._seen[(source, word)] = self._category(source, word)
            return self._seen[(source, word)]
        return self._category(source, word)

    def _category(self, source, word) -> tuple:
        if word is None or not str(word).strip():
            return UNKNOWN, True  # no word: nothing to map
        key = _key(word)
        found = self._exact.get(source, {}).get(key)
        if found:
            return found, True
        for prefix, category in self._prefixes.get(source, ()):
            if key.startswith(prefix):
                return category, True
        return UNKNOWN, False

    def sources(self) -> list:
        return sorted(self._exact)


def load(path=None, labels_path=None, sources_path=None) -> StatusMap:
    """Read and check config/status.yaml, keyed by the id each source's records carry (sources.yaml records_as)."""
    from .sources import pipelines

    path = Path(path or os.environ.get("LC_STATUS_CONFIG") or CONFIG / "status.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if data.get("version") != 1 or not isinstance(data.get("sources"), dict):
        raise StatusConfigError(f"{path}: needs version: 1 and a sources map")
    sources_file = Path(sources_path or os.environ.get("LC_SOURCES_CONFIG") or CONFIG / "sources.yaml")
    vocabulary = categories(labels_path)
    StatusMap(data["sources"], vocabulary)  # every entry is checked, built or not
    carried = {p["id"]: p["records_as"] for p in pipelines(sources_file)}
    return StatusMap({carried[sid]: words for sid, words in data["sources"].items() if sid in carried}, vocabulary)


def unregistered(path=None, sources_path=None) -> list:
    """Sources in config/status.yaml that config/sources.yaml doesn't list (a test keeps this empty)."""
    path = Path(path or os.environ.get("LC_STATUS_CONFIG") or CONFIG / "status.yaml")
    sources_file = Path(sources_path or os.environ.get("LC_SOURCES_CONFIG") or CONFIG / "sources.yaml")
    listed = {s["id"] for s in (yaml.safe_load(sources_file.read_text(encoding="utf-8")) or {}).get("sources", [])}
    return sorted(set((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("sources") or {}) - listed)


@lru_cache(maxsize=4)
def _cached(path, labels_path, sources_path) -> StatusMap:
    return load(path, labels_path, sources_path)


def default() -> StatusMap:
    """The map from the configured files, read once per process (per set of paths)."""
    return _cached(os.environ.get("LC_STATUS_CONFIG"), os.environ.get("LC_LABELS_CONFIG"),
                   os.environ.get("LC_SOURCES_CONFIG"))
