"""The maker and the checker: two models from different vendors (config/models.yaml tasks.web_claims), both
through Vertex AI with the job's own identity (the jobs network reaches Google APIs privately).

The maker reads a record's minimised page lines into a strict claim schema; the checker, from the other vendor,
says whether the same lines support each claim. Neither schema has a field for a person: only companies, their
register numbers and LEIs.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}

MAKER_SCHEMA = {
    "type": "object",
    "properties": {
        "operator": {
            "type": "object",
            "properties": {
                "legalName": NULLABLE_STRING,
                "registrationNumbers": {"type": "array", "items": {"type": "string"}},
                "lei": NULLABLE_STRING,
            },
            "required": ["legalName", "registrationNumbers", "lei"],
            "additionalProperties": False,
        },
        "parent": {"anyOf": [{"type": "null"}, {
            "type": "object",
            "properties": {
                "legalName": {"type": "string"},
                "registrationNumber": NULLABLE_STRING,
                "lei": NULLABLE_STRING,
                "country": NULLABLE_STRING,
                "relation": {"type": "string", "enum": ["direct", "ultimate", "unspecified"]},
            },
            "required": ["legalName", "registrationNumber", "lei", "country", "relation"],
            "additionalProperties": False,
        }]},
    },
    "required": ["operator", "parent"],
    "additionalProperties": False,
}

MAKER_SYSTEM = (
    "You read short excerpts of a company's web pages and extract facts about companies only. Never output a "
    "person's name, email address, phone number or any other detail about a person; if a fact is about a person, "
    "leave it out. Report only what the excerpts state explicitly; never guess or use outside knowledge. "
    "operator: the company that runs the website, as the excerpts name it (its full legal name with its legal "
    "form), the register or business identification numbers shown for it, and its LEI if shown. parent: only if "
    "the excerpts state that the operator is owned by, a subsidiary of, or part of a group headed by another "
    "company; give that company's legal name as written, and its number, LEI and country only if shown. "
    "relation is direct for an immediate parent, ultimate for the group's top company, else unspecified. "
    "Use null or an empty list when the excerpts don't say.")

CHECKER_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "operatorSupported": {"type": "BOOLEAN"},
        "parentSupported": {"type": "BOOLEAN", "nullable": True},
    },
    "required": ["operatorSupported", "parentSupported"],
}

CHECKER_PROMPT = (
    "You check claims extracted from excerpts of a company's web pages. Answer only from the excerpts. "
    "operatorSupported: true only if the excerpts show that the company named as operator (with the register "
    "numbers and LEI given, where any are given) runs or is described by these pages. parentSupported: null if "
    "no parent is claimed; true only if the excerpts explicitly state that the operator is owned by, a "
    "subsidiary of, or part of the group of the claimed parent company; false otherwise.")


class ModelError(Exception):
    """A model call failed (unavailable model, refused or malformed answer). ``kind`` is safe to log."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(f"{kind}: {message}"[:300])
        self.kind = kind


@dataclass
class Usage:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    by_model: dict = field(default_factory=dict)
    lock: object = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, model, tokens_in: int, tokens_out: int) -> None:
        with self.lock:
            self._add(model, tokens_in, tokens_out)

    def _add(self, model, tokens_in: int, tokens_out: int) -> None:
        cost = tokens_in / 1e6 * model.price_in + tokens_out / 1e6 * model.price_out
        self.calls += 1
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.usd += cost
        m = self.by_model.setdefault(model.model_class, {"calls": 0, "tokensIn": 0, "tokensOut": 0})
        m["calls"] += 1
        m["tokensIn"] += tokens_in
        m["tokensOut"] += tokens_out


def _excerpt_message(record: dict, domain: str, excerpts: str) -> str:
    return (f"Website: {domain}\nThe directory's record for it: {record.get('legalName', '')} "
            f"({record.get('country', '')}).\n\nExcerpts:\n{excerpts}")


class ClaudeMaker:
    """The maker: Claude through Vertex AI, with structured output (the answer is valid JSON for MAKER_SCHEMA)."""

    def __init__(self, model, project_id: str, client=None):
        if client is None:
            from anthropic import AnthropicVertex

            client = AnthropicVertex(project_id=project_id, region=model.location)
        self.model, self._client = model, client

    def extract(self, record: dict, domain: str, excerpts: str, usage: Usage) -> dict:
        try:
            resp = self._client.messages.create(
                model=self.model.model_id,
                max_tokens=2000,
                thinking={"type": "disabled"},
                # Sent as extra_body so any recent SDK passes it through unchanged.
                extra_body={"output_config": {"effort": "low", "format": {"type": "json_schema", "schema": MAKER_SCHEMA}}},
                system=MAKER_SYSTEM,
                messages=[{"role": "user", "content": _excerpt_message(record, domain, excerpts)}],
            )
        except Exception as exc:  # the SDK's errors: kept to their type and status
            raise ModelError("maker-call", f"{type(exc).__name__} {getattr(exc, 'status_code', '')}") from exc
        u = getattr(resp, "usage", None)
        usage.add(self.model, getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0)
        if getattr(resp, "stop_reason", "") not in ("end_turn", "stop_sequence"):
            raise ModelError("maker-stopped", str(getattr(resp, "stop_reason", "")))
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        try:
            out = json.loads(text)
        except ValueError as exc:
            raise ModelError("maker-not-json") from exc
        if not isinstance(out, dict) or not isinstance(out.get("operator"), dict):
            raise ModelError("maker-bad-shape")
        return out


class GeminiChecker:
    """The checker: Gemini through Vertex AI's generateContent, JSON answers only (CHECKER_SCHEMA)."""

    def __init__(self, model, project_id: str, session=None):
        if session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            session = AuthorizedSession(creds)
        host = "aiplatform.googleapis.com" if model.location == "global" else f"{model.location}-aiplatform.googleapis.com"
        self.url = (f"https://{host}/v1/projects/{project_id}/locations/{model.location}"
                    f"/publishers/google/models/{model.model_id}:generateContent")
        self.model, self._session = model, session

    def check(self, record: dict, domain: str, excerpts: str, claims: dict, usage: Usage) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": CHECKER_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": _excerpt_message(record, domain, excerpts)
                                                     + "\n\nClaims:\n" + json.dumps(claims, ensure_ascii=False)}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": CHECKER_SCHEMA,
                                 "maxOutputTokens": 2000},
        }
        try:
            resp = self._session.post(self.url, json=body, timeout=120)
        except Exception as exc:
            raise ModelError("checker-call", type(exc).__name__) from exc
        if resp.status_code != 200:
            raise ModelError("checker-call", f"HTTP {resp.status_code}")
        try:
            data = resp.json()
            meta = data.get("usageMetadata") or {}
            usage.add(self.model, int(meta.get("promptTokenCount", 0)),
                      int(meta.get("candidatesTokenCount", 0)) + int(meta.get("thoughtsTokenCount", 0)))
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            out = json.loads(text)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError("checker-not-json") from exc
        if not isinstance(out, dict) or not isinstance(out.get("operatorSupported"), bool):
            raise ModelError("checker-bad-shape")
        return out
