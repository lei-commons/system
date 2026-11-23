"""Hard match evidence: what ties a page to a record. Deterministic, no model.

In order of strength: the record's LEI on the page; its register number; its exact legal name plus its
country. Anything less is no evidence, and a claim without evidence is discarded, never shown as "likely".
"""

from __future__ import annotations

import re

from .names import fold

LEI, NUMBER, NAME = "lei", "registration-number", "legal-name+country"
STRENGTH = {LEI: 3, NUMBER: 2, NAME: 1}

# Country names as pages write them (English and local), for the name-plus-country evidence. A page on the
# country's own domain ending counts as naming the country.
COUNTRY_WORDS = {
    "FI": ["finland", "suomi", "helsinki", "espoo", "tampere", "turku", "oulu", "vantaa"],
    "GB": ["united kingdom", "england", "scotland", "wales", "northern ireland", "london", "uk"],
    "US": ["united states", "usa", "u.s.a"],
    "SG": ["singapore"],
    "AT": ["austria", "osterreich"], "BE": ["belgium", "belgique", "belgie"], "BG": ["bulgaria"],
    "CY": ["cyprus"], "CZ": ["czech", "cesko", "ceska republika"], "DE": ["germany", "deutschland"],
    "DK": ["denmark", "danmark"], "EE": ["estonia", "eesti"], "ES": ["spain", "espana"],
    "FR": ["france"], "GR": ["greece", "hellas", "ellada"], "HR": ["croatia", "hrvatska"],
    "HU": ["hungary", "magyarorszag"], "IE": ["ireland", "eire", "dublin"], "IT": ["italy", "italia"],
    "LT": ["lithuania", "lietuva"], "LU": ["luxembourg"], "LV": ["latvia", "latvija"], "MT": ["malta"],
    "NL": ["netherlands", "nederland", "the netherlands"], "PL": ["poland", "polska"],
    "PT": ["portugal"], "RO": ["romania"], "SE": ["sweden", "sverige"], "SI": ["slovenia", "slovenija"],
    "SK": ["slovakia", "slovensko"],
}
US_STATES = {
    "NY": "new york", "CO": "colorado", "OR": "oregon", "CT": "connecticut", "CA": "california", "TX": "texas",
    "DE": "delaware", "FL": "florida", "IL": "illinois", "MA": "massachusetts", "NJ": "new jersey",
    "PA": "pennsylvania", "WA": "washington", "GA": "georgia", "NC": "north carolina", "VA": "virginia",
    "OH": "ohio", "MI": "michigan", "AZ": "arizona", "MN": "minnesota", "NV": "nevada",
}
CC_ENDINGS = {"GB": (".uk",), "US": (".us",)}


def number_variants(number: str) -> list:
    """Ways a register number is written: as stored, without separators, without leading zeros, and Finland's
    business ID as its VAT number (FI + the eight digits). Only variants of six characters or more: a shorter
    one would match too much."""
    raw = (number or "").strip().upper()
    alnum = re.sub(r"[^0-9A-Z]", "", raw)
    out = {raw, alnum, alnum.lstrip("0")}
    if re.fullmatch(r"\d{7}-\d", raw):
        out.add("FI" + alnum)
    return sorted((v for v in out if len(re.sub(r"[^0-9A-Z]", "", v)) >= 6), key=len, reverse=True)


def _contains_token(haystack: str, needle: str) -> bool:
    return re.search(r"(?<![0-9A-Z])" + re.escape(needle) + r"(?![0-9A-Z])", haystack) is not None


def _spaced(text: str) -> str:
    """Upper case with separators inside numbers removed, so 1234567-8, 1234 5678 and 12.345.678 read whole."""
    up = text.upper()
    return re.sub(r"(?<=[0-9A-Z])[\s.\-/](?=[0-9])", "", up)


def country_named(text_folded: str, domain: str, country: str, region: str = "") -> bool:
    if domain.endswith("." + country.lower()) or any(domain.endswith(e) for e in CC_ENDINGS.get(country, ())):
        return True
    words = list(COUNTRY_WORDS.get(country, []))
    if country == "US" and region:
        state = US_STATES.get(region.upper())
        words += [state] if state else []
    return any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", text_folded) for w in words)


def find(record: dict, texts: list, domain: str) -> dict | None:
    """The strongest evidence in a record's page texts: {kind, detail}, or None."""
    joined = "\n".join(texts)
    upper = joined.upper()
    lei = (record.get("lei") or "").upper()
    if re.fullmatch(r"[0-9A-Z]{18}\d{2}", lei) and _contains_token(upper, lei):
        return {"kind": LEI}
    number = record.get("registrationNumber") or ""
    if number:
        squeezed = _spaced(joined)
        for v in number_variants(number):
            if _contains_token(upper, v) or _contains_token(squeezed, re.sub(r"[^0-9A-Z]", "", v)):
                return {"kind": NUMBER}
    folded = fold(joined)
    for name in [record.get("legalName") or ""] + list(record.get("otherNames") or [])[:3]:
        n = fold(name)
        if len(n) >= 4 and re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", folded):
            if country_named(folded, domain, record.get("country", ""), record.get("region", "")):
                return {"kind": NAME}
    return None
