"""PostToolUse hook: run ruff format and ruff check --fix on an edited .py file.

Reads the hook JSON from stdin. Standard library only. Exit 2 (stderr shown to
Claude) if ruff still reports problems after fixing.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    raw = (event.get("tool_input") or {}).get("file_path")
    if not isinstance(raw, str) or not raw.endswith(".py"):
        return 0

    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()).resolve()
    path = Path(raw).resolve()
    if not path.is_file() or not path.is_relative_to(project):
        return 0

    ruff = [sys.executable, "-m", "ruff"]
    probe = subprocess.run([*ruff, "--version"], capture_output=True, text=True, cwd=project)
    if probe.returncode != 0:
        return 0  # ruff not installed yet (before the first uv sync)

    subprocess.run([*ruff, "format", str(path)], capture_output=True, text=True, cwd=project)
    check = subprocess.run(
        [*ruff, "check", "--fix", str(path)], capture_output=True, text=True, cwd=project
    )
    if check.returncode != 0:
        sys.stderr.write(check.stdout + check.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
