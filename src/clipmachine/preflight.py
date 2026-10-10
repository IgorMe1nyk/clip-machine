"""Startup checks for external tools."""

import shutil

REQUIRED_TOOLS = ("ffmpeg", "ffprobe")


class MissingToolError(Exception):
    """A required external tool is not on PATH."""


def check_ffmpeg() -> None:
    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        raise MissingToolError(
            f"{' and '.join(missing)} not found on PATH. On Windows: run "
            "'winget install Gyan.FFmpeg', open a new terminal, then check 'ffmpeg -version'."
        )
