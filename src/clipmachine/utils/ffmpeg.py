"""ffprobe wrappers. The input file is untrusted.

Every subprocess call: argument list (never a shell string), stdin closed, a timeout, captured
output. Inputs are absolute resolved paths with the `file:` prefix plus
`-protocol_whitelist file`, so a crafted container cannot make ffmpeg open URLs or other files.
"""

import contextlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Iterable, Iterator, Sequence
from fractions import Fraction
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from clipmachine.models import AudioStreamInfo, FetchConfig, MediaInfo, VideoStreamInfo
from clipmachine.utils.crop import CropBox, display_size

if TYPE_CHECKING:  # numpy is imported lazily, inside read_frames
    import numpy as np
    from numpy.typing import NDArray

PROBE_TIMEOUT_SECONDS = 60
MAX_PROBE_OUTPUT_CHARS = 10_000_000
ALLOWED_CONTAINER_TOKENS = frozenset({"mov", "mp4", "matroska", "webm"})
NOT_READABLE = "not a readable media file"


class MediaError(Exception):
    """A problem with the media or the tools, with a one-line message fit to show the user."""


def input_url(path: Path) -> str:
    """Absolute resolved path with the `file:` protocol prefix, as ffmpeg/ffprobe should get it."""
    return f"file:{path.resolve()}"


def run_tool(
    args: list[str], timeout: float, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
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


def frame_rate(value: object) -> Fraction | None:
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
    avg, real = frame_rate(avg_text), frame_rate(r_text)
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


# --- audio extraction ---------------------------------------------------------------------

AUDIO_SAMPLE_RATE = 16000
AUDIO_CODEC = "pcm_s16le"
VERSION_TIMEOUT_SECONDS = 10
EXTRACT_BASE_TIMEOUT_SECONDS = 120
EXTRACT_TIMEOUT_PER_SOURCE_SECOND = 0.5
DURATION_TOLERANCE_SECONDS = 0.5
DURATION_TOLERANCE_FRACTION = 0.002  # 0.2 % of the source duration
MAX_ERROR_LINE_CHARS = 200


def extract_args(source: Path, temp_wav: Path) -> list[str]:
    """16 kHz mono 16-bit WAV of the first audio stream: the one meta.json records."""
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-i",
        input_url(source),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-c:a",
        AUDIO_CODEC,
        "-f",
        "wav",
        "-y",
        str(temp_wav.resolve()),
    ]


def extract_timeout(source_duration_seconds: float) -> float:
    return (
        EXTRACT_BASE_TIMEOUT_SECONDS + EXTRACT_TIMEOUT_PER_SOURCE_SECOND * source_duration_seconds
    )


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "no details from ffmpeg"
    cleaned = "".join(ch if ch.isprintable() else " " for ch in lines[-1])
    return cleaned[:MAX_ERROR_LINE_CHARS]


def extract_audio(source: Path, temp_wav: Path, source_duration_seconds: float) -> None:
    """Write temp_wav from source. The caller owns the temp file and the os.replace."""
    result = run_tool(extract_args(source, temp_wav), extract_timeout(source_duration_seconds))
    if result.returncode != 0:
        raise MediaError(f"audio extraction failed: {_last_line(result.stderr)}")


def duration_tolerance(source_duration_seconds: float) -> float:
    return max(DURATION_TOLERANCE_SECONDS, DURATION_TOLERANCE_FRACTION * source_duration_seconds)


def verify_audio(audio: MediaInfo, source_duration_seconds: float) -> None:
    """Check the extracted WAV is what transcription expects and as long as the source."""
    stream = audio.audio
    if stream is None or audio.video is not None:
        raise MediaError("extracted audio is not a plain audio file")
    if (stream.codec, stream.sample_rate, stream.channels) != (
        AUDIO_CODEC,
        AUDIO_SAMPLE_RATE,
        1,
    ):
        raise MediaError(
            f"extracted audio is {stream.codec} {stream.sample_rate} Hz {stream.channels} ch, "
            f"expected {AUDIO_CODEC} {AUDIO_SAMPLE_RATE} Hz 1 ch"
        )
    if audio.duration_seconds is None:
        raise MediaError("extracted audio has no duration")
    tolerance = duration_tolerance(source_duration_seconds)
    gap = abs(audio.duration_seconds - source_duration_seconds)
    if gap > tolerance:
        raise MediaError(
            f"extracted audio is {audio.duration_seconds:.1f} s but the source is "
            f"{source_duration_seconds:.1f} s (allowed difference {tolerance:.1f} s)"
        )


