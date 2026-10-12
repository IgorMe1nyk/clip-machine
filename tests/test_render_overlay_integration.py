"""The watermark overlay against real ffmpeg: both blends, a canvas larger than the frame, a moving
crop. Skipped (see conftest) when ffmpeg is not on PATH. The input is generated; the images are
synthetic."""

import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from campaign_helpers import black_mark, transparent_mark, write_png
from clipmachine.utils import ffmpeg
from clipmachine.utils.crop import CropBox

pytestmark = pytest.mark.ffmpeg

OUT_W, OUT_H = 270, 480
SECONDS = 3.0
CROP = CropBox(w=152, h=270, x=164, y=0)  # 9:16 out of the 480x270 source


def make_source(path: Path) -> None:
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=duration=4:size=480x270:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ]  # fmt: skip
    result = subprocess.run(
        args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr


def prepare(folder: Path) -> None:
    make_source(folder / "source.mp4")
    (folder / "fonts").mkdir()
    shutil.copy(
        Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Montserrat-Bold.ttf",
        folder / "fonts" / "Montserrat-Bold.ttf",
    )
    (folder / "c.ass").write_text(
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {OUT_W}\nPlayResY: {OUT_H}\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, Alignment\n"
        "Style: Default,Montserrat Bold,20,&H00FFFFFF,2\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n",
        encoding="utf-8",
    )


def render(folder: Path, name: str, *, graph: str | None, image: Path | None, moving: bool) -> Path:
    sendcmd = None
    if moving:
        (folder / "c.cmd").write_text(
            ffmpeg.crop_commands([(0.0, 164), (1.0, 100), (2.0, 20)]), encoding="utf-8"
        )
        sendcmd = "c.cmd"
    chain = ffmpeg.video_filter(CROP, OUT_W, OUT_H, "c.ass", "fonts", sendcmd)
    out = folder / name
    common: dict[str, Any] = {
        "start": 0.5,
        "duration": SECONDS,
        "crf": 18,
        "preset": "ultrafast",
        "audio_bitrate": "64k",
        "audio_fade": 0.05,
    }
    if graph is None:
        args = ffmpeg.render_args(folder / "source.mp4", out, vf=chain, **common)
    else:
        args = ffmpeg.render_args(
            folder / "source.mp4",
            out,
            filter_complex=graph.replace("CHAIN", chain),
            overlay_image=image,
            **common,
        )
        ffmpeg.assert_source_audio_only(args)
    ffmpeg.render_clip(args, folder, SECONDS)
    ffmpeg.verify_clip(ffmpeg.probe(out), width=OUT_W, height=OUT_H, duration=SECONDS)
    return out


def frames_of(path: Path) -> tuple["np.ndarray[Any, Any]", "np.ndarray[Any, Any]"]:
    frames = ffmpeg.read_timed_frames(
        ffmpeg.timed_frame_args(path, start=0.0, duration=SECONDS, width=OUT_W), OUT_W, OUT_H, 60
    )
    assert len(frames) >= 25
    return frames[1][1].astype(np.int32), frames[-2][1].astype(np.int32)


def test_a_transparent_png_with_blend_normal_stays_on_screen_for_the_whole_clip(
    tmp_path: Path,
) -> None:
    prepare(tmp_path)
    write_png(tmp_path / "mark.png", transparent_mark(100, 30, (20, 9, 80, 21)))
    x, y = 105, 60  # the bar lands at x 125..185, y 69..81
    graph = ffmpeg.overlay_graph("CHAIN", x=x, y=y, size=None, blend="normal", frame=(OUT_W, OUT_H))

    out = render(tmp_path, "wm.mp4", graph=graph, image=tmp_path / "mark.png", moving=False)

    plain = render(tmp_path, "plain.mp4", graph=None, image=None, moving=False)
    audio = ffmpeg.probe(out).audio
    assert audio is not None and audio.codec == "aac"
    for frame, plain_frame in zip(frames_of(out), frames_of(plain), strict=True):
        bar = frame[72:79, 130:180]
        assert bar.min() >= 225  # white on screen, at the start and at the end
        assert np.abs(frame[300:460] - plain_frame[300:460]).mean() < 4  # nothing else changed
        assert not (
            np.abs(plain_frame[72:79, 130:180]) >= 225
        ).all()  # the test pattern is not white


def test_an_opaque_black_png_larger_than_the_frame_with_blend_screen(tmp_path: Path) -> None:
    prepare(tmp_path)
    # a 400x100 canvas, wider than the 270 px frame; the white bar is 120 px wide in its middle
    write_png(tmp_path / "mark.png", black_mark(400, 100, (140, 35, 260, 65)))
    x, y = -65, 40  # the bar lands at x 75..195, y 75..105; the canvas reaches past both edges
    graph = ffmpeg.overlay_graph("CHAIN", x=x, y=y, size=None, blend="screen", frame=(OUT_W, OUT_H))

    out = render(tmp_path, "wm.mp4", graph=graph, image=tmp_path / "mark.png", moving=True)

    plain = render(tmp_path, "plain.mp4", graph=None, image=None, moving=True)
    audio = ffmpeg.probe(out).audio
    assert audio is not None and audio.codec == "aac"
    for frame, plain_frame in zip(frames_of(out), frames_of(plain), strict=True):
        assert frame[80:100, 85:185].min() >= 225  # the bar is on screen for the whole clip
        # the black margin of the canvas (x 0..70, y 40..140 outside the bar) is nearly untouched
        margin = np.abs(frame[45:135, 2:70] - plain_frame[45:135, 2:70])
        assert margin.mean() < 6
        assert np.abs(frame[200:460] - plain_frame[200:460]).mean() < 4


def test_a_scaled_image_and_the_same_png_drawn_with_blend_normal(tmp_path: Path) -> None:
    prepare(tmp_path)
    write_png(tmp_path / "mark.png", transparent_mark(200, 60, (40, 18, 160, 42)))
    # scaled to half: 100x30, bar x 20..80, y 9..21; placed at (90, 200)
    graph = ffmpeg.overlay_graph(
        "CHAIN", x=90, y=200, size=(100, 30), blend="normal", frame=(OUT_W, OUT_H)
    )

    out = render(tmp_path, "wm.mp4", graph=graph, image=tmp_path / "mark.png", moving=False)

    for frame in frames_of(out):
        assert frame[212:219, 115:165].min() >= 225
