import os
import shutil
from pathlib import Path

import pytest

from clipmachine import pipeline
from clipmachine.context import RunContext

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
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may build the real API client: tests that need one replace claude.new_client."""

    def refuse(api_key: str) -> object:
        raise AssertionError("a test tried to create the real API client; fake claude.new_client")

    monkeypatch.setattr("clipmachine.utils.claude.new_client", refuse)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway project root holding copies of the real config, clients and prompts."""
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    shutil.copytree(REPO_ROOT / "clients", tmp_path / "clients")
    shutil.copytree(REPO_ROOT / "prompts", tmp_path / "prompts")
    shutil.copytree(REPO_ROOT / "assets", tmp_path / "assets")
    (tmp_path / "runs").mkdir()
    return tmp_path
