"""Builders for the campaign tests: synthetic watermark PNGs and campaign folders.

All words and names here are invented.
"""

import struct
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml
from numpy.typing import NDArray

Bar = tuple[int, int, int, int]  # x1, y1, x2, y2 of the white bar, x2 and y2 exclusive
DEFAULT_BAR: Bar = (130, 38, 230, 58)


def write_png(path: Path, pixels: NDArray[Any]) -> None:
    """Encode with OpenCV (BGR/BGRA/grey, 8 or 16 bit) and write the bytes."""
    ok, buf = cv2.imencode(".png", pixels)
    assert ok
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.tobytes())


def transparent_mark(
    width: int = 360, height: int = 96, bar: Bar = DEFAULT_BAR
) -> NDArray[np.uint8]:
    """A white bar on a fully transparent canvas (BGRA)."""
    pixels = np.zeros((height, width, 4), np.uint8)
    x1, y1, x2, y2 = bar
    pixels[y1:y2, x1:x2] = 255
    return pixels


def black_mark(width: int = 360, height: int = 96, bar: Bar = DEFAULT_BAR) -> NDArray[np.uint8]:
    """A white bar on an opaque black canvas (BGRA)."""
    pixels = np.zeros((height, width, 4), np.uint8)
    pixels[:, :, 3] = 255
    x1, y1, x2, y2 = bar
    pixels[y1:y2, x1:x2, :3] = 255
    return pixels


