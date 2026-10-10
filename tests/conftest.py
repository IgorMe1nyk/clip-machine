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
    for item in items:
        if not FFMPEG_AVAILABLE and "ffmpeg" in item.keywords:
            item.add_marker(skip_ffmpeg)
        if os.environ.get(WHISPER_ENV) != "1" and "whisper" in item.keywords:
            item.add_marker(skip_whisper)


WHISPER_ENV = "CLIPMACHINE_WHISPER_TESTS"


@pytest.fixture(autouse=True)
def _no_tools_needed(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests never need ffmpeg or a Whisper model: those stages are stubbed in the pipeline.

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


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway project root holding copies of the real config and example client."""
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    shutil.copytree(REPO_ROOT / "clients", tmp_path / "clients")
    (tmp_path / "runs").mkdir()
    return tmp_path
