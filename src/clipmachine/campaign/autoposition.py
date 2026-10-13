"""`watermark.position: auto` (Brief 11.1): choose where the watermark goes, once per campaign.

A fixed grid of centre positions is tried. A candidate is kept when the visible box passes the
geometry checks an explicit position must pass (frame, corner zones, safe area), stays clear of
the caption envelope (the widest, tallest caption box, so no clip's captions can ever reach it)
and, for every sampled clip, leaves room for the hook and does not touch it or its caption boxes
(the same checks the render makes). The survivors are scored over the first N moments being
rendered:

    score = face_weight * face_cost + light_weight * luminance / 255

TikTok's button column (safe.button_left and button_top, D-023) is a no-go zone too.

where face_cost is the share of the clip's face samples whose face boxes overlap the candidate
(from the track; 0 and a note when the crop is the centre crop, which has no track) and luminance
is the median grey of the source pixels behind the candidate on a few frames per clip, read
through the ffmpeg frame pipe into memory (nothing is written to disk). Darker is better because
a screen blend adds light. Ties go to the position closer to the captions (larger y), then to the
centre. The choice is locked in campaigns/<slug>/auto_position.json and reused while the
watermark file, its scale and blend, and the frame geometry stay the same.

Console lines carry numbers only; the moment ids sampled are the one identifier they hold.
"""

import dataclasses
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from clipmachine.campaign.loader import Campaign, _rect_at, place_watermark, placement_problem
from clipmachine.campaign.models import AutoCandidate, AutoPosition, Moment, UsedLog
from clipmachine.campaign.render import MomentContext, MomentFailed, _check_times, _choose_times
from clipmachine.campaign.render import _Work as Work
from clipmachine.campaign.spelling import apply_spelling
from clipmachine.campaign.used import posted_entry
from clipmachine.runs import RunError, write_json_atomic
from clipmachine.utils import ffmpeg
from clipmachine.utils.captions import (
    HOOK_CAPTION_GAP_PX,
    build_hook,
    build_karaoke,
    caption_top,
    clip_words,
)
from clipmachine.utils.crop import display_size
from clipmachine.utils.safezone import CAPTION_BOTTOM_GAP_PX, Rect, face_rects_timed, outside_count
from clipmachine.utils.wordlists import mask_text, mask_words

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

AUTO_POSITION_FILE = "auto_position.json"
LUMA_WIDTH = 480  # frames are decoded this wide for the luminance; enough for a median
# BT.601 weights for grey from RGB
_LUMA = (0.299, 0.587, 0.114)


@dataclass(frozen=True)
class Candidate:
    x_center: float
    y_center: float
    rect: Rect  # the visible box on the output frame


@dataclass(frozen=True)
class _Sample:
    """One moment as the scorer sees it."""

    name: str
    length: float
    hook: str  # as drawn (masked)
    hook_seconds: float
    caption_boxes: tuple[Rect, ...]
    faces: tuple[tuple[float, Rect], ...] | None  # (sample time, box); None: no track
    face_times: tuple[float, ...]  # distinct sample times with a face check, up to the length
    crop_times: tuple[float, ...]  # the track's samples: when the crop x changes
    crop_xs: tuple[int, ...]
    frames: tuple[tuple[float, "NDArray[np.float32]"], ...]  # (time, grey of the decoded frame)
    frame_scale: float  # decoded pixels per upright source pixel


@dataclass(frozen=True)
class _Score:
    candidate: Candidate
    face_cost: float
    luminance: float
    score: float


def candidates(ctx: MomentContext) -> tuple[list[Candidate], int]:
    """The grid positions whose visible box passes the geometry checks and stays clear of the
    caption envelope, and how many positions the grid had."""
    app, plan, asset = ctx.app, ctx.plan, ctx.campaign.asset
    karaoke, metrics, safe = plan.karaoke, plan.metrics, plan.safe
    assert karaoke is not None and metrics is not None and safe is not None
    width, height = app.video.width, app.video.height
    centre = (safe.x1 + safe.x2) / 2
    envelope = Rect(
        centre - karaoke.max_width_px / 2,
        caption_top(karaoke, metrics, safe) - HOOK_CAPTION_GAP_PX,
        centre + karaoke.max_width_px / 2,
        safe.y2 - CAPTION_BOTTOM_GAP_PX,
    )
    x_step, y_step = app.campaign.auto_watermark_x_step, app.campaign.auto_watermark_y_step
    xs = [0.5]
    k = 1
    while 0.5 - k * x_step >= 0 and 0.5 + k * x_step <= 1:
        xs.extend((round(0.5 - k * x_step, 6), round(0.5 + k * x_step, 6)))
        k += 1
    ys: list[float] = []
    y = safe.y1 / height
    while y <= safe.y2 / height:
        ys.append(round(y, 6))
        y += y_step
    column = Rect(
        float(app.safe.button_left), float(app.safe.button_top), float(width), float(height)
    )
    kept: list[Candidate] = []
    for y_center in ys:
        for x_center in xs:
            if placement_problem(asset, x_center, y_center, app) is not None:
                continue
            _, _, rect = _rect_at(asset, x_center, y_center, app)
            if rect.intersects(envelope) or rect.intersects(column):
                continue
            kept.append(Candidate(x_center, y_center, rect))
    return kept, len(xs) * len(ys)


