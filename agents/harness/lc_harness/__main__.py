"""Run one session:

    python -m lc_harness --role engineering --task-file task.md --root . --trace trace.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from lc_core.control import SystemHalted, require_running

from .loop import Budgets, run_session
from .model import ClaudeVertex
from .tools import Tools


def build_system(root: Path, role: str) -> str:
    """The constitution always comes first. No constitution, no session."""
    constitution = (root / "constitution.md").read_text(encoding="utf-8")
    prompt = (root / "agents" / "roles" / f"{role}.md").read_text(encoding="utf-8")
    return f"{constitution}\n\n{prompt}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", required=True)
    ap.add_argument("--task-file", required=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--trace", required=True)
    ap.add_argument("--model-class", default="claude-opus")
    a = ap.parse_args(argv)

    component = f"agent:{a.role}"
    try:
        require_running(component)  # before any model client exists
    except SystemHalted as exc:
        print(f"not starting: {exc}", file=sys.stderr)
        return 3

    root = Path(a.root).resolve()
    with open(a.trace, "a", encoding="utf-8") as out:
        def trace(kind, **fields):
            out.write(json.dumps({"ts": time.time(), "role": a.role, "kind": kind, **fields}, sort_keys=True) + "\n")
            out.flush()

        result = run_session(
            model=ClaudeVertex(os.environ["GCP_PROJECT_ID"], a.model_class),
            tools=Tools(root),
            system=build_system(root, a.role),
            task=Path(a.task_file).read_text(encoding="utf-8"),
            budgets=Budgets(),
            gate=lambda: require_running(component),
            trace=trace,
        )
    print(json.dumps({"status": result.status, "reason": result.reason, "steps": result.steps, "tokens": result.tokens}))
    return 0 if result.status == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
