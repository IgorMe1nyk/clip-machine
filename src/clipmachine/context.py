from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from clipmachine.config import ResolvedConfig
from clipmachine.models import ScoreBatch


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
    # Brief 12 (scout): where candidates.json and scored.json are written and read; None means
    # the run folder, as a normal run does. The transcript is always read from the run folder.
    out_dir: Path | None = None
    # Brief 12 (scout): extra text for the {scout_block} placeholder of the two prompts (the
    # campaign's rules and topics); empty on a normal run
    scout_block: str = ""
    # Brief 12 (scout): one more check of the scorer's answer, run with the id check; a message
    # sends the answer back for a repair, like the id check does
    extra_check: Callable[[ScoreBatch], str | None] | None = None

    @property
    def stage_dir(self) -> Path:
        return self.out_dir if self.out_dir is not None else self.run_dir
