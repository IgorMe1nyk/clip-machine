"""Run folders in, restartable stage execution out."""

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import ValidationError

from clipmachine.config import dump_resolved, load_resolved, load_resolved_file
from clipmachine.context import RunContext
from clipmachine.models import RunState, StageState
from clipmachine.preflight import check_ffmpeg
from clipmachine.runs import (
    RunError,
    check_input_size,
    create_run_dir,
    validate_input,
    write_json_atomic,
)
from clipmachine.stages import candidates, fetch, render, report, score, transcribe

log = logging.getLogger("clipmachine")

RUN_JSON = "run.json"
RESOLVED_CONFIG = "config.resolved.yaml"

# Order matters: this is the pipeline.
STAGES: dict[str, Callable[[RunContext], None]] = {
    "fetch": fetch.run,
    "transcribe": transcribe.run,
    "candidates": candidates.run,
    "score": score.run,
    "render": render.run,
    "report": report.run,
}
STAGE_NAMES = tuple(STAGES)


@dataclass
class PipelineOutcome:
    ran: list[str] = field(default_factory=list)
    failed_stage: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.failed_stage is None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def is_run_folder(path: Path) -> bool:
    return path.is_dir() and (path / RUN_JSON).is_file()


def load_state(run_dir: Path) -> RunState:
    path = run_dir / RUN_JSON
    try:
        state = RunState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise RunError(f"cannot read {path}: {exc}") from exc
    if set(state.stages) != set(STAGE_NAMES):
        raise RunError(f"{path}: stages {sorted(state.stages)} do not match this version")
    return state


def save_state(run_dir: Path, state: RunState) -> None:
    write_json_atomic(run_dir / RUN_JSON, state.model_dump(mode="json"))


def recover_interrupted(state: RunState) -> list[str]:
    """A stage left 'running' means the process died; count it as failed."""
    recovered = []
    for name, stage in state.stages.items():
        if stage.status == "running":
            stage.status = "failed"
            stage.finished_at = _now()
            stage.error = "interrupted: stage was still marked running (crash?)"
            recovered.append(name)
    return recovered


def reset_from(state: RunState, stage_name: str) -> None:
    """Reset stage_name and every later stage to pending."""
    if stage_name not in STAGE_NAMES:
        raise RunError(f"unknown stage {stage_name!r}; choose from: {', '.join(STAGE_NAMES)}")
    for name in STAGE_NAMES[STAGE_NAMES.index(stage_name) :]:
        state.stages[name] = StageState()


def start_new_run(root: Path, input_path: Path, client: str, today: date | None = None) -> Path:
    """Validate everything, then create the run folder. Nothing is created on bad input."""
    resolved = load_resolved(root, client)
    validate_input(input_path)
    check_input_size(input_path, resolved.app.fetch.max_size_gb)
    check_ffmpeg()
    run_dir = create_run_dir(root / "runs", input_path, today or date.today())
    try:
        dump_resolved(resolved, run_dir / RESOLVED_CONFIG)
        state = RunState(
            input=str(input_path.resolve()),
            client=client,
            created_at=_now(),
            stages={name: StageState() for name in STAGE_NAMES},
        )
        save_state(run_dir, state)
    except BaseException:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    return run_dir


def run_pipeline(run_dir: Path, from_stage: str | None = None) -> PipelineOutcome:
    state = load_state(run_dir)
    resolved = load_resolved_file(run_dir / RESOLVED_CONFIG)
    check_ffmpeg()
    if from_stage is not None:
        reset_from(state, from_stage)
    recover_interrupted(state)
    save_state(run_dir, state)

    ctx = RunContext(run_dir=run_dir, config=resolved)
    outcome = PipelineOutcome()
    for name, stage_fn in STAGES.items():
        stage = state.stages[name]
        if stage.status == "done":
            continue
        stage.status = "running"
        stage.started_at = _now()
        stage.finished_at = None
        stage.error = None
        save_state(run_dir, state)
        try:
            stage_fn(ctx)
        except Exception as exc:
            stage.status = "failed"
            stage.finished_at = _now()
            stage.error = f"{type(exc).__name__}: {exc}"
            save_state(run_dir, state)
            outcome.failed_stage = name
            outcome.error = stage.error
            return outcome
        stage.status = "done"
        stage.finished_at = _now()
        save_state(run_dir, state)
        outcome.ran.append(name)
    return outcome
