"""Stage 1: check the input is real media within limits, copy it in, extract 16 kHz mono audio.

Outputs, all inside the run folder: source.<ext>, audio.wav, meta.json (written last, so its
presence means the stage finished). The original file is only ever opened for reading.
"""

import hashlib
import logging
import os
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from clipmachine.context import RunContext
from clipmachine.models import FetchConfig, FetchMeta
from clipmachine.runs import (
    ALLOWED_EXTENSIONS,
    GIB,
    RunError,
    check_input_size,
    validate_input,
    write_json_atomic,
)
from clipmachine.utils import ffmpeg

log = logging.getLogger("clipmachine")

AUDIO_FILE = "audio.wav"
META_FILE = "meta.json"
COPY_CHUNK_BYTES = 1024 * 1024
DISK_RESERVE_BYTES = GIB

# The only names this stage ever creates or deletes inside the run folder.
OUTPUT_NAMES = (*(f"source{ext}" for ext in sorted(ALLOWED_EXTENSIONS)), AUDIO_FILE, META_FILE)
TEMP_PATTERNS = (".source.*.tmp", f".{AUDIO_FILE}.*.tmp", f".{META_FILE}.*.tmp")


def _remove_outputs(run_dir: Path, *, strict: bool) -> None:
    """Delete this stage's outputs and temp files. Nothing else in the folder is touched."""
    doomed = [run_dir / name for name in OUTPUT_NAMES]
    for pattern in TEMP_PATTERNS:
        doomed.extend(run_dir.glob(pattern))
    for path in doomed:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            if strict:
                raise


def check_disk_space(run_dir: Path, size_bytes: int) -> None:
    needed = 2 * size_bytes + DISK_RESERVE_BYTES
    free = shutil.disk_usage(run_dir).free
    if free < needed:
        raise RunError(
            f"not enough free disk space: need {needed / GIB:.2f} GB "
            f"(2 x {size_bytes / GIB:.2f} GB file + 1 GB), only {free / GIB:.2f} GB free"
        )


def copy_with_hash(
    src: Path, dst_dir: Path, final_name: str, expected_size: int
) -> tuple[str, int]:
    """Copy src to dst_dir/final_name via a temp file; hash in the same single pass.

    Returns (sha256 hex, bytes copied). The original is opened read-only.
    """
    digest = hashlib.sha256()
    copied = 0
    fd, tmp_name = tempfile.mkstemp(dir=dst_dir, prefix=".source.", suffix=".tmp")
    try:
        with src.open("rb") as reader, os.fdopen(fd, "wb") as writer:
            while chunk := reader.read(COPY_CHUNK_BYTES):
                digest.update(chunk)
                writer.write(chunk)
                copied += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if copied != expected_size:
            raise RunError("input file changed while it was being copied")
        os.replace(tmp_name, dst_dir / final_name)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return digest.hexdigest(), copied


def run(ctx: RunContext) -> None:
    started = time.perf_counter()
    run_dir = ctx.run_dir.resolve()
    original = ctx.input_path
    limits = ctx.config.app.fetch

    _remove_outputs(run_dir, strict=True)  # a rerun replaces outputs cleanly
    try:
        _fetch(run_dir, original, limits, started)
    except BaseException:
        _remove_outputs(run_dir, strict=False)
        raise


def _fetch(run_dir: Path, original: Path, limits: FetchConfig, started: float) -> None:
    validate_input(original)
    size = check_input_size(original, limits.max_size_gb)
    mtime_ns = original.stat().st_mtime_ns  # before the copy, to recognise the same source later

    media = ffmpeg.probe(original)
    duration = ffmpeg.check_source(media, limits)
    if media.video is not None and media.video.variable_frame_rate:
        log.info(
            f"note: variable frame rate ({media.video.avg_frame_rate} average vs "
            f"{media.video.r_frame_rate} base); not changed"
        )

    check_disk_space(run_dir, size)

    source_name = f"source{original.suffix.lower()}"
    sha256, copied = copy_with_hash(original, run_dir, source_name, size)

    fd, tmp_name = tempfile.mkstemp(dir=run_dir, prefix=f".{AUDIO_FILE}.", suffix=".tmp")
    os.close(fd)
    temp_wav = Path(tmp_name)
    ffmpeg.extract_audio(run_dir / source_name, temp_wav, duration)
    ffmpeg.verify_audio(ffmpeg.probe(temp_wav), duration)
    os.replace(temp_wav, run_dir / AUDIO_FILE)

    meta = FetchMeta(
        original_filename=original.name,
        source_file=source_name,
        sha256=sha256,
        size_bytes=copied,
        media=media,
        audio_file=AUDIO_FILE,
        ffmpeg_version=ffmpeg.ffmpeg_version(),
        fetched_at=datetime.now(UTC).isoformat(),
        fetch_seconds=round(time.perf_counter() - started, 3),
        source_mtime_ns=mtime_ns,
    )
    write_json_atomic(run_dir / META_FILE, meta.model_dump(mode="json"))
    log.info(f"fetched {original.name}: {duration:.1f} s, {AUDIO_FILE} ready")
