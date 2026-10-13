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

import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from clipmachine.campaign.loader import Campaign
from clipmachine.campaign.models import (
    CHECKS,
    CampaignClip,
    CampaignClipWatermark,
    Moment,
    UsedEntry,
    UsedLog,
)
from clipmachine.campaign.spelling import apply_spelling, find_names
from clipmachine.campaign.used import overlapping_posted, posted_entry, record_render, take_closer
from clipmachine.models import AppConfig, RenderedOutput
from clipmachine.runs import RunError, write_json_atomic, write_text_atomic
from clipmachine.stages import render as stage
from clipmachine.utils import ffmpeg
from clipmachine.utils.captions import clip_words
from clipmachine.utils.image import color_type_name
from clipmachine.utils.snapping import EPSILON, SentenceIndex, auto_end, snap_near

Check = Literal["pass", "warn", "fail"]
Outcome = Literal["rendered", "failed", "refused"]

# The clip length is compared with this much slack, as the render stage's checks are.
LENGTH_SLACK = 0.001


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
    out_dir: Path
    plan: stage.Plan
    app: AppConfig
    campaign: Campaign
    units: SentenceIndex
    duration: float  # of the source audio
    source_sha256: str


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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _outputs(ctx: MomentContext, name: str) -> list[Path]:
    out = ctx.out_dir
    return [
        out / f"{name}.mp4",
        out / f"{name}.post.txt",
        out / f"{name}.ass",
        out / f"{name}{stage.CROP_CMD_SUFFIX}",
        out / f"{name}{stage.TEMP_SUFFIX}",
    ]


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
    wm = ctx.campaign.watermark
    moment = work.moment
    known = work.start is not None and work.end is not None
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
        checks={name: work.checks[name] for name in CHECKS if name in work.checks},
        closer=work.closer,
        hook_mode=ctx.campaign.config.hook.mode,
        hook_font_size=ctx.campaign.hook_font_size,
        hook_position=ctx.campaign.hook_position,
        watermark=CampaignClipWatermark(
            sha256=wm.sha256,
            blend=wm.blend,
            x=wm.x,
            y=wm.y,
            width=wm.size[0],
            height=wm.size[1],
            rect=(wm.rect.x1, wm.rect.y1, wm.rect.x2, wm.rect.y2),
            background=wm.background,
            color_type=color_type_name(wm.info.color_type),
        ),
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


def _post_text(ctx: MomentContext, moment: Moment, closer: str) -> str:
    post = ctx.campaign.config.post
    first = " ".join(
        part for part in (post.prefix.get(moment.platform, ""), moment.caption) if part
    )
    lines = [first]
    if post.hashtags:
        lines.append(" ".join(post.hashtags))
    lines.append(closer)
    return "\n".join(lines) + "\n"


