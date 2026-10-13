"""Stage 5: scored.json -> clips/<id>.mp4, <id>.ass and <id>.json for every kept clip.

One ffmpeg pass per clip: cut, crop to the output aspect, scale, burn in word-group captions. The
crop is the centre of the frame (video.crop: center), or follows the active speaker (speaker):
then each clip gets a track (clips/<id>.track.json, reused when valid, else analysed here, see
tracking.py), the crop path is written to clips/<id>.crop.cmd and ffmpeg's `sendcmd` filter moves
the crop in the same pass. A track that fell back to the centre crop renders exactly as centre
mode does. Speaker mode needs both face model files and never downloads them.

Captions have two styles (captions.style). "legacy" writes the older white outlined word groups
exactly as before Brief R1. "box_karaoke" writes one line of words at a time on a drawn rounded
box, each word turning yellow at its own start, placed inside the safe margins (config `safe`),
and, when hook.enabled, the clip's hook line for the first seconds, moved below any face the clip's
track found there (centre-crop mode has no track and says so with a note). Both go in the clip's
.ass file; the clip json holds counts and reason codes only.

Everything is written inside <run folder>/clips/. An output is built as
<id>.tmp.mp4, checked with ffprobe, then renamed; <id>.json is written last, so a clip with a
JSON file next to its MP4 finished.

A forced redo (`--from-stage render`, or a rerun after an earlier stage) rebuilds every clip. Only
when this stage failed on an earlier run and is being resumed (ctx.resuming) are clips that are
already valid kept: an output is valid when its JSON matches the current clip times, crop and
settings and ffprobe confirms the file. In speaker mode the crop comes from the track: the
clip's track file must still be valid and be the very file the output was rendered from.

ffmpeg runs with the run folder as its working directory, so the subtitle file and the font folder
are short relative paths and no drive letter reaches the -vf string.

No transcript text, hook or title is logged or written to clips/<id>.json (only counts and
reason codes).
"""

import hashlib
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import ValidationError

from clipmachine.context import RunContext
from clipmachine.models import (
    AppConfig,
    ClipCropBox,
    FetchMeta,
    RenderedClip,
    RenderedOutput,
    RenderedSettings,
    RenderedSource,
    ScoredClip,
    ScoredFile,
    TrackFile,
    TranscriptSegment,
    VideoStreamInfo,
    ZoomConfig,
)
from clipmachine.runs import RunError, write_json_atomic, write_text_atomic
from clipmachine.stages.candidates import _load_transcript
from clipmachine.stages.fetch import META_FILE
from clipmachine.stages.score import SCORED_JSON
from clipmachine.stages.transcribe import REPO_ROOT
from clipmachine.utils import audio, ffmpeg
from clipmachine.utils.captions import (
    AssEvent,
    CaptionWord,
    KaraokeSettings,
    build_ass,
    build_ass_v2,
    build_events,
    build_hook,
    build_karaoke,
    clip_words,
    group_words,
)
from clipmachine.utils.crop import (
    CropBox,
    center_crop,
    display_size,
    effective_boundaries,
    frame_path,
)
from clipmachine.utils.fontmetrics import FontError, FontMetrics, load_font
from clipmachine.utils.safezone import Rect, face_rects, outside_count, safe_rect
from clipmachine.utils.snapping import SentenceIndex, build_index
from clipmachine.utils.wordlists import (
    Hit,
    WordListError,
    WordLists,
    load_wordlists,
    mask_text,
    mask_words,
)
from clipmachine.utils.zoom import (
    SentenceStart,
    ZoomSpan,
    select_zooms,
    upscale_factor,
    zoomed_path,
)

if TYPE_CHECKING:  # tracking imports this module, so it is imported inside the speaker branch
    from clipmachine.tracking import TrackPlan

log = logging.getLogger("clipmachine")

CLIPS_DIR = "clips"
FONTS_DIR = Path("assets") / "fonts"
TEMP_SUFFIX = ".tmp.mp4"
CROP_CMD_SUFFIX = ".crop.cmd"  # the sendcmd file of a speaker-crop clip
OUTPUT_SUFFIXES = (
    ".mp4",
    ".ass",
    ".json",
    CROP_CMD_SUFFIX,
    ".tmp",
)  # ".tmp": atomic-write leftovers
DEFAULT_PATH_FPS = 30.0  # when the source reports no usable frame rate
MAX_PATH_FPS = 60.0  # a variable-rate file can report a huge rate; the crop path must stay small
TRACK_JSON_SUFFIX = ".track.json"  # written by `clipmachine track` (tracking.py)
TRACK_PREVIEW_SUFFIX = ".track.preview.mp4"
TRACK_SUFFIXES = (TRACK_JSON_SUFFIX, TRACK_PREVIEW_SUFFIX)
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
    speaker: "TrackPlan | None" = None  # set in speaker mode
    preview: bool = False  # speaker mode: write the debug preview when a clip is analysed
    # style box_karaoke only: the caption font's metrics, the line settings and the safe area
    metrics: FontMetrics | None = None
    karaoke: KaraokeSettings | None = None
    safe: Rect | None = None
    wordlists: WordLists | None = None  # None: captions.mask_profanity is off
    zoom: ZoomConfig | None = None  # None: punch-in zooms are off (Brief R2)
    sentences: SentenceIndex | None = (
        None  # the transcript's sentences; set with zoom in speaker mode
    )


