"""Brief 12.1 against real ffmpeg: a generated source with a magenta "panel" beside a (faked)
face; the text detector is faked to report the panel. The shot with the panel is rendered in
the fit layout (the panel visible, whole), the other shot in the speaker crop (no panel), and
the sendcmd lines addressed to `crop@spk` move the crop. Skipped without ffmpeg."""

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

import graphics_fakes
import track_fakes
from clipmachine.models import GraphicsConfig
from clipmachine.stages import render
from clipmachine.utils import ffmpeg
from graphics_fakes import Box
from test_render import make_ctx, write_inputs

pytestmark = pytest.mark.ffmpeg

PANEL = Box(0.05, 0.25, 0.30, 0.42)  # x 16-96, y 45-76 of a 320x180 frame
CUT_AT = 1.5  # seconds from the clip start: the panel shot, then a plain one


def make_source_with_panel(path: Path) -> None:
    args = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=gray:duration=4:size=320x180:rate=10,"
        "drawbox=x=16:y=45:w=80:h=31:color=magenta:t=fill",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=4",
        "-c:v",
        "mpeg4",
        "-q:v",
        "2",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    result = subprocess.run(
        args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr


def magenta_pixels(path: Path, at: float) -> int:
    """How many clearly magenta pixels one frame of the output has at `at` seconds. ffmpeg is
    run here directly: the fakes patch the frame reader the analyses use."""
    args = ffmpeg.frame_args(path, start=at, duration=0.3, fps=5, width=270)
    result = subprocess.run(
        args, stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    frame = np.frombuffer(result.stdout[: 270 * 480 * 3], dtype=np.uint8).reshape(480, 270, 3)
    r, g, b = frame[:, :, 0].astype(int), frame[:, :, 1].astype(int), frame[:, :, 2].astype(int)
    return int(np.sum((r > 150) & (b > 150) & (g < 90)))


def test_the_panel_shot_is_shown_whole_and_the_other_shot_is_cropped(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(project)  # legacy captions: no safe area, the frame is centred on the canvas
    write_inputs(ctx.run_dir, [("c01", 1, 0.5, 3.5)], width=320, height=180)
    make_source_with_panel(ctx.run_dir / "source.mp4")
    ctx.config.app.video.crop = "speaker"
    ctx.config.app.video.graphics = GraphicsConfig(enabled=True)
    ctx.config.app.speaker = ctx.config.app.speaker.model_copy(update={"signal": "mouth"})
    track_fakes.write_model(project)
    graphics_fakes.write_text_model(project)
    # one face on the right, further right after the cut, so the crop moves (a sendcmd line)
    fakes = track_fakes.install(
        monkeypatch,
        lambda t: [
            track_fakes.RawFace(0.6 if t < CUT_AT else 0.8, 0.5, 0.2, 0.3, track_fakes.talking(t))
        ],
        track_fakes.cut_at(CUT_AT),
    )
    graphics_fakes.install(
        monkeypatch,
        lambda t: [PANEL] if t < CUT_AT else [],
        track_fakes=fakes,
    )

    render.run(ctx)

    clips = ctx.run_dir / "clips"
    data = json.loads((clips / "c01.json").read_text(encoding="utf-8"))
    (span,) = data["fit_spans"]  # the exact cut frame, found from the source's own frames
    assert span[0] == 0.0 and span[1] == pytest.approx(CUT_AT, abs=0.1)
    assert data["fit_seconds"] == pytest.approx(CUT_AT, abs=0.1)
    cmd = (clips / "c01.crop.cmd").read_text(encoding="utf-8")
    assert "crop@spk x" in cmd and " crop x" not in cmd
    info = ffmpeg.probe(clips / "c01.mp4")
    assert info.video is not None and (info.video.width, info.video.height) == (1080, 1920)
    assert magenta_pixels(clips / "c01.mp4", 0.6) > 300  # the panel, whole, in the fit shot
    assert magenta_pixels(clips / "c01.mp4", 2.4) == 0  # the speaker crop, right of the panel
