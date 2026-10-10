"""A real Whisper model on the CPU. Skipped unless CLIPMACHINE_WHISPER_TESTS=1 (see conftest).

The first run downloads the `tiny` model (about 75 MB) into <repo>/models/, which is git-ignored.
The audio is a lavfi sine tone, so the transcript is expected to hold no speech: the point is that
the whole real path (model load, VAD, decode, validation, both outputs) works and is schema-valid.
"""

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from clipmachine.config import load_resolved
from clipmachine.context import RunContext
from clipmachine.models import TranscribeConfig, Transcript
from clipmachine.stages import transcribe

pytestmark = [pytest.mark.whisper, pytest.mark.ffmpeg]

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_audio(path: Path, seconds: int) -> None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_tiny_model_on_cpu_writes_a_valid_transcript(project: Path, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    make_audio(run_dir / "audio.wav", 5)
    resolved = load_resolved(project, "example-client")
    cfg = TranscribeConfig(model="tiny", device="cpu", compute_type="int8", language="en")
    resolved = dataclasses.replace(
        resolved, app=resolved.app.model_copy(update={"transcribe": cfg})
    )
    ctx = RunContext(
        run_dir=run_dir, config=resolved, input_path=run_dir / "x.mp4", project_root=REPO_ROOT
    )

    transcribe.run(ctx)

    data = json.loads((run_dir / "transcript.json").read_text(encoding="utf-8"))
    transcript = Transcript.model_validate(data)
    assert (transcript.device, transcript.compute_type, transcript.model) == ("cpu", "int8", "tiny")
    assert transcript.audio_duration_seconds == pytest.approx(5, abs=0.1)
    assert transcript.transcribe_seconds > 0
    assert (run_dir / "transcript.txt").is_file()
    assert not [p for p in run_dir.iterdir() if p.name.endswith(".tmp")]