class ClipLike(Protocol):
    """What the speaker crop and the captions need of a clip: a scored clip, or a campaign
    moment with its final times."""

    @property
    def id(self) -> str: ...

    @property
    def start(self) -> float: ...

    @property
    def end(self) -> float: ...

    @property
    def hook_line(self) -> str: ...


@dataclass(frozen=True)
class HookSpec:
    """How the hook is drawn: how long, the fade, the base font size and the rectangles it must
    stay clear of. The render stage builds one from the settings."""

    seconds: float
    fade_ms: int
    font_size: int
    avoid: tuple[Rect, ...] = ()
    position: str = "avoid_faces"  # top: the top of the safe area, faces and avoid ignored


@dataclass(frozen=True)
class SpeakerCrop:
    """How one clip is cropped in speaker mode."""

    crop: CropBox  # the window with the first x
    sendcmd_rel: str | None  # the command file, relative to the run folder; None: the crop is fixed
    track_sha256: str
    fallback: str | None
    changes: int
    reused: bool
    cuts: int
    switches: int
    track: TrackFile  # the track this crop came from; its faces place the hook
    # Brief R2: the crop window at every change (the first is `crop`), the punch-ins and the
    # reason codes of zooms that were skipped; all empty with zoom off
    boxes: tuple[tuple[float, CropBox], ...] = ()
    zooms: tuple[ZoomSpan, ...] = ()
    zoom_notes: tuple[str, ...] = ()


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
        # <id>.track.json and <id>.track.preview.mp4 belong to the track command, not to this
        # stage: a clip that is still kept keeps them, even on a forced redo.
        track_owner = next(
            (name[: -len(s)] for s in TRACK_SUFFIXES if name.endswith(s) and not is_temp), None
        )
        if track_owner is not None:
            if track_owner not in keep_ids:
                path.unlink(missing_ok=True)
            continue
        if name.endswith(CROP_CMD_SUFFIX):  # Path.stem would give "<id>.crop"
            if everything or name[: -len(CROP_CMD_SUFFIX)] not in keep_ids:
                path.unlink(missing_ok=True)
            continue
        stem = path.stem
        if everything or is_temp or stem not in keep_ids:
            path.unlink(missing_ok=True)


def _relative(run_dir: Path, path: Path) -> str:
    """`path` relative to the run folder, forward slashes (ffmpeg runs in run_dir)."""
    try:
        relative = os.path.relpath(path, run_dir)
    except ValueError as exc:  # another drive: no relative path exists
        raise RunError(
            "the run folder and assets/fonts are on different drives; keep runs/ inside the project"
        ) from exc
    return Path(relative).as_posix()


def _fonts_relative(run_dir: Path, fonts_dir: Path) -> str:
    """The font folder relative to the run folder, forward slashes (ffmpeg runs in run_dir)."""
    return _relative(run_dir, fonts_dir)


def _settings(ctx: RunContext) -> RenderedSettings:
    return _settings_of(ctx.config.app)


def _settings_of(app: AppConfig) -> RenderedSettings:
    zoom = app.video.zoom
    on = zoom.enabled  # off: the six fields keep their defaults, so older output is not stale
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
        audio_fade_seconds=app.render.audio_fade_seconds,
        caption_style=app.captions.style,
        max_words=app.captions.max_words,
        max_width_px=app.captions.max_width_px,
        word_gap_em=app.captions.word_gap_em,
        letter_spacing_px=app.captions.letter_spacing_px,
        line_gap_em=app.captions.line_gap_em,
        spoken_color=app.captions.spoken_color,
        upcoming_color=app.captions.upcoming_color,
        box_opacity=app.captions.box_opacity,
        box_padding_px=app.captions.box_padding_px,
        hook_enabled=app.hook.enabled,
        hook_seconds=app.hook.seconds,
        hook_fade_ms=app.hook.fade_ms,
        safe_top=app.safe.top,
        safe_bottom=app.safe.bottom,
        safe_left=app.safe.left,
        safe_right=app.safe.right,
        hook_font_size=app.hook.font_size,
        hook_position=app.hook.position,
        mask_profanity=app.captions.mask_profanity,
        zoom_enabled=on,
        zoom_scale=zoom.scale if on else 0.0,
        zoom_loud_pct=zoom.loud_pct if on else 0.0,
        zoom_min_gap_s=zoom.min_gap_s if on else 0.0,
        zoom_max_hold_s=zoom.max_hold_s if on else 0.0,
        zoom_max_upscale=zoom.max_upscale if on else 0.0,
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
        if plan.speaker is None:
            same = old.crop_mode == "center" and (
                old.id,
                old.start,
                old.end,
                old.crop,
                old.settings,
            ) == (clip.id, clip.start, clip.end, _crop_json(plan.crop), plan.settings)
        else:
            same = old.crop_mode == "speaker" and _speaker_output_matches(plan, clip, old)
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


