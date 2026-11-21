"""Stop a deploy unless the owner's control state lets it run.

Every deploy workflow calls this right after it signs in to Google Cloud:

    python3 scripts/check_system_state.py deploy:<name>

The control state lives only in the cloud, in ``gs://<project>-control/state.json`` (owner decision
2026-11-21); the owner changes it on the admin page. The rule is lc_core.control's, unchanged: a deploy
runs only while the system is ``running`` and neither ``deploy:<name>`` nor a wildcard such as
``deploy:*`` is among the paused components. ``manual`` stops deploys too ("nothing merges or deploys
without the owner"), as do ``paused`` and ``stopped``; a malformed file fails closed.

An unreadable file (the deployer may read state.json only from the bootstrap rerun that grants it on)
fails closed once the repository variable ``STATE_CHECK_ENFORCED`` is ``true``; until then it warns and
the deploy continues. The log shows the state word only, because this repository's logs are public.

Standard library plus lc_core from this checkout, so a deploy needs nothing installed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "lc_core"))

from lc_core.control import SystemHalted, require_running  # noqa: E402


def read_state(uri: str) -> str | None:
    """The file's text, or None if it can't be read (no access yet, missing, or no network)."""
    try:
        out = subprocess.run(["gcloud", "storage", "cat", uri], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def decide(component: str, text: str | None, enforced: bool) -> tuple[bool, str]:
    """(deploy may go ahead, a line for the log)."""
    if text is None:
        if enforced:
            return False, f"::error::control state unreadable; {component} stops (STATE_CHECK_ENFORCED)"
        return True, (f"::warning::control state unreadable; {component} goes ahead because STATE_CHECK_ENFORCED "
                      "isn't true yet (it is set after the bootstrap rerun that lets the deployer read state.json)")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        path.write_text(text, encoding="utf-8")
        try:
            st = require_running(component, {"LC_CONTROL_STATE_URI": str(path)})
        except SystemHalted as exc:
            return False, f"::error::{exc}; the owner's control state stops this deploy"
    return True, f"system is {st.state}; {component} goes ahead"


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not argv[0].startswith("deploy:"):
        print("usage: check_system_state.py deploy:<name>", file=sys.stderr)
        return 2
    project = os.environ.get("GCP_PROJECT_ID", "")
    text = read_state(f"gs://{project}-control/state.json") if project else None
    ok, line = decide(argv[0], text, os.environ.get("STATE_CHECK_ENFORCED", "").strip().lower() == "true")
    print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
