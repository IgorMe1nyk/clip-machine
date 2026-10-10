from dataclasses import dataclass
from pathlib import Path

from clipmachine.config import ResolvedConfig


@dataclass(frozen=True)
class RunContext:
    """What every stage receives: where to read/write files, and the resolved config."""

    run_dir: Path
    config: ResolvedConfig
    input_path: Path  # the original media file; only the fetch stage reads it
