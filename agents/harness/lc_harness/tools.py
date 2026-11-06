"""The fixed tool set. Tools are narrow by design and confined to the checkout.

Anything a tool returns is data from the repository, not instructions. The
sandbox still limits what a misled agent could do.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

MAX_READ_BYTES = 200_000
MAX_OUTPUT_CHARS = 20_000
TEST_TIMEOUT = 300
# Only an owner edits these (constitution, Articles 1 and 2).
READ_ONLY = {"constitution.md"}
SAFE_ENV = ("PATH", "HOME", "LANG", "PYTHONPATH", "TMPDIR")


class ToolError(Exception):
    """A refusal or failure the model should see and can react to."""


def _schema(**props):
    return {"type": "object", "properties": {k: {"type": "string", "description": v} for k, v in props.items()},
            "required": list(props), "additionalProperties": False}


DEFINITIONS = [
    {"name": "read_file", "description": "Read a text file in the checkout.",
     "input_schema": _schema(path="Path relative to the checkout root")},
    {"name": "list_dir", "description": "List a directory in the checkout.",
     "input_schema": _schema(path="Path relative to the checkout root; use . for the root")},
    {"name": "write_file", "description": "Create or replace a file in the checkout.",
     "input_schema": _schema(path="Path relative to the checkout root", content="The whole new file")},
    {"name": "edit_file", "description": "Replace one exact, unique string in a file in the checkout.",
     "input_schema": _schema(path="Path relative to the checkout root", old="Exact text, which must occur once",
                             new="Replacement text")},
    {"name": "run_tests", "description": "Run pytest on a path in the checkout.",
     "input_schema": _schema(path="File or directory relative to the checkout root")},
]


class Tools:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def definitions(self):
        return DEFINITIONS

    def _path(self, raw) -> Path:
        """Resolve a path, following links, and refuse anything outside the checkout."""
        if not isinstance(raw, str) or not raw or "\0" in raw:
            raise ToolError("path must be a non-empty string")
        path = (self.root / raw).resolve()
        if path != self.root and self.root not in path.parents:
            raise ToolError(f"{raw} is outside the checkout")
        if ".git" in path.relative_to(self.root).parts:
            raise ToolError("the .git directory is off limits")
        return path

    def _writable(self, raw) -> Path:
        path = self._path(raw)
        if path.relative_to(self.root).as_posix() in READ_ONLY:
            raise ToolError(f"{raw} can only be changed by the owner")
        return path

    def read_file(self, path):
        p = self._path(path)
        if not p.is_file():
            raise ToolError(f"{path} is not a file")
        if p.stat().st_size > MAX_READ_BYTES:
            raise ToolError(f"{path} is larger than {MAX_READ_BYTES} bytes")
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"{path} is not UTF-8 text") from None

    def list_dir(self, path):
        p = self._path(path)
        if not p.is_dir():
            raise ToolError(f"{path} is not a directory")
        names = [c.name + ("/" if c.is_dir() else "") for c in sorted(p.iterdir()) if c.name != ".git"]
        return "\n".join(names) or "(empty)"

    def write_file(self, path, content):
        p = self._writable(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {path}"

    def edit_file(self, path, old, new):
        p = self._writable(path)
        text = self.read_file(path)
        count = text.count(old) if old else 0
        if count != 1:
            raise ToolError(f"old text occurs {count} times; it must occur exactly once")
        p.write_text(text.replace(old, new), encoding="utf-8")
        return f"edited {path}"

    def run_tests(self, path):
        p = self._path(path)
        env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}  # no tokens or credentials
        try:
            done = subprocess.run([sys.executable, "-m", "pytest", "-q", str(p)], cwd=self.root, env=env,
                                  capture_output=True, text=True, timeout=TEST_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ToolError(f"tests took longer than {TEST_TIMEOUT}s") from None
        return f"exit code {done.returncode}\n{(done.stdout + done.stderr)[-MAX_OUTPUT_CHARS:]}"

    def call(self, name, args):
        """Run a tool. Returns (output, is_error) and never raises."""
        spec = next((d for d in DEFINITIONS if d["name"] == name), None)
        if spec is None:
            return f"unknown tool {name}", True
        wanted = set(spec["input_schema"]["required"])
        if not isinstance(args, dict) or set(args) != wanted or not all(isinstance(v, str) for v in args.values()):
            return f"{name} needs exactly these string fields: {sorted(wanted)}", True
        try:
            return getattr(self, name)(**args), False
        except ToolError as exc:
            return str(exc), True
        except Exception as exc:
            return f"{name} failed: {type(exc).__name__}", True
