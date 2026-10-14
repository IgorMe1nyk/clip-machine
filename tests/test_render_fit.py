"""Brief 12.1: the render stage with the graphics-aware fit layout (fakes for ffmpeg, the face
finder and the text detector)."""

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

import graphics_fakes
import track_fakes
from clipmachine.context import RunContext
from clipmachine.models import GraphicsConfig, GraphicsFile
from clipmachine.stages import render
from clipmachine.utils import ffmpeg, graphics
from graphics_fakes import LOGO, PANEL, TITLE
from test_render import (
    BIG,
    SMOOTH,
    FakeFfmpeg,
    clip_files,
    expected_commands,
    file_hash,
    read_json,
    speaker_ctx,
    vf_of,
)

# the two-person scenario switches speaker at 10 s of the clip; the panel is in the second shot
# for 6 of its 10 s, so the whole shot (10-20 s) is shown in the fit layout
PANEL_SPAN = (10.0, 16.0)
DEFAULT_SCENARIO = graphics_fakes.panel_between(*PANEL_SPAN)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeFfmpeg:
    fake = FakeFfmpeg()
    monkeypatch.setattr(render.ffmpeg, "render_clip", fake.render_clip)
    monkeypatch.setattr(render.ffmpeg, "probe", fake.probe)
    return fake


def graph_of(args: list[str]) -> str:
    return args[args.index("-filter_complex") + 1]


def fit_ctx(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: graphics_fakes.Scenario = DEFAULT_SCENARIO,
    *,
    style: str = "box_karaoke",
    enabled: bool = True,
    **keys: Any,
) -> tuple[RunContext, graphics_fakes.GraphicsFakes]:
    """The speaker rig with the text detector faked and video.graphics on (unless `enabled` is
    False), on the big source so the crop window is narrow."""
    ctx, fakes = speaker_ctx(project, monkeypatch, style=style)
    graphics_fakes.write_text_model(project)
    gfakes = graphics_fakes.install(monkeypatch, scenario, track_fakes=fakes)
    ctx.config.app.video.graphics = GraphicsConfig(enabled=enabled, **keys)
    return ctx, gfakes


