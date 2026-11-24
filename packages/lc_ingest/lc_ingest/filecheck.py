"""Checking a downloaded file before anything is read from it, for every source.

A download can be cut short, replaced by an error page, or corrupt. Each is caught here, by the file's
format, before a single record is read: the runner then fetches it again (lc_ingest.runner), logging
every bad download, and stops with a clear error if it never comes right. Nothing from a bad file is
stored. (Owner requirement 2026-11-17, after one bad Brazilian download failed a daily cycle.)
"""

from __future__ import annotations

import gzip
import os
import zipfile
import zlib

SNIFF = 64 * 1024
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
HTML_STARTS = (b"<!doctype html", b"<html", b"<?xml version", b"<head", b"<body")


class BadFile(Exception):
    """The downloaded file isn't what was expected: cut short, an error page, the wrong format or corrupt."""


# Errors that mean the file itself is bad (and fetching it again may help), as opposed to a bug.
FILE_ERRORS = (BadFile, zipfile.BadZipFile, zlib.error, EOFError, gzip.BadGzipFile, UnicodeDecodeError)


def kind_for(filename: str) -> str:
    """The format to check, from the file's name: zip, gzip, csv, json or other."""
    name = filename.lower()
    if name.endswith((".zip", ".xlsx")):
        return "zip"
    if name.endswith(".gz"):
        return "gzip"
    if name.endswith((".csv", ".tsv", ".txt")):
        return "csv"
    if name.endswith(".json"):
        return "json"
    return "other"


def _head(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read(SNIFF)


def _looks_like_html(head: bytes) -> bool:
    return head.lstrip().lower().startswith(HTML_STARTS)


def check(path: str, kind: str = None, encoding: str = "utf-8") -> None:
    """Raise BadFile unless ``path`` is a complete, readable file of ``kind`` (from its name if not given)."""
    kind = kind or kind_for(path)
    size = os.path.getsize(path)
    if size == 0:
        raise BadFile("the file is empty")
    head = _head(path)
    if _looks_like_html(head) and kind != "other":
        raise BadFile(f"an HTML page arrived instead of a {kind} file")
    if kind == "zip":
        if not zipfile.is_zipfile(path):
            raise BadFile("not a ZIP archive (cut short, or something else was sent)")
        try:
            with zipfile.ZipFile(path) as z:
                if not z.namelist():
                    raise BadFile("the ZIP archive is empty")
                bad = z.testzip()  # reads every member and checks its CRC
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as exc:
            raise BadFile(f"the ZIP archive is corrupt ({type(exc).__name__})") from exc
        if bad:
            raise BadFile(f"the ZIP archive is corrupt: {bad} fails its checksum")
    elif kind == "gzip":
        try:
            with gzip.open(path, "rb") as f:
                while f.read(1 << 20):
                    pass
        except (gzip.BadGzipFile, zlib.error, EOFError, OSError) as exc:
            raise BadFile(f"the gzip file is corrupt or cut short ({type(exc).__name__})") from exc
    elif kind == "csv":
        if head.startswith(UTF16_BOMS):  # a Windows export: UTF-16 with its byte-order mark
            try:
                first = head[:len(head) - len(head) % 2].decode("utf-16").split("\n", 1)[0]
            except UnicodeDecodeError as exc:
                raise BadFile("the text isn't utf-16, though it starts with its byte-order mark") from exc
            if not any(d in first for d in (",", "\t", ";", "|")):
                raise BadFile("the first line isn't a delimited header")
            return
        try:
            head.decode(encoding)
        except UnicodeDecodeError as exc:
            if exc.start < len(head) - 4:  # not merely a character cut at the end of the sample
                raise BadFile(f"the text isn't {encoding}") from exc
        first = head.split(b"\n", 1)[0]
        if b"," not in first and b"\t" not in first and b";" not in first and b"|" not in first:
            raise BadFile("the first line isn't a delimited header")
    elif kind == "json":
        start = head.lstrip()[:1]
        if start not in (b"[", b"{"):
            raise BadFile("not JSON")
