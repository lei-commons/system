"""The decision, with no I/O.

The guard is the one automated writer of the control state, and only ever in one
direction: it can pause a system that is working, and it can never resume one or
touch a system the owner has stopped or paused.

Spend is compared with the budget in Google's own message, in the budget's own
currency, so no exchange rate is involved. Forecasts are ignored: only money
actually spent pauses the system.
"""

from __future__ import annotations

import base64
import binascii
import json
import math

BUDGET_NAME = "lei-commons monthly cap"
PAUSABLE = ("running", "manual")


def parse_push(body: bytes):
    """Return the budget notification from a Pub/Sub push body, or None if it isn't one."""
    try:
        data = json.loads(body)["message"]["data"]
        payload = json.loads(base64.b64decode(data, validate=True))
        cost, budget = float(payload["costAmount"]), float(payload["budgetAmount"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None
    if not math.isfinite(cost) or not math.isfinite(budget) or budget <= 0 or cost < 0:
        return None
    if payload.get("budgetDisplayName") != BUDGET_NAME:
        return None
    return {"cost": cost, "budget": budget, "currency": str(payload.get("currencyCode", ""))}


def decide(notice: dict, state):
    """Return the new control state, or None if nothing should change."""
    if notice["cost"] < notice["budget"]:
        return None
    if not isinstance(state, dict):
        # Unreadable state already stops everything: the control gate fails closed.
        return None
    if state.get("state") not in PAUSABLE:
        return None
    return {**state, "state": "paused", "updated_by": "budget-guard"}
