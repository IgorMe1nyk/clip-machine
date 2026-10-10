import shutil
from pathlib import Path

import pytest

from clipmachine import pipeline
from clipmachine.context import RunContext

REPO_ROOT = Path(__file__).resolve().parents[1]
FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tests marked `ffmpeg` are skipped when ffmpeg or ffprobe is not on PATH."""
    if FFMPEG_AVAILABLE:
        return
    skip = pytest.mark.skip(reason="ffmpeg/ffprobe not on PATH")
    for item in items:
        if "ffmpeg" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_ffmpeg_needed(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests never need ffmpeg: the tool check is stubbed and the fetch stage is a no-op.

    Tests marked `ffmpeg` get the real check and the real fetch stage. Tests of the stage itself
    call `fetch.run` directly, so they are unaffected.
    """
    if request.node.get_closest_marker("ffmpeg") is not None:
        return

    def silent_fetch(ctx: RunContext) -> None:
        return None

    monkeypatch.setattr("clipmachine.pipeline.check_ffmpeg", lambda: None)
    monkeypatch.setitem(pipeline.STAGES, "fetch", silent_fetch)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway project root holding copies of the real config and example client."""
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    shutil.copytree(REPO_ROOT / "clients", tmp_path / "clients")
    (tmp_path / "runs").mkdir()
    return tmp_path