# --- clip rendering -----------------------------------------------------------------------

RENDER_BASE_TIMEOUT_SECONDS = 120
RENDER_TIMEOUT_PER_CLIP_SECOND = 20.0
CLIP_DURATION_TOLERANCE_SECONDS = 0.15
# What may appear in a path inside the -vf string: no drive colon, backslash, quote, comma or space.
FILTER_PATH_RE = re.compile(r"[A-Za-z0-9._/-]+")


def _filter_path(path: str, what: str) -> str:
    """A relative, forward-slash path that is safe inside a filter string, or MediaError."""
    if not FILTER_PATH_RE.fullmatch(path) or path.startswith("/"):
        raise MediaError(
            f"{what} cannot be used inside an ffmpeg filter: {path!r} (needs a relative path of "
            "letters, digits and . _ - / only)"
        )
    return path


def crop_commands(path: Sequence[tuple[float, int]]) -> str:
    """The text of a `sendcmd` file that moves the crop: one `<time> crop x <x>;` line for every
    change point after the first (the first x goes into the crop filter itself). Times are
    seconds from the seek point. Empty when the crop never moves."""
    return "".join(f"{t:.6f} crop x {x};\n" for t, x in path[1:])


def crop_box_commands(path: Sequence[tuple[float, CropBox]]) -> str:
    """The text of a `sendcmd` file that moves and resizes the crop (punch-in zooms): one line
    for every change point after the first, like `<time> crop w 964, crop h 1714, crop x 120;`
    with only the fields that changed. Times are seconds from the seek point. Empty when the
    box never changes.

    The commands of a line run in order and the crop filter checks the window against the frame
    after each one, so the order keeps every in-between state inside the frame: size first
    (w, h, x, y) when the window gets smaller, position first (x, y, w, h) otherwise. A change
    of x alone is exactly the line `crop_commands` writes."""
    lines: list[str] = []
    for (_, before), (t, after) in zip(path, path[1:], strict=False):
        shrinks = after.w < before.w or after.h < before.h
        fields = ("w", "h", "x", "y") if shrinks else ("x", "y", "w", "h")
        changed = [
            f"crop {name} {getattr(after, name)}"
            for name in fields
            if getattr(after, name) != getattr(before, name)
        ]
        if changed:
            lines.append(f"{t:.6f} " + ", ".join(changed) + ";\n")
    return "".join(lines)


def video_filter(
    crop: CropBox,
    width: int,
    height: int,
    subtitles_rel: str,
    fontsdir_rel: str,
    sendcmd_rel: str | None = None,
    extra: str | None = None,
) -> str:
    """[sendcmd ->] crop -> scale -> setsar -> subtitles [-> extra], all numbers plain integers.

    The paths must be relative to the directory ffmpeg runs in, so no drive letter ever reaches
    the filter parser. `sendcmd_rel` is a command file made from crop_commands. `extra` is a
    filter chain appended last (the safe-zone preview's lines); the render stage never uses it.
    """
    ass = _filter_path(subtitles_rel, "subtitle file path")
    fonts = _filter_path(fontsdir_rel, "font folder path")
    chain = (
        f"crop={crop.w}:{crop.h}:{crop.x}:{crop.y},scale={width}:{height},setsar=1,"
        f"subtitles=filename={ass}:fontsdir={fonts}"
    )
    if extra is not None:
        chain = f"{chain},{extra}"
    if sendcmd_rel is None:
        return chain
    return f"sendcmd=f={_filter_path(sendcmd_rel, 'crop command file path')},{chain}"


# TikTok's button column, in pixels on a 1080x1920 canvas: about 300 px wide, from y 840 down
TIKTOK_COLUMN_LEFT = 780
TIKTOK_COLUMN_TOP = 840
SAFE_LINE_PX = 2


