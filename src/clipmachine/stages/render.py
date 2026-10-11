"""Stage 5: scored.json -> clips/<id>.mp4, <id>.ass and <id>.json for every kept clip.

One ffmpeg pass per clip: cut, centre crop to the output aspect, scale, burn in word-group
captions. Everything is written inside <run folder>/clips/. An output is built as
<id>.tmp.mp4, checked with ffprobe, then renamed; <id>.json is written last, so a clip with a
JSON file next to its MP4 finished.

A forced redo (`--from-stage render`, or a rerun after an earlier stage) rebuilds every clip. Only
when this stage failed on an earlier run and is being resumed (ctx.resuming) are clips that are
already valid kept: an output is valid when its JSON matches the current clip times, crop and
settings and ffprobe confirms the file.

ffmpeg runs with the run folder as its working directory, so the subtitle file and the font folder
are short relative paths and no drive letter reaches the -vf string.

No transcript text, hook or title is logged or written to clips/<id>.json.
"""

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from clipmachine.context import RunContext
from clipmachine.models import (
    ClipCropBox,
    FetchMeta,
    RenderedClip,
    RenderedOutput,
    RenderedSettings,
    RenderedSource,
    ScoredClip,
    ScoredFile,
    TranscriptSegment,
    VideoStreamInfo,
)
from clipmachine.runs import RunError, write_json_atomic, write_text_atomic
from clipmachine.stages.candidates import _load_transcript
from clipmachine.stages.fetch import META_FILE
from clipmachine.stages.score import SCORED_JSON
from clipmachine.stages.transcribe import REPO_ROOT
from clipmachine.utils import ffmpeg
from clipmachine.utils.captions import build_ass, build_events, clip_words, group_words
from clipmachine.utils.crop import CropBox, center_crop

log = logging.getLogger("clipmachine")

CLIPS_DIR = "clips"
FONTS_DIR = Path("assets") / "fonts"
TEMP_SUFFIX = ".tmp.mp4"
OUTPUT_SUFFIXES = (".mp4", ".ass", ".json", ".tmp")  # ".tmp": atomic-write leftovers
CLIP_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")


@dataclass(frozen=True)
class Plan:
    """Everything the clips of one run share."""

    run_dir: Path
    clips_dir: Path
    source: Path
    video: VideoStreamInfo
    crop: CropBox
    settings: RenderedSettings
    fonts_rel: str  # font folder relative to run_dir, forward slashes
    segments: list[TranscriptSegment]


def _load_scored(run_dir: Path) -> ScoredFile:
    path = run_dir / SCORED_JSON
    if not path.is_file():
        raise RunError(f"{SCORED_JSON} not found in the run folder; run the score stage first")
    try:
        return ScoredFile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunError(f"{SCORED_JSON} is not a valid scored file ({type(exc).__name__})") from exc


def _load_meta(run_dir: Path) -> FetchMeta:
    path = run_dir / META_FILE
    if not path.is_file():
        raise RunError(f"{META_FILE} not found in the run folder; run the fetch stage first")
    try:
        return FetchMeta.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunError(f"{META_FILE} is not valid ({type(exc).__name__})") from exc


def _sweep(clips_dir: Path, keep_ids: set[str], *, everything: bool) -> None:
    """Delete temp files and the outputs of clips that are not kept; all outputs if `everything`.

    Only files directly inside clips/ with our suffixes are touched.
    """
    if not clips_dir.is_dir():
        return
    for path in clips_dir.iterdir():
        name = path.name
        if not path.is_file() or not name.endswith(OUTPUT_SUFFIXES):
            continue
        is_temp = name.endswith((TEMP_SUFFIX, ".tmp"))
        stem = path.stem
        if everything or is_temp or stem not in keep_ids:
            path.unlink(missing_ok=True)