def _speaker_output_matches(plan: Plan, clip: ScoredClip, old: RenderedClip) -> bool:
    """A speaker-mode output is current when the clip, the settings and the crop path are: the
    clip's track must still be valid and be the file the output was rendered from."""
    from clipmachine import tracking  # tracking imports this module

    assert plan.speaker is not None
    if (old.id, old.start, old.end, old.settings) != (clip.id, clip.start, clip.end, plan.settings):
        return False
    track = tracking.load_plan_track(
        plan.speaker, clip.id, clip.start, clip.end, crop_w=plan.crop.w
    )
    if track is None:
        return False
    if old.track_sha256 != _file_sha256(tracking.track_path(plan.clips_dir, clip.id)):
        return False
    return (old.crop_changes > 0) == (plan.clips_dir / f"{clip.id}{CROP_CMD_SUFFIX}").is_file()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_fps(video: VideoStreamInfo) -> float:
    """The frame rate the crop path is worked out at: r_frame_rate, else avg_frame_rate, else 30;
    never above 60."""
    for text in (video.r_frame_rate, video.avg_frame_rate):
        rate = ffmpeg.frame_rate(text)
        if rate is not None:
            return min(float(rate), MAX_PATH_FPS)
    return DEFAULT_PATH_FPS


def _speaker_crop(plan: Plan, clip: ClipLike) -> SpeakerCrop:
    """The clip's track (reused or analysed) turned into a crop; writes clips/<id>.crop.cmd when
    the crop moves."""
    from clipmachine import tracking  # tracking imports this module
    from clipmachine.utils import faces

    assert plan.speaker is not None
    try:
        track, reused = tracking.track_for_clip(
            plan.speaker,
            clip.id,
            clip.start,
            clip.end,
            preview=plan.preview,
            crop_w=plan.crop.w,
        )
    except (faces.FacesError, ffmpeg.MediaError) as exc:
        raise RunError(f"speaker tracking failed for {clip.id}: {exc}") from exc
    return _crop_from_track(plan, clip, track, reused, plan.clips_dir)


def _sentence_starts(
    plan: Plan, clip: ClipLike, index: SentenceIndex
) -> tuple[list[SentenceStart], list[str]]:
    """The clip's sentences as (start, mean loudness), from the one holding its first spoken word
    through the one holding its last. Starts are seconds from the clip start (the first is
    clamped to 0), so index 0 is the first-line sentence. The notes say when loudness could not
    be measured."""
    assert plan.speaker is not None
    words = index.words
    first_word = next((i for i, w in enumerate(words) if w.end > clip.start), None)
    last_word = next((i for i in range(len(words) - 1, -1, -1) if words[i].start < clip.end), None)
    if first_word is None or last_word is None or first_word > last_word:
        return [], []

    notes: list[str] = []
    envelope: list[float] | None = None
    if plan.speaker.audio is None:
        notes.append("zoom_no_audio")
    else:
        envelope = audio.loudness_envelope(plan.speaker.audio, clip.start, clip.end)
    starts: list[SentenceStart] = []
    for n in range(index.sentence_of(first_word), index.sentence_of(last_word) + 1):
        sentence = index.sentences[n]
        values: list[float] = []
        if envelope is not None:
            for word in words[sentence.first : sentence.last + 1]:
                low = max(word.start, clip.start) - clip.start
                high = min(word.end, clip.end) - clip.start
                low_hop = int(round(low / audio.HOP_SECONDS, 9))
                high_hop = math.ceil(round(high / audio.HOP_SECONDS, 9))
                values.extend(envelope[low_hop:high_hop])
        loudness = sum(values) / len(values) if values else None
        starts.append(SentenceStart(max(words[sentence.first].start - clip.start, 0.0), loudness))
    return starts, notes


