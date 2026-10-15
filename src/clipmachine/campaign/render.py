"""Render one campaign moment (Brief 11): snap, check, caption, overlay the watermark, write the
post text and the clip's json.

The render stage's own helpers do the captions, the crop and the ffmpeg run; this module adds what
only campaign mode needs: times chosen by a person (snapped to clean edges, or an end picked
automatically), spelling fixes, the watermark on top, the post text and the checks.

A moment fails alone: the others still render. Every failure removes that moment's earlier
outputs, so nothing stale can be posted, and writes a json that says why. The json is deleted
first and written last. Reasons, notes and console lines carry numbers and codes only, never the
hook, a caption or a name.
"""

import contextlib
import os
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from clipmachine.campaign.loader import Campaign
from clipmachine.campaign.models import (
    CHECKS,
    CampaignClip,
    CampaignClipWatermark,
    Moment,
    SpellingRule,
    UsedEntry,
    UsedLog,
    moment_fingerprint,
)
from clipmachine.campaign.post import TITLE_NOTE, post_text, title_left_out_words
from clipmachine.campaign.spelling import apply_spelling, find_names
from clipmachine.campaign.used import (
    is_rejected,
    overlapping_posted,
    posted_entry,
    record_render,
    take_closer,
)
from clipmachine.models import AppConfig, GraphicsBox, RenderedOutput
from clipmachine.runs import RunError, write_json_atomic, write_text_atomic
from clipmachine.stages import render as stage
from clipmachine.utils import ffmpeg
from clipmachine.utils.captions import clip_words
from clipmachine.utils.image import color_type_name
from clipmachine.utils.snapping import EPSILON, SentenceIndex, auto_end, snap_near
from clipmachine.utils.wordlists import Hit, sensitive_hits

Check = Literal["pass", "warn", "fail", "n/a"]  # n/a: the campaign does not require it
# rejected: Brief 12; unchanged: Brief 12.2 (`produce` keeps a clip whose entry did not change)
Outcome = Literal["rendered", "failed", "refused", "rejected", "unchanged"]

# The clip length is compared with this much slack, as the render stage's checks are.
LENGTH_SLACK = 0.001
REVIEW_SUFFIX = ".review.txt"  # masked and sensitive words, for the report and Igor; local only
META_DIR = "_meta"  # Brief 11.2: everything but the mp4 and the post text lives here
POST_SUFFIX = ".txt"  # the paste-ready post text, next to the mp4 in the platform folder
OPENING_SUFFIX = ".opening"  # <clip>.opening.track.json: the track before a short opening shot


