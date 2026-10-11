from dataclasses import dataclass
from pathlib import Path

from clipmachine.config import ResolvedConfig


@dataclass(frozen=True)
class RunContext:
    """What every stage receives: where to read/write files, and the resolved config."""

    run_dir: Path
    config: ResolvedConfig
    input_path: Path  # the original media file; only the fetch stage reads it
    project_root: Path | None = None  # holds models/ and tools/; None = this checkout
    # True only when this stage failed or was interrupted on an earlier run and is being resumed.
    # A stage may then keep outputs it can prove are good; otherwise it redoes everything.
    resuming: bool = False
