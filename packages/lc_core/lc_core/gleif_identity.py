"""How a GLEIF record's registration authority and number become a directory ID: the ``gleif_identity``
section of config/sources.yaml (owner decision 2026-11-20, "GLEIF re-key / aliases").

A register we read directly mints its records' IDs from its own key: country, GLEIF's code for the
register, and the register's number (``lc_core.directory_id.mint``). A company with an LEI merges with its
register record only if GLEIF's record gives the same three. Mostly it does. Where GLEIF writes them
differently, an entry here says how to read GLEIF's version:

- ``registry``: the code the register is read under, when GLEIF files some of its companies under another
  code for the same register (Norway: RA000473 for RA000472; Japan: RA000412, the commercial registration,
  for RA001075, the corporate number). Without it, GLEIF's own code.
- ``country``: the register's country, so a company with a foreign address still gets the register's ID.
  Without it, the record's address country (or jurisdiction), as for every other authority.
- ``number``: rules, in order, from ``RULES`` below. A rule that doesn't fit the number (it returns None)
  leaves the whole record as GLEIF writes it, under GLEIF's own code: a guess is never minted.

An authority not listed here mints with GLEIF's own code, number and the address country. Every entry is
checked when read (ConfigError), so a job stops before it writes anything rather than guess.

``fingerprint`` names one set of rules. Stored GLEIF records are re-keyed once for each fingerprint
(lc_ingest.rekey): a record whose ID changes leaves a ``merged`` tombstone at its old ID. Changing a
rule's code without changing its name must bump RULES_VERSION, so the re-key runs again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from .directory_id import normalise_number

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "sources.yaml"
SECTION = "gleif_identity"
RULES_VERSION = 1
SETTINGS = {"registry", "country", "number", "note"}
AUTHORITY = re.compile(r"^RA\d{6}$")
REGISTRY = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,31}$")
COUNTRY = re.compile(r"^[A-Z]{2}$")


class ConfigError(ValueError):
    """The gleif_identity section is missing or invalid."""


def jp_check_digit(base12: str) -> str:
    """The check digit of a Japanese corporate number (法人番号), from its 12 other digits.

    The NTA's rule: with P(n) the n-th digit counted from the right and Q(n) 1 for odd n and 2 for even n,
    the check digit is 9 - (sum of P(n) * Q(n)) mod 9. It is the corporate number's first digit.

    >>> jp_check_digit("180301018771")  # 1180301018771, Toyota Motor Corporation
    '1'
    """
    if not re.fullmatch(r"\d{12}", base12):
        raise ValueError("a corporate number's base is 12 digits")
    total = sum(int(d) * (1 if i % 2 == 0 else 2) for i, d in enumerate(reversed(base12)))
    return str(9 - total % 9)


def _strip_leading_zeros(number: str):
    """Oregon and Canada write numbers without leading zeros; GLEIF sometimes keeps them ("057491-14")."""
    stripped = number.lstrip("0")
    return stripped or None


def _jp_corporate_number(number: str):
    """A 12-digit commercial registration number (会社法人等番号) becomes the 13-digit corporate number: its
    check digit first. A 13-digit number with the right check digit is already one. Anything else: None."""
    if re.fullmatch(r"\d{12}", number):
        return jp_check_digit(number) + number
    if re.fullmatch(r"\d{13}", number) and jp_check_digit(number[1:]) == number[0]:
        return number
    return None


# Each rule takes a number already normalised (NFKC, no spaces, dots, dashes or slashes, upper case) and
# returns the register's form of it, or None when the number doesn't fit.
RULES = {"strip-leading-zeros": _strip_leading_zeros, "jp-corporate-number": _jp_corporate_number}


@dataclass(frozen=True)
class Authority:
    code: str
    registry: str
    country: str | None = None
    number: tuple = ()


@dataclass(frozen=True)
class Identity:
    authorities: dict  # GLEIF code -> Authority

    @property
    def fingerprint(self) -> str:
        """12 hex characters naming these rules; the re-key runs once for each."""
        canon = {"version": RULES_VERSION, "authorities": {
            code: [a.registry, a.country, list(a.number)] for code, a in sorted(self.authorities.items())}}
        return hashlib.sha256(json.dumps(canon, sort_keys=True).encode("utf-8")).hexdigest()[:12]

    def key(self, authority: str, number: str, country: str) -> tuple:
        """(country, registry, number) to mint the directory ID from, for GLEIF's ``registeredAt`` and
        ``registeredAs`` and the record's own country."""
        rule = self.authorities.get(authority)
        if rule is None:
            return country, authority, number
        value = number
        if rule.number:
            value = normalise_number(unicodedata.normalize("NFKC", number))
            for name in rule.number:
                value = RULES[name](value)
                if value is None:  # the number isn't in the form the rules expect: as GLEIF writes it
                    return country, authority, number
        return rule.country or country, rule.registry, value


def _authority(code, entry) -> Authority:
    where = f"{SECTION}.{code}"
    if not isinstance(code, str) or not AUTHORITY.match(code):
        raise ConfigError(f"{where}: an authority is a GLEIF code, RA and six digits")
    if not isinstance(entry, dict):
        raise ConfigError(f"{where}: must be a mapping")
    unknown = set(entry) - SETTINGS
    if unknown:
        raise ConfigError(f"{where}: unknown settings {', '.join(sorted(unknown))}")
    registry = entry.get("registry", code)
    if not isinstance(registry, str) or not REGISTRY.match(registry):
        raise ConfigError(f"{where}.registry: must be a register code (upper case letters, digits, dashes)")
    country = entry.get("country")
    if country is not None and (not isinstance(country, str) or not COUNTRY.match(country)):
        raise ConfigError(f"{where}.country: must be an ISO 3166-1 alpha-2 code")
    number = entry.get("number", [])
    if not isinstance(number, list) or not all(isinstance(r, str) for r in number):
        raise ConfigError(f"{where}.number: must be a list of rules")
    for name in number:
        if name not in RULES:
            raise ConfigError(f"{where}.number: unknown rule {name!r} (known: {', '.join(sorted(RULES))})")
    if registry == code and country is None and not number:
        raise ConfigError(f"{where}: says nothing (give a registry, a country or number rules)")
    return Authority(code, registry, country, tuple(number))


def parse(data) -> Identity:
    if not isinstance(data, dict):
        raise ConfigError("not a sources file")
    section = data.get(SECTION)
    if not isinstance(section, dict):
        raise ConfigError(f"{SECTION}: missing")
    authorities = {code: _authority(code, entry) for code, entry in section.items()}
    aliased = {a.code for a in authorities.values() if a.registry != a.code}
    chained = sorted(a.registry for a in authorities.values() if a.registry in aliased)
    if chained:  # an alias to an alias would depend on the order rules are applied in
        raise ConfigError(f"{SECTION}: {', '.join(chained)} is aliased itself; point at the final code")
    return Identity(authorities)


def load(path=None) -> Identity:
    """The rules from LC_SOURCES_CONFIG, or the repo's config/sources.yaml."""
    path = path or os.environ.get("LC_SOURCES_CONFIG") or DEFAULT_PATH
    return parse(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def default() -> Identity:
    """The rules every GLEIF mapping uses unless given others: read once per process."""
    return load()
