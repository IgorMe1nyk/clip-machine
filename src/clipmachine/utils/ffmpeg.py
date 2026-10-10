"""ffprobe wrappers. The input file is untrusted.

Every subprocess call: argument list (never a shell string), stdin closed, a timeout, captured
output. Inputs are absolute resolved paths with the `file:` prefix plus
`-protocol_whitelist file`, so a crafted container cannot make ffmpeg open URLs or other files.
"""

import json
import math
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from clipmachine.models import AudioStreamInfo, FetchConfig, MediaInfo, VideoStreamInfo

PROBE_TIMEOUT_SECONDS = 60
MAX_PROBE_OUTPUT_CHARS = 10_000_000
ALLOWED_CONTAINER_TOKENS = frozenset({"mov", "mp4", "matroska", "webm"})
NOT_READABLE = "not a readable media file"


class MediaError(Exception):
    """A problem with the media or the tools, with a one-line message fit to show the user."""


def input_url(path: Path) -> str:
    """Absolute resolved path with the `file:` protocol prefix, as ffmpeg/ffprobe should get it."""
    return f"file:{path.resolve()}"


def run_tool(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{args[0]} timed out after {timeout:g} s") from exc
    except OSError as exc:
        raise MediaError(f"could not run {args[0]}: {exc.strerror or exc}") from exc


def probe_args(path: Path) -> list[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-protocol_whitelist",
        "file",
        "-i",
        input_url(path),
    ]


def _number(value: object) -> float | None:
    """A finite number from an ffprobe value (they arrive as strings or numbers), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _positive_int(value: object) -> int | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    return int(number)


def _rate(value: object) -> Fraction | None:
    """A frame rate such as "30000/1001" as a fraction; None for "0/0", missing or garbage."""
    if not isinstance(value, str):
        return None
    try:
        rate = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def _rotation(stream: dict[str, Any]) -> int:
    """Clockwise degrees, 0-359.

    The Display Matrix side data is ffprobe's counter-clockwise angle, so it is negated.
    The `rotate` tag is already clockwise.
    """
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if isinstance(item, dict):
                angle = _number(item.get("rotation"))
                if angle is not None:
                    return int(round(-angle)) % 360
    tags = stream.get("tags")
    if isinstance(tags, dict):
        angle = _number(tags.get("rotate"))
        if angle is not None:
            return int(round(angle)) % 360
    return 0


def _is_cover_art(stream: dict[str, Any]) -> bool:
    disposition = stream.get("disposition")
    return isinstance(disposition, dict) and disposition.get("attached_pic") == 1


def _video_stream(stream: dict[str, Any]) -> VideoStreamInfo | None:
    width = _positive_int(stream.get("width"))
    height = _positive_int(stream.get("height"))
    if width is None or height is None:
        return None
    avg_text = str(stream.get("avg_frame_rate", "0/0"))
    r_text = str(stream.get("r_frame_rate", "0/0"))
    avg, real = _rate(avg_text), _rate(r_text)
    return VideoStreamInfo(
        codec=str(stream.get("codec_name", "unknown")),
        width=width,
        height=height,
        avg_frame_rate=avg_text,
        r_frame_rate=r_text,
        rotation=_rotation(stream),
        variable_frame_rate=avg is not None and real is not None and avg != real,
    )


def _audio_stream(stream: dict[str, Any]) -> AudioStreamInfo | None:
    sample_rate = _positive_int(stream.get("sample_rate"))
    channels = _positive_int(stream.get("channels"))
    if sample_rate is None or channels is None:
        return None
    return AudioStreamInfo(
        codec=str(stream.get("codec_name", "unknown")),
        sample_rate=sample_rate,
        channels=channels,
    )


def parse_probe(data: object, fallback_size: int) -> MediaInfo:
    """Turn ffprobe's JSON into MediaInfo. Anything that is not an ffprobe report is rejected."""
    if not isinstance(data, dict) or not isinstance(data.get("format"), dict):
        raise MediaError(NOT_READABLE)
    fmt: dict[str, Any] = data["format"]
    streams = data.get("streams")
    stream_list = [s for s in streams if isinstance(s, dict)] if isinstance(streams, list) else []

    video: VideoStreamInfo | None = None
    audio: AudioStreamInfo | None = None
    for stream in stream_list:
        kind = stream.get("codec_type")
        if kind == "video" and video is None and not _is_cover_art(stream):
            video = _video_stream(stream)
        elif kind == "audio" and audio is None:
            audio = _audio_stream(stream)

    duration = _number(fmt.get("duration"))
    size = _positive_int(fmt.get("size"))
    return MediaInfo(
        format_name=str(fmt.get("format_name", "")),
        duration_seconds=duration if duration is not None and duration >= 0 else None,
        size_bytes=size if size is not None else fallback_size,
        video=video,
        audio=audio,
    )


def probe(path: Path) -> MediaInfo:
    """Ask ffprobe what a file is. Raises MediaError("not a readable media file") if it fails."""
    result = run_tool(probe_args(path), PROBE_TIMEOUT_SECONDS)
    if result.returncode != 0 or len(result.stdout) > MAX_PROBE_OUTPUT_CHARS:
        raise MediaError(NOT_READABLE)
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise MediaError(NOT_READABLE) from exc
    return parse_probe(data, fallback_size=path.stat().st_size)


def check_source(info: MediaInfo, limits: FetchConfig) -> float:
    """Reject media we cannot make clips from, with one clear line. Returns the duration."""
    tokens = set(info.format_name.split(","))
    if not tokens & ALLOWED_CONTAINER_TOKENS:
        raise MediaError(
            f"unsupported container {info.format_name!r} (need mp4, mov, matroska or webm)"
        )
    if info.video is None:
        raise MediaError("no video stream (video needed for clips)")
    if info.audio is None:
        raise MediaError("no audio stream (audio needed for transcription)")
    duration = info.duration_seconds
    if duration is None:
        raise MediaError("video duration is missing")
    if duration < limits.min_duration_seconds:
        raise MediaError(
            f"video is too short: {duration:.1f} s, minimum is "
            f"{limits.min_duration_seconds:g} s (fetch.min_duration_seconds)"
        )
    if duration > limits.max_duration_minutes * 60:
        raise MediaError(
            f"video is too long: {duration / 60:.1f} min, maximum is "
            f"{limits.max_duration_minutes:g} min (fetch.max_duration_minutes)"
        )
    return duration
