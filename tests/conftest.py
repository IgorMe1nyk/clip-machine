import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway project root holding copies of the real config and example client."""
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    shutil.copytree(REPO_ROOT / "clients", tmp_path / "clients")
    (tmp_path / "runs").mkdir()
    return tmp_path
