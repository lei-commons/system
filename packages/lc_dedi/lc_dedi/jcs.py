"""JSON Canonicalization Scheme (RFC 8785), for the JSON that DeDi documents carry.

DeDi signs the JCS form of a document (minus its ``proof``). JCS is: no insignificant
whitespace, object members sorted by the UTF-16 code units of their names, strings
escaped minimally (only ``"``, ``\\`` and control characters), and numbers in the
ECMAScript shortest form.

lei-commons' DeDi documents hold strings, integers, booleans, null, arrays and objects.
Integers within +-2**53 serialise identically under ECMAScript and Python, so they are
supported; floats are refused rather than risk a canonical form that differs from
other implementations (counts, sizes and dates never need them). Cross-checked against
dedi-cli (Go, github.com/gowebpki/jcs) in tests/test_conformance.py.
"""

from __future__ import annotations

import json

MAX_SAFE_INTEGER = 2**53 - 1


class CanonicalizationError(ValueError):
    """The value can't be canonicalised without ambiguity."""


def _key(name: str):
    # RFC 8785 3.2.3: sort by UTF-16 code units, not by code points.
    return name.encode("utf-16-be")


def _string(s: str) -> str:
    # json.dumps with ensure_ascii=False escapes exactly ", \ and U+0000..U+001F, using
    # the short forms \b \f \n \r \t and \u00XX otherwise: the same as RFC 8785 3.2.2.2.
    return json.dumps(s, ensure_ascii=False)


def _write(value, out: list) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonicalizationError(f"integer {value} is outside the IEEE 754 safe range")
        out.append(str(value))
    elif isinstance(value, float):
        raise CanonicalizationError("floats are not used in lei-commons DeDi documents")
    elif isinstance(value, str):
        _check_text(value)
        out.append(_string(value))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _write(item, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        for i, name in enumerate(sorted(value, key=_key_checked)):
            if i:
                out.append(",")
            out.append(_string(name))
            out.append(":")
            _write(value[name], out)
        out.append("}")
    else:
        raise CanonicalizationError(f"unsupported type {type(value).__name__}")


def _key_checked(name):
    if not isinstance(name, str):
        raise CanonicalizationError("object member names must be strings")
    _check_text(name)
    return _key(name)


def _check_text(s: str) -> None:
    # Lone surrogates can't be encoded as UTF-8; I-JSON (RFC 7493) forbids them.
    try:
        s.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalizationError("strings must be valid Unicode (no lone surrogates)") from exc


def canonicalize(value) -> bytes:
    """The RFC 8785 canonical UTF-8 bytes of ``value``.

    >>> canonicalize({"b": [1, "é", None], "a": True})
    b'{"a":true,"b":[1,"\\xc3\\xa9",null]}'
    """
    out: list = []
    _write(value, out)
    return "".join(out).encode("utf-8")
