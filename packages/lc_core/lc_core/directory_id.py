"""lei-commons directory identifiers.

A directory ID has the same shape as an LEI (ISO 17442): 20 characters,
alphanumeric, ending in two ISO 7064 MOD 97-10 check digits. It can never be
mistaken for a real LEI because it uses the reserved prefix ``LECO`` followed by
``ZZ`` in positions 5-6.

    LECO ZZ XXXXXXXXXXXX CC
    |    |  |            +- check digits (ISO 7064 MOD 97-10)
    |    |  +- 12 base-36 characters of SHA-256(country|registry|number[|salt])
    |    +- fixed "ZZ" marker
    +- lei-commons prefix

The ID is deterministic: the same registry record always mints the same ID, so
any agent (or anyone else) can recompute it. ``salt`` is only used when two
different inputs collide on the 12-character body, which the resolver detects.
"""

from __future__ import annotations

import hashlib
import re

PREFIX = "LECO"
MARKER = "ZZ"
BODY_LEN = 12
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ID_RE = re.compile(r"^LECOZZ[0-9A-Z]{12}[0-9]{2}$")
_LEI_RE = re.compile(r"^[0-9A-Z]{18}[0-9]{2}$")


def normalise_number(number: str) -> str:
    """Registration numbers are compared without spaces, dots, dashes or slashes."""
    return re.sub(r"[\s.\-/]", "", number).upper()


def _base36(n: int, width: int) -> str:
    out = []
    for _ in range(width):
        n, r = divmod(n, 36)
        out.append(_ALPHABET[r])
    return "".join(reversed(out))


def _to_digits(s: str) -> int:
    return int("".join(str(int(c, 36)) for c in s))


def check_digits(body18: str) -> str:
    """ISO 7064 MOD 97-10 check digits for an 18-character LEI body."""
    if len(body18) != 18 or not body18.isalnum() or body18.upper() != body18:
        raise ValueError("body must be 18 upper-case alphanumeric characters")
    return f"{98 - _to_digits(body18 + '00') % 97:02d}"


def is_valid(code: str) -> bool:
    """True if ``code`` is a well-formed LEI-shaped code with correct check digits."""
    return bool(_LEI_RE.match(code)) and _to_digits(code) % 97 == 1


def is_directory_id(code: str) -> bool:
    """True if ``code`` is a valid lei-commons directory ID (not a real LEI)."""
    return bool(_ID_RE.match(code)) and is_valid(code)


def mint(country: str, registry: str, number: str, salt: int = 0) -> str:
    """Mint the directory ID for a registry record.

    >>> mint("GB", "GB-COH", "01234567")
    'LECOZZFZ6LZCOJBDKT73'
    """
    country = country.strip().upper()
    registry = registry.strip().upper()
    number = normalise_number(number)
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError(f"country must be ISO 3166-1 alpha-2, got {country!r}")
    if not registry or not number:
        raise ValueError("registry and number are required")
    if salt < 0:
        raise ValueError("salt must be >= 0")
    key = f"{country}|{registry}|{number}"
    if salt:
        key += f"|{salt}"
    digest = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16)
    body = PREFIX + MARKER + _base36(digest % 36**BODY_LEN, BODY_LEN)
    return body + check_digits(body)
