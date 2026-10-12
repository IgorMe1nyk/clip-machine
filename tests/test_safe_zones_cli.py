"""`clipmachine safe-zones` with ffmpeg faked: it renders one clip into runs/_scratch/safe-zones/
and must leave the run folder exactly as it was."""

import hashlib
import shutil
from pathlib import Path

import pytest

from clipmachine import cli
from clipmachine.config import dump_resolved
from clipmachine.models import RunState, StageState
from clipmachine.pipeline import RESOLVED_CONFIG, STAGE_NAMES, save_state
from clipmachine.stages import render
from test_render import (
    DRIVE_OR_BACKSLASH,
    FakeFfmpeg,
    make_ctx,
    speaker_ctx,
    write_inputs,
)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeFfmpeg:
    fake = FakeFfmpeg()
    monkeypatch.setattr(render.ffmpeg, "render_clip", fake.render_clip)
    monkeypatch.setattr(render.ffmpeg, "probe", fake.probe)
    return fake


def save_run(ctx_run_dir: Path, ctx_config: object, project: Path) -> Path:
    dump_resolved(ctx_config, ctx_run_dir / RESOLVED_CONFIG)  # type: ignore[arg-type]
    state = RunState(
        input=str(project / "input.mp4"),
        client="example-client",
        created_at="2026-10-12T00:00:00+00:00",
        stages={name: StageState() for name in STAGE_NAMES},
    )
    save_state(ctx_run_dir, state)
    return ctx_run_dir


def centre_run(project: Path, style: str = "legacy") -> Path:
    ctx = make_ctx(project, style=style)
    write_inputs(ctx.run_dir)
    return save_run(ctx.run_dir, ctx.config, project)


def speaker_run(
    project: Path, monkeypatch: pytest.MonkeyPatch, style: str = "legacy", *, render_first: bool
) -> Path:
    ctx, _ = speaker_ctx(project, monkeypatch, style=style)
    if render_first:
        render.run(ctx)  # the clips get their track files
    return save_run(ctx.run_dir, ctx.config, project)


def listing(folder: Path) -> dict[str, str]:
    """Every file under the folder (recursive) and its hash."""
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def vf_of(fake_ffmpeg: FakeFfmpeg) -> str:
    args = fake_ffmpeg.calls[-1][1]
    return args[args.index("-vf") + 1]


def scratch_files(project: Path) -> list[str]:
    scratch = project / "runs" / "_scratch"
    return sorted(p.relative_to(scratch).as_posix() for p in scratch.rglob("*") if p.is_file())


# --- the render stage never draws the margins ---------------------------------------------------


def test_the_normal_render_stage_never_uses_drawbox(project: Path, fake: FakeFfmpeg) -> None:
    for style in ("legacy", "box_karaoke"):
        ctx = make_ctx(project, style=style, run_name=style)
        write_inputs(ctx.run_dir)
        render.run(ctx)
    assert fake.calls and all(
        "drawbox" not in call[1][call[1].index("-vf") + 1] for call in fake.calls
    )


# --- the command --------------------------------------------------------------------------------


@pytest.mark.parametrize("style", ["legacy", "box_karaoke"])
def test_it_writes_only_under_the_scratch_folder_and_leaves_the_run_folder_alone(
    project: Path, fake: FakeFfmpeg, capsys: pytest.CaptureFixture[str], style: str
) -> None:
    run_dir = centre_run(project, style)
    before = listing(run_dir)

    code = cli.main(["safe-zones", str(run_dir), "--clip", "c01"], root=project)

    out = capsys.readouterr().out.strip()
    assert code == 0
    assert listing(run_dir) == before  # nothing written, deleted or changed in the run folder
    assert scratch_files(project) == ["safe-zones/run/c01.ass", "safe-zones/run/c01.mp4"]
    expected = (project / "runs" / "_scratch" / "safe-zones" / "run" / "c01.mp4").resolve()
    assert Path(out).resolve() == expected
    assert fake.ids == ["c01"]
    _, args, cwd = fake.calls[0]
    assert cwd == run_dir.resolve()  # ffmpeg still runs in the run folder
    vf = args[args.index("-vf") + 1]
    assert "subtitles=filename=../_scratch/safe-zones/run/c01.ass:fontsdir=../../assets/fonts" in vf
    assert vf.count("drawbox") == 5 and vf.endswith("color=yellow@0.5:t=2")
    assert not DRIVE_OR_BACKSLASH.search(vf)
    ass = (expected.parent / "c01.ass").read_text(encoding="utf-8")
    assert ("Style: Cap" in ass) == (style == "box_karaoke")


