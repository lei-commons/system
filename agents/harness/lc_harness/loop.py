"""The session loop: call the model, run its tools, stop at a budget.

This is a manual loop rather than the SDK tool runner because the owner's
control gate has to run before every model call and every tool call
(constitution Article 1.2), and a halted gate must end the session at once.

Messages are only ever appended, and the model's content goes back unchanged,
which keeps thinking blocks valid.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from lc_core.control import SystemHalted


@dataclass(frozen=True)
class Budgets:
    max_steps: int = 40
    max_tokens: int = 2_000_000
    max_seconds: float = 3600.0


@dataclass(frozen=True)
class Result:
    status: str   # done | halted | budget | refused | truncated | failed
    reason: str
    steps: int
    tokens: int
    text: str = ""


def _text(response) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text")


def _tokens(response) -> int:
    usage = response.usage
    return (usage.input_tokens + usage.output_tokens
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0))


def run_session(*, model, tools, system, task, budgets, gate, trace, clock=time.monotonic) -> Result:
    """Run one session. ``gate()`` raises SystemHalted when the agent must stop."""
    messages = [{"role": "user", "content": task}]
    start, steps, tokens = clock(), 0, 0

    def finish(status, reason, text=""):
        trace("end", status=status, reason=reason, steps=steps, tokens=tokens)
        return Result(status, reason, steps, tokens, text)

    while True:
        try:
            gate()
        except SystemHalted as exc:
            return finish("halted", str(exc))
        if steps >= budgets.max_steps:
            return finish("budget", f"step budget of {budgets.max_steps} reached")
        if tokens >= budgets.max_tokens:
            return finish("budget", f"token budget of {budgets.max_tokens} reached")
        if clock() - start >= budgets.max_seconds:
            return finish("budget", f"time budget of {budgets.max_seconds}s reached")

        try:
            response = model.create(system=system, tools=tools.definitions(), messages=messages)
        except Exception as exc:
            return finish("failed", f"model call failed: {type(exc).__name__}")
        steps += 1
        tokens += _tokens(response)
        stop = response.stop_reason
        trace("model", step=steps, stop_reason=stop, tokens=tokens)

        if stop == "end_turn":
            return finish("done", "the model finished", _text(response))
        if stop == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        if stop == "refusal":
            return finish("refused", "the model declined the request")
        if stop == "max_tokens":
            # Tool input may be cut off, so nothing from this turn is run.
            return finish("truncated", "the response hit max_tokens")
        if stop != "tool_use":
            return finish("failed", f"unexpected stop reason {stop!r}")

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in (b for b in response.content if b.type == "tool_use"):
            try:
                gate()
            except SystemHalted as exc:
                return finish("halted", str(exc))
            output, is_error = tools.call(block.name, block.input)
            trace("tool", step=steps, tool=block.name, is_error=is_error)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error})
        messages.append({"role": "user", "content": results})
