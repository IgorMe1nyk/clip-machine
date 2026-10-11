"""`clipmachine track` end to end with the frame pipe and the landmarker faked."""

import json
import re
from pathlib import Path

import pytest

import track_fakes
from clipmachine import cli
from clipmachine.config import dump_resolved
from clipmachine.models import RunState, StageState, TrackFile
from clipmachine.pipeline import RESOLVED_CONFIG, STAGE_NAMES, save_state
from clipmachine.utils import faces
from test_render import make_ctx, write_inputs
from track_fakes import write_model


def make_run(project: Path, *, preview_config: bool = False) -> Path:
    ctx = make_ctx(project)
    write_inputs(ctx.run_dir)
    if preview_config:
        ctx.config.app.speaker.debug_preview = True
    dump_resolved(ctx.config, ctx.run_dir / RESOLVED_CONFIG)
    state = RunState(
        input=str(project / "input.mp4"),
        client="example-client",
        created_at="2026-10-12T00:00:00+00:00",
        stages={name: StageState() for name in STAGE_NAMES},
    )
    save_state(ctx.run_dir, state)
    return ctx.run_dir


def clip_files(run_dir: Path) -> list[str]:
    folder = run_dir / "clips"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def test_track_writes_a_track_file_per_kept_clip_and_prints_one_line_each(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = make_run(project)
    write_model(project)
    track_fakes.install(monkeypatch, track_fakes.two_people(switch_at=10.0))
    run_json_before = (run_dir / "run.json").read_bytes()

    code = cli.main(["track", str(run_dir)], root=project)

    out = capsys.readouterr().out
    assert code == 0
    assert clip_files(run_dir) == ["c01.track.json", "c02.track.json"]  # c03 was not kept
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 2
    shape = (
        r"{id}: ok, faces 2, 2\.00 per sample, cuts 0, switches 1, face time 100%, "
        r"mouth read 100%, \d+\.\d s"
    )
    assert re.fullmatch(shape.format(id="c01"), lines[0])
    assert re.fullmatch(shape.format(id="c02"), lines[1])
    assert "w10" not in out  # no transcript text

    track = TrackFile.model_validate_json((run_dir / "clips" / "c02.track.json").read_text("utf-8"))
    assert (track.clip_id, track.clip_start, track.clip_end) == ("c02", 40.0, 62.5)
    assert track.source.sha256 == "0" * 64
    assert track.model_sha256 == faces.file_sha256(faces.model_dir(project) / faces.LANDMARKER.name)
    detector = faces.model_dir(project, faces.DETECTOR) / faces.DETECTOR.name
    assert track.detector_sha256 == faces.file_sha256(detector)
    assert (run_dir / "run.json").read_bytes() == run_json_before  # stage states untouched


def test_track_prints_the_fallback_reason(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = make_run(project)
    write_model(project)
    track_fakes.install(monkeypatch, track_fakes.nobody)

    assert cli.main(["track", str(run_dir)], root=project) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("c01: no_face, faces 0, 0.00 per sample, cuts 0, switches 0, ")
    assert "face time 0%, mouth read 0%, " in lines[0]


def test_preview_flag_writes_the_preview_files(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = make_run(project)
    write_model(project)
    fakes = track_fakes.install(monkeypatch, track_fakes.two_people())

    assert cli.main(["track", str(run_dir), "--preview"], root=project) == 0

    assert clip_files(run_dir) == [
        "c01.track.json",
        "c01.track.preview.mp4",
        "c02.track.json",
        "c02.track.preview.mp4",
    ]
    assert len(fakes.previews) == 2


def test_the_config_can_ask_for_the_preview(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = make_run(project, preview_config=True)
    write_model(project)
    fakes = track_fakes.install(monkeypatch, track_fakes.two_people())

    assert cli.main(["track", str(run_dir)], root=project) == 0

    assert len(fakes.previews) == 2


def test_no_preview_by_default(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = make_run(project)
    write_model(project)
    fakes = track_fakes.install(monkeypatch, track_fakes.two_people())
    cli.main(["track", str(run_dir)], root=project)
    assert fakes.previews == []


def test_a_run_without_kept_clips_says_so(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = make_ctx(project)
    write_inputs(ctx.run_dir, [("c03", None, 70.0, 95.0)])
    dump_resolved(ctx.config, ctx.run_dir / RESOLVED_CONFIG)
    save_state(
        ctx.run_dir,
        RunState(
            input="x.mp4",
            client="example-client",
            created_at="2026-10-12T00:00:00+00:00",
            stages={name: StageState() for name in STAGE_NAMES},
        ),
    )
    fakes = track_fakes.install(monkeypatch)

    assert cli.main(["track", str(ctx.run_dir)], root=project) == 0

    assert "no kept clips" in capsys.readouterr().out
    assert fakes.opened == 0
    assert clip_files(ctx.run_dir) == []


def test_a_missing_model_is_an_error_and_nothing_is_written(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = make_run(project)  # no model file
    fakes = track_fakes.install(monkeypatch)

    code = cli.main(["track", str(run_dir)], root=project)

    assert code == 2
    err = capsys.readouterr().err
    assert "face model not found" in err and "fetch-face-model" in err
    assert not (faces.model_dir(project)).exists()  # the track command never downloads
    assert clip_files(run_dir) == []
    assert fakes.opened == 0


def test_a_missing_detector_is_an_error_and_nothing_is_written(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = make_run(project)
    write_model(project)
    detector = faces.model_dir(project, faces.DETECTOR) / faces.DETECTOR.name
    detector.unlink()
    fakes = track_fakes.install(monkeypatch)

    code = cli.main(["track", str(run_dir)], root=project)

    assert code == 2
    err = capsys.readouterr().err
    assert "face detector model not found" in err
    assert "clipmachine fetch-face-model" in err
    assert not detector.exists()  # the track command never downloads
    assert clip_files(run_dir) == []
    assert fakes.opened == 0


def test_a_folder_that_is_not_a_run_is_an_error(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["track", str(project / "runs")], root=project) == 2
    assert "not a run folder" in capsys.readouterr().err


def test_a_media_error_is_reported_as_a_usage_error(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from clipmachine.utils.ffmpeg import MediaError

    run_dir = make_run(project)
    write_model(project)
    fakes = track_fakes.install(monkeypatch)

    def broken(*args: object, **kwargs: object) -> object:
        raise MediaError("frame extraction failed: boom")

    monkeypatch.setattr(track_fakes.tracking.ffmpeg, "read_frames", broken)

    assert cli.main(["track", str(run_dir)], root=project) == 2
    assert "frame extraction failed: boom" in capsys.readouterr().err
    assert fakes.closed == 1  # the landmarker was closed


def test_the_old_resolved_config_without_a_speaker_section_still_tracks(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    run_dir = make_run(project)
    write_model(project)
    path = run_dir / RESOLVED_CONFIG
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    del data["config"]["speaker"]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    track_fakes.install(monkeypatch, track_fakes.two_people())

    assert cli.main(["track", str(run_dir)], root=project) == 0

    stored = json.loads((run_dir / "clips" / "c01.track.json").read_text("utf-8"))
    assert stored["settings"]["sample_fps"] == 10  # the defaults
