"""What the console's actions touch: the control state, the cycle's jobs, the decision log.

The console's identity may write the control bucket and create decision-log
entries; it may start the daily cycle's jobs, each granted on the job itself.
"""

from __future__ import annotations

import json
import secrets


class Conflict(Exception):
    """The control state changed while the action was being applied."""


class GoogleBackend:
    def __init__(self, project: str, region: str):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        from google.cloud import storage

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self._http = AuthorizedSession(credentials)
        self._storage = storage.Client(project=project)
        self.project, self.region = project, region

    def _state_blob(self):
        return self._storage.bucket(f"{self.project}-control").blob("state.json")

    def read_state(self):
        """(state dict or None, generation). None if missing or unreadable; generation 0 if missing."""
        blob = self._state_blob()
        try:
            blob.reload()
        except Exception:
            return None, 0
        try:
            data = json.loads(blob.download_as_bytes(if_generation_match=blob.generation))
        except ValueError:
            data = None
        return data, blob.generation

    def write_state(self, state: dict, generation: int) -> None:
        from google.api_core.exceptions import PreconditionFailed

        try:
            self._state_blob().upload_from_string(json.dumps(state, indent=2, sort_keys=True) + "\n",
                                                  content_type="application/json", if_generation_match=generation)
        except PreconditionFailed as exc:
            raise Conflict("the control state changed meanwhile; try again") from exc

    def run_job(self, job: str) -> str:
        url = f"https://run.googleapis.com/v2/projects/{self.project}/locations/{self.region}/jobs/{job}:run"
        response = self._http.post(url, json={}, timeout=30)
        response.raise_for_status()
        return ((response.json().get("metadata") or {}).get("name") or "").rsplit("/", 1)[-1]

    def record(self, event: dict) -> None:
        name = f"owner-control/{event['ts']}-console-{secrets.token_hex(3)}.json"
        self._storage.bucket(f"{self.project}-decision-log").blob(name).upload_from_string(
            json.dumps(event, indent=2, sort_keys=True), content_type="application/json", if_generation_match=0)