def test_a_shot_with_a_panel_outside_the_crop_is_rendered_in_the_fit_layout(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, gfakes = fit_ctx(project, monkeypatch)
    render.run(ctx)

    clips = ctx.run_dir / "clips"
    args = fake.calls[0][1]
    assert "-vf" not in args and args[args.index("-map") + 1] == "[v]"
    graph = graph_of(args)
    assert graph.startswith("[0:v:0]split=3[fit_src][fit_frame][fit_fill];")
    assert "[fit_src]sendcmd=f=clips/c01.crop.cmd,crop@spk=202:360:" in graph
    assert graph.endswith("subtitles=filename=clips/c01.ass:fontsdir=../../assets/fonts[v]")
    assert "enable='between(t,10.000,20.000)'" in graph  # the whole second shot
    # the command file addresses the named crop, with the same moves as before
    text = (clips / "c01.crop.cmd").read_text(encoding="utf-8")
    assert text == expected_commands(ctx, "c01", 20.0).replace("crop x", "crop@spk x")
    data = read_json(clips / "c01.json")
    assert data["fit_spans"] == [[10.0, 20.0]] and data["fit_seconds"] == 10.0
    assert "graphics_fit" in data["notes"]  # type: ignore[operator]
    assert data["static_overlays"] == [TITLE.to_model().model_dump(), LOGO.to_model().model_dump()]
    assert data["graphics_sha256"] == file_hash(clips / "c01.graphics.json")
    assert data["settings"]["graphics_enabled"] is True  # type: ignore[index]
    found = GraphicsFile.model_validate_json((clips / "c01.graphics.json").read_text("utf-8"))
    assert [s.fit for s in found.shots] == [False, True]
    assert len(gfakes.opened) == 2  # one analysis per clip


def test_captions_in_a_fit_span_sit_below_the_frame_and_inside_the_safe_area(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = fit_ctx(project, monkeypatch)
    render.run(ctx)

    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["outside_safe_zone"] == 0
    ass = (ctx.run_dir / "clips" / "c01.ass").read_text(encoding="utf-8")
    # \pos(x,y) of the caption text lines: those in the span are higher up than the others
    positions = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:") and "\\k" in line:
            start = line.split(",")[1]
            h, m, s = start.split(":")
            t = int(h) * 3600 + int(m) * 60 + float(s)
            y = int(line.split("\\pos(")[1].split(")")[0].split(",")[1])
            positions.append((t, y))
    inside = {y for t, y in positions if 10.0 <= t < 20.0}
    outside = {y for t, y in positions if not 10.0 <= t < 20.0}
    assert inside and outside and max(inside) < min(outside)


def test_no_zoom_starts_inside_a_fit_span(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = fit_ctx(project, monkeypatch)
    ctx.config.app.video.zoom = SMOOTH
    render.run(ctx)

    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["fit_spans"] == [[10.0, 20.0]]
    for start, _end in data["zoom_spans"]:  # type: ignore[misc]
        assert not (10.0 <= start < 20.0)
    text = (ctx.run_dir / "clips" / "c01.crop.cmd").read_text(encoding="utf-8")
    assert "crop@spk" in text and " crop x" not in text


def test_without_graphics_outside_the_crop_the_chain_is_exactly_as_before(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, gfakes = fit_ctx(project, monkeypatch, graphics_fakes.overlays_only, style="legacy")
    render.run(ctx)

    args = fake.calls[0][1]
    assert "-filter_complex" not in args
    assert vf_of(args).startswith("sendcmd=f=clips/c01.crop.cmd,crop=202:360:")
    text = (ctx.run_dir / "clips" / "c01.crop.cmd").read_text(encoding="utf-8")
    assert text == expected_commands(ctx, "c01", 20.0)  # the plain target
    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["fit_spans"] == [] and data["notes"] == []
    assert data["graphics_sha256"] is not None  # analysed, nothing to fit
    assert len(gfakes.opened) == 2


def test_graphics_off_makes_no_call_writes_no_file_and_keeps_the_old_arguments(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, gfakes = fit_ctx(project, monkeypatch, enabled=False, style="legacy")
    render.run(ctx)

    assert gfakes.opened == []  # the detector was never opened
    assert not [n for n in clip_files(ctx.run_dir) if n.endswith(".graphics.json")]
    args = fake.calls[0][1]
    assert vf_of(args).startswith("sendcmd=f=clips/c01.crop.cmd,crop=202:360:")
    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["graphics_sha256"] is None and data["fit_spans"] == []
    assert data["settings"]["graphics_enabled"] is False  # type: ignore[index]
    assert data["settings"]["graphics_blur"] == 0  # type: ignore[index]


def test_a_missing_text_model_is_a_clear_error_before_anything_is_rendered(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = fit_ctx(project, monkeypatch)
    (project / "models" / "textdet" / "text_detection_en_ppocrv3_2023may.onnx").unlink()
    with pytest.raises(render.RunError, match="clipmachine fetch-text-model"):
        render.run(ctx)
    assert fake.calls == []


def test_the_fallback_track_is_judged_against_the_centre_crop(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, fakes = speaker_ctx(project, monkeypatch, track_fakes.nobody)  # no face: centre crop
    graphics_fakes.write_text_model(project)
    graphics_fakes.install(monkeypatch, graphics_fakes.panel_between(0.0, 8.0), track_fakes=fakes)
    ctx.config.app.video.graphics = GraphicsConfig(enabled=True)
    render.run(ctx)

    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["track_fallback"] == "no_face"
    assert data["fit_spans"] == [[0.0, 20.0]]  # one shot, the panel at x 0.07-0.38 is outside
    graph = graph_of(fake.calls[0][1])
    assert "[fit_src]crop@spk=202:360:218:0," in graph and "sendcmd" not in graph


def test_resume_keeps_a_fit_clip_and_rerenders_when_graphics_change(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, gfakes = fit_ctx(project, monkeypatch)
    render.run(ctx)
    assert fake.ids == ["c01", "c02"]

    ctx = dataclasses.replace(ctx, resuming=True)
    render.run(ctx)
    assert fake.ids == ["c01", "c02"]  # nothing re-rendered, nothing re-analysed
    assert len(gfakes.opened) == 2

    ctx.config.app.video.graphics = GraphicsConfig(enabled=True, min_presence=0.5)
    render.run(ctx)
    assert fake.ids == ["c01", "c02", "c01", "c02"]  # settings changed: stale
    assert len(gfakes.opened) == 4

    ctx.config.app.video.graphics = GraphicsConfig(enabled=False)
    render.run(ctx)
    assert len(fake.ids) == 6  # graphics off is a different output too


def test_a_stale_graphics_file_is_analysed_again_on_resume(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, gfakes = fit_ctx(project, monkeypatch)
    render.run(ctx)
    path = graphics.graphics_path(ctx.run_dir / "clips", "c01")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["model_sha256"] = "f" * 64
    path.write_text(json.dumps(data), encoding="utf-8")

    ctx = dataclasses.replace(ctx, resuming=True)
    render.run(ctx)
    assert fake.ids == ["c01", "c02", "c01"] and len(gfakes.opened) == 3


def test_the_log_line_names_the_fit_seconds_and_the_overlays(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    ctx, _ = fit_ctx(project, monkeypatch, style="legacy")
    with caplog.at_level("INFO", logger="clipmachine"):
        render.run(ctx)
    line = next(r.getMessage() for r in caplog.records if "render: c01" in r.getMessage())
    assert "fit 10.0 s in 1 spans, 2 static overlays" in line
    assert "synthetic" not in line and "w1" not in line


def test_the_hook_sees_faces_through_the_fitted_frame(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the panel shot is the first 8 s, when the hook is on; the hook avoids faces
    ctx, _ = fit_ctx(project, monkeypatch, graphics_fakes.panel_between(0.0, 8.0))
    ctx.config.app.hook = ctx.config.app.hook.model_copy(update={"position": "avoid_faces"})
    render.run(ctx)
    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["fit_spans"][0] == [0.0, 10.0]  # type: ignore[index]
    assert data["hook_shown"] is True and "hook_faces_unknown" not in data["notes"]  # type: ignore[operator]


def test_big_source_fit_graph_uses_the_run_layout(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_render import write_inputs

    ctx, _ = fit_ctx(project, monkeypatch)
    write_inputs(ctx.run_dir, **BIG)
    render.run(ctx)
    graph = graph_of(fake.calls[0][1])
    assert "[fit_frame]scale=1080:608,setsar=1[fit_fg];" in graph
    assert "[fit_fill]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920," in graph
    assert "boxblur=20,eq=brightness=-0.350" in graph
    ffmpeg.assert_source_audio_only(fake.calls[0][1])
    assert PANEL.area > 0.02
