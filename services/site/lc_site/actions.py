"""Owner actions taken inline on the site's pages (Data sources, System).

POST /api/v1/act with a JSON body {action, reason, code}. Accepted only when:
- the request carries the owner's IAP login (checked by the server first);
- the browser marks it same-origin (Sec-Fetch-Site), with a JSON body, so no
  other site and no plain HTML form can send it;
- the reason is given and the authenticator code is fresh: a code works once,
  across the site and the owner console (lc_core.totp).
The work itself is lc_core.owner.perform, the same code the console runs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from lc_core.owner import Outcome, perform

MAX_BODY = 4096
PENDING = "pending:"  # the console's marker for a key that hasn't been confirmed yet


class OwnerActions:
    def __init__(self, *, backend, verifier, read_secret, sources, registry, clock=None, log=None, on_change=None):
        self.backend, self.verifier, self._read_secret = backend, verifier, read_secret
        self.sources, self.registry = sources, registry
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.log = log or (lambda record: print(json.dumps(record, sort_keys=True), flush=True))
        self.on_change = on_change or (lambda: None)

    def secret(self):
        value = self._read_secret()
        return None if not value or value.startswith(PENDING) else value

    def handle(self, email: str, headers, body: bytes) -> Outcome:
        if headers.get("Sec-Fetch-Site") != "same-origin":
            return Outcome(403, "Actions must come from this site's own pages.")
        if not (headers.get("Content-Type") or "").startswith("application/json"):
            return Outcome(415, "Send the action as JSON.")
        if not body or len(body) > MAX_BODY:
            return Outcome(400, "The request is empty or too big.")
        try:
            data = json.loads(body)
            action, reason, code = str(data.get("action", "")), str(data.get("reason", "")), str(data.get("code", ""))
        except (ValueError, AttributeError):
            return Outcome(400, "The request isn't valid JSON.")
        try:
            secret = self.secret()
        except Exception as exc:
            self.log({"event": "secret-unavailable", "error": type(exc).__name__})
            return Outcome(503, "The second factor can't be checked right now, so nothing was done.")
        outcome = perform(backend=self.backend, verifier=self.verifier, secret=secret, sources=self.sources,
                          registry=self.registry, action=action, reason=reason, code=code, email=email,
                          now=self.clock().strftime("%Y-%m-%dT%H:%M:%SZ"), trigger="site", log=self.log)
        self.log({"event": "owner-action", "action": action, "status": outcome.status})
        if outcome.ok:
            self.on_change()
        return outcome