def safe_zone_lines(
    width: int,
    height: int,
    top: int,
    bottom: int,
    left: int,
    right: int,
    *,
    column_left: int = TIKTOK_COLUMN_LEFT,
    column_top: int = TIKTOK_COLUMN_TOP,
) -> str:
    """A `drawbox` chain for the safe-zone preview: four translucent red lines just inside the
    margins and the outline of TikTok's button column in yellow (`column_left`, `column_top`
    on a 1080x1920 canvas, from safe.button_left/button_top). Plain integers only."""
    line = SAFE_LINE_PX
    column_x = round(column_left * width / 1080)
    column_y = round(column_top * height / 1920)
    boxes = [
        (0, top, width, line, "red@0.6", "fill"),
        (0, height - bottom - line, width, line, "red@0.6", "fill"),
        (left, 0, line, height, "red@0.6", "fill"),
        (width - right - line, 0, line, height, "red@0.6", "fill"),
        (column_x, column_y, width - column_x, height - column_y, "yellow@0.5", str(line)),
    ]
    return ",".join(
        f"drawbox=x={x}:y={y}:w={w}:h={h}:color={c}:t={t}" for x, y, w, h, c, t in boxes
    )


def overlay_graph(
    chain: str,
    *,
    x: int,
    y: int,
    size: tuple[int, int] | None,
    blend: str,
    frame: tuple[int, int],
) -> str:
    """The `-filter_complex` text that puts the watermark image on the video.

    `chain` is what video_filter() returns: crop, scale, subtitles. Draw order: video, then the
    subtitles (the captions, and the hook on higher ASS layers), then the watermark, so the
    watermark is on top of both. The image stays for the whole clip: there is no `enable=`, and
    `eof_action=repeat` holds its one frame. `size` is the scaled image size, or None to use the
    image as it is (no scale filter). `x` and `y` are where its top-left corner goes and may be
    negative. `frame` is the output size.

    blend "normal": a plain overlay, the PNG's alpha honoured. blend "screen": the image is first
    drawn on a black picture of the frame size, then screen-blended onto the video, so black adds
    nothing (an opaque black background disappears) and a PNG's alpha is honoured as well.
    """
    if blend not in ("normal", "screen"):
        raise MediaError(f"unknown watermark blend {blend!r}; use normal or screen")
    if size is None:
        wm, scaled = "[1:v:0]", ""
    else:
        wm, scaled = "[wm]", f"[1:v:0]scale={size[0]}:{size[1]}[wm];"
    place = f"overlay=x={x}:y={y}:eof_action=repeat"
    if blend == "normal":
        return f"[0:v:0]{chain}[base];{scaled}[base]{wm}{place}[v]"
    return (
        f"[0:v:0]{chain},format=gbrp[base];"
        f"color=black:s={frame[0]}x{frame[1]}[blk];{scaled}"
        f"[blk]{wm}{place},format=gbrp[mark];"
        "[base][mark]blend=all_mode=screen:shortest=1[v]"
    )


def render_args(
    source: Path,
    temp_out: Path,
    *,
    start: float,
    duration: float,
    vf: str | None = None,
    crf: int,
    preset: str,
    audio_bitrate: str,
    log_level: str = "error",
    audio_fade: float = 0.0,
    filter_complex: str | None = None,
    overlay_image: Path | None = None,
) -> list[str]:
    """One re-encoding pass: seek before the input (accurate when re-encoding), first video and
    first audio stream only, H.264 + AAC in MP4. Never a stream copy.

    `audio_fade` (seconds) adds a fade-in at the clip start and a fade-out at its end so a tight
    cut never clicks. Audio timestamps start at 0 after the input seek, so the fade times are
    seconds from the clip start. With 0, or a clip shorter than two fades, there is no `-af`.

    Exactly one of `vf` and `filter_complex` is given. `filter_complex` (see overlay_graph) goes
    with `overlay_image`, a PNG that becomes the second input; the output is its `[v]` pad and
    the source's own audio, `0:a:0`."""
    if (vf is None) == (filter_complex is None):
        raise MediaError("render_args needs exactly one of vf and filter_complex")
    if (overlay_image is None) != (filter_complex is None):
        raise MediaError("an overlay image goes with filter_complex, and only with it")
    fade = (
        [
            "-af",
            f"afade=t=in:st=0:d={audio_fade:.3f},"
            f"afade=t=out:st={duration - audio_fade:.3f}:d={audio_fade:.3f}",
        ]
        if audio_fade > 0 and duration > 2 * audio_fade
        else []
    )
    image_input = (
        []
        if overlay_image is None
        else ["-protocol_whitelist", "file", "-i", input_url(overlay_image)]
    )
    graph = ["-vf", vf] if filter_complex is None else ["-filter_complex", filter_complex]
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v",
        log_level,
        "-protocol_whitelist",
        "file",
        "-ss",
        f"{start:.3f}",
        "-i",
        input_url(source),
        *image_input,
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:v:0" if filter_complex is None else "[v]",
        "-map",
        "0:a:0",
        *graph,
        *fade,
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        "-y",
        str(temp_out.resolve()),
    ]