def png_bytes(
    width: int,
    height: int,
    color_type: int,
    bit_depth: int,
    rows: bytes,
    *,
    before_idat: tuple[tuple[bytes, bytes], ...] = (),
) -> bytes:
    """A PNG from the standard library; `rows` is the raw scanline data with filter bytes."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    parts = [b"\x89PNG\r\n\x1a\n", chunk(b"IHDR", header)]
    parts += [chunk(kind, body) for kind, body in before_idat]
    parts += [chunk(b"IDAT", zlib.compress(rows)), chunk(b"IEND", b"")]
    return b"".join(parts)


def base_campaign() -> dict[str, Any]:
    return {
        "name": "Demo Campaign",
        "source_note": "invented",
        "clip_length": {"min": 20, "max": 60},
        # the rig's watermark sits at the top of the safe area, so the hook must go below it
        "hook": {"mode": "full", "max_chars": 30, "position": "avoid_faces"},
        "watermark": {
            "file": "assets/mark.png",
            "position": {"x_center": 0.5, "y_center": 0.18},
            "scale": 1.0,
        },
        "captions": {"style": "box_karaoke"},
        "spelling": [{"from": "zorble", "to": "Zorb1e"}, {"from": "q x v", "to": "QXV"}],
        "names_to_check": ["Zorb1e", "QXV"],
        "post": {
            "prefix": {"tiktok": "@demo"},
            "closers": ["closer one", "closer two", "closer three"],
            "hashtags": [],
        },
    }


def write_campaign(
    project: Path,
    slug: str = "demo",
    *,
    mutate: Callable[[dict[str, Any]], None] | None = None,
    png: NDArray[Any] | None = None,
) -> Path:
    """Write campaigns/<slug>/ (campaign.yaml and assets/mark.png) and return the folder."""
    folder = project / "campaigns" / slug
    data = base_campaign()
    if mutate is not None:
        mutate(data)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "campaign.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    write_png(folder / "assets" / "mark.png", transparent_mark() if png is None else png)
    return folder


def moment(**fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "A01",
        "start": "10:00",
        "end": "10:40",
        "hook": "AN INVENTED HOOK",
        "caption": "says something invented.",
        "platform": "tiktok",
    }
    entry.update(fields)
    return entry


def write_moments(folder: Path, entries: list[dict[str, Any]]) -> None:
    (folder / "moments.yaml").write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")


# --- a run folder with a transcript of real sentences ---------------------------------------------
#
# Sentence k has 8 words, each 0.4 s long with 0.1 s between them, and a 1.0 s pause after it, so
# it starts at 4.9 * k and ends at 4.9 * k + 3.9. With the default pads (0.25 s before, 0.5 s
# after) its clean cut times are 4.9 * k - 0.25 (0.0 for the first) and 4.9 * k + 4.4.

SENTENCE_PERIOD = 4.9
SOURCE_SECONDS = 100.0


def sentence_start_cut(k: int) -> float:
    return 0.0 if k == 0 else round(SENTENCE_PERIOD * k - 0.25, 3)


def sentence_end_cut(k: int) -> float:
    return round(SENTENCE_PERIOD * k + 4.4, 3)


def write_run(
    project: Path,
    *,
    name: str = "run",
    source: Path | None = None,
    mtime: str = "source",  # "source": the source file's own; "none": not recorded; "other": wrong
    size: str = "source",  # "source" or "other"
    transcribe_done: bool = True,
    created_at: str = "2026-10-12T00:00:00+00:00",
    overrides: dict[tuple[int, int], str] | None = None,
    sentences: int = 20,
    width: int = 640,
    height: int = 360,
) -> Path:
    """runs/<name>/ as it is after fetch and transcribe: run.json, config.resolved.yaml, meta.json,
    a stub source.mp4 and transcript.json. `overrides` replaces single words: (sentence, word)."""
    from clipmachine.config import dump_resolved, load_resolved
    from clipmachine.models import (
        AudioStreamInfo,
        FetchMeta,
        MediaInfo,
        RunState,
        StageState,
        TranscribeConfig,
        Transcript,
        TranscriptSegment,
        TranscriptWord,
        VideoStreamInfo,
    )
    from clipmachine.pipeline import RESOLVED_CONFIG, STAGE_NAMES, save_state
    from clipmachine.runs import write_json_atomic

    run_dir = project / "runs" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    dump_resolved(load_resolved(project, "general"), run_dir / RESOLVED_CONFIG)
    stages = {n: StageState() for n in STAGE_NAMES}
    stages["fetch"] = StageState(status="done")
    if transcribe_done:
        stages["transcribe"] = StageState(status="done")
    input_path = source.resolve() if source is not None else project / "input.mp4"
    save_state(
        run_dir,
        RunState(input=str(input_path), client="general", created_at=created_at, stages=stages),
    )
    real = source.stat() if source is not None else None
    size_bytes = 4 if real is None else real.st_size + (1 if size == "other" else 0)
    mtime_ns: int | None = None
    if real is not None and mtime == "source":
        mtime_ns = real.st_mtime_ns
    elif mtime == "other":
        mtime_ns = (0 if real is None else real.st_mtime_ns) + 1_000_000_000
    video = VideoStreamInfo(
        codec="h264",
        width=width,
        height=height,
        avg_frame_rate="30/1",
        r_frame_rate="30/1",
        rotation=0,
        variable_frame_rate=False,
    )
    meta = FetchMeta(
        original_filename="original.mp4",
        source_file="source.mp4",
        sha256="b" * 64,
        size_bytes=size_bytes,
        media=MediaInfo(
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            duration_seconds=SOURCE_SECONDS,
            size_bytes=size_bytes,
            video=video,
            audio=AudioStreamInfo(codec="aac", sample_rate=48000, channels=2),
        ),
        audio_file="audio.wav",
        ffmpeg_version="ffmpeg version test",
        fetched_at="2026-10-10T00:00:00+00:00",
        fetch_seconds=0.1,
        source_mtime_ns=mtime_ns,
    )
    write_json_atomic(run_dir / "meta.json", meta.model_dump(mode="json"))
    (run_dir / "source.mp4").write_bytes(b"fake")

    changed = overrides or {}
    segments = []
    for k in range(sentences):
        words = []
        for j in range(8):
            start = round(SENTENCE_PERIOD * k + 0.5 * j, 3)
            text = changed.get((k, j), f"s{k}w{j}") + ("." if j == 7 else "")
            words.append(
                TranscriptWord(
                    start=start, end=round(start + 0.4, 3), word=f" {text}", probability=0.9
                )
            )
        segments.append(
            TranscriptSegment(
                id=k,
                start=words[0].start,
                end=words[-1].end,
                text=" ".join(w.word.strip() for w in words),
                avg_logprob=0.0,
                no_speech_prob=0.0,
                words=words,
            )
        )
    transcript = Transcript(
        model="small",
        device="cpu",
        compute_type="int8",
        settings=TranscribeConfig(model="small"),
        language="en",
        language_probability=0.99,
        audio_duration_seconds=SOURCE_SECONDS,
        transcribe_seconds=1.0,
        realtime_factor=100.0,
        segments=segments,
        warnings=[],
    )
    write_json_atomic(run_dir / "transcript.json", transcript.model_dump(mode="json"))
    return run_dir