def _grey(frame: "NDArray[np.uint8]") -> "NDArray[np.float32]":
    import numpy as np

    rgb = frame.astype(np.float32)
    out: NDArray[np.float32] = (
        rgb[..., 0] * _LUMA[0] + rgb[..., 1] * _LUMA[1] + rgb[..., 2] * _LUMA[2]
    )
    return out


def _read_frames(
    ctx: MomentContext, start: float, length: float
) -> list[tuple[float, "NDArray[np.float32]"]]:
    """A few frames of the clip, evenly spaced, as grey arrays with their clip-relative times."""
    plan, video = ctx.plan, ctx.plan.video
    count = ctx.app.campaign.auto_watermark_frames
    frame_w, frame_h = ffmpeg.frame_size_after_scale(
        video.width, video.height, video.rotation, LUMA_WIDTH
    )
    fps = count / length
    args = ffmpeg.frame_args(plan.source, start=start, duration=length, fps=fps, width=frame_w)
    frames: list[tuple[float, NDArray[np.float32]]] = []
    for k, frame in enumerate(
        ffmpeg.read_frames(
            args, frame_w, frame_h, ffmpeg.frames_timeout(length), scratch_dir=ctx.meta_dir
        )
    ):
        frames.append((k / fps, _grey(frame)))
        if len(frames) >= count:
            break
    return frames


def _sample(ctx: MomentContext, moment: Moment) -> _Sample | None:
    """The moment prepared for scoring; None when it could not be rendered anyway."""
    plan, campaign = ctx.plan, ctx.campaign
    karaoke, metrics, safe = plan.karaoke, plan.metrics, plan.safe
    assert karaoke is not None and metrics is not None and safe is not None
    work = Work(moment)
    try:
        start, end, _, _ = _choose_times(ctx, work)
        length = _check_times(ctx, start, end)
    except MomentFailed:
        return None
    words = clip_words(plan.segments, start, end)
    if not words:
        return None
    words, _ = apply_spelling(words, ctx.spelling_rules)
    hook = moment.hook
    if plan.wordlists is not None:
        words, _ = mask_words(words, plan.wordlists)
        hook, _ = mask_text(hook, plan.wordlists)
    built = build_karaoke(
        words,
        karaoke,
        metrics,
        safe,
        clip_seconds=length,
        offset_seconds=plan.settings.caption_offset_seconds,
    )
    hook_seconds = length if campaign.config.hook.mode == "full" else campaign.hook_seconds

    faces: tuple[tuple[float, Rect], ...] | None = None
    face_times: tuple[float, ...] = ()
    crop_times: tuple[float, ...] = ()
    crop_xs: tuple[int, ...] = (plan.crop.x,)
    if plan.speaker is not None:
        from clipmachine import tracking  # tracking imports the render stage
        from clipmachine.utils import faces as face_models

        try:
            track, _ = tracking.track_for_clip(
                plan.speaker, moment.clip_name, start, end, crop_w=plan.crop.w
            )
        except (face_models.FacesError, ffmpeg.MediaError) as exc:
            raise RunError(f"speaker tracking failed for {moment.clip_name}: {exc}") from exc
        source_w, source_h = display_size(plan.video.width, plan.video.height, plan.video.rotation)
        # No `boxes` here: this runs before any zoom is chosen, and the position score is a
        # heuristic, so it uses the fixed window and does not apply the punch-in zoom.
        faces = tuple(
            face_rects_timed(
                track.faces,
                track.samples,
                source_w=source_w,
                source_h=source_h,
                crop_w=plan.crop.w,
                crop_h=plan.crop.h,
                crop_y=plan.crop.y,
                out_w=plan.settings.width,
                out_h=plan.settings.height,
                until=length,
            )
        )
        face_times = tuple(sorted({s.t for s in track.samples if s.t <= length}))
        if track.fallback_reason is None and track.samples:
            ordered = sorted(track.samples, key=lambda s: s.t)
            crop_times = tuple(s.t for s in ordered)
            crop_xs = tuple(s.crop_x for s in ordered)
    source_w, _ = display_size(plan.video.width, plan.video.height, plan.video.rotation)
    frames = _read_frames(ctx, start, length)
    frame_scale = frames[0][1].shape[1] / source_w if frames else 1.0
    return _Sample(
        name=moment.clip_name,
        length=length,
        hook=hook,
        hook_seconds=hook_seconds,
        caption_boxes=tuple(built.boxes),
        faces=faces,
        face_times=face_times,
        crop_times=crop_times,
        crop_xs=crop_xs,
        frames=tuple(frames),
        frame_scale=frame_scale,
    )