# an audio source or mixer as a filter name (at the start of a filter, never inside a file path),
# or an audio pad of an input
_AUDIO_FILTER_RE = re.compile(
    r"(?:^|[;,\]])\s*(?:amix|amerge|amovie|anullsrc|aevalsrc|sine)(?![A-Za-z0-9_])|:a\]|:a:"
)
_AUDIO_MAP_RE = re.compile(r"-?\d+:a(?::|$)|\[(?!v\])")


def _values(args: Sequence[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]


def assert_source_audio_only(args: Sequence[str]) -> None:
    """Campaign clips carry the source's own audio and nothing else: exactly one mapped audio
    stream, `0:a:0`; at most two inputs, the second a PNG; only afade filters on the audio; and
    no audio source or mixer inside the filter graph. MediaError otherwise."""
    inputs = _values(args, "-i")
    if len(inputs) > 2:
        raise MediaError("a campaign render has more than two inputs")
    if len(inputs) == 2 and not inputs[1].lower().endswith(".png"):
        raise MediaError("the second input of a campaign render must be a PNG image")
    audio_maps = [m for m in _values(args, "-map") if _AUDIO_MAP_RE.match(m)]
    if audio_maps != ["0:a:0"]:
        raise MediaError("a campaign render must map exactly the source's audio, 0:a:0")
    for value in _values(args, "-af"):
        if any(not part.startswith("afade=") for part in value.split(",")):
            raise MediaError("the audio of a campaign render may only be faded")
    for graph in _values(args, "-filter_complex"):
        if _AUDIO_FILTER_RE.search(graph):
            raise MediaError("the filter graph of a campaign render must not touch audio")


def render_timeout(duration_seconds: float) -> float:
    return RENDER_BASE_TIMEOUT_SECONDS + RENDER_TIMEOUT_PER_CLIP_SECOND * duration_seconds


def render_clip(args: list[str], cwd: Path, duration_seconds: float) -> None:
    """Run a render command with `cwd` as working directory. The caller owns the temp file."""
    result = run_tool(args, render_timeout(duration_seconds), cwd=cwd)
    if result.returncode != 0:
        raise MediaError(f"ffmpeg render failed: {_last_line(result.stderr)}")


def verify_clip(info: MediaInfo, *, width: int, height: int, duration: float) -> None:
    """An output clip must be MP4 with H.264 video of the exact size, AAC audio and the expected
    length (within CLIP_DURATION_TOLERANCE_SECONDS)."""
    if "mp4" not in info.format_name.split(","):
        raise MediaError(f"output is {info.format_name!r}, expected mp4")
    video, audio = info.video, info.audio
    if video is None or video.codec != "h264":
        raise MediaError(f"output video is {video.codec if video else 'missing'}, expected h264")
    if (video.width, video.height) != (width, height):
        raise MediaError(f"output is {video.width}x{video.height}, expected {width}x{height}")
    if audio is None or audio.codec != "aac":
        raise MediaError(f"output audio is {audio.codec if audio else 'missing'}, expected aac")
    if info.duration_seconds is None:
        raise MediaError("output has no duration")
    gap = abs(info.duration_seconds - duration)
    if gap > CLIP_DURATION_TOLERANCE_SECONDS:
        raise MediaError(
            f"output is {info.duration_seconds:.2f} s, expected {duration:.2f} s "
            f"(allowed difference {CLIP_DURATION_TOLERANCE_SECONDS} s)"
        )


def ffmpeg_version() -> str:
    """First line of `ffmpeg -version`."""
    result = run_tool(["ffmpeg", "-version"], VERSION_TIMEOUT_SECONDS)
    lines = result.stdout.splitlines()
    if result.returncode != 0 or not lines or not lines[0].strip():
        raise MediaError("could not read the ffmpeg version")
    return lines[0].strip()


# --- frames for analysis and the debug preview ---------------------------------------------

FRAMES_BASE_TIMEOUT_SECONDS = 60
FRAMES_TIMEOUT_PER_CLIP_SECOND = 10.0
RGB_CHANNELS = 3
KILL_WAIT_SECONDS = 5


def frame_args(source: Path, *, start: float, duration: float, fps: float, width: int) -> list[str]:
    """Decode only [start, start + duration) of the first video stream to raw RGB on stdout.

    Frames are resampled to `fps` and scaled to `width` (height from the aspect ratio, made even).
    ffmpeg's default auto-rotation applies, so frames are upright like the render stage's crop.
    Seeking before the input is accurate when decoding, as in render_args.
    """
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-ss",
        f"{start:.3f}",
        "-i",
        input_url(source),
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        f"fps={fps:g},scale={width}:-2",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "-",
    ]