def _zoom_spans(
    plan: Plan, clip: ClipLike, track: TrackFile, base: CropBox
) -> tuple[list[ZoomSpan], list[str]]:
    """The punch-ins of a clip with a followed face, and reason codes for what was skipped.

    Nothing is chosen when the zoomed window would be enlarged more than `max_upscale`. Each
    span is centred vertically on the face of the shot where it starts."""
    zoom = plan.zoom
    if zoom is None or plan.sentences is None:
        return [], []
    settings = plan.settings
    if upscale_factor(base, zoom.scale, settings.width, settings.height) > zoom.max_upscale:
        return [], ["zoom_skipped_upscale"]
    starts, notes = _sentence_starts(plan, clip, plan.sentences)
    duration = clip.end - clip.start
    shots = [(s.start, s.end) for s in track.shots]
    boundaries = effective_boundaries(shots, track.cuts, track.cuts_exact, duration)
    cuts = track.cuts_exact if len(track.cuts_exact) == len(track.cuts) else track.cuts
    excluded = [(s.start, s.end) for s in track.shots if s.face_cy is None]
    chosen = select_zooms(starts, 0, boundaries, cuts, duration, zoom, excluded=excluded)
    spans: list[ZoomSpan] = []
    for start, end in chosen:
        holding = [s for s in track.shots if s.start <= start + 1e-6]
        face_cy = holding[-1].face_cy if holding else None
        if face_cy is not None:
            spans.append(ZoomSpan(start, end, face_cy))
    return spans, notes


def _crop_from_track(
    plan: Plan, clip: ClipLike, track: TrackFile, reused: bool, out_dir: Path
) -> SpeakerCrop:
    """The crop a clip's track gives; the sendcmd file (if the crop moves) goes in `out_dir`."""
    from clipmachine import tracking  # tracking imports this module

    sha = _file_sha256(tracking.track_path(plan.clips_dir, clip.id))
    cuts = track.summary.cuts
    if track.fallback_reason is not None:  # exactly the centre crop
        no_track: tuple[str, ...] = () if plan.zoom is None else ("zoom_no_track",)
        return SpeakerCrop(
            plan.crop,
            None,
            sha,
            track.fallback_reason,
            0,
            reused,
            cuts,
            0,
            track,
            zoom_notes=no_track,
        )
    duration = clip.end - clip.start
    path = frame_path(
        [(s.t, s.crop_x) for s in track.samples],
        [(s.start, s.end) for s in track.shots],
        track.cuts,
        track.cuts_exact,
        _source_fps(plan.video),
        duration,
    )
    switches = track.summary.switches
    if plan.zoom is None:
        box = CropBox(w=plan.crop.w, h=plan.crop.h, x=path[0][1], y=plan.crop.y)
        sendcmd_rel = None
        if len(path) > 1:
            cmd = out_dir / f"{clip.id}{CROP_CMD_SUFFIX}"
            write_text_atomic(cmd, ffmpeg.crop_commands(path))
            sendcmd_rel = _relative(plan.run_dir, cmd)
        return SpeakerCrop(
            box, sendcmd_rel, sha, None, len(path) - 1, reused, cuts, switches, track
        )

    spans, notes = _zoom_spans(plan, clip, track, plan.crop)
    frame_w, frame_h = display_size(plan.video.width, plan.video.height, plan.video.rotation)
    boxes = zoomed_path(path, plan.crop, spans, plan.zoom.scale, frame_w, frame_h, duration)
    sendcmd_rel = None
    if len(boxes) > 1:
        cmd = out_dir / f"{clip.id}{CROP_CMD_SUFFIX}"
        write_text_atomic(cmd, ffmpeg.crop_box_commands(boxes))
        sendcmd_rel = _relative(plan.run_dir, cmd)
    return SpeakerCrop(
        boxes[0][1],
        sendcmd_rel,
        sha,
        None,
        len(boxes) - 1,
        reused,
        cuts,
        switches,
        track,
        boxes=tuple(boxes),
        zooms=tuple(spans),
        zoom_notes=tuple(notes),
    )


@dataclass(frozen=True)
class Captions:
    """The .ass text of one clip and the counts that go in its json (no text)."""

    text: str
    events: int  # caption Dialogue lines written (box and text lines in box_karaoke; no hook)
    lines: int
    max_words: int
    hook_shown: bool = False
    hook_moved: bool = False
    outside_safe_zone: int = 0
    notes: tuple[str, ...] = ()
    caption_boxes: tuple[Rect, ...] = ()  # one per caption line (box_karaoke)
    hook_band: Rect | None = None  # the box behind the hook, when it is shown
    # words masked in the captions and in the hook; shown nowhere but a local report
    masked: tuple[Hit, ...] = ()
    hook_masked: tuple[Hit, ...] = ()


def _masked_words(plan: Plan, words: list[CaptionWord]) -> tuple[list[CaptionWord], list[Hit]]:
    if plan.wordlists is None:
        return words, []
    return mask_words(words, plan.wordlists)