def _fonts_relative(run_dir: Path, fonts_dir: Path) -> str:
    """The font folder relative to the run folder, forward slashes (ffmpeg runs in run_dir)."""
    try:
        relative = os.path.relpath(fonts_dir, run_dir)
    except ValueError as exc:  # another drive: no relative path exists
        raise RunError(
            "the run folder and assets/fonts are on different drives; keep runs/ inside the project"
        ) from exc
    return Path(relative).as_posix()


def _settings(ctx: RunContext) -> RenderedSettings:
    app = ctx.config.app
    return RenderedSettings(
        width=app.video.width,
        height=app.video.height,
        crf=app.render.crf,
        preset=app.render.preset,
        audio_bitrate=app.render.audio_bitrate,
        font=app.captions.font,
        font_file=app.captions.font_file,
        font_size=app.captions.font_size,
        margin_v=app.captions.margin_v,
        words_per_line=app.captions.words_per_line,
        highlight_color=app.captions.highlight_color,
        caption_offset_seconds=app.captions.offset_seconds,
    )


def _already_rendered(plan: Plan, clip: ScoredClip) -> bool:
    """True when clips/<id>.mp4 and <id>.json describe exactly this clip with these settings and
    ffprobe agrees the file is a good output."""
    mp4 = plan.clips_dir / f"{clip.id}.mp4"
    info = plan.clips_dir / f"{clip.id}.json"
    if not (mp4.is_file() and info.is_file()):
        return False
    try:
        old = RenderedClip.model_validate_json(info.read_text(encoding="utf-8"))
        same = (old.id, old.start, old.end, old.crop, old.settings) == (
            clip.id,
            clip.start,
            clip.end,
            _crop_json(plan.crop),
            plan.settings,
        )
        if not same:
            return False
        ffmpeg.verify_clip(
            ffmpeg.probe(mp4),
            width=plan.settings.width,
            height=plan.settings.height,
            duration=clip.end - clip.start,
        )
    except (OSError, ValueError, ValidationError, ffmpeg.MediaError):
        return False
    return True


def _render_clip(plan: Plan, clip: ScoredClip) -> None:
    started = time.perf_counter()
    settings = plan.settings
    duration = clip.end - clip.start
    mp4 = plan.clips_dir / f"{clip.id}.mp4"
    temp = plan.clips_dir / f"{clip.id}{TEMP_SUFFIX}"
    ass = plan.clips_dir / f"{clip.id}.ass"
    info = plan.clips_dir / f"{clip.id}.json"

    info.unlink(missing_ok=True)  # the JSON must never describe an output that is being replaced
    words = clip_words(plan.segments, clip.start, clip.end)
    events = build_events(
        group_words(words, settings.words_per_line),
        settings.highlight_color,
        duration,
        settings.caption_offset_seconds,
    )
    write_text_atomic(
        ass,
        build_ass(
            events,
            font=settings.font,
            font_size=settings.font_size,
            margin_v=settings.margin_v,
            width=settings.width,
            height=settings.height,
        ),
    )
    crop = plan.crop

    vf = ffmpeg.video_filter(
        crop, settings.width, settings.height, f"{CLIPS_DIR}/{ass.name}", plan.fonts_rel
    )
    args = ffmpeg.render_args(
        plan.source,
        temp,
        start=clip.start,
        duration=duration,
        vf=vf,
        crf=settings.crf,
        preset=settings.preset,
        audio_bitrate=settings.audio_bitrate,
    )
    try:
        ffmpeg.render_clip(args, plan.run_dir, duration)
        output = ffmpeg.probe(temp)
        ffmpeg.verify_clip(output, width=settings.width, height=settings.height, duration=duration)
        os.replace(temp, mp4)
    except ffmpeg.MediaError as exc:
        raise RunError(f"render failed for {clip.id}: {exc}") from exc
    finally:
        temp.unlink(missing_ok=True)

    video, audio = output.video, output.audio
    if video is None or audio is None or output.duration_seconds is None:  # verify_clip ensured
        raise RunError(f"render failed for {clip.id}: output has no video, audio or duration")
    seconds = round(time.perf_counter() - started, 2)
    write_json_atomic(
        info,
        RenderedClip(
            id=clip.id,
            rank=clip.rank,
            score=clip.score,
            start=clip.start,
            end=clip.end,
            duration=round(duration, 3),
            source=RenderedSource(
                width=plan.video.width,
                height=plan.video.height,
                rotation=plan.video.rotation,
                r_frame_rate=plan.video.r_frame_rate,
                avg_frame_rate=plan.video.avg_frame_rate,
                variable_frame_rate=plan.video.variable_frame_rate,
            ),
            crop=_crop_json(crop),
            settings=settings,
            caption_events=len(events),
            output=RenderedOutput(
                width=video.width,
                height=video.height,
                video_codec=video.codec,
                audio_codec=audio.codec,
                duration_seconds=output.duration_seconds,
                size_bytes=mp4.stat().st_size,
            ),
            render_seconds=seconds,
        ).model_dump(mode="json"),
    )
    log.info(
        f"render: {clip.id} {clip.start:.2f}-{clip.end:.2f} s, crop {crop.w}x{crop.h}"
        f"+{crop.x}+{crop.y}, {mp4.stat().st_size / 1_000_000:.1f} MB, {seconds:.1f} s"
    )