def test_speaker_mode_uses_the_clips_track_and_writes_the_crop_commands_to_scratch(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = speaker_run(project, monkeypatch, "box_karaoke", render_first=True)
    fake.calls.clear()
    before = listing(run_dir)

    code = cli.main(["safe-zones", str(run_dir), "--clip", "c01"], root=project)

    assert code == 0
    assert listing(run_dir) == before
    assert scratch_files(project) == [
        "safe-zones/run/c01.ass",
        "safe-zones/run/c01.crop.cmd",
        "safe-zones/run/c01.mp4",
    ]
    vf = vf_of(fake)
    assert vf.startswith("sendcmd=f=../_scratch/safe-zones/run/c01.crop.cmd,crop=202:360:")
    assert "drawbox" in vf and not DRIVE_OR_BACKSLASH.search(vf)
    cmd = project / "runs" / "_scratch" / "safe-zones" / "run" / "c01.crop.cmd"
    assert cmd.read_text(encoding="utf-8") == (run_dir / "clips" / "c01.crop.cmd").read_text(
        encoding="utf-8"
    )


def test_running_it_twice_replaces_the_output(project: Path, fake: FakeFfmpeg) -> None:
    run_dir = centre_run(project)
    assert cli.main(["safe-zones", str(run_dir), "--clip", "c01"], root=project) == 0
    assert cli.main(["safe-zones", str(run_dir), "--clip", "c01"], root=project) == 0
    assert scratch_files(project) == ["safe-zones/run/c01.ass", "safe-zones/run/c01.mp4"]


# --- errors -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("clip", "message"),
    [
        ("c09", "c09 is not a kept clip"),  # not in the run
        ("c03", "c03 is not a kept clip"),  # in scored.json but not kept
        ("..\\evil", "is not a clip id"),
        ("", "is not a clip id"),
    ],
)
def test_an_unknown_or_unkept_clip_is_a_clean_error(
    project: Path,
    fake: FakeFfmpeg,
    capsys: pytest.CaptureFixture[str],
    clip: str,
    message: str,
) -> None:
    run_dir = centre_run(project)
    before = listing(run_dir)

    code = cli.main(["safe-zones", str(run_dir), "--clip", clip], root=project)

    err = capsys.readouterr().err
    assert code == 2 and err.startswith("error: ") and message in err
    assert fake.calls == [] and listing(run_dir) == before
    assert not (project / "runs" / "_scratch").exists()


def test_a_missing_track_says_to_render_the_run_first(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = speaker_run(project, monkeypatch, render_first=False)
    before = listing(run_dir)

    code = cli.main(["safe-zones", str(run_dir), "--clip", "c01"], root=project)

    assert code == 2
    assert "no valid track for c01; render the run first" in capsys.readouterr().err
    assert fake.calls == [] and listing(run_dir) == before
    assert not (project / "runs" / "_scratch").exists()


def test_a_stale_track_is_not_used(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = speaker_run(project, monkeypatch, render_first=True)
    (run_dir / "clips" / "c01.track.json").write_text("{not json", encoding="utf-8")
    before = listing(run_dir)

    code = cli.main(["safe-zones", str(run_dir), "--clip", "c01"], root=project)

    assert code == 2 and "no valid track for c01" in capsys.readouterr().err
    assert listing(run_dir) == before
    assert not (project / "runs" / "_scratch").exists()


def test_a_run_folder_outside_the_project_is_refused(
    project: Path,
    fake: FakeFfmpeg,
    capsys: pytest.CaptureFixture[str],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    run_dir = centre_run(project)
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "run"
    shutil.copytree(run_dir, elsewhere)
    before = listing(elsewhere)

    code = cli.main(["safe-zones", str(elsewhere), "--clip", "c01"], root=project)

    assert code == 2 and "not a run folder inside" in capsys.readouterr().err
    assert fake.calls == [] and listing(elsewhere) == before
    assert not (project / "runs" / "_scratch").exists()


def test_a_folder_that_is_not_a_run_is_an_error(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "runs" / "plain").mkdir()
    assert (
        cli.main(["safe-zones", str(project / "runs" / "plain"), "--clip", "c01"], root=project)
        == 2
    )
    assert "not a run folder" in capsys.readouterr().err


def test_a_failed_render_leaves_no_temp_file(
    project: Path, fake: FakeFfmpeg, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = centre_run(project)
    fake.fail_on.add("c01")

    code = cli.main(["safe-zones", str(run_dir), "--clip", "c01"], root=project)

    assert code == 2 and "render failed for c01" in capsys.readouterr().err
    assert scratch_files(project) == ["safe-zones/run/c01.ass"]


# --- the parser ---------------------------------------------------------------------------------


def test_the_parser_knows_safe_zones() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["safe-zones", "runs/x", "--clip", "c01"])
    assert (args.command, args.run_folder, args.clip) == ("safe-zones", Path("runs/x"), "c01")
    assert "safe-zones" in parser.format_help()


def test_the_clip_option_is_required() -> None:
    with pytest.raises(SystemExit) as info:
        cli.build_parser().parse_args(["safe-zones", "runs/x"])
    assert info.value.code == 2
