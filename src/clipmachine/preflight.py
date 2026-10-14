"""Startup checks for external tools."""

import shutil
from pathlib import Path

from clipmachine.utils import faces

REQUIRED_TOOLS = ("ffmpeg", "ffprobe")


class MissingToolError(Exception):
    """A required external tool is not on PATH."""


def check_ffmpeg() -> None:
    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        raise MissingToolError(
            f"{' and '.join(missing)} not found on PATH. On Windows: run "
            "'winget install Gyan.FFmpeg.Essentials', open a new terminal, then check "
            "'ffmpeg -version'."
        )


def check_face_models(root: Path) -> None:
    """Speaker crop needs both face model files. Raises faces.FacesError (the text names the file
    and the fetch command) before any stage runs; nothing is downloaded here."""
    try:
        faces.ensure_model(faces.model_dir(root))
        faces.ensure_model(faces.model_dir(root, faces.DETECTOR), spec=faces.DETECTOR)
    except faces.FacesError as exc:
        raise faces.FacesError(f"{exc}. Or set video.crop: center in the config") from exc


def check_text_model(root: Path) -> None:
    """The graphics-aware layout (Brief 12.1) needs the text detector file. Raises
    faces.FacesError naming the file and the fetch command; nothing is downloaded here."""
    try:
        faces.ensure_model(faces.model_dir(root, faces.TEXT_DETECTOR), spec=faces.TEXT_DETECTOR)
    except faces.FacesError as exc:
        raise faces.FacesError(
            f"{exc}. Or set video.graphics.enabled: false in the config"
        ) from exc