def _crop_x(sample: _Sample, t: float) -> int:
    if not sample.crop_times:
        return sample.crop_xs[0]
    index = max(0, bisect_right(sample.crop_times, t) - 1)
    return sample.crop_xs[index]


def _hook_fits(ctx: MomentContext, sample: _Sample, rect: Rect) -> bool:
    """The render's checks for one clip with the watermark at `rect`: the hook fits, nothing
    overlaps the watermark or the hook, everything stays in the safe area."""
    plan, campaign = ctx.plan, ctx.campaign
    karaoke, metrics, safe = plan.karaoke, plan.metrics, plan.safe
    assert karaoke is not None and metrics is not None and safe is not None
    if any(box.intersects(rect) for box in sample.caption_boxes):
        return False
    at_top = campaign.hook_position == "top"
    face_boxes: list[Rect] | None
    if at_top or sample.faces is None:
        face_boxes = None
    else:
        face_boxes = [
            box for t, box in sample.faces if t <= min(sample.hook_seconds, sample.length)
        ]
    result = build_hook(
        sample.hook,
        karaoke,
        metrics,
        safe,
        face_boxes,
        seconds=sample.hook_seconds,
        fade_ms=0,
        line_gap_em=plan.settings.line_gap_em,
        clip_seconds=sample.length,
        font_size=campaign.hook_font_size,
        avoid=(rect,),
        position=campaign.hook_position,
    )
    band = result.band
    if band is None or not result.shown:
        return False
    if "hook_missing" in result.notes or "hook_truncated" in result.notes:
        return False
    if band.intersects(rect) or any(box.intersects(band) for box in sample.caption_boxes):
        return False
    return outside_count([*sample.caption_boxes, band], safe) == 0


def _face_cost(sample: _Sample, rect: Rect) -> float | None:
    if sample.faces is None:
        return None
    if not sample.face_times:
        return 0.0
    hit = {t for t, box in sample.faces if box.intersects(rect)}
    return len(hit) / len(sample.face_times)


def _luminance(ctx: MomentContext, sample: _Sample, rect: Rect) -> float | None:
    """The median grey behind the rectangle over the clip's frames; None without frames."""
    import numpy as np

    plan = ctx.plan
    out_w, out_h = plan.settings.width, plan.settings.height
    sx, sy = plan.crop.w / out_w, plan.crop.h / out_h
    medians: list[float] = []
    for t, grey in sample.frames:
        crop_x = _crop_x(sample, t)
        x1 = int((rect.x1 * sx + crop_x) * sample.frame_scale)
        x2 = int(np.ceil((rect.x2 * sx + crop_x) * sample.frame_scale))
        y1 = int((rect.y1 * sy + plan.crop.y) * sample.frame_scale)
        y2 = int(np.ceil((rect.y2 * sy + plan.crop.y) * sample.frame_scale))
        h, w = grey.shape
        x1, x2 = max(0, min(w, x1)), max(0, min(w, x2))
        y1, y2 = max(0, min(h, y1)), max(0, min(h, y2))
        if x2 > x1 and y2 > y1:
            medians.append(float(np.median(grey[y1:y2, x1:x2])))
    if not medians:
        return None
    return float(np.median(medians))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def score(
    ctx: MomentContext, grid: Sequence[Candidate], samples: Sequence[_Sample]
) -> list[_Score]:
    """Every candidate that passes the per-clip checks, best first."""
    app = ctx.app.campaign
    scored: list[_Score] = []
    for cand in grid:
        if not all(_hook_fits(ctx, s, cand.rect) for s in samples):
            continue
        faces = [c for c in (_face_cost(s, cand.rect) for s in samples) if c is not None]
        lights = [v for v in (_luminance(ctx, s, cand.rect) for s in samples) if v is not None]
        face_cost, luminance = _mean(faces), _mean(lights)
        total = (
            app.auto_watermark_face_weight * face_cost
            + app.auto_watermark_light_weight * luminance / 255
        )
        scored.append(_Score(cand, face_cost, luminance, total))
    scored.sort(
        key=lambda s: (
            round(s.score, 6),
            -s.candidate.y_center,
            abs(s.candidate.x_center - 0.5),
        )
    )
    return scored


