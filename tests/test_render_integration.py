"""Render against real ffmpeg/ffprobe, with the bundled font. Skipped (see conftest) when the
tools are not on PATH. The input is generated: a 4 s test pattern with a sine tone."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from clipmachine.models import Transcript, TranscriptSegment, TranscriptWord
from clipmachine.runs import write_json_atomic
from clipmachine.stages import render
from clipmachine.utils import ffmpeg
from clipmachine.utils.crop import CropBox
from test_render import make_ctx, write_inputs

pytestmark = pytest.mark.ffmpeg


def make_source(path: Path) -> None:
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
        "testsrc=duration=4:size=320x180:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=4",
        "-c:v",
        "mpeg4",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    result = subprocess.run(
        args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr


def test_real_render_makes_a_vertical_h264_aac_clip(project: Path) -> None:
    ctx = make_ctx(project)
    write_inputs(ctx.run_dir, [("c01", 1, 0.5, 3.5)], width=320, height=180)
    make_source(ctx.run_dir / "source.mp4")
    # a two-word transcript inside the clip
    transcript = Transcript.model_validate_json(
        (ctx.run_dir / "transcript.json").read_text(encoding="utf-8")
    )
    transcript.segments = [
        TranscriptSegment(
            id=0,
            start=1.0,
            end=2.5,
            text="",
            avg_logprob=0.0,
            no_speech_prob=0.0,
            words=[
                TranscriptWord(start=1.0, end=1.4, word=" Hello", probability=0.9),
                TranscriptWord(start=2.0, end=2.5, word=" there.", probability=0.9),
            ],
        )
    ]
    write_json_atomic(ctx.run_dir / "transcript.json", transcript.model_dump(mode="json"))

    render.run(ctx)

    clips = ctx.run_dir / "clips"
    assert sorted(p.name for p in clips.iterdir()) == ["c01.ass", "c01.json", "c01.mp4"]
    info = ffmpeg.probe(clips / "c01.mp4")
    assert info.video is not None and info.audio is not None
    assert (info.video.width, info.video.height) == (1080, 1920)
    assert info.video.codec == "h264"
    assert info.audio.codec == "aac"
    assert info.duration_seconds == pytest.approx(3.0, abs=0.15)
    data = json.loads((clips / "c01.json").read_text(encoding="utf-8"))
    assert data["crop"] == {"w": 100, "h": 180, "x": 110, "y": 0}
    assert data["caption_events"] == 2
    assert data["settings"]["audio_fade_seconds"] == 0.08  # rendered with the fade, still verified
    assert data["output"]["size_bytes"] == (clips / "c01.mp4").stat().st_size


def test_real_render_of_box_karaoke_captions_and_the_hook(project: Path) -> None:
    ctx = make_ctx(project, style="box_karaoke")
    write_inputs(ctx.run_dir, [("c01", 1, 0.5, 3.5)], width=320, height=180)
    make_source(ctx.run_dir / "source.mp4")
    transcript = Transcript.model_validate_json(
        (ctx.run_dir / "transcript.json").read_text(encoding="utf-8")
    )
    transcript.segments = [
        TranscriptSegment(
            id=0,
            start=1.0,
            end=2.5,
            text="",
            avg_logprob=0.0,
            no_speech_prob=0.0,
            words=[
                TranscriptWord(start=1.0, end=1.4, word=" Hello", probability=0.9),
                TranscriptWord(start=1.5, end=1.9, word=" there", probability=0.9),
            ],
        )
    ]
    write_json_atomic(ctx.run_dir / "transcript.json", transcript.model_dump(mode="json"))

    render.run(ctx)

    clips = ctx.run_dir / "clips"
    info = ffmpeg.probe(clips / "c01.mp4")  # libass parsed the file and ffmpeg finished
    assert info.video is not None and (info.video.width, info.video.height) == (1080, 1920)
    data = json.loads((clips / "c01.json").read_text(encoding="utf-8"))
    assert data["caption_lines"] == 1 and data["caption_max_words"] == 2
    assert data["hook_shown"] is True and data["outside_safe_zone"] == 0


def test_real_safe_zone_preview_is_a_valid_clip_in_the_scratch_folder(project: Path) -> None:
    ctx = make_ctx(project, style="box_karaoke")
    write_inputs(ctx.run_dir, [("c01", 1, 0.5, 3.5)], width=320, height=180)
    make_source(ctx.run_dir / "source.mp4")
    before = sorted(p.relative_to(ctx.run_dir) for p in ctx.run_dir.rglob("*"))

    out = render.render_safe_zone_preview(ctx.run_dir, project, ctx.config.app, "c01")

    assert out == (project / "runs" / "_scratch" / "safe-zones" / "run" / "c01.mp4").resolve()
    info = ffmpeg.probe(out)  # libass and drawbox accepted the filter; the size is verified too
    assert info.video is not None and (info.video.width, info.video.height) == (1080, 1920)
    assert sorted(p.relative_to(ctx.run_dir) for p in ctx.run_dir.rglob("*")) == before


def make_halves_source(path: Path) -> None:
    """5 s at 30 fps, 320x180: the left half red, the right half blue, with a sine tone."""
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=160x180:r=30:d=5",
        "-f", "lavfi", "-i", "color=c=blue:s=160x180:r=30:d=5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
        "-filter_complex", "[0:v][1:v]hstack[v]",
        "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(path),
    ]  # fmt: skip
    result = subprocess.run(
        args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr


def test_a_sendcmd_file_moves_the_crop_on_the_exact_frame(tmp_path: Path) -> None:
    make_halves_source(tmp_path / "source.mp4")
    (tmp_path / "fonts").mkdir()
    shutil.copy(
        Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Montserrat-Bold.ttf",
        tmp_path / "fonts" / "Montserrat-Bold.ttf",
    )
    (tmp_path / "c.ass").write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 108\nPlayResY: 192\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, Alignment\n"
        "Style: Default,Montserrat Bold,20,&H00FFFFFF,2\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n",
        encoding="utf-8",
    )
    # a 100x180 window: x 0 is all red, x 220 is all blue; right at 1.0 s, back at 2.0165 s
    # (between two frames) of the clip, which starts 1.0 s into the source
    to_right, to_left = 1.0, 2.0165
    commands = ffmpeg.crop_commands([(0.0, 0), (to_right, 220), (to_left, 0)])
    (tmp_path / "c.cmd").write_text(commands, encoding="utf-8")
    vf = ffmpeg.video_filter(
        CropBox(w=100, h=180, x=0, y=0), 108, 192, "c.ass", "fonts", sendcmd_rel="c.cmd"
    )
    out = tmp_path / "out.mp4"
    args = ffmpeg.render_args(
        tmp_path / "source.mp4",
        out,
        start=1.0,
        duration=3.0,
        vf=vf,
        crf=18,
        preset="ultrafast",
        audio_bitrate="64k",
    )
    ffmpeg.render_clip(args, tmp_path, 3.0)

    frames = ffmpeg.read_timed_frames(
        ffmpeg.timed_frame_args(out, start=0.0, duration=3.0, width=108), 108, 192, 60
    )
    assert len(frames) == pytest.approx(90, abs=2)

    def colour(frame: "np.ndarray[Any, Any]") -> str:
        red, _, blue = (int(v) for v in frame[10, 54])  # upper part, away from the captions
        return "red" if red > blue + 80 else "blue" if blue > red + 80 else "other"

    seen = [(t, colour(frame)) for t, frame in frames]
    for change, before, after in ((to_right, "red", "blue"), (to_left, "blue", "red")):
        last_before = max(k for k, (t, _) in enumerate(seen) if t < change)
        assert seen[last_before][1] == before, (change, seen[last_before])
        assert seen[last_before + 1][1] == after, (change, seen[last_before + 1])
        assert seen[last_before + 1][0] >= change
    assert [c for _, c in seen].count("other") == 0


def test_a_sendcmd_file_zooms_in_and_out_on_the_exact_frames(tmp_path: Path) -> None:
    """A punch-in (Brief R2) is a crop that changes size and position in one command line:
    the output keeps its size and the picture jumps on the frame the line is timed for."""
    make_halves_source(tmp_path / "source.mp4")
    (tmp_path / "fonts").mkdir()
    shutil.copy(
        Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Montserrat-Bold.ttf",
        tmp_path / "fonts" / "Montserrat-Bold.ttf",
    )
    (tmp_path / "c.ass").write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 108\nPlayResY: 192\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, Alignment\n"
        "Style: Default,Montserrat Bold,20,&H00FFFFFF,2\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n",
        encoding="utf-8",
    )
    # The base window 100x180 at x 60 sees only red (the probe pixel, the middle of the output,
    # is at source x 110). The zoomed window 50x90 at x 160 sees only blue. In at 1.0 s, out at
    # 2.0165 s (between two frames) of the clip, which starts 1.0 s into the source.
    base = CropBox(w=100, h=180, x=60, y=0)
    zoomed = CropBox(w=50, h=90, x=160, y=45)
    zoom_in, zoom_out = 1.0, 2.0165
    commands = ffmpeg.crop_box_commands([(0.0, base), (zoom_in, zoomed), (zoom_out, base)])
    (tmp_path / "c.cmd").write_text(commands, encoding="utf-8")
    vf = ffmpeg.video_filter(base, 108, 192, "c.ass", "fonts", sendcmd_rel="c.cmd")
    out = tmp_path / "out.mp4"
    args = ffmpeg.render_args(
        tmp_path / "source.mp4",
        out,
        start=1.0,
        duration=3.0,
        vf=vf,
        crf=18,
        preset="ultrafast",
        audio_bitrate="64k",
    )
    ffmpeg.render_clip(args, tmp_path, 3.0)

    frames = ffmpeg.read_timed_frames(
        ffmpeg.timed_frame_args(out, start=0.0, duration=3.0, width=108), 108, 192, 60
    )
    assert len(frames) == pytest.approx(90, abs=2)
    assert all(frame.shape == (192, 108, 3) for _, frame in frames)  # the size never changes

    def colour(frame: "np.ndarray[Any, Any]") -> str:
        red, _, blue = (int(v) for v in frame[10, 54])  # upper part, away from the captions
        return "red" if red > blue + 80 else "blue" if blue > red + 80 else "other"

    seen = [(t, colour(frame)) for t, frame in frames]
    for change, before, after in ((zoom_in, "red", "blue"), (zoom_out, "blue", "red")):
        last_before = max(k for k, (t, _) in enumerate(seen) if t < change)
        assert seen[last_before][1] == before, (change, seen[last_before])
        assert seen[last_before + 1][1] == after, (change, seen[last_before + 1])
        assert seen[last_before + 1][0] >= change
    assert [c for _, c in seen].count("other") == 0