def _legacy_captions(plan: Plan, words: list[CaptionWord], duration: float) -> Captions:
    settings = plan.settings
    words, masked = _masked_words(plan, words)
    groups = group_words(words, settings.words_per_line)
    events = build_events(
        groups, settings.highlight_color, duration, settings.caption_offset_seconds
    )
    text = build_ass(
        events,
        font=settings.font,
        font_size=settings.font_size,
        margin_v=settings.margin_v,
        width=settings.width,
        height=settings.height,
    )
    return Captions(
        text,
        len(events),
        len(groups),
        max((len(g) for g in groups), default=0),
        masked=tuple(masked),
    )


def _face_boxes(plan: Plan, chosen: SpeakerCrop | None, until: float) -> list[Rect] | None:
    """The faces of the clip's track up to `until` seconds, in output pixels; None in centre-crop
    mode, which has no track."""
    if chosen is None:
        return None
    settings = plan.settings
    source_w, source_h = display_size(plan.video.width, plan.video.height, plan.video.rotation)
    return face_rects(
        chosen.track.faces,
        chosen.track.samples,
        source_w=source_w,
        source_h=source_h,
        crop_w=plan.crop.w,
        crop_h=plan.crop.h,
        crop_y=plan.crop.y,
        out_w=settings.width,
        out_h=settings.height,
        until=until,
        boxes=chosen.boxes or None,
    )


def _karaoke_captions(
    plan: Plan,
    clip: ClipLike,
    words: list[CaptionWord],
    chosen: SpeakerCrop | None,
    duration: float,
    hook: HookSpec | None = None,
) -> Captions:
    """`hook` is how the hook is drawn; None builds it from the settings, and then the hook is
    shown only when hook.enabled. A given spec always shows the hook."""
    settings = plan.settings
    karaoke, metrics, safe = plan.karaoke, plan.metrics, plan.safe
    assert karaoke is not None and metrics is not None and safe is not None
    words, masked = _masked_words(plan, words)
    built = build_karaoke(
        words,
        karaoke,
        metrics,
        safe,
        clip_seconds=duration,
        offset_seconds=settings.caption_offset_seconds,
    )
    boxes = list(built.boxes)
    notes = list(built.notes)
    hook_events: list[AssEvent] = []
    hook_band: Rect | None = None
    hook_masked: list[Hit] = []
    shown = moved = False
    if hook is None and settings.hook_enabled:
        hook = HookSpec(
            settings.hook_seconds,
            settings.hook_fade_ms,
            settings.hook_font_size,
            position=settings.hook_position,
        )
    if hook is not None:
        # centre-crop mode has no track: faces unknown
        at_top = hook.position == "top"  # faces are not needed then
        face_boxes = None if at_top else _face_boxes(plan, chosen, min(hook.seconds, duration))
        hook_line = clip.hook_line
        if plan.wordlists is not None:
            hook_line, hook_masked = mask_text(hook_line, plan.wordlists)
        result = build_hook(
            hook_line,
            karaoke,
            metrics,
            safe,
            face_boxes,
            seconds=hook.seconds,
            fade_ms=hook.fade_ms,
            line_gap_em=settings.line_gap_em,
            clip_seconds=duration,
            font_size=hook.font_size,
            avoid=hook.avoid,
            position=hook.position,
        )
        hook_events = result.events
        shown, moved = result.shown, result.moved
        notes.extend(result.notes)
        hook_band = result.band
        if result.band is not None:
            boxes.append(result.band)
    text = build_ass_v2(
        built.events,
        hook_events,
        font=settings.font,
        settings=karaoke,
        width=settings.width,
        height=settings.height,
    )
    return Captions(
        text,
        len(built.events),
        built.lines,
        built.max_words,
        shown,
        moved,
        outside_count(boxes, safe),
        tuple(notes),
        caption_boxes=tuple(built.boxes),
        hook_band=hook_band,
        masked=tuple(masked),
        hook_masked=tuple(hook_masked),
    )


def _captions_log(settings: RenderedSettings, captions: Captions) -> str:
    """Counts and codes only; empty for the legacy style, whose log line is unchanged."""
    if settings.caption_style == "legacy":
        return ""
    if not settings.hook_enabled:
        hook = "hook off"
    elif "hook_missing" in captions.notes:
        hook = "hook missing"
    else:
        hook = "hook moved" if captions.hook_moved else "hook shown"
    count = len(captions.masked) + len(captions.hook_masked)
    return f", captions {captions.lines} lines, {hook}, {count} masked"


