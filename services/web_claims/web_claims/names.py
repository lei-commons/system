"""Legal-form words, candidate web addresses from a legal name, and the entity-name guard.

Reference data, not settings: the legal-form words of the pilot's countries (and their common spellings).
"""

from __future__ import annotations

import re
import unicodedata

# Legal-form words and abbreviations, lower case, without dots. A name's trailing words that are in here are
# its legal form; a parent named without any of them (and without a register number or LEI) might be a person,
# so it is never kept.
LEGAL_FORMS = {
    # English-speaking
    "ltd", "limited", "plc", "llp", "lp", "llc", "inc", "incorporated", "corp", "corporation", "co", "company",
    "pte", "private", "public", "cic", "pllc", "pc", "lllp", "trust", "foundation", "association",
    # Nordic
    "oy", "oyj", "ab", "abp", "ky", "ay", "osk", "ry", "sr", "as", "asa", "a/s", "aps", "i/s", "hf", "ehf",
    # German, Austrian, Swiss, Dutch, Belgian
    "gmbh", "ag", "kg", "ohg", "ug", "se", "ev", "eg", "kgaa", "bv", "nv", "vof", "cv", "bvba", "sprl", "srl",
    # French, Italian, Spanish, Portuguese
    "sa", "sas", "sasu", "sarl", "eurl", "snc", "sca", "spa", "sapa", "scarl", "sl", "slu", "sau", "lda",
    "ltda", "sgps",
    # Central and Eastern Europe, Baltics, Greece, Cyprus, Malta
    "sro", "spol", "zoo", "sp", "ska", "kft", "zrt", "nyrt", "bt", "doo", "dd", "ad", "ood", "eood", "uab",
    "sia", "ou", "tuu", "ae", "epe", "ike", "oe",
}
WORD = re.compile(r"[a-z0-9]+")


def fold(text: str) -> str:
    """Lower case, accents removed, & as and, whitespace collapsed."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).replace("&", " and ")
    return " ".join(text.lower().split())


def words(name: str) -> list:
    return WORD.findall(fold(name).replace("/", ""))


def core_words(name: str) -> list:
    """The name without its legal-form words at either end, and without 'the'."""
    w = words(name)
    while w and w[-1] in LEGAL_FORMS:
        w.pop()
    while w and (w[0] in LEGAL_FORMS or w[0] == "the"):
        w.pop(0)
    return w


def has_legal_form(name: str) -> bool:
    return any(w in LEGAL_FORMS for w in words(name))


def legal_form_pattern() -> re.Pattern:
    """A legal-form word in page text. Forms of three letters or more in any case; two-letter forms (Oy, AB,
    AG, SA...) only capitalised or in capitals, so that the English words 'as', 'co' and 'ad' don't count."""
    long = sorted((re.escape(f) for f in LEGAL_FORMS if len(f) >= 3), key=len, reverse=True)
    short = sorted({v for f in LEGAL_FORMS if len(f) == 2 for v in (f.upper(), f.capitalize())})
    return re.compile(r"(?<![A-Za-z0-9])(?:(?i:" + "|".join(long) + r")|" + "|".join(short) + r")\.?(?![A-Za-z0-9])")


def candidate_domains(name: str, country: str, country_domains: dict, everywhere: tuple, limit: int) -> list:
    """Web addresses a company of this name might use, likeliest first: the core name joined and hyphenated,
    under the country's usual endings, then the ones used everywhere (.com)."""
    core = core_words(name)
    if not core or len("".join(core)) < 3:
        return []
    labels = ["".join(core)]
    if len(core) > 1:
        labels.append("-".join(core))
        if len(core) > 2:
            labels.append("".join(core[:2]))
    endings = list(country_domains.get(country, (country.lower(),)))
    endings += [e for e in everywhere if e not in endings]
    out = []
    for ending in endings:
        for label in labels:
            if 3 <= len(label) <= 63:
                domain = f"{label}.{ending}"
                if domain not in out:
                    out.append(domain)
    return out[:limit]


def registrable(host: str) -> str:
    """The host without a leading www."""
    host = (host or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def ascii_host(host: str) -> str:
    """The host without a leading www, as the gateway takes it: an internationalised name in its ASCII form
    (IDNA); '' if it has none."""
    host = registrable(host)
    try:
        return host.encode("idna").decode("ascii") if host else ""
    except UnicodeError:
        return ""
