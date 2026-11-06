"""Claude through Vertex AI. All model calls use the project's own identity."""

from __future__ import annotations

# Model classes from config/models.yaml, mapped to current model IDs.
CLAUDE_MODELS = {
    "claude-opus": "claude-opus-5-5",
    "claude-sonnet": "claude-sonnet-5-5",
    "claude-haiku": "claude-haiku-5-5",
}
MAX_TOKENS = 64_000


class ClaudeVertex:
    def __init__(self, project_id: str, model_class: str = "claude-opus", region: str = "global",
                 effort: str = "high", client=None):
        if model_class not in CLAUDE_MODELS:
            raise ValueError(f"unknown model class {model_class!r}")
        if client is None:
            from anthropic import AnthropicVertex

            client = AnthropicVertex(project_id=project_id, region=region)
        self._client = client
        self._model = CLAUDE_MODELS[model_class]
        self._effort = effort

    def create(self, *, system, tools, messages):
        with self._client.messages.stream(
            model=self._model,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": self._effort},
            system=system,
            tools=tools,
            messages=messages,
        ) as stream:
            return stream.get_final_message()
