"""Common Crawl, read through the conduct gateway: its index (index.commoncrawl.org) and archived pages
(data.commoncrawl.org, one WARC record per ranged request). Both hosts are on the egress allow-list as
programmatic hosts, so the gateway asks each one request at a time, at most one a second (system#141).

A page is read from the archive, never from the company's own site, so the pilot puts no load on anyone's
site. Its bytes are held in memory only while the record is checked; only their SHA-256 is kept.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import urllib.parse
import zlib
from dataclasses import dataclass

from lc_core.fetch import FetchError

INDEX = "https://index.commoncrawl.org"
DATA = "https://data.commoncrawl.org"
CRAWL_ID = re.compile(r"^CC-MAIN-\d{4}-\d{2}$")
WARC_PATH = re.compile(r"^crawl-data/CC-MAIN-\d{4}-\d{2}/[A-Za-z0-9._/\-]+\.warc\.gz$")
FIELDS = "url,timestamp,status,mime,filename,offset,length"


class ArchiveError(Exception):
    """An index answer or an archived record isn't what Common Crawl promises."""


@dataclass(frozen=True)
class Capture:
    url: str
    timestamp: str     # YYYYMMDDhhmmss
    filename: str
    offset: int
    length: int

    @property
    def captured_at(self) -> str:
        t = self.timestamp
        return f"{t[0:4]}-{t[4:6]}-{t[6:8]}T{t[8:10]}:{t[10:12]}:{t[12:14]}Z"


@dataclass(frozen=True)
class Page:
    capture: Capture
    html: bytes
    sha256: str
    charset: str


def _read(gateway, target: str, headers=None, limit: int = 50_000_000) -> bytes:
    conn, resp = gateway.open(target, headers=headers)
    try:
        data = resp.read(limit + 1)
        if len(data) > limit:
            raise ArchiveError(f"answer larger than {limit} bytes")
        return data
    finally:
        conn.close()


def crawls(gateway, count: int) -> list:
    """The newest ``count`` crawls' IDs, newest first (collinfo.json)."""
    try:
        info = json.loads(_read(gateway, f"{INDEX}/collinfo.json", {"Accept": "application/json"}, 5_000_000))
    except ValueError as exc:
        raise ArchiveError("collinfo.json isn't JSON") from exc
    ids = [c.get("id") for c in info if isinstance(c, dict) and CRAWL_ID.match(str(c.get("id", "")))]
    if not ids:
        raise ArchiveError("collinfo.json names no crawl")
    return sorted(ids, reverse=True)[:count]


def lookup(gateway, crawl: str, domain: str, limit: int) -> list:
    """Captures of ``domain`` and its subdomains in one crawl's index: HTML pages that answered 200.
    A domain the crawl never saw gives []."""
    if not CRAWL_ID.match(crawl):
        raise ValueError(f"not a crawl id: {crawl!r}")
    query = urllib.parse.urlencode([("url", domain), ("matchType", "domain"), ("output", "json"),
                                    ("fl", FIELDS), ("limit", str(limit)), ("filter", "=status:200")])
    try:
        text = _read(gateway, f"{INDEX}/{crawl}-index?{query}", {"Accept": "application/json"}, 20_000_000)
    except FetchError as exc:
        if exc.status == 404:  # "No Captures found"
            return []
        raise
    out = []
    for line in text.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or str(row.get("status")) != "200" or "html" not in str(row.get("mime", "")):
            continue
        filename = str(row.get("filename", ""))
        try:
            offset, length = int(row["offset"]), int(row["length"])
        except (KeyError, TypeError, ValueError):
            continue
        ts = str(row.get("timestamp", ""))
        if not WARC_PATH.match(filename) or offset < 0 or length <= 0 or not re.fullmatch(r"\d{14}", ts):
            continue
        out.append(Capture(str(row.get("url", "")), ts, filename, offset, length))
    return out


def _charset(content_type: str, html: bytes) -> str:
    m = re.search(r"charset=[\"']?([A-Za-z0-9._\-]+)", content_type or "")
    if not m:
        m = re.search(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9._\-]+)", html[:4096], re.I)
        name = m.group(1).decode("ascii", "replace") if m else "utf-8"
    else:
        name = m.group(1)
    try:
        "".encode(name)
        return name.lower()
    except LookupError:
        return "utf-8"


def parse_record(raw: bytes, max_bytes: int) -> tuple:
    """(HTTP payload bytes, Content-Type) of one gzipped WARC response record."""
    try:
        data = gzip.decompress(raw)
    except (OSError, EOFError, zlib.error) as exc:
        raise ArchiveError("the archived record isn't gzip") from exc
    if not data.startswith(b"WARC/"):
        raise ArchiveError("not a WARC record")
    head, sep, rest = data.partition(b"\r\n\r\n")
    if not sep or b"WARC-Type: response" not in head:
        raise ArchiveError("not a WARC response record")
    http_head, sep, body = rest.partition(b"\r\n\r\n")
    if not sep:
        raise ArchiveError("the record has no HTTP headers")
    content_type, encoding = "", ""
    for line in http_head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        name = name.strip().lower()
        if name == b"content-type":
            content_type = value.strip().decode("latin-1")
        elif name == b"content-encoding":
            encoding = value.strip().decode("latin-1").lower()
    if encoding in ("gzip", "x-gzip"):  # Common Crawl normally stores the decoded payload; just in case
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError, zlib.error):
            pass
    return body[:max_bytes], content_type


def read_page(gateway, capture: Capture, max_bytes: int) -> Page:
    """One archived page, by a ranged request for its WARC record."""
    if not WARC_PATH.match(capture.filename):
        raise ValueError("not a Common Crawl WARC path")
    end = capture.offset + capture.length - 1
    raw = _read(gateway, f"{DATA}/{capture.filename}", {"Range": f"bytes={capture.offset}-{end}"},
                capture.length + 1024)
    html, content_type = parse_record(raw, max_bytes)
    return Page(capture, html, "sha256:" + hashlib.sha256(html).hexdigest(), _charset(content_type, html))