def _render_clip(plan: Plan, clip: ScoredClip) -> None:
    started = time.perf_counter()
    settings = plan.settings
    duration = clip.end - clip.start
    mp4 = plan.clips_dir / f"{clip.id}.mp4"
    temp = plan.clips_dir / f"{clip.id}{TEMP_SUFFIX}"
    ass = plan.clips_dir / f"{clip.id}.ass"
    info = plan.clips_dir / f"{clip.id}.json"

    info.unlink(missing_ok=True)  # the JSON must never describe an output that is being replaced
    (plan.clips_dir / f"{clip.id}{CROP_CMD_SUFFIX}").unlink(missing_ok=True)
    chosen = _speaker_crop(plan, clip) if plan.speaker is not None else None
    words = clip_words(plan.segments, clip.start, clip.end)
    if settings.caption_style == "legacy":
        captions = _legacy_captions(plan, words, duration)
    else:
        captions = _karaoke_captions(plan, clip, words, chosen, duration)
    write_text_atomic(ass, captions.text)
    crop = plan.crop if chosen is None else chosen.crop
    zooms = () if chosen is None else chosen.zooms
    notes = list(captions.notes)
    if chosen is not None:
        notes.extend(chosen.zoom_notes)
    elif plan.zoom is not None:  # centre-crop mode has no followed face
        notes.append("zoom_no_track")

    vf = ffmpeg.video_filter(
        crop,
        settings.width,
        settings.height,
        f"{CLIPS_DIR}/{ass.name}",
        plan.fonts_rel,
        None if chosen is None else chosen.sendcmd_rel,
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
        audio_fade=settings.audio_fade_seconds,
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
            caption_events=captions.events,
            output=RenderedOutput(
                width=video.width,
                height=video.height,
                video_codec=video.codec,
                audio_codec=audio.codec,
                duration_seconds=output.duration_seconds,
                size_bytes=mp4.stat().st_size,
            ),
            render_seconds=seconds,
            crop_mode="center" if chosen is None else "speaker",
            track_sha256=None if chosen is None else chosen.track_sha256,
            track_fallback=None if chosen is None else chosen.fallback,
            crop_changes=0 if chosen is None else chosen.changes,
            notes=notes,
            caption_lines=captions.lines,
            caption_max_words=captions.max_words,
            hook_shown=captions.hook_shown,
            hook_moved=captions.hook_moved,
            outside_safe_zone=captions.outside_safe_zone,
            masked_words=len(captions.masked) + len(captions.hook_masked),
            zoom_count=len(zooms),
            zoom_spans=[(round(z.start, 3), round(z.end, 3)) for z in zooms],
        ).model_dump(mode="json"),
    )
    mode = "center" if chosen is None else _speaker_log(chosen, plan.zoom is not None)
    log.info(
        f"render: {clip.id} {clip.start:.2f}-{clip.end:.2f} s, crop {crop.w}x{crop.h}"
        f"+{crop.x}+{crop.y} ({mode}){_captions_log(settings, captions)}, "
        f"{mp4.stat().st_size / 1_000_000:.1f} MB, {seconds:.1f} s"
    )


def _speaker_log(chosen: SpeakerCrop, zoom_on: bool = False) -> str:
    """Numbers and a reason code only: no file names or text."""
    text = f"speaker, track {'reused' if chosen.reused else 'analysed'}"
    if chosen.fallback is not None:
        text += f", fallback {chosen.fallback}"
    text += f", {chosen.cuts} cuts, {chosen.switches} switches, {chosen.changes} crop changes"
    return f"{text}, {len(chosen.zooms)} zooms" if zoom_on else text


def _crop_json(crop: CropBox) -> ClipCropBox:
    return ClipCropBox(w=crop.w, h=crop.h, x=crop.x, y=crop.y)


@dataclass(frozen=True)
class Inputs:
    """What the render stage reads before it touches any file: shared by run() and the
    safe-zone preview."""

    fonts_dir: Path
    metrics: FontMetrics | None  # box_karaoke only
    kept: list[ScoredClip]
    video: VideoStreamInfo
    source: Path
    segments: list[TranscriptSegment] | None  # set in speaker mode
    track_plan: "TrackPlan | None"
    wordlists: WordLists | None = None  # None: captions.mask_profanity is off


def _load_wordlists(root: Path, app: AppConfig) -> WordLists | None:
    """The word lists when masking is on; a missing list file is an error, never a silent skip."""
    if not app.captions.mask_profanity:
        return None
    try:
        return load_wordlists(root)
    except WordListError as exc:
        raise RunError(f"{exc} (captions.mask_profanity is on)") from exc


def _load_font(root: Path, app: AppConfig) -> tuple[Path, FontMetrics | None]:
    """The font folder and, for box_karaoke, the font's metrics. A missing font is an error,
    never a silent fallback."""
    fonts_dir = root / FONTS_DIR
    if not (fonts_dir / app.captions.font_file).is_file():
        raise RunError(
            f"caption font not found: {FONTS_DIR.as_posix()}/{app.captions.font_file} "
            "(captions.font_file); the render stage does not fall back to another font"
        )

    metrics: FontMetrics | None = None
    if app.captions.style == "box_karaoke":  # the box is sized from the font file, so read it now
        try:
            metrics = load_font(fonts_dir / app.captions.font_file)
        except FontError as exc:
            raise RunError(
                f"caption font {app.captions.font_file} (captions.font_file) cannot be used: {exc}"
            ) from exc
    return fonts_dir, metrics


