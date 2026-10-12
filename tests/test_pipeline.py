import json
import logging
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

import track_fakes
from clipmachine import cli, pipeline
from clipmachine.config import ConfigError
from clipmachine.context import RunContext
from clipmachine.models import RunState, StageState
from clipmachine.pipeline import (
    STAGE_NAMES,
    load_state,
    recover_interrupted,
    reset_from,
    run_pipeline,
    start_new_run,
)
from clipmachine.preflight import MissingToolError, check_ffmpeg
from clipmachine.runs import RunError
from clipmachine.utils import faces
from clipmachine.utils.faces import FacesError
from helpers import edit_yaml

TODAY = date(2026, 10, 12)


def new_run(project: Path, name: str = "sample.mp4") -> Path:
    media = project / name
    media.write_bytes(b"")
    return start_new_run(project, media, "example-client", TODAY)


def test_stage_order() -> None:
    assert STAGE_NAMES == ("fetch", "transcribe", "candidates", "score", "render", "report")


def test_new_run_files_and_initial_state(project: Path) -> None:
    run_dir = new_run(project)
    assert run_dir.name == "2026-10-12_sample"
    assert (run_dir / "config.resolved.yaml").is_file()
    raw = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["client"] == "example-client"
    assert raw["input"].endswith("sample.mp4")
    assert raw["created_at"]
    assert list(raw["stages"]) == list(STAGE_NAMES)
    assert all(
        s == {"status": "pending", "started_at": None, "finished_at": None, "error": None}
        for s in raw["stages"].values()
    )


def test_full_run_marks_everything_done(project: Path) -> None:
    run_dir = new_run(project)
    outcome = run_pipeline(run_dir)
    assert outcome.ok
    assert outcome.ran == list(STAGE_NAMES)
    state = load_state(run_dir)
    for stage in state.stages.values():
        assert stage.status == "done"
        assert stage.started_at and stage.finished_at
        assert stage.error is None


def test_no_stage_logs_not_implemented(project: Path, caplog: pytest.LogCaptureFixture) -> None:
    run_dir = new_run(project)
    with caplog.at_level(logging.INFO, logger="clipmachine"):
        run_pipeline(run_dir)
    # every stage is real now (silent no-ops in unit tests)
    for name in STAGE_NAMES:
        assert f"stage {name} not implemented yet" not in caplog.text


def test_done_stages_are_skipped(project: Path) -> None:
    run_dir = new_run(project)
    run_pipeline(run_dir)
    before = (run_dir / "run.json").read_bytes()
    outcome = run_pipeline(run_dir)
    assert outcome.ok
    assert outcome.ran == []
    assert (run_dir / "run.json").read_bytes() == before


def test_from_stage_reruns_only_that_stage_and_later(project: Path) -> None:
    run_dir = new_run(project)
    run_pipeline(run_dir)
    before = load_state(run_dir)
    outcome = run_pipeline(run_dir, from_stage="render")
    after = load_state(run_dir)
    assert outcome.ran == ["render", "report"]
    for name in ("fetch", "transcribe", "candidates", "score"):
        assert after.stages[name] == before.stages[name]
    for name in ("render", "report"):
        assert after.stages[name].started_at != before.stages[name].started_at
        assert after.stages[name].finished_at != before.stages[name].finished_at
        assert after.stages[name].status == "done"


def test_unknown_from_stage_is_rejected(project: Path) -> None:
    run_dir = new_run(project)
    with pytest.raises(RunError, match="unknown stage"):
        run_pipeline(run_dir, from_stage="upload")


def test_reset_from_resets_later_stages_only() -> None:
    state = RunState(
        input="x.mp4",
        client="c",
        created_at="t",
        stages={n: StageState(status="done", finished_at="t") for n in STAGE_NAMES},
    )
    reset_from(state, "score")
    assert [state.stages[n].status for n in STAGE_NAMES] == [
        "done",
        "done",
        "done",
        "pending",
        "pending",
        "pending",
    ]
    assert state.stages["score"].finished_at is None


def test_running_stage_counts_as_failed_on_next_start() -> None:
    state = RunState(
        input="x.mp4",
        client="c",
        created_at="t",
        stages={n: StageState() for n in STAGE_NAMES},
    )
    state.stages["fetch"] = StageState(status="done", finished_at="t")
    state.stages["transcribe"] = StageState(status="running", started_at="t")
    assert recover_interrupted(state) == ["transcribe"]
    stuck = state.stages["transcribe"]
    assert stuck.status == "failed"
    assert stuck.error and "interrupted" in stuck.error
    assert state.stages["fetch"].status == "done"


def test_crashed_run_resumes_without_redoing_finished_stages(project: Path) -> None:
    run_dir = new_run(project)
    run_pipeline(run_dir)
    state = load_state(run_dir)
    done_fetch = state.stages["fetch"]
    state.stages["candidates"] = StageState(status="running", started_at="t")
    for name in ("score", "render", "report"):
        state.stages[name] = StageState()
    pipeline.save_state(run_dir, state)

    outcome = run_pipeline(run_dir)

    assert outcome.ran == ["candidates", "score", "render", "report"]
    after = load_state(run_dir)
    assert after.stages["fetch"] == done_fetch
    assert after.stages["candidates"].status == "done"
    assert after.stages["candidates"].error is None


