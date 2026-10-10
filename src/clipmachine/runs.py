"""Run folders, input validation and atomic JSON writes."""

import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

ALLOWED_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".m4a", ".wav", ".mp3"})
MAX_SLUG_LENGTH = 50
FALLBACK_SLUG = "episode"


class RunError(Exception):
    """A run-folder or input problem with a message fit to show the user."""


def slugify(stem: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    slug = slug[:MAX_SLUG_LENGTH].strip("-")
    return slug or FALLBACK_SLUG


def validate_input(path: Path) -> Path:
    """Light checks only; full container/size/duration checks arrive in Brief 2."""
    if not path.exists():
        raise RunError(f"input file not found: {path}")
    if not path.is_file():
        raise RunError(f"input is not a regular file: {path}")
    if path.suffix.lower() not in ALLOWED_EXTENSIONS:
        allowed = " ".join(sorted(ALLOWED_EXTENSIONS))
        raise RunError(f"unsupported input type {path.suffix or '(none)'!r}; allowed: {allowed}")
    return path


def ensure_inside(root: Path, candidate: Path) -> Path:
    """Return candidate resolved, or raise if it does not resolve directly inside root."""
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if resolved.parent != resolved_root:
        raise RunError(f"run folder would be outside {root}: {candidate}")
    return resolved


def create_run_dir(runs_root: Path, input_path: Path, today: date) -> Path:
    """Create runs/<YYYY-MM-DD>_<slug>[-N] and return it."""
    runs_root.mkdir(parents=True, exist_ok=True)
    base = f"{today.isoformat()}_{slugify(input_path.stem)}"
    attempt = 1
    while True:
        name = base if attempt == 1 else f"{base}-{attempt}"
        if Path(name).name != name or name in {".", ".."}:
            raise RunError(f"run folder name is not a single path component: {name!r}")
        target = ensure_inside(runs_root, runs_root / name)
        try:
            target.mkdir()
        except FileExistsError:
            attempt += 1
            continue
        return target


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON to a temp file in the same folder, then os.replace it into place."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
