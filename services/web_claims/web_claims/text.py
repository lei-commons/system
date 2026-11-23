"""Page text, the choice of pages, and what a model may see.

Page text lives in memory only while one record is checked; it is never stored or logged. Before any text goes
to a model it is minimised (owner, 2026-11-23: "make sure NO people data is processed or stored"): only lines
that name a company, a register number or a group link are kept, and any line with a person's role or title,
an email address or a phone number is dropped.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
              "footer", "header", "address", "dd", "dt", "table", "ul", "ol", "nav", "main", "aside", "title"}


class _Text(HTMLParser):
    def __init__(self, limit: int):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.size, self.limit = [], 0, 0, limit

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip or self.size >= self.limit:
            return
        self.parts.append(data)
        self.size += len(data)


def html_text(html: bytes, charset: str, limit: int) -> str:
    """The page's visible text, one block per line, at most ``limit`` characters."""
    p = _Text(limit)
    try:
        p.feed(html.decode(charset or "utf-8", "replace"))
        p.close()
    except Exception:  # a broken page gives what was read before it broke
        pass
    lines = (" ".join(line.split()) for line in "".join(p.parts).splitlines())
    return "\n".join(line for line in lines if line)[:limit]


# Pages most likely to state the operator's legal name, register number and group, in the pilot's languages.
EVIDENCE_PATHS = re.compile(
    r"(?i)(impressum|imprint|legal|mentions|aviso|note-legali|colofon|about|company|corporate|who-we-are|"
    r"yhteystiedot|yritys|meista|tietoa|om-oss|om-os|ueber-uns|uber-uns|a-propos|qui-sommes|chi-siamo|"
    r"sobre|quienes|over-ons|o-nas|contact|kontakt|terms|privacy|tietosuoja|datenschutz|group|konserni)")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def same_site(url: str, domain: str) -> bool:
    host = _host(url)
    return host == domain or host == "www." + domain


def choose(captures: list, domain: str, max_pages: int) -> list:
    """The home page, then evidence pages (legal, imprint, about, contact...), newest capture of each path,
    only on ``domain`` itself or its www host, no query strings."""
    newest = {}
    for c in captures:
        if not same_site(c.url, domain):
            continue
        parts = urlsplit(c.url)
        if parts.query:
            continue
        path = parts.path.rstrip("/") or "/"
        if path not in newest or c.timestamp > newest[path].timestamp:
            newest[path] = c
    if not newest:
        return []
    home = newest.get("/") or min(newest.values(), key=lambda c: (len(urlsplit(c.url).path), c.url))
    rest = sorted((c for p, c in newest.items() if c is not home and EVIDENCE_PATHS.search(p)),
                  key=lambda c: (_rank(urlsplit(c.url).path), len(c.url), c.url))
    return [home] + rest[:max_pages - 1]


def _rank(path: str) -> int:
    """Legal pages first (they must show the register number), then about, then contact, then the rest."""
    for rank, words in enumerate((r"impressum|imprint|legal|mentions|aviso|note-legali|colofon",
                                  r"about|company|corporate|yritys|meista|tietoa|om-|ber-uns|propos|qui-|chi-|"
                                  r"sobre|quienes|over-ons|o-nas|group|konserni",
                                  r"contact|kontakt|yhteystiedot")):
        if re.search(words, path, re.I):
            return rank
    return 3


EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+|\[at\]|\(at\)|\bmailto\b", re.I)
PHONE = re.compile(r"(\+|\b00)\d[\d\s().\-/]{6,}\d|\b\d{2,4}[\s.\-/]\d{2,4}[\s.\-/]\d{2,4}[\s.\-/]?\d{0,4}\b|"
                   r"(?i:\b(tel|puh|phone|fax|telefon|téléphone|mobile|gsm)\b)")
# A person's role or title: a line naming one is dropped whole (the name usually follows the role).
PERSON = re.compile(
    r"(?i)\b(ceo|cfo|coo|cto|md|chair(man|woman|person)?|president|director|managing|manager|founder|co-founder|"
    r"owner|partner|board|officer|secretary|contact person|yhteyshenkilö|toimitusjohtaja|hallitus|"
    r"geschäftsführer|geschaftsfuhrer|geschäftsführung|vorstand|inhaber|vertreten durch|represented by|"
    r"gérant|président|directeur|amministratore|direttore|director general|bestuurder|directeur|"
    r"verantwortlich|responsible|editor|redaktion|mr|mrs|ms|miss|dr|prof|herr|frau|mme|m\.|sig\.|sr\.|sra\.)\b")
# What a line must hold to be useful: a legal-form word, a number that could be a register number, an LEI, or
# a group link.
GROUP = re.compile(
    r"(?i)\b(group|parent|subsidiar(y|ies)|owned|part of|member of|affiliate|holding|konserni|emoyhtiö|"
    r"tytäryhtiö|omistama|osa|tochter|mutter|konzern|gruppe|groupe|filiale|société mère|gruppo|controllata|"
    r"grupo|filial|dochter|moeder|dotterbolag|moderbolag|koncern)\b")
NUMBER = re.compile(r"\b[A-Z]{0,3}[\s\-]?\d[\d\s.\-]{4,14}[\dA-Z]\b|\b[0-9A-Z]{18}\d{2}\b")


def minimise(texts: list, legal_forms: re.Pattern, limit: int) -> str:
    """The lines a model may see, from all of a record's pages, at most ``limit`` characters."""
    kept, seen, size = [], set(), 0
    for text in texts:
        for line in text.splitlines():
            line = line.strip()
            if not line or line in seen or len(line) > 400:
                continue
            if EMAIL.search(line) or PHONE.search(line) or PERSON.search(line):
                continue
            if not (legal_forms.search(line) or GROUP.search(line) or NUMBER.search(line)):
                continue
            seen.add(line)
            if size + len(line) + 1 > limit:
                return "\n".join(kept)
            kept.append(line)
            size += len(line) + 1
    return "\n".join(kept)