def _load_source(run_dir: Path) -> tuple[FetchMeta, VideoStreamInfo, Path]:
    meta = _load_meta(run_dir)
    video = meta.media.video
    if video is None:
        raise RunError(f"{META_FILE} has no video stream")
    source = run_dir / meta.source_file
    if not source.is_file():
        raise RunError(f"source video not found in the run folder: {meta.source_file}")
    return meta, video, source


def _load_speaker(
    run_dir: Path,
    root: Path,
    app: AppConfig,
    meta: FetchMeta,
    video: VideoStreamInfo,
    source: Path,
) -> tuple[list[TranscriptSegment], "TrackPlan"]:
    """The transcript and the track plan; checks both model files before anything is rendered."""
    from clipmachine import tracking  # tracking imports this module
    from clipmachine.utils import faces

    segments = _load_transcript(run_dir).segments
    try:
        track_plan = tracking.build_plan(
            run_dir,
            root,
            app,
            video=video,
            source=source,
            source_sha256=meta.sha256,
            segments=segments,
        )
    except faces.FacesError as exc:
        raise RunError(str(exc)) from exc
    return segments, track_plan


def _load_inputs(run_dir: Path, root: Path, app: AppConfig) -> Inputs:
    fonts_dir, metrics = _load_font(root, app)
    scored = _load_scored(run_dir)
    meta, video, source = _load_source(run_dir)

    kept = sorted((c for c in scored.clips if c.kept), key=lambda c: c.rank or 0)
    if any(not CLIP_ID_RE.fullmatch(c.id) for c in kept):
        raise RunError(f"{SCORED_JSON} has a clip id that is not a safe file name")

    # Speaker mode checks both model files before anything is deleted or rendered.
    track_plan: TrackPlan | None = None
    segments: list[TranscriptSegment] | None = None
    if app.video.crop == "speaker" and kept:
        segments, track_plan = _load_speaker(run_dir, root, app, meta, video, source)
    return Inputs(
        fonts_dir, metrics, kept, video, source, segments, track_plan, _load_wordlists(root, app)
    )


def load_source_inputs(run_dir: Path, root: Path, app: AppConfig) -> Inputs:
    """Everything _load_inputs reads except scored.json (campaign mode has no scored clips):
    `kept` is empty, and in speaker mode the transcript and the track plan are always loaded."""
    fonts_dir, metrics = _load_font(root, app)
    meta, video, source = _load_source(run_dir)
    track_plan: TrackPlan | None = None
    segments: list[TranscriptSegment] | None = None
    if app.video.crop == "speaker":
        segments, track_plan = _load_speaker(run_dir, root, app, meta, video, source)
    return Inputs(
        fonts_dir, metrics, [], video, source, segments, track_plan, _load_wordlists(root, app)
    )


def _make_plan(run_dir: Path, clips_dir: Path, app: AppConfig, inputs: Inputs) -> Plan:
    settings = _settings_of(app)
    video = inputs.video
    try:
        box = center_crop(
            video.width, video.height, video.rotation, settings.width, settings.height
        )
    except ValueError as exc:
        raise RunError(f"cannot crop the source: {exc}") from exc
    metrics = inputs.metrics
    segments = (
        inputs.segments if inputs.segments is not None else _load_transcript(run_dir).segments
    )
    zoom = app.video.zoom if app.video.zoom.enabled else None
    sentences = None
    if zoom is not None and inputs.track_plan is not None:
        sentences = build_index(
            segments,
            app.candidates.fallback_max_words,
            min_pause=app.snap.min_pause_seconds,
            min_cut_gap=app.snap.min_cut_gap,
        )
    return Plan(
        run_dir=run_dir,
        clips_dir=clips_dir,
        source=inputs.source,
        video=video,
        crop=box,
        settings=settings,
        fonts_rel=_fonts_relative(run_dir, inputs.fonts_dir),
        segments=segments,
        speaker=inputs.track_plan,
        preview=app.speaker.debug_preview,
        metrics=metrics,
        wordlists=inputs.wordlists,
        zoom=zoom,
        sentences=sentences,
        karaoke=None if metrics is None else KaraokeSettings.from_config(app.captions),
        safe=None
        if metrics is None
        else safe_rect(
            settings.width,
            settings.height,
            settings.safe_top,
            settings.safe_bottom,
            settings.safe_left,
            settings.safe_right,
        ),
    )


