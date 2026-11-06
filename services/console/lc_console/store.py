"""Where the second-factor secret lives. Only the console identity can read it."""

from __future__ import annotations


class SecretManagerStore:
    def __init__(self, project: str, secret_id: str = "console-totp"):
        from google.cloud import secretmanager

        self._client = secretmanager.SecretManagerServiceClient()
        self._parent = f"projects/{project}/secrets/{secret_id}"

    def get(self):
        """Return the secret, or None if nobody has enrolled yet."""
        from google.api_core.exceptions import FailedPrecondition, NotFound

        try:
            response = self._client.access_secret_version(name=f"{self._parent}/versions/latest")
        except (NotFound, FailedPrecondition):
            return None
        return response.payload.data.decode("ascii")

    def put(self, value: str) -> None:
        self._client.add_secret_version(parent=self._parent, payload={"data": value.encode("ascii")})
