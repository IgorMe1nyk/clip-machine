"""Stage 2: audio.wav -> transcript.json (word timestamps) and transcript.txt.

Outputs, all inside the run folder: transcript.txt, then transcript.json (written last, so its
presence means the stage finished). The transcript is untrusted data: it is stored and never
logged, and it must never be passed to a model as instructions.
"""

import logging
import math
import os
import re
import time
import wave
from dataclasses import replace
from pathlib import Path

from clipmachine.context import RunContext
from clipmachine.models import Transcript, TranscriptSegment, TranscriptWord
from clipmachine.runs import RunError, write_json_atomic, write_text_atomic
from clipmachine.stages.fetch import AUDIO_FILE
from clipmachine.utils import whisper
from clipmachine.utils.whisper import RawSegment, RawWord

log = logging.getLogger("clipmachine")

TRANSCRIPT_JSON = "transcript.json"
TRANSCRIPT_TXT = "transcript.txt"
OUTPUT_NAMES = (TRANSCRIPT_JSON, TRANSCRIPT_TXT)
TEMP_PATTERNS = (f".{TRANSCRIPT_JSON}.*.tmp", f".{TRANSCRIPT_TXT}.*.tmp")

PROGRESS_INTERVAL_SECONDS = 60.0
END_SLACK_SECONDS = 1.0  # a time may exceed the audio duration by this much
WORD_OVERLAP_SECONDS = 0.05  # a word may start this much before the previous one ended
MOST = 0.5  # more than this share of words out of range fails the stage
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
REPO_ROOT = Path(__file__).resolve().parents[3]


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


def audio_duration_seconds(wav_path: Path) -> float:
    """Length of a PCM WAV from its header (audio.wav was verified by the fetch stage)."""
    try:
        with wave.open(str(wav_path), "rb") as reader:
            rate = reader.getframerate()
            frames = reader.getnframes()
    except (wave.Error, EOFError, OSError) as exc:
        raise RunError(f"{wav_path.name} is not a readable WAV file: {exc}") from exc
    if rate <= 0:
        raise RunError(f"{wav_path.name} has no sample rate")
    return frames / rate


# --- validation --------------------------------------------------------------------------------


def _in_range(start: float, end: float, limit: float) -> bool:
    return math.isfinite(start) and math.isfinite(end) and 0.0 <= start <= end <= limit