def test_forced_failure_stops_pipeline(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def boom(ctx: RunContext) -> None:
        raise RuntimeError("boom")

    def make_recorder(name: str) -> Callable[[RunContext], None]:
        def recorder(ctx: RunContext) -> None:
            called.append(name)

        return recorder

    for name in STAGE_NAMES:
        monkeypatch.setitem(pipeline.STAGES, name, make_recorder(name))
    monkeypatch.setitem(pipeline.STAGES, "candidates", boom)

    run_dir = new_run(project)
    outcome = run_pipeline(run_dir)

    assert not outcome.ok
    assert outcome.failed_stage == "candidates"
    assert outcome.error == "RuntimeError: boom"
    assert outcome.ran == ["fetch", "transcribe"]
    assert called == ["fetch", "transcribe"]
    state = load_state(run_dir)
    assert [state.stages[n].status for n in STAGE_NAMES] == [
        "done",
        "done",
        "failed",
        "pending",
        "pending",
        "pending",
    ]
    assert state.stages["candidates"].error == "RuntimeError: boom"
    assert state.stages["candidates"].finished_at


def test_failed_stage_is_retried_on_resume(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(ctx: RunContext) -> None:
        raise RuntimeError("boom")

    run_dir = new_run(project)
    with monkeypatch.context() as m:
        m.setitem(pipeline.STAGES, "score", boom)
        assert run_pipeline(run_dir).failed_stage == "score"

    outcome = run_pipeline(run_dir)  # real stub is back
    assert outcome.ok
    assert outcome.ran == ["score", "render", "report"]
    assert load_state(run_dir).stages["score"].error is None


def test_missing_ffmpeg_creates_nothing(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> None:
        raise MissingToolError("ffmpeg not found on PATH")

    monkeypatch.setattr(pipeline, "check_ffmpeg", missing)
    media = project / "sample.mp4"
    media.write_bytes(b"")
    with pytest.raises(MissingToolError):
        start_new_run(project, media, "example-client", TODAY)
    assert list((project / "runs").iterdir()) == []


def test_check_ffmpeg_reports_which_tool_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clipmachine.preflight.shutil.which", lambda tool: None)
    with pytest.raises(MissingToolError, match="ffmpeg and ffprobe"):
        check_ffmpeg()
    monkeypatch.setattr(
        "clipmachine.preflight.shutil.which",
        lambda tool: "C:/bin/ffmpeg.exe" if tool == "ffmpeg" else None,
    )
    with pytest.raises(MissingToolError, match="ffprobe not found"):
        check_ffmpeg()


def test_bad_client_creates_nothing(project: Path) -> None:
    media = project / "sample.mp4"
    media.write_bytes(b"")
    with pytest.raises(ConfigError):
        start_new_run(project, media, "../evil", TODAY)
    assert list((project / "runs").iterdir()) == []


def test_bad_input_creates_nothing(project: Path) -> None:
    notes = project / "notes.txt"
    notes.write_text("hi", encoding="utf-8")
    with pytest.raises(RunError):
        start_new_run(project, notes, "example-client", TODAY)
    assert list((project / "runs").iterdir()) == []


def test_oversize_input_creates_nothing(project: Path) -> None:
    edit_yaml(
        project / "config" / "default.yaml", lambda d: d["fetch"].update(max_size_gb=0.000001)
    )
    media = project / "big.mp4"
    media.write_bytes(b"x" * 2000)  # limit is about 1074 bytes
    with pytest.raises(RunError, match="too large"):
        start_new_run(project, media, "example-client", TODAY)
    assert list((project / "runs").iterdir()) == []


def test_audio_only_input_creates_nothing(project: Path) -> None:
    media = project / "talk.mp3"
    media.write_bytes(b"")
    with pytest.raises(RunError, match="audio-only"):
        start_new_run(project, media, "example-client", TODAY)
    assert list((project / "runs").iterdir()) == []


def test_corrupt_run_json_is_reported(project: Path) -> None:
    run_dir = new_run(project)
    (run_dir / "run.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RunError, match="cannot read"):
        run_pipeline(run_dir)


# --- the face-model check before a speaker-crop run (D-019 default) -------------------------


def speaker_project(project: Path) -> Path:
    edit_yaml(project / "config" / "default.yaml", lambda d: d["video"].update(crop="speaker"))
    return project


def record_stages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ran: list[str] = []
    for name in STAGE_NAMES:
        monkeypatch.setitem(pipeline.STAGES, name, lambda c, n=name: ran.append(n))  # type: ignore[misc]
    return ran


def freeze_crop(run_dir: Path, crop: str) -> None:
    edit_yaml(run_dir / "config.resolved.yaml", lambda d: d["config"]["video"].update(crop=crop))


def test_speaker_with_a_missing_model_stops_a_new_run_before_anything_is_created(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    speaker_project(project)
    ran = record_stages(monkeypatch)
    track_fakes.write_model(project)
    (faces.model_dir(project, faces.DETECTOR) / faces.DETECTOR.name).unlink()
    media = project / "sample.mp4"
    media.write_bytes(b"")

    with pytest.raises(FacesError, match=r"clipmachine fetch-face-model.*video\.crop: center"):
        start_new_run(project, media, "example-client", TODAY)

    assert ran == []
    assert list((project / "runs").iterdir()) == []


def test_speaker_with_a_missing_model_stops_a_resume_before_any_stage(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = new_run(project)  # made on the pinned centre config
    freeze_crop(run_dir, "speaker")
    ran = record_stages(monkeypatch)
    before = {p.name: p.read_bytes() for p in run_dir.iterdir()}

    with pytest.raises(FacesError, match=r"face model not found.*clipmachine fetch-face-model"):
        run_pipeline(run_dir, project_root=project)

    assert ran == []
    assert {p.name: p.read_bytes() for p in run_dir.iterdir()} == before  # run.json untouched


def test_speaker_with_a_missing_model_exits_two_with_one_line(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    speaker_project(project)
    ran = record_stages(monkeypatch)
    media = project / "sample.mp4"
    media.write_bytes(b"")

    code = cli.main(["run", str(media), "--client", "example-client"], root=project)

    err = capsys.readouterr().err
    assert code == 2 and ran == []
    assert err.startswith("error: ") and err.count("\n") == 1
    assert "clipmachine fetch-face-model" in err and "center" in err


def test_speaker_with_both_models_present_passes_the_check(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    speaker_project(project)
    track_fakes.write_model(project)
    ran = record_stages(monkeypatch)

    run_dir = new_run(project)
    outcome = run_pipeline(run_dir, project_root=project)

    assert outcome.ok and ran == list(STAGE_NAMES)


def test_center_needs_no_model_files(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ran = record_stages(monkeypatch)
    run_dir = new_run(project)
    assert run_pipeline(run_dir, project_root=project).ok
    assert ran == list(STAGE_NAMES)
    assert not (project / "models").exists()


def test_a_resume_that_does_not_reach_render_does_not_check_the_models(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = new_run(project)
    ran = record_stages(monkeypatch)
    assert run_pipeline(run_dir, project_root=project).ok  # every stage done, centre crop
    freeze_crop(run_dir, "speaker")
    ran.clear()

    outcome = run_pipeline(run_dir, from_stage="report", project_root=project)
    assert outcome.ok and ran == ["report"]

    nothing = run_pipeline(run_dir, project_root=project)  # nothing left to run
    assert nothing.ok and nothing.ran == []

    with pytest.raises(FacesError):  # but a redo that includes render is checked
        run_pipeline(run_dir, from_stage="render", project_root=project)
    assert ran == ["report"]


# --- until (Brief 11: campaign mode stops after transcribe) ---------------------------------------


def test_until_stops_after_that_stage(project: Path) -> None:
    run_dir = new_run(project)

    outcome = run_pipeline(run_dir, until="transcribe")

    assert outcome.ok and outcome.ran == ["fetch", "transcribe"]
    stages = load_state(run_dir).stages
    assert [stages[n].status for n in STAGE_NAMES] == ["done", "done"] + ["pending"] * 4


def test_until_a_stage_that_is_already_done_runs_nothing_and_changes_nothing(
    project: Path,
) -> None:
    run_dir = new_run(project)
    run_pipeline(run_dir, until="transcribe")
    before = (run_dir / "run.json").read_text(encoding="utf-8")

    outcome = run_pipeline(run_dir, until="transcribe")

    assert outcome.ok and outcome.ran == []
    assert load_state(run_dir).stages["candidates"].status == "pending"
    assert (run_dir / "run.json").read_text(encoding="utf-8") == before


def test_until_after_the_earlier_stages_were_done_finishes_the_ones_up_to_it(
    project: Path,
) -> None:
    run_dir = new_run(project)
    run_pipeline(run_dir, until="fetch")

    outcome = run_pipeline(run_dir, until="score")

    assert outcome.ran == ["transcribe", "candidates", "score"]
    assert load_state(run_dir).stages["render"].status == "pending"


def test_a_failing_stage_before_until_is_reported(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(ctx: RunContext) -> None:
        raise RuntimeError("stage broke")

    monkeypatch.setitem(pipeline.STAGES, "transcribe", boom)
    run_dir = new_run(project)

    outcome = run_pipeline(run_dir, until="transcribe")

    assert outcome.failed_stage == "transcribe" and "stage broke" in (outcome.error or "")


def test_an_unknown_until_is_an_error_before_anything_is_touched(project: Path) -> None:
    run_dir = new_run(project)
    before = (run_dir / "run.json").read_text(encoding="utf-8")

    with pytest.raises(RunError, match="unknown stage 'nope'"):
        run_pipeline(run_dir, until="nope")

    assert (run_dir / "run.json").read_text(encoding="utf-8") == before


def test_without_until_every_stage_runs(project: Path) -> None:
    assert run_pipeline(new_run(project)).ran == list(STAGE_NAMES)