def frame_size_after_scale(
    width: int, height: int, rotation: int, target_width: int
) -> tuple[int, int]:
    """The (width, height) of the frames frame_args produces for a stored source size: the upright
    size scaled to target_width, with the height rounded to the nearest even number like
    ffmpeg's `scale=W:-2`."""
    frame_w, frame_h = display_size(width, height, rotation)
    scaled = target_width * frame_h / frame_w
    return target_width, max(2, int(round(scaled / 2)) * 2)


def frames_timeout(duration_seconds: float) -> float:
    return FRAMES_BASE_TIMEOUT_SECONDS + FRAMES_TIMEOUT_PER_CLIP_SECOND * duration_seconds


def _stderr_sink(scratch_dir: Path | None) -> IO[bytes] | int:
    """Where a streaming ffmpeg's stderr goes: an unnamed temp file inside scratch_dir (never a
    pipe that could fill and deadlock the stream), or nowhere."""
    if scratch_dir is None:
        return subprocess.DEVNULL
    return tempfile.TemporaryFile(dir=scratch_dir)


def _stderr_text(sink: IO[bytes] | int) -> str:
    if isinstance(sink, int):
        return ""
    sink.seek(0)
    return sink.read(MAX_PROBE_OUTPUT_CHARS).decode("utf-8", errors="replace")


def _stop(proc: "subprocess.Popen[bytes]") -> None:
    if proc.poll() is None:
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):  # a killed process does not linger
        proc.wait(timeout=KILL_WAIT_SECONDS)


def read_frames(
    args: list[str],
    frame_w: int,
    frame_h: int,
    timeout: float,
    *,
    scratch_dir: Path | None = None,
) -> Iterator["NDArray[np.uint8]"]:
    """Run a command that writes raw rgb24 frames of frame_w x frame_h to stdout and yield each as
    a writable (frame_h, frame_w, 3) uint8 array.

    The command is an argument list, never a shell string. ffmpeg's stderr goes to a temp file in
    scratch_dir (or nowhere), so it cannot block the stream. A non-zero exit raises MediaError
    with ffmpeg's last stderr line; so does passing `timeout` seconds. The process is always
    killed when the iterator ends, even if the caller stops early.
    """
    import numpy as np

    size = frame_w * frame_h * RGB_CHANNELS
    sink = _stderr_sink(scratch_dir)
    try:
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=sink
            )
        except OSError as exc:
            raise MediaError(f"could not run {args[0]}: {exc.strerror or exc}") from exc
        deadline = time.monotonic() + timeout
        try:
            stdout = proc.stdout
            if stdout is None:  # pragma: no cover - PIPE always gives one
                raise MediaError("could not read frames from ffmpeg")
            while True:
                chunk = stdout.read(size)
                if len(chunk) < size:  # end of stream (a partial frame is dropped)
                    break
                if time.monotonic() > deadline:
                    raise MediaError(f"{args[0]} timed out after {timeout:g} s")
                yield np.frombuffer(chunk, dtype=np.uint8).reshape(frame_h, frame_w, 3).copy()
            try:
                code = proc.wait(timeout=KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                raise MediaError(f"{args[0]} did not finish after its last frame") from exc
            if code != 0:
                raise MediaError(f"frame extraction failed: {_last_line(_stderr_text(sink))}")
        finally:
            _stop(proc)
            if proc.stdout is not None:
                proc.stdout.close()
    finally:
        if not isinstance(sink, int):
            sink.close()


SHOWINFO_TIME_RE = re.compile(r"Parsed_showinfo.*?pts_time:\s*(-?[0-9.]+)")


def timed_frame_args(source: Path, *, start: float, duration: float, width: int) -> list[str]:
    """Decode [start, start + duration) of the first video stream at the source's own frame rate,
    to raw RGB on stdout, with one `showinfo` line per frame on stderr.

    No `fps` filter and `-fps_mode passthrough`, so ffmpeg neither drops nor repeats a frame and
    the frames line up one to one with the logged timestamps. showinfo comes first in the chain,
    so it logs the timestamps of the decoded frames, and it logs at info level, hence `-v info`.
    The timestamps are seconds from the `-ss` point.
    """
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-v",
        "info",
        "-protocol_whitelist",
        "file",
        "-ss",
        f"{start:.3f}",
        "-i",
        input_url(source),
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        f"showinfo,scale={width}:-2",
        "-fps_mode",
        "passthrough",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "-",
    ]