def _record(s: _Score) -> AutoCandidate:
    return AutoCandidate(
        x_center=s.candidate.x_center,
        y_center=s.candidate.y_center,
        score=round(s.score, 4),
        face_cost=round(s.face_cost, 4),
        luminance=round(s.luminance, 1),
    )


def choose_position(
    ctx: MomentContext, moments: Sequence[Moment], used: UsedLog, *, force_reuse: bool
) -> tuple[Campaign, AutoPosition]:
    """Pick the position from the first `auto_watermark_sample` renderable moments, write the lock
    and return the campaign with its watermark placed. RunError when no position is possible."""
    app, campaign = ctx.app, ctx.campaign
    grid, tried = candidates(ctx)
    if not grid:
        w, h = campaign.asset.box_size
        raise RunError(
            f"watermark.position: auto found no place for the visible watermark ({w:g}x{h:g} px at "
            f"scale {campaign.asset.scale:g}) inside the safe area above the captions; lower "
            "watermark.scale or set an explicit position"
        )
    samples: list[_Sample] = []
    notes: list[str] = []
    for moment in moments:
        if len(samples) >= app.campaign.auto_watermark_sample:
            break
        if posted_entry(used, moment.id, moment.platform) is not None and not force_reuse:
            continue
        sample = _sample(ctx, moment)
        if sample is not None:
            samples.append(sample)
    if not samples:
        raise RunError(
            "watermark.position: auto needs at least one moment that can be rendered; "
            "none of the selected moments passes the time checks"
        )
    if any(s.faces is None for s in samples):
        notes.append("faces unknown (centre crop): the face cost is 0 for every position")
    if not all(s.frames for s in samples):
        notes.append("no frames could be read for a sampled clip; its luminance is left out")
    if campaign.hook_position == "top":
        notes.append("hook.position top: positions under the hook band are left out")
    ranked = score(ctx, grid, samples)
    if not ranked:
        raise RunError(
            f"watermark.position: auto found no place among {len(grid)} positions where the hook "
            "and the captions of the sampled moments leave room for the watermark; lower "
            "watermark.scale, shorten the hooks or set an explicit position"
        )
    best, runner_up = ranked[0], (ranked[1] if len(ranked) > 1 else None)
    lock = AutoPosition(
        x_center=best.candidate.x_center,
        y_center=best.candidate.y_center,
        score=round(best.score, 4),
        face_cost=round(best.face_cost, 4),
        luminance=round(best.luminance, 1),
        runner_up=None if runner_up is None else _record(runner_up),
        sampled=[s.name for s in samples],
        candidates=tried,
        placeable=len(grid),
        scored=len(ranked),
        watermark_sha256=campaign.asset.sha256,
        scale=campaign.asset.scale,
        blend=campaign.asset.blend,
        frame=(app.video.width, app.video.height),
        safe=(app.safe.top, app.safe.bottom, app.safe.left, app.safe.right),
        button_column=(app.safe.button_left, app.safe.button_top),
        corner_zone=app.campaign.corner_zone,
        hook_position=campaign.hook_position,
        notes=notes,
        chosen_at=datetime.now(UTC).isoformat(),
    )
    write_json_atomic(campaign.folder / AUTO_POSITION_FILE, lock.model_dump(mode="json"))
    placed = place_watermark(campaign.asset, lock.x_center, lock.y_center, app)
    return dataclasses.replace(campaign, watermark=placed, auto=lock), lock


def describe(lock: AutoPosition) -> str:
    """One console line about the choice: numbers and moment ids only."""
    text = (
        f"watermark: auto position x {lock.x_center:.2f}, y {lock.y_center:.2f} "
        f"(face {lock.face_cost:.2f}, light {lock.luminance:.0f}, score {lock.score:.3f})"
    )
    if lock.runner_up is not None:
        r = lock.runner_up
        text += f"; runner-up x {r.x_center:.2f}, y {r.y_center:.2f} (score {r.score:.3f})"
    return text + f"; from {len(lock.sampled)} moment(s): {', '.join(lock.sampled)}"
