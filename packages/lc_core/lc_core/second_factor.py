"""The owner's second factor (an authenticator app's time-based codes): setting it up and rotating its key.

The key lives in Secret Manager (secret ``console-totp``; each change adds a version). Its value is one of:

- ``pending:<key>``: not set up yet. The key is shown once on the admin page and becomes active when the
  owner enters a code from it (so a restart can't swap the key after the owner has added it to their app);
- ``<key>``: set up; every owner action needs a fresh code from it (lc_core.totp, lc_core.owner);
- ``rotating:<new>:<current>``: a new key is being set up. The current key still signs every action until the
  owner enters a code from the new one, which then replaces it. Starting a rotation needs a code from the
  current key, so seeing the admin page (IAP with re-authentication) is never enough to change the key once
  one is set up.

Only the admin area offers these steps (owner decision 2026-11-21); the functions here are pure, so the site
and the tests share them.
"""

from __future__ import annotations

import base64
import secrets
from urllib.parse import quote

from .totp import DIGITS, STEP

PENDING = "pending:"
ROTATING = "rotating:"


def new_key() -> str:
    """A fresh 160-bit key, base32 (what authenticator apps take)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def otpauth_uri(key: str, account: str) -> str:
    return (f"otpauth://totp/lei-commons:{quote(account)}?secret={key}&issuer=lei-commons"
            f"&digits={DIGITS}&period={STEP}")


def parse(value: str | None) -> dict:
    """The stored value as {active, waiting}: the key actions use now, and a key waiting for its first code.

    >>> parse(None)
    {'active': None, 'waiting': None}
    >>> parse("pending:AAAA")
    {'active': None, 'waiting': 'AAAA'}
    >>> parse("rotating:BBBB:AAAA")
    {'active': 'AAAA', 'waiting': 'BBBB'}
    >>> parse("AAAA")
    {'active': 'AAAA', 'waiting': None}
    """
    if not value:
        return {"active": None, "waiting": None}
    if value.startswith(PENDING):
        return {"active": None, "waiting": value[len(PENDING):] or None}
    if value.startswith(ROTATING):
        new, _, current = value[len(ROTATING):].partition(":")
        return {"active": current or None, "waiting": new or None}
    return {"active": value, "waiting": None}


def active(value: str | None) -> str | None:
    """The key every owner action is checked against, or None if none is set up."""
    return parse(value)["active"]


def status(value: str | None) -> str:
    """not-set-up, setting-up, active or rotating."""
    p = parse(value)
    if p["active"] is None:
        return "setting-up" if p["waiting"] else "not-set-up"
    return "rotating" if p["waiting"] else "active"


def begin_setup(value: str | None) -> str | None:
    """The value to store so a key waits for its first code, or None if nothing needs storing.

    Only when no key is set up: a set-up key changes only through a rotation."""
    p = parse(value)
    if p["active"] is not None or p["waiting"] is not None:
        return None
    return PENDING + new_key()


def begin_rotation(value: str | None) -> str:
    """The value that keeps the current key active while a new one waits. The caller has checked a code from
    the current key first."""
    p = parse(value)
    if p["active"] is None:
        raise ValueError("no key is set up yet, so there is nothing to rotate")
    return f"{ROTATING}{new_key()}:{p['active']}"


def confirm(value: str | None) -> str:
    """The waiting key, which becomes the active one. The caller has checked a code from the waiting key."""
    p = parse(value)
    if p["waiting"] is None:
        raise ValueError("no key is waiting to be confirmed")
    return p["waiting"]


def cancel_rotation(value: str | None) -> str:
    """Back to the current key alone."""
    p = parse(value)
    if p["active"] is None or p["waiting"] is None:
        raise ValueError("no rotation is in progress")
    return p["active"]