class MomentFailed(Exception):
    """A moment that cannot be rendered, with a reason fit to show: numbers and codes only.
    `check` names the check that failed, when there is one."""

    def __init__(self, reason: str, check: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.check = check


@dataclass(frozen=True)
class MomentContext:
    """What every moment of one command shares."""

    root: Path
    run_dir: Path
    out_dir: Path  # runs/<run>/campaign/<slug>: the platform folders, _meta, report and plan
    plan: stage.Plan
    app: AppConfig
    campaign: Campaign
    units: SentenceIndex
    duration: float  # of the source audio
    source_sha256: str
    # Brief 11.1: the campaign's rules plus the automatic ones; the variant texts to time; the
    # names listed for a visual check (names_to_check plus names)
    spelling_rules: tuple[SpellingRule, ...] = ()
    variant_texts: tuple[str, ...] = ()
    names_to_check: tuple[str, ...] = ()
    # Brief 11.2: words a YouTube title keeps as written (names, acronyms)
    title_keep: tuple[str, ...] = ()
    source_name: str = ""  # the source video's file name as fetched (Brief 12)

    @property
    def meta_dir(self) -> Path:
        return self.out_dir / META_DIR

    def platform_dir(self, platform: str) -> Path:
        return self.out_dir / platform

    def mp4_path(self, moment: Moment) -> Path:
        return self.platform_dir(moment.platform) / f"{moment.id}.mp4"

    def post_path(self, moment: Moment) -> Path:
        return self.platform_dir(moment.platform) / f"{moment.id}{POST_SUFFIX}"


@dataclass(frozen=True)
class MomentClipObject:
    """The clip as the render stage's helpers see it (they need only these four things)."""

    id: str
    start: float
    end: float
    hook_line: str


@dataclass(frozen=True)
class MomentResult:
    outcome: Outcome
    used: UsedLog
    clip: CampaignClip | None = None
    reason: str | None = None
    start_moved: float | None = None
    end_moved: float | None = None
    closer_number: int | None = None  # 1-based position in the campaign's closers
    variant_times: dict[str, list[float]] | None = None  # name variants heard: clip seconds


@dataclass
class _Work:
    """What is known about a moment while it is being rendered; feeds the json, also on failure."""

    moment: Moment
    start: float | None = None
    end: float | None = None
    auto_target: float | None = None
    snap_notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    spelling: int = 0
    names: dict[str, list[float]] = field(default_factory=dict)
    checks: dict[str, Check] = field(default_factory=dict)
    closer: str | None = None
    review: list[str] = field(default_factory=list)  # lines of <clip>.review.txt
    masked: int = 0
    sensitive: int = 0
    zoom_count: int = 0
    zoom_spans: list[tuple[float, float]] = field(default_factory=list)
    zoom_ramps: list[tuple[float, float]] = field(default_factory=list)
    # Brief 12.1
    fit_spans: list[tuple[float, float]] = field(default_factory=list)
    fit_seconds: float = 0.0
    static_overlays: list[GraphicsBox] = field(default_factory=list)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _outputs(ctx: MomentContext, moment: Moment) -> list[Path]:
    """Everything a render of this moment writes, apart from its json."""
    name = moment.clip_name
    meta = ctx.meta_dir
    return [
        ctx.mp4_path(moment),
        ctx.post_path(moment),
        meta / f"{name}.ass",
        meta / f"{name}{stage.CROP_CMD_SUFFIX}",
        meta / f"{name}{stage.TEMP_SUFFIX}",
        meta / f"{name}{REVIEW_SUFFIX}",
    ]


def review_line(what: str, where: str, hit: Hit) -> str:
    """One tab-separated line of <clip>.review.txt: what (masked | sensitive), where (captions |
    hook | post), the word as written (a slur is never written, a dash stands in) and the kind."""
    word = "-" if hit.kind == "slur" else hit.word
    return "\t".join((what, where, word, hit.kind))


def _review(work: _Work, what: str, where: str, hits: Sequence[Hit]) -> None:
    work.review.extend(review_line(what, where, hit) for hit in hits)


def _clip_json(
    ctx: MomentContext,
    work: _Work,
    status: Literal["rendered", "failed"],
    *,
    error: str | None = None,
    crop_mode: Literal["center", "speaker"] | None = None,
    output: RenderedOutput | None = None,
    render_seconds: float | None = None,
    rendered_at: str | None = None,
) -> CampaignClip:
    wm = ctx.campaign.watermark  # None: no watermark required (Brief 12)
    moment = work.moment
    known = work.start is not None and work.end is not None
    watermark = None
    if wm is not None:
        watermark = CampaignClipWatermark(
            sha256=wm.sha256,
            blend=wm.blend,
            position_mode="auto" if ctx.campaign.auto is not None else "explicit",
            x=wm.x,
            y=wm.y,
            width=wm.size[0],
            height=wm.size[1],
            rect=(wm.rect.x1, wm.rect.y1, wm.rect.x2, wm.rect.y2),
            background=wm.background,
            color_type=color_type_name(wm.info.color_type),
        )
    return CampaignClip(
        id=moment.id,
        platform=moment.platform,
        status=status,
        error=error,
        requested_start=moment.start,
        requested_end=moment.end,
        start=work.start,
        end=work.end,
        duration=round(work.end - work.start, 3)
        if known and work.start is not None and work.end is not None
        else None,
        end_mode="given" if moment.end is not None else "auto",
        auto_end_target=work.auto_target,
        snap_notes=work.snap_notes,
        warnings=work.warnings,
        notes=work.notes,
        spelling_replacements=work.spelling,
        names_to_check=work.names,
        masked_words=work.masked,
        sensitive_words=work.sensitive,
        zoom_count=work.zoom_count,
        zoom_spans=work.zoom_spans,
        zoom_ramps=work.zoom_ramps,
        fit_spans=work.fit_spans,
        fit_seconds=work.fit_seconds,
        static_overlays=work.static_overlays,
        checks={name: work.checks[name] for name in CHECKS if name in work.checks},
        closer=work.closer,
        hook_mode=ctx.campaign.config.hook.mode,
        hook_font_size=ctx.campaign.hook_font_size,
        hook_position=ctx.campaign.hook_position,
        watermark=watermark,
        settings=ctx.plan.settings if status == "rendered" else None,
        crop_mode=crop_mode,
        output=output,
        render_seconds=render_seconds,
        rendered_at=rendered_at,
        source_sha256=ctx.source_sha256,
        api_calls=0,
    )


def _choose_times(
    ctx: MomentContext, work: _Work
) -> tuple[float, float, float | None, float | None]:
    """Snap the start (and the end, or pick it): (start, end, start_moved, end_moved)."""
    app, moment = ctx.app, work.moment
    length_range = ctx.campaign.config.clip_length
    pads = {"start_pad": app.snap.max_start_pad_seconds, "end_pad": app.snap.max_end_pad_seconds}
    near = snap_near(
        ctx.units,
        moment.start,
        moment.end,
        duration=ctx.duration,
        max_move=app.campaign.max_snap_seconds,
        **pads,
    )
    work.snap_notes.extend(near.notes)
    start, end, end_moved = near.start, near.end, near.end_moved
    work.start = start
    if end is None:  # `end: auto`: the clean end nearest to the target length
        target = app.campaign.auto_end_target_s
        work.auto_target = target
        chosen = auto_end(
            ctx.units,
            start,
            target=target,
            min_length=length_range.min,
            max_length=length_range.max,
            duration=ctx.duration,
            **pads,
        )
        if chosen is None:
            raise MomentFailed(
                f"no clean sentence end gives a clip of {length_range.min:g}-{length_range.max:g} "
                "s from this start",
                "length_in_range",
            )
        end = chosen.end
        work.snap_notes.append(
            f"end chosen automatically: target {target:g} s, actual {chosen.length:.2f} s"
        )
    work.end = end
    return start, end, near.start_moved, end_moved


def _check_times(ctx: MomentContext, start: float, end: float) -> float:
    if start >= ctx.duration or end > ctx.duration + EPSILON:
        raise MomentFailed("the moment is outside the source", "length_in_range")
    length = end - start
    cl = ctx.campaign.config.clip_length
    if not (cl.min - LENGTH_SLACK <= length <= cl.max + LENGTH_SLACK):
        raise MomentFailed(
            f"{length:.1f} s is outside the allowed {cl.min:g}-{cl.max:g} s", "length_in_range"
        )
    return length


def _skip_opening_shot(ctx: MomentContext, work: _Work, start: float, end: float) -> float:
    """The clip's start, moved to a camera cut that comes within campaign.skip_opening_shot_s
    with no word starting before it (the last such cut, so two flashes both go): a flash of
    another shot (often the guest) is not what the viewer should see first. Unchanged when the
    move would take the clip outside its length range or there is no tracker (centre-crop mode).

    The track at the original start is kept as <clip>.opening.track.json when the start moves,
    so the next render reuses it and the moved clip's own track alike."""
    from clipmachine import tracking  # tracking imports the render stage
    from clipmachine.utils import faces

    plan = ctx.plan
    limit = ctx.app.campaign.skip_opening_shot_s
    if plan.speaker is None or limit <= 0:
        return start
    name = work.moment.clip_name
    kept = f"{name}{OPENING_SUFFIX}"
    try:
        track = tracking.load_plan_track(plan.speaker, kept, start, end, crop_w=plan.crop.w)
        cached = track is not None
        if track is None:
            track, _ = tracking.track_for_clip(
                plan.speaker, name, start, end, preview=plan.preview, crop_w=plan.crop.w
            )
    except (faces.FacesError, ffmpeg.MediaError):
        return start  # the crop below reports a tracking failure properly
    words = clip_words(plan.segments, start, end)
    first_word = min((w.start for w in words), default=end - start)
    cuts = [c for c in track.cuts_exact if 0 < c <= min(limit, first_word)]
    if not cuts:
        return start  # no early cut, or speech begins before it (cutting would clip a word)
    cut = max(cuts)
    moved = round(start + cut, 3)
    cl = ctx.campaign.config.clip_length
    if not cl.min - LENGTH_SLACK <= end - moved <= cl.max + LENGTH_SLACK:
        return start
    if not cached:
        clips_dir = plan.speaker.clips_dir
        # a cache only: without it the next render analyses the original start again
        with contextlib.suppress(OSError):
            shutil.copyfile(
                tracking.track_path(clips_dir, name), tracking.track_path(clips_dir, kept)
            )
    work.snap_notes.append(
        f"start moved +{cut:.2f} s to a camera cut (a short opening shot skipped)"
    )
    work.start = moved
    return moved


def _check_sensitive(ctx: MomentContext, moment: Moment, work: _Work) -> None:
    """Flag words and slurs in the hook and the post caption: a note and review lines; with
    hook.sensitive: fail, a hook hit fails the moment. The hook is never rewritten."""
    lists = ctx.plan.wordlists
    if lists is None:
        return
    hook_hits = sensitive_hits(moment.hook, lists)
    caption_hits = sensitive_hits(moment.caption, lists)
    _review(work, "sensitive", "hook", hook_hits)
    _review(work, "sensitive", "post", caption_hits)
    work.sensitive = len(hook_hits) + len(caption_hits)
    if hook_hits:
        work.notes.append("hook_sensitive")
    if caption_hits:
        work.notes.append("caption_sensitive")
    if hook_hits and ctx.campaign.config.hook.sensitive == "fail":
        raise MomentFailed(
            f"the hook has {len(hook_hits)} sensitive word(s) (hook.sensitive: fail); reword it, "
            "see the report",
            "hook_from_start",
        )


def _render_moment(
    ctx: MomentContext, moment: Moment, used: UsedLog, was_posted: bool, work: _Work
) -> MomentResult:
    name = moment.clip_name
    plan, app, campaign = ctx.plan, ctx.app, ctx.campaign
    wm = campaign.placed if campaign.has_watermark else None  # Brief 12: optional
    settings = plan.settings
    started = time.perf_counter()

    start, end, start_moved, end_moved = _choose_times(ctx, work)
    _check_times(ctx, start, end)
    opening = _skip_opening_shot(ctx, work, start, end)
    if opening != start:
        start_moved = round((start_moved or 0.0) + opening - start, 3)
        start = opening
    length = _check_times(ctx, start, end)
    work.checks["length_in_range"] = "pass"

    for entry in overlapping_posted(used, start, end, ctx.source_sha256):
        work.warnings.append(f"overlaps a posted moment ({entry.id} {entry.platform})")

    words = clip_words(plan.segments, start, end)
    if not words:
        raise MomentFailed("no words in this stretch, so there would be no captions", "captions_on")
    variant_times = find_names(words, ctx.variant_texts)  # before the rules change them
    words, work.spelling = apply_spelling(words, ctx.spelling_rules)
    work.names = find_names(words, ctx.names_to_check)
    _check_sensitive(ctx, moment, work)

    clip = MomentClipObject(name, start, end, moment.hook)
    chosen = stage._speaker_crop(plan, clip) if plan.speaker is not None else None
    if chosen is not None:
        work.notes.extend(chosen.zoom_notes)
        work.zoom_count = len(chosen.zooms)
        work.zoom_spans = [(round(z.start, 3), round(z.end, 3)) for z in chosen.zooms]
        work.zoom_ramps = [(round(r.start, 3), round(r.end, 3)) for r in chosen.ramps]
        if chosen.fit_spans:  # Brief 12.1
            work.notes.append("graphics_fit")
        work.fit_spans = list(chosen.fit_spans)
        work.fit_seconds = stage.fit_seconds(chosen.fit_spans)
        work.static_overlays = list(chosen.static)
    elif plan.zoom is not None:  # centre-crop mode has no followed face
        work.notes.append("zoom_no_track")
    hook_cfg = campaign.config.hook
    if hook_cfg.mode == "full":
        seconds, fade = length, 0
    else:  # intro: the first seconds only; faces are checked over that window
        seconds, fade = campaign.hook_seconds, app.hook.fade_ms
    avoid = () if wm is None else (wm.rect,)
    spec = stage.HookSpec(
        seconds, fade, campaign.hook_font_size, avoid, position=campaign.hook_position
    )
    captions = stage._karaoke_captions(plan, clip, words, chosen, length, spec)
    work.notes.extend(captions.notes)
    _review(work, "masked", "captions", captions.masked)
    _review(work, "masked", "hook", captions.hook_masked)
    work.masked = len(captions.masked) + len(captions.hook_masked)

    if not moment.hook:
        raise MomentFailed("no hook in moments.yaml; write one", "hook_from_start")
    if "hook_missing" in captions.notes or "hook_truncated" in captions.notes:
        raise MomentFailed(
            f"the hook does not fit at font size {campaign.hook_font_size}; shorten it",
            "hook_from_start",
        )
    band = captions.hook_band
    if band is None or not captions.hook_shown:
        raise MomentFailed("the hook could not be placed", "hook_from_start")
    if wm is not None and band.intersects(wm.rect):
        raise MomentFailed("the hook overlaps the watermark", "watermark_full_and_clear")
    if wm is not None and any(box.intersects(wm.rect) for box in captions.caption_boxes):
        raise MomentFailed(
            "a caption would cover the watermark; move the watermark", "watermark_full_and_clear"
        )
    if any(box.intersects(band) for box in captions.caption_boxes):
        raise MomentFailed(
            "the hook does not fit between the watermark and the captions", "hook_from_start"
        )
    if captions.outside_safe_zone:
        raise MomentFailed(
            "the hook or a caption would sit outside the safe area", "hook_from_start"
        )
    if wm is not None:
        faces = stage._face_boxes(plan, chosen, length)
        if faces is None:
            work.notes.append("watermark_faces_unknown")
        elif any(face.intersects(wm.rect) for face in faces):
            work.notes.append("watermark_over_face")

    ass = ctx.meta_dir / f"{name}.ass"
    write_text_atomic(ass, captions.text)
    crop = plan.crop if chosen is None else chosen.crop
    fit = chosen is not None and bool(chosen.fit_spans)  # Brief 12.1: a graph, not a chain
    if chosen is not None and fit:
        chain = stage._fit_graph(plan, crop, stage._relative(ctx.run_dir, ass), chosen)
    else:
        chain = ffmpeg.video_filter(
            crop,
            settings.width,
            settings.height,
            stage._relative(ctx.run_dir, ass),
            plan.fonts_rel,
            None if chosen is None else chosen.sendcmd_rel,
        )
    temp = ctx.meta_dir / f"{name}{stage.TEMP_SUFFIX}"
    mp4 = ctx.mp4_path(moment)
    graph: str | None = f"{chain}[v]" if fit else None
    if wm is not None:
        graph = ffmpeg.overlay_graph(
            chain,
            x=wm.x,
            y=wm.y,
            size=None if wm.size == (wm.info.width, wm.info.height) else wm.size,
            blend=wm.blend,
            frame=(settings.width, settings.height),
        )
    # no watermark: the plain filter chain and one input; else the overlay graph and the PNG;
    # a fit layout is always a graph (one input without a watermark)
    args = ffmpeg.render_args(
        plan.source,
        temp,
        start=start,
        duration=length,
        vf=chain if graph is None else None,
        filter_complex=graph,
        overlay_image=None if wm is None else wm.file,
        crf=settings.crf,
        preset=settings.preset,
        audio_bitrate=settings.audio_bitrate,
        audio_fade=settings.audio_fade_seconds,
    )
    ffmpeg.assert_source_audio_only(args)
    try:
        ffmpeg.render_clip(args, ctx.run_dir, length)
        info = ffmpeg.probe(temp)
        ffmpeg.verify_clip(info, width=settings.width, height=settings.height, duration=length)
        os.replace(temp, mp4)
    finally:
        temp.unlink(missing_ok=True)
    video, audio = info.video, info.audio
    if video is None or audio is None or info.duration_seconds is None:
        raise MomentFailed("the output has no video, audio or duration")

    closers = campaign.config.post.closers
    closer: str | None = None
    if closers:  # Brief 12: a campaign may ask for no closer line
        platforms = campaign.config.platforms
        first = platforms.index(moment.platform) if moment.platform in platforms else 0
        closer, used = take_closer(used, closers, moment.platform, first=first % len(closers))
    work.closer = closer
    post_path = ctx.post_path(moment)
    text, post_hits = post_text(
        campaign.config, moment, closer, plan.wordlists, keep=ctx.title_keep
    )
    write_text_atomic(post_path, text)
    _review(work, "masked", "post", post_hits)
    if moment.platform == "youtube" and title_left_out_words(moment.hook, plan.wordlists):
        work.notes.append(TITLE_NOTE)  # the title differs from the hook: Igor checks it reads
    work.masked += len(post_hits)
    if work.review:
        write_text_atomic(ctx.meta_dir / f"{name}{REVIEW_SUFFIX}", "\n".join(work.review) + "\n")
    rendered_at = _now()
    used = record_render(
        used,
        UsedEntry(
            id=moment.id,
            platform=moment.platform,
            start=start,
            end=end,
            hook=moment.hook,
            closer=closer,
            rendered_at=rendered_at,
            output=mp4.relative_to(ctx.root).as_posix(),
            source_sha256=ctx.source_sha256,
            source_name=ctx.source_name or None,
            spec_sha256=moment_fingerprint(moment, ctx.source_sha256),
        ),
    )

    last_line = post_path.read_text(encoding="utf-8").rstrip("\n").split("\n")[-1]
    reused = bool(work.warnings) or was_posted
    closer_check: Check = "n/a" if closer is None else ("pass" if last_line == closer else "fail")
    work.checks.update(
        {
            "hook_from_start": "pass",
            "captions_on": "pass" if captions.lines >= 1 else "fail",
            "watermark_full_and_clear": "n/a" if wm is None else "pass",
            "source_audio_only": "pass",
            "closer_present": closer_check,
            "not_previously_used": "warn" if reused else "pass",
            "names_listed": "pass" if set(work.names) == set(ctx.names_to_check) else "fail",
        }
    )
    seconds_taken = round(time.perf_counter() - started, 2)
    record = _clip_json(
        ctx,
        work,
        "rendered",
        crop_mode="center" if chosen is None else "speaker",
        output=RenderedOutput(
            width=video.width,
            height=video.height,
            video_codec=video.codec,
            audio_codec=audio.codec,
            duration_seconds=info.duration_seconds,
            size_bytes=mp4.stat().st_size,
        ),
        render_seconds=seconds_taken,
        rendered_at=rendered_at,
    )
    write_json_atomic(ctx.meta_dir / f"{name}.json", record.model_dump(mode="json"))
    return MomentResult(
        "rendered",
        used,
        record,
        start_moved=start_moved,
        end_moved=end_moved,
        closer_number=None if closer is None else closers.index(closer) + 1,
        variant_times=variant_times,
    )


def is_unchanged(ctx: MomentContext, moment: Moment, used: UsedLog) -> bool:
    """True when the latest unposted render of this moment on its platform was made from the
    same entry and source (its fingerprint) and its files are still there (Brief 12.2)."""
    latest = next(
        (
            e
            for e in reversed(used.entries)
            if e.id == moment.id and e.platform == moment.platform and not e.posted_at
        ),
        None,
    )
    if latest is None or latest.spec_sha256 != moment_fingerprint(moment, ctx.source_sha256):
        return False
    if not (ctx.mp4_path(moment).is_file() and ctx.post_path(moment).is_file()):
        return False
    meta = ctx.meta_dir / f"{moment.clip_name}.json"
    try:
        record = CampaignClip.model_validate_json(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return False
    return record.status == "rendered"


def render_moment(
    ctx: MomentContext,
    moment: Moment,
    used: UsedLog,
    *,
    force_reuse: bool,
    skip_unchanged: bool = False,
) -> MomentResult:
    """Render one moment. Returns the outcome with the (possibly updated) used log.
    `skip_unchanged`: keep a clip whose entry and source did not change (outcome unchanged)."""
    name = moment.clip_name
    if is_rejected(used, moment.id):
        return MomentResult("rejected", used)  # Igor said no (Brief 12); nothing is touched
    was_posted = posted_entry(used, moment.id, moment.platform) is not None
    if was_posted and not force_reuse:
        return MomentResult("refused", used)  # nothing of this moment is touched
    if skip_unchanged and not was_posted and is_unchanged(ctx, moment, used):
        return MomentResult("unchanged", used)
    ctx.meta_dir.mkdir(parents=True, exist_ok=True)
    ctx.platform_dir(moment.platform).mkdir(parents=True, exist_ok=True)
    (ctx.meta_dir / f"{name}.json").unlink(missing_ok=True)  # it must never describe a stale clip
    work = _Work(moment)
    try:
        return _render_moment(ctx, moment, used, was_posted, work)
    except (MomentFailed, RunError, ffmpeg.MediaError) as exc:
        reason = exc.reason if isinstance(exc, MomentFailed) else str(exc)
        if isinstance(exc, MomentFailed) and exc.check is not None:
            work.checks[exc.check] = "fail"
        for path in _outputs(ctx, moment):
            path.unlink(missing_ok=True)
        record = _clip_json(ctx, work, "failed", error=reason)
        write_json_atomic(ctx.meta_dir / f"{name}.json", record.model_dump(mode="json"))
        return MomentResult("failed", used, record, reason=reason)