def read_timed_frames(
    args: list[str], frame_w: int, frame_h: int, timeout: float
) -> list[tuple[float, "NDArray[np.uint8]"]]:
    """Run a timed_frame_args command and return (timestamp, frame) pairs: the timestamp in
    seconds from the `-ss` point and a writable (frame_h, frame_w, 3) uint8 array.

    This is a fraction of a second of video, so the output is read whole. showinfo can log one
    frame more than is written (the one past `-t`): the first len(frames) timestamps are used.
    Fewer timestamps than frames, a non-zero exit or `timeout` seconds raise MediaError.
    """
    import numpy as np

    try:
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{args[0]} timed out after {timeout:g} s") from exc
    except OSError as exc:
        raise MediaError(f"could not run {args[0]}: {exc.strerror or exc}") from exc
    log_text = result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0:
        raise MediaError(f"frame extraction failed: {_last_line(log_text)}")
    size = frame_w * frame_h * RGB_CHANNELS
    count = len(result.stdout) // size  # a partial frame is dropped
    times = [float(m) for m in SHOWINFO_TIME_RE.findall(log_text)]
    if len(times) < count:
        raise MediaError(f"ffmpeg logged {len(times)} frame times for {count} frames")
    return [
        (
            times[i],
            np.frombuffer(result.stdout, dtype=np.uint8, count=size, offset=i * size)
            .reshape(frame_h, frame_w, 3)
            .copy(),
        )
        for i in range(count)
    ]


def preview_args(
    source: Path,
    temp_out: Path,
    *,
    start: float,
    duration: float,
    fps: float,
    width: int,
    height: int,
) -> list[str]:
    """Encode raw rgb24 frames from stdin (width x height at fps) into a small H.264 MP4, with the
    clip's own audio taken from the source (seek and length before that input). The output ends
    with the shorter of the two. No -nostdin: stdin carries the frames."""
    return [
        "ffmpeg",
        "-hide_banner",
        "-v",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        f"{fps:g}",
        "-i",
        "pipe:0",
        "-protocol_whitelist",
        "file",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-vn",
        "-i",
        input_url(source),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "30",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-shortest",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        "-y",
        str(temp_out.resolve()),
    ]


def write_preview(
    frames: Iterable["NDArray[np.uint8]"],
    args: list[str],
    temp_out: Path,
    out: Path,
    timeout: float,
    *,
    scratch_dir: Path | None = None,
) -> None:
    """Pipe frames into the command built by preview_args, then move temp_out to out in one step.
    The temp file is removed on any failure."""
    sink = _stderr_sink(scratch_dir)
    try:
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=sink
            )
        except OSError as exc:
            raise MediaError(f"could not run {args[0]}: {exc.strerror or exc}") from exc
        try:
            deadline = time.monotonic() + timeout
            stdin = proc.stdin
            if stdin is None:  # pragma: no cover - PIPE always gives one
                raise MediaError("could not write frames to ffmpeg")
            try:
                for frame in frames:
                    stdin.write(frame.tobytes())
                    if time.monotonic() > deadline:
                        raise MediaError(f"{args[0]} timed out after {timeout:g} s")
                stdin.close()
            except OSError:  # BrokenPipeError: ffmpeg stopped reading
                _stop(proc)
                raise MediaError(
                    f"preview encode failed: {_last_line(_stderr_text(sink))}"
                ) from None
            try:
                code = proc.wait(timeout=max(deadline - time.monotonic(), KILL_WAIT_SECONDS))
            except subprocess.TimeoutExpired as exc:
                raise MediaError(f"{args[0]} timed out after {timeout:g} s") from exc
            if code != 0:
                raise MediaError(f"preview encode failed: {_last_line(_stderr_text(sink))}")
            os.replace(temp_out, out)
        finally:
            _stop(proc)
            temp_out.unlink(missing_ok=True)
    finally:
        if not isinstance(sink, int):
            sink.close()
