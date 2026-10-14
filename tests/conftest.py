import dataclasses
import os
import shutil
from pathlib import Path

import pytest

from clipmachine import pipeline
from clipmachine.context import RunContext
from clipmachine.utils import faces

REPO_ROOT = Path(__file__).resolve().parents[1]
FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tests marked `ffmpeg` are skipped when ffmpeg or ffprobe is not on PATH.

    Tests marked `whisper` run a real model and need CLIPMACHINE_WHISPER_TESTS=1.
    """
    skip_ffmpeg = pytest.mark.skip(reason="ffmpeg/ffprobe not on PATH")
    skip_whisper = pytest.mark.skip(reason=f"set {WHISPER_ENV}=1 to run real Whisper tests")
    skip_mediapipe = pytest.mark.skip(reason=f"set {MEDIAPIPE_ENV}=1 to run real MediaPipe tests")
    for item in items:
        if os.environ.get(MEDIAPIPE_ENV) != "1" and "mediapipe" in item.keywords:
            item.add_marker(skip_mediapipe)
        if not FFMPEG_AVAILABLE and "ffmpeg" in item.keywords:
            item.add_marker(skip_ffmpeg)
        if os.environ.get(WHISPER_ENV) != "1" and "whisper" in item.keywords:
            item.add_marker(skip_whisper)


WHISPER_ENV = "CLIPMACHINE_WHISPER_TESTS"
MEDIAPIPE_ENV = "CLIPMACHINE_MEDIAPIPE_TESTS"


@pytest.fixture(autouse=True)
def _no_tools_needed(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests never need ffmpeg, a Whisper model or the API: those stages are stubbed in the
    pipeline.

    Tests marked `ffmpeg` get the real tool check and the real fetch stage; tests marked `whisper`
    also get the real transcribe stage. Tests of a stage itself call its `run` directly, so they
    are unaffected.
    """

    def silent(ctx: RunContext) -> None:
        return None

    if request.node.get_closest_marker("ffmpeg") is None:
        monkeypatch.setattr("clipmachine.pipeline.check_ffmpeg", lambda: None)
        monkeypatch.setitem(pipeline.STAGES, "fetch", silent)
    if request.node.get_closest_marker("whisper") is None:
        monkeypatch.setitem(pipeline.STAGES, "transcribe", silent)
    # candidates calls the API: pipeline tests never reach it. Tests of the stage itself call
    # candidates.run directly (or put it back) with a faked client.
    monkeypatch.setitem(pipeline.STAGES, "candidates", silent)
    monkeypatch.setitem(pipeline.STAGES, "score", silent)
    # render needs a real run folder (scored.json, source video): its own tests call render.run
    # with a faked ffmpeg, and the ffmpeg-marked test uses real tools.
    monkeypatch.setitem(pipeline.STAGES, "render", silent)
    # report needs scored.json, transcript.json and meta.json: its own tests call report.run
    # directly, or put it back with the other stages' outputs written by hand.
    monkeypatch.setitem(pipeline.STAGES, "report", silent)


@pytest.fixture(autouse=True)
def _no_pinned_model_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests write tiny fake model files; the real models' pinned SHA-256 must not apply to them.
    Tests of the hash check pass `expected_sha256` explicitly."""
    for name in ("LANDMARKER", "DETECTOR", "TEXT_DETECTOR"):
        spec = getattr(faces, name)
        monkeypatch.setattr(faces, name, dataclasses.replace(spec, sha256=None))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may build the real API client: tests that need one replace claude.new_client."""

    def refuse(api_key: str) -> object:
        raise AssertionError("a test tried to create the real API client; fake claude.new_client")

    monkeypatch.setattr("clipmachine.utils.claude.new_client", refuse)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway project root holding copies of the real config, clients and prompts."""
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    # The shipped default is crop: speaker, which needs model files. Tests that are not about
    # speaker mode run the centre crop; speaker tests switch it on in their own copy.
    default_yaml = tmp_path / "config" / "default.yaml"
    text = default_yaml.read_text(encoding="utf-8")
    assert "  crop: speaker " in text
    # Likewise zoom is shipped on; tests run with it off, and zoom tests switch it on.
    assert "    enabled: true            # false = the crop" in text
    text = text.replace("  crop: speaker ", "  crop: center ", 1)
    text = text.replace(
        "    enabled: true            # false = the crop",
        "    enabled: false           # false = the crop",
        1,
    )
    # And the graphics-aware layout (Brief 12.1), which needs the text detector file.
    assert "    enabled: true            # false = today's output" in text
    text = text.replace(
        "    enabled: true            # false = today's output",
        "    enabled: false           # false = today's output",
        1,
    )
    default_yaml.write_text(text, encoding="utf-8")
    shutil.copytree(REPO_ROOT / "clients", tmp_path / "clients")
    shutil.copytree(REPO_ROOT / "prompts", tmp_path / "prompts")
    shutil.copytree(REPO_ROOT / "assets", tmp_path / "assets")
    (tmp_path / "runs").mkdir()
    return tmp_path