def _render_moment(
    ctx: MomentContext, moment: Moment, used: UsedLog, was_posted: bool, work: _Work
) -> MomentResult:
    name = moment.clip_name
    plan, app, campaign = ctx.plan, ctx.app, ctx.campaign
    wm = campaign.watermark
    settings = plan.settings
    started = time.perf_counter()

    start, end, start_moved, end_moved = _choose_times(ctx, work)
    length = _check_times(ctx, start, end)
    work.checks["length_in_range"] = "pass"

    for entry in overlapping_posted(used, start, end, ctx.source_sha256):
        work.warnings.append(f"overlaps a posted moment ({entry.id} {entry.platform})")

    words = clip_words(plan.segments, start, end)
    if not words:
        raise MomentFailed("no words in this stretch, so there would be no captions", "captions_on")
    words, work.spelling = apply_spelling(words, campaign.config.spelling)
    work.names = find_names(words, campaign.config.names_to_check)

    clip = MomentClipObject(name, start, end, moment.hook)
    chosen = stage._speaker_crop(plan, clip) if plan.speaker is not None else None
    hook_cfg = campaign.config.hook
    if hook_cfg.mode == "full":
        seconds, fade = length, 0
    else:  # intro: the first seconds only; faces are checked over that window
        seconds, fade = campaign.hook_seconds, app.hook.fade_ms
    spec = stage.HookSpec(
        seconds, fade, campaign.hook_font_size, (wm.rect,), position=campaign.hook_position
    )
    captions = stage._karaoke_captions(plan, clip, words, chosen, length, spec)
    work.notes.extend(captions.notes)

    if "hook_missing" in captions.notes or "hook_truncated" in captions.notes:
        raise MomentFailed(
            f"the hook does not fit at font size {campaign.hook_font_size}; shorten it",
            "hook_from_start",
        )
    band = captions.hook_band
    if band is None or not captions.hook_shown:
        raise MomentFailed("the hook could not be placed", "hook_from_start")
    if band.intersects(wm.rect):
        raise MomentFailed("the hook overlaps the watermark", "watermark_full_and_clear")
    if any(box.intersects(wm.rect) for box in captions.caption_boxes):
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
    faces = stage._face_boxes(plan, chosen, length)
    if faces is None:
        work.notes.append("watermark_faces_unknown")
    elif any(face.intersects(wm.rect) for face in faces):
        work.notes.append("watermark_over_face")

    ass = ctx.out_dir / f"{name}.ass"
    write_text_atomic(ass, captions.text)
    crop = plan.crop if chosen is None else chosen.crop
    chain = ffmpeg.video_filter(
        crop,
        settings.width,
        settings.height,
        stage._relative(ctx.run_dir, ass),
        plan.fonts_rel,
        None if chosen is None else chosen.sendcmd_rel,
    )
    graph = ffmpeg.overlay_graph(
        chain,
        x=wm.x,
        y=wm.y,
        size=None if wm.size == (wm.info.width, wm.info.height) else wm.size,
        blend=wm.blend,
        frame=(settings.width, settings.height),
    )
    temp = ctx.out_dir / f"{name}{stage.TEMP_SUFFIX}"
    mp4 = ctx.out_dir / f"{name}.mp4"
    args = ffmpeg.render_args(
        plan.source,
        temp,
        start=start,
        duration=length,
        filter_complex=graph,
        overlay_image=wm.file,
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
    closer, used = take_closer(used, closers)
    work.closer = closer
    post_path = ctx.out_dir / f"{name}.post.txt"
    write_text_atomic(post_path, _post_text(ctx, moment, closer))
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
        ),
    )

    last_line = post_path.read_text(encoding="utf-8").rstrip("\n").split("\n")[-1]
    reused = bool(work.warnings) or was_posted
    work.checks.update(
        {
            "hook_from_start": "pass",
            "captions_on": "pass" if captions.lines >= 1 else "fail",
            "watermark_full_and_clear": "pass",
            "source_audio_only": "pass",
            "closer_present": "pass" if last_line == closer else "fail",
            "not_previously_used": "warn" if reused else "pass",
            "names_listed": "pass"
            if set(work.names) == set(campaign.config.names_to_check)
            else "fail",
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
    write_json_atomic(ctx.out_dir / f"{name}.json", record.model_dump(mode="json"))
    return MomentResult(
        "rendered",
        used,
        record,
        start_moved=start_moved,
        end_moved=end_moved,
        closer_number=closers.index(closer) + 1,
    )


def render_moment(
    ctx: MomentContext, moment: Moment, used: UsedLog, *, force_reuse: bool
) -> MomentResult:
    """Render one moment. Returns the outcome with the (possibly updated) used log."""
    name = moment.clip_name
    was_posted = posted_entry(used, moment.id, moment.platform) is not None
    if was_posted and not force_reuse:
        return MomentResult("refused", used)  # nothing of this moment is touched
    (ctx.out_dir / f"{name}.json").unlink(missing_ok=True)  # it must never describe a stale clip
    work = _Work(moment)
    try:
        return _render_moment(ctx, moment, used, was_posted, work)
    except (MomentFailed, RunError, ffmpeg.MediaError) as exc:
        reason = exc.reason if isinstance(exc, MomentFailed) else str(exc)
        if isinstance(exc, MomentFailed) and exc.check is not None:
            work.checks[exc.check] = "fail"
        for path in _outputs(ctx, name):
            path.unlink(missing_ok=True)
        record = _clip_json(ctx, work, "failed", error=reason)
        write_json_atomic(ctx.out_dir / f"{name}.json", record.model_dump(mode="json"))
        return MomentResult("failed", used, record, reason=reason)