def _clamp(value: float, limit: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(max(value, 0.0), limit)


def _clamp_pair(start: float, end: float, limit: float) -> tuple[float, float]:
    start = _clamp(start, limit)
    end = _clamp(end, limit)
    return start, max(start, end)


def validate_segments(
    segments: list[RawSegment], duration: float
) -> tuple[list[RawSegment], list[str]]:
    """Clamp small timing problems (each counted in a warning); fail on absurd output.

    Every time ends up with 0 <= start <= end <= duration + 1 s, and each word starts no more than
    0.05 s before the previous word ended.
    """
    limit = duration + END_SLACK_SECONDS
    total_words = sum(len(s.words) for s in segments)
    bad_words = sum(1 for s in segments for w in s.words if not _in_range(w.start, w.end, limit))
    bad_segments = sum(1 for s in segments if not _in_range(s.start, s.end, limit))

    # With words present they are the evidence; without them the segments are.
    units, bad_units = (total_words, bad_words) if total_words else (len(segments), bad_segments)
    if units and bad_units / units > MOST:
        raise RunError(
            f"transcript timestamps are not usable: {bad_units} of {units} "
            f"{'words' if total_words else 'segments'} are outside the {duration:.1f} s of audio"
        )

    moved_overlaps = 0
    cleaned: list[RawSegment] = []
    previous_end = 0.0
    for segment in segments:
        words: list[RawWord] = []
        for word in segment.words:
            start, end = _clamp_pair(word.start, word.end, limit)
            if start < previous_end - WORD_OVERLAP_SECONDS:
                start = previous_end
                end = max(start, end)
                moved_overlaps += 1
            words.append(replace(word, start=start, end=end))
            previous_end = max(previous_end, end)
        start, end = _clamp_pair(segment.start, segment.end, limit)
        cleaned.append(replace(segment, start=start, end=end, words=words))

    warnings: list[str] = []
    if bad_segments:
        warnings.append(f"{bad_segments} segment time(s) were outside 0..{limit:.1f} s; clamped")
    if bad_words:
        warnings.append(f"{bad_words} word time(s) were outside 0..{limit:.1f} s; clamped")
    if moved_overlaps:
        warnings.append(
            f"{moved_overlaps} word(s) started more than {WORD_OVERLAP_SECONDS} s before the "
            "previous word ended; start moved"
        )
    if not any(s.text.strip() for s in segments):
        warnings.append("no speech was found in the audio")
    return cleaned, warnings


# --- output formats ----------------------------------------------------------------------------


def format_timestamp(seconds: float) -> str:
    whole = int(max(seconds, 0.0))
    return f"[{whole // 60:02d}:{whole % 60:02d}]"


def render_txt(segments: list[TranscriptSegment]) -> str:
    """One line per segment: `[mm:ss] text`. Line breaks and control characters become spaces."""
    lines = []
    for segment in segments:
        text = " ".join(CONTROL_CHARS_RE.sub(" ", segment.text).split())
        if text:
            lines.append(f"{format_timestamp(segment.start)} {text}")
    return "\n".join(lines) + ("\n" if lines else "")


def _to_model(segment: RawSegment) -> TranscriptSegment:
    return TranscriptSegment(
        id=segment.id,
        start=round(segment.start, 3),
        end=round(segment.end, 3),
        text=segment.text.strip(),
        avg_logprob=_finite_or_zero(segment.avg_logprob),
        no_speech_prob=_finite_or_zero(segment.no_speech_prob),
        words=[
            TranscriptWord(
                start=round(w.start, 3),
                end=round(w.end, 3),
                word=w.word,
                probability=_finite_or_zero(w.probability),
            )
            for w in segment.words
        ],
    )


def _finite_or_zero(value: float) -> float:
    return value if math.isfinite(value) else 0.0


# --- the stage ---------------------------------------------------------------------------------


def run(ctx: RunContext) -> None:
    run_dir = ctx.run_dir.resolve()
    _remove_outputs(run_dir, strict=True)  # a rerun replaces outputs cleanly
    try:
        _transcribe(ctx, run_dir)
    except BaseException:
        _remove_outputs(run_dir, strict=False)
        raise


def _transcribe(ctx: RunContext, run_dir: Path) -> None:
    cfg = ctx.config.app.transcribe
    audio = run_dir / AUDIO_FILE
    if not audio.is_file():
        raise RunError(f"{AUDIO_FILE} not found in the run folder; run the fetch stage first")
    duration = audio_duration_seconds(audio)
    root = ctx.project_root if ctx.project_root is not None else REPO_ROOT

    load_started = time.perf_counter()
    loaded = whisper.open_model(cfg, root)
    log.info(
        f"transcribe: model={cfg.model} device={loaded.device} "
        f"compute_type={loaded.compute_type} pid={os.getpid()} "
        f"(model ready in {time.perf_counter() - load_started:.1f} s)"
    )
    try:
        info, raw_iter = whisper.transcribe(loaded, audio, cfg)
        started = time.perf_counter()
        raw: list[RawSegment] = []
        next_report = PROGRESS_INTERVAL_SECONDS
        for segment in raw_iter:
            raw.append(segment)
            if segment.end >= next_report:
                percent = min(100.0, 100.0 * segment.end / duration) if duration > 0 else 100.0
                log.info(
                    f"transcribe: {percent:.0f}% ({segment.end:.0f} of {duration:.0f} s of audio), "
                    f"{time.perf_counter() - started:.0f} s elapsed"
                )
                next_report = (
                    segment.end // PROGRESS_INTERVAL_SECONDS + 1
                ) * PROGRESS_INTERVAL_SECONDS
        seconds = time.perf_counter() - started
    finally:
        loaded.close()

    cleaned, warnings = validate_segments(raw, duration)
    segments = [_to_model(s) for s in cleaned]
    transcript = Transcript(
        model=cfg.model,
        device=loaded.device,
        compute_type=loaded.compute_type,
        settings=cfg,
        language=info.language,
        language_probability=round(info.language_probability, 4),
        audio_duration_seconds=round(duration, 3),
        transcribe_seconds=round(seconds, 3),
        realtime_factor=round(duration / seconds, 2) if seconds > 0 else 0.0,
        segments=segments,
        warnings=[*loaded.warnings, *warnings],
    )
    write_text_atomic(run_dir / TRANSCRIPT_TXT, render_txt(segments))
    write_json_atomic(run_dir / TRANSCRIPT_JSON, transcript.model_dump(mode="json"))
    log.info(
        f"transcribed {duration:.1f} s of audio in {seconds:.1f} s "
        f"(x{transcript.realtime_factor:g} realtime), {len(segments)} segments, "
        f"{len(transcript.warnings)} warning(s)"
    )
