"""Render against real ffmpeg/ffprobe, with the bundled font. Skipped (see conftest) when the
tools are not on PATH. The input is generated: a 4 s test pattern with a sine tone."""

import json
import subprocess
from pathlib import Path

import pytest

from clipmachine.models import Transcript, TranscriptSegment, TranscriptWord
from clipmachine.runs import write_json_atomic
from clipmachine.stages import render
from clipmachine.utils import ffmpeg
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
    assert data["output"]["size_bytes"] == (clips / "c01.mp4").stat().st_size