def _crop_json(crop: CropBox) -> ClipCropBox:
    return ClipCropBox(w=crop.w, h=crop.h, x=crop.x, y=crop.y)


def run(ctx: RunContext) -> None:
    run_dir = ctx.run_dir.resolve()
    root = (ctx.project_root if ctx.project_root is not None else REPO_ROOT).resolve()
    app = ctx.config.app
    if app.video.crop != "center":
        raise RunError(
            f"video.crop {app.video.crop!r} is not implemented in the render stage yet; use "
            "'center'"
        )

    # Preflight: a missing font is an error, never a silent fallback.
    fonts_dir = root / FONTS_DIR
    if not (fonts_dir / app.captions.font_file).is_file():
        raise RunError(
            f"caption font not found: {FONTS_DIR.as_posix()}/{app.captions.font_file} "
            "(captions.font_file); the render stage does not fall back to another font"
        )

    scored = _load_scored(run_dir)
    meta = _load_meta(run_dir)
    video = meta.media.video
    if video is None:
        raise RunError(f"{META_FILE} has no video stream")
    source = run_dir / meta.source_file
    if not source.is_file():
        raise RunError(f"source video not found in the run folder: {meta.source_file}")

    kept = sorted((c for c in scored.clips if c.kept), key=lambda c: c.rank or 0)
    if any(not CLIP_ID_RE.fullmatch(c.id) for c in kept):
        raise RunError(f"{SCORED_JSON} has a clip id that is not a safe file name")

    clips_dir = run_dir / CLIPS_DIR
    _sweep(clips_dir, {c.id for c in kept}, everything=not ctx.resuming)
    if not kept:
        log.info("render: 0 kept clips, nothing to render")
        return
    clips_dir.mkdir(exist_ok=True)

    settings = _settings(ctx)
    try:
        box = center_crop(
            video.width, video.height, video.rotation, settings.width, settings.height
        )
    except ValueError as exc:
        raise RunError(f"cannot crop the source: {exc}") from exc
    plan = Plan(
        run_dir=run_dir,
        clips_dir=clips_dir,
        source=source,
        video=video,
        crop=box,
        settings=settings,
        fonts_rel=_fonts_relative(run_dir, fonts_dir),
        segments=_load_transcript(run_dir).segments,
    )

    rendered = skipped = 0
    for clip in kept:
        if ctx.resuming and _already_rendered(plan, clip):
            skipped += 1
            log.info(f"render: {clip.id} already rendered, kept")
            continue
        _render_clip(plan, clip)
        rendered += 1
    log.info(f"render: {rendered} rendered, {skipped} already done")