def run(ctx: RunContext) -> None:
    run_dir = ctx.run_dir.resolve()
    root = (ctx.project_root if ctx.project_root is not None else REPO_ROOT).resolve()
    app = ctx.config.app

    inputs = _load_inputs(run_dir, root, app)
    kept = inputs.kept
    clips_dir = run_dir / CLIPS_DIR
    _sweep(clips_dir, {c.id for c in kept}, everything=not ctx.resuming)
    if not kept:
        log.info("render: 0 kept clips, nothing to render")
        return
    clips_dir.mkdir(exist_ok=True)
    plan = _make_plan(run_dir, clips_dir, app, inputs)

    rendered = skipped = 0
    for clip in kept:
        if ctx.resuming and _already_rendered(plan, clip):
            skipped += 1
            log.info(f"render: {clip.id} already rendered, kept")
            continue
        _render_clip(plan, clip)
        rendered += 1
    log.info(f"render: {rendered} rendered, {skipped} already done")


SAFE_ZONE_DIR = Path("runs") / "_scratch" / "safe-zones"


def render_safe_zone_preview(run_dir: Path, root: Path, app: AppConfig, clip_id: str) -> Path:
    """Render one kept clip with the safe-zone margins drawn on it, for checking placement by eye.

    Everything is written to <root>/runs/_scratch/safe-zones/<run folder name>/ (<id>.mp4, <id>.ass
    and, when the crop moves, <id>.crop.cmd) and nowhere else: the run folder is only read, so no
    sweep, no clip json and no analysis. In speaker mode the clip needs a valid track from an
    earlier render. ffmpeg still runs with the run folder as its working directory. Returns the
    path of the mp4.
    """
    run_dir = run_dir.resolve()
    root = root.resolve()
    if run_dir.parent != (root / "runs").resolve():
        raise RunError(f"{run_dir} is not a run folder inside {root / 'runs'}")
    scratch = (root / "runs" / "_scratch").resolve()
    out_dir = (root / SAFE_ZONE_DIR / run_dir.name).resolve()
    if not out_dir.is_relative_to(scratch):
        raise RunError(f"refusing to write outside {scratch}")
    if not CLIP_ID_RE.fullmatch(clip_id):
        raise RunError(f"{clip_id!r} is not a clip id")

    if app.video.crop == "speaker" and not (run_dir / CLIPS_DIR).is_dir():
        # build_plan would create clips/; this command must not write in the run folder
        raise RunError(f"no valid track for {clip_id}; render the run first")
    inputs = _load_inputs(run_dir, root, app)
    clip = next((c for c in inputs.kept if c.id == clip_id), None)
    if clip is None:
        raise RunError(f"{clip_id} is not a kept clip of this run")
    plan = _make_plan(run_dir, run_dir / CLIPS_DIR, app, inputs)
    settings = plan.settings
    duration = clip.end - clip.start

    chosen: SpeakerCrop | None = None
    track: TrackFile | None = None
    if plan.speaker is not None:
        from clipmachine import tracking  # tracking imports this module

        track = tracking.load_plan_track(
            plan.speaker, clip.id, clip.start, clip.end, crop_w=plan.crop.w
        )
        if track is None:
            raise RunError(f"no valid track for {clip.id}; render the run first")
    out_dir.mkdir(parents=True, exist_ok=True)
    if track is not None:
        chosen = _crop_from_track(plan, clip, track, True, out_dir)
    crop = plan.crop if chosen is None else chosen.crop

    words = clip_words(plan.segments, clip.start, clip.end)
    if settings.caption_style == "legacy":
        captions = _legacy_captions(plan, words, duration)
    else:
        captions = _karaoke_captions(plan, clip, words, chosen, duration)
    ass = out_dir / f"{clip.id}.ass"
    write_text_atomic(ass, captions.text)

    vf = ffmpeg.video_filter(
        crop,
        settings.width,
        settings.height,
        _relative(run_dir, ass),
        plan.fonts_rel,
        None if chosen is None else chosen.sendcmd_rel,
        ffmpeg.safe_zone_lines(
            settings.width,
            settings.height,
            settings.safe_top,
            settings.safe_bottom,
            settings.safe_left,
            settings.safe_right,
            column_left=app.safe.button_left,
            column_top=app.safe.button_top,
        ),
    )
    temp = out_dir / f"{clip.id}{TEMP_SUFFIX}"
    mp4 = out_dir / f"{clip.id}.mp4"
    args = ffmpeg.render_args(
        plan.source,
        temp,
        start=clip.start,
        duration=duration,
        vf=vf,
        crf=settings.crf,
        preset=settings.preset,
        audio_bitrate=settings.audio_bitrate,
        audio_fade=settings.audio_fade_seconds,
    )
    try:
        ffmpeg.render_clip(args, run_dir, duration)
        ffmpeg.verify_clip(
            ffmpeg.probe(temp), width=settings.width, height=settings.height, duration=duration
        )
        os.replace(temp, mp4)
    except ffmpeg.MediaError as exc:
        raise RunError(f"render failed for {clip.id}: {exc}") from exc
    finally:
        temp.unlink(missing_ok=True)
    return mp4
