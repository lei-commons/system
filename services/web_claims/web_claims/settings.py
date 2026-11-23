"""config/web_claims.yaml and the pilot's line in config/models.yaml, checked when read: a missing or
out-of-range value stops the run before it fetches anything."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


class SettingsError(ValueError):
    """config/web_claims.yaml or config/models.yaml is missing something, or has a value out of range."""


COUNTRY = re.compile(r"^[A-Z]{2}$")
TLD = re.compile(r"^[a-z]{2,10}(\.[a-z]{2,10})?$")
TASK = "web_claims"


@dataclass(frozen=True)
class Model:
    vendor: str        # claude or gemini
    model_class: str   # e.g. claude-haiku
    model_id: str      # the Vertex AI model ID, from models.yaml model_ids
    location: str      # Vertex AI location
    price_in: float    # USD per million input tokens (approximate)
    price_out: float


@dataclass(frozen=True)
class Settings:
    enabled: bool
    with_website_count: int
    with_website_countries: tuple
    without_website_count: int
    without_website_groups: dict          # {group: (countries...)}
    status_category: str
    discovery_truth_sample: int
    max_candidates: int
    country_domains: dict                 # {country: (tlds...)}
    everywhere: tuple
    crawls: int
    index_limit: int
    max_pages: int
    max_record_bytes: int
    max_text_chars: int
    max_model_chars: int
    run_minutes: int
    workers: int
    max_model_usd_per_run: float
    max_model_usd_total: float
    max_model_errors_in_a_row: int
    sample: int
    precision_bar: float
    maker: Model
    checker: Model

    def countries(self) -> set:
        """Every country a candidate can come from."""
        return set(self.with_website_countries) | {c for cs in self.without_website_groups.values() for c in cs}


def _num(cfg: dict, path: str, low, high, kind=int):
    node = cfg
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            raise SettingsError(f"web_claims.yaml needs {path}")
        node = node[key]
    if isinstance(node, bool) or not isinstance(node, (int, float)) or (kind is int and not isinstance(node, int)):
        raise SettingsError(f"{path} must be a {'whole ' if kind is int else ''}number")
    if not low <= node <= high:
        raise SettingsError(f"{path} must be from {low} to {high}")
    return kind(node)


def _countries(value, path: str) -> tuple:
    if not isinstance(value, list) or not value or not all(isinstance(c, str) and COUNTRY.match(c) for c in value):
        raise SettingsError(f"{path} must be a list of two-letter country codes")
    return tuple(value)


def _model(models: dict, role: str) -> Model:
    task = (models.get("tasks") or {}).get(TASK) or {}
    entry = task.get(role)
    if not isinstance(entry, dict):
        raise SettingsError(f"models.yaml tasks.{TASK}.{role} is missing")
    vendor, model_class = entry.get("vendor"), entry.get("model")
    if vendor not in ("claude", "gemini"):
        raise SettingsError(f"tasks.{TASK}.{role}.vendor must be claude or gemini")
    model_id = (models.get("model_ids") or {}).get(model_class)
    if not isinstance(model_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.\-@]{2,80}", model_id):
        raise SettingsError(f"models.yaml model_ids has no usable ID for {model_class!r}")
    location = entry.get("location") or ((models.get("endpoints") or {}).get(vendor) or {}).get("location")
    if not isinstance(location, str) or not re.fullmatch(r"[a-z0-9-]{2,40}", location):
        raise SettingsError(f"no Vertex AI location for tasks.{TASK}.{role}")
    price = (models.get("prices_usd_per_mtok") or {}).get(model_class)
    if (not isinstance(price, list) or len(price) != 2
            or not all(isinstance(p, (int, float)) and not isinstance(p, bool) and 0 < p < 1000 for p in price)):
        raise SettingsError(f"models.yaml prices_usd_per_mtok needs [input, output] for {model_class!r}")
    return Model(vendor, model_class, model_id, location, float(price[0]), float(price[1]))


def parse(cfg: dict, models: dict) -> Settings:
    if not isinstance(cfg, dict) or cfg.get("version") != 1:
        raise SettingsError("web_claims.yaml must be a mapping with version: 1")
    if not isinstance(cfg.get("enabled"), bool):
        raise SettingsError("enabled must be true or false")
    cand = cfg.get("candidates") or {}
    groups = (cand.get("without_website") or {}).get("groups")
    if not isinstance(groups, dict) or not groups:
        raise SettingsError("candidates.without_website.groups must name at least one group")
    groups = {str(g): _countries(cs, f"candidates.without_website.groups.{g}") for g, cs in groups.items()}
    status = cand.get("status_category")
    if not isinstance(status, str) or not status:
        raise SettingsError("candidates.status_category is missing")
    disc = cfg.get("discovery") or {}
    domains = disc.get("country_domains") or {}
    if not isinstance(domains, dict):
        raise SettingsError("discovery.country_domains must be a mapping")
    for country, tlds in domains.items():
        if not COUNTRY.match(str(country)) or not isinstance(tlds, list) or not all(isinstance(t, str) and TLD.match(t) for t in tlds):
            raise SettingsError(f"discovery.country_domains.{country} must be a list of domain endings")
    everywhere = disc.get("everywhere")
    if not isinstance(everywhere, list) or not all(isinstance(t, str) and TLD.match(t) for t in everywhere):
        raise SettingsError("discovery.everywhere must be a list of domain endings")
    models = models or {}
    s = Settings(
        enabled=cfg["enabled"],
        with_website_count=_num(cfg, "candidates.with_website.count", 0, 100_000),
        with_website_countries=_countries((cand.get("with_website") or {}).get("countries"), "candidates.with_website.countries"),
        without_website_count=_num(cfg, "candidates.without_website.count", 0, 100_000),
        without_website_groups=groups,
        status_category=status,
        discovery_truth_sample=_num(cfg, "candidates.discovery_truth_sample", 0, 100_000),
        max_candidates=_num(cfg, "discovery.max_candidates", 1, 20),
        country_domains={str(c): tuple(t) for c, t in domains.items()},
        everywhere=tuple(everywhere),
        crawls=_num(cfg, "pages.crawls", 1, 2),
        index_limit=_num(cfg, "pages.index_limit", 10, 2000),
        max_pages=_num(cfg, "pages.max_pages", 1, 8),
        max_record_bytes=_num(cfg, "pages.max_record_bytes", 10_000, 20_000_000),
        max_text_chars=_num(cfg, "pages.max_text_chars", 1000, 500_000),
        max_model_chars=_num(cfg, "pages.max_model_chars", 500, 50_000),
        run_minutes=_num(cfg, "run.minutes", 5, 1400),
        workers=_num(cfg, "run.workers", 1, 16),
        max_model_usd_per_run=_num(cfg, "run.max_model_usd_per_run", 0, 40, float),
        max_model_usd_total=_num(cfg, "run.max_model_usd_total", 0, 1000, float),
        max_model_errors_in_a_row=_num(cfg, "run.max_model_errors_in_a_row", 1, 100),
        sample=_num(cfg, "report.sample", 0, 200),
        precision_bar=_num(cfg, "report.precision_bar", 0.5, 1.0, float),
        maker=_model(models, "maker"),
        checker=_model(models, "checker"),
    )
    if s.maker.vendor == s.checker.vendor:
        raise SettingsError("the checker must come from a different vendor than the maker")
    limit = (models.get("limits") or {}).get("max_daily_model_usd")
    if isinstance(limit, (int, float)) and s.max_model_usd_per_run > limit:
        raise SettingsError("run.max_model_usd_per_run can't be above models.yaml limits.max_daily_model_usd")
    return s


def load(path=None, models_path=None) -> Settings:
    path = Path(path or os.environ.get("LC_WEB_CLAIMS_CONFIG", "config/web_claims.yaml"))
    models_path = Path(models_path or os.environ.get("LC_MODELS_CONFIG", "config/models.yaml"))
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        models = yaml.safe_load(models_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SettingsError(f"cannot read the pilot's settings: {exc}") from exc
    return parse(cfg, models)
