"""Punch-in zooms (Brief R2): where a clip jumps to a crop `scale` times tighter, and the crop
boxes that result. Pure arithmetic, no I/O. The render stage feeds it the clip's sentence starts,
loudness and shot boundaries, and writes the boxes it returns as sendcmd lines.

The choice is deterministic: the sentence holding the clip's first spoken line, plus the loudest
`loud_pct` of the clip's sentences. No model picks the zoom points (campaign Rule 3)."""

import math
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from clipmachine.models import ZoomConfig
from clipmachine.utils.crop import TIME_DIGITS, CropBox, even_round

NEAR_CUT_S = 0.5  # no zoom change this close to a camera cut, except one that lands on the cut
EPS = 1e-9  # slack for comparing times that were computed along different routes


@dataclass(frozen=True)
class SentenceStart:
    """One sentence of the clip: where it starts (seconds from the clip start, never below 0)
    and its mean loudness in dB, or None when the audio has nothing for it."""

    t: float
    loudness_db: float | None


@dataclass(frozen=True)
class ZoomSpan:
    """One zoom-in: from `start` to `end` seconds, centred vertically on `face_cy` (0..1 of the
    frame height, the followed face in the shot where the zoom starts)."""

    start: float
    end: float
    face_cy: float


def _zoomed_size(box: CropBox, scale: float) -> tuple[int, int]:
    """The window `scale` times tighter, in even pixels (at least 2)."""
    return max(2, even_round(box.w / scale)), max(2, even_round(box.h / scale))


def upscale_factor(
    box: CropBox, scale: float, out_w: int, out_h: int, style: str = "punch"
) -> float:
    """How much larger than the source's pixels the zoomed window is drawn on the output: the
    bigger of the two axes' ratios. The same for every zoom of a clip, because the window size
    does not depend on where the zoom is."""
    zoomed_w, zoomed_h = _zoomed_size(box, scale) if style == "punch" else smooth_size(box, scale)
    return max(out_w / zoomed_w, out_h / zoomed_h)


def select_zooms(
    starts: Sequence[SentenceStart],
    first: int,
    boundaries: Sequence[float],
    cuts: Sequence[float],
    duration: float,
    cfg: ZoomConfig,
    *,
    excluded: Sequence[tuple[float, float]] = (),
) -> list[tuple[float, float]]:
    """The (in, out) times of the clip's zooms, in order.

    `starts` are the clip's sentences in time order, `first` the index of the one holding the
    clip's first spoken line (the hook moment; its start is clamped to 0 by the caller).
    `boundaries` are the times where a new shot begins (camera cuts and speaker switches, as
    `effective_boundaries` gives them); the clip's end counts as one whether or not it is listed.
    `cuts` are the exact camera-cut times. `excluded` are spans that never get a zoom (shots
    without a face).

    A sentence is picked when it holds the first spoken line or is among the loudest
    ceil(loud_pct * n) of the n sentences (ties go to the earlier one; a sentence with no
    loudness never ranks). Walking the picks in time order, a pick becomes a zoom-in when

    - it is at least `min_gap_s` after the previous zoom-out (a zoom-out is a change like a
      zoom-in: the two alternate and keep the gap),
    - it is at least 0.5 s from every camera cut,
    - it is not inside an excluded span, and
    - the next shot boundary (or excluded span, or the clip's end) is at least `min_gap_s` away.

    The zoom-in ends at the earliest of `max_hold_s` later, the next shot boundary, and the
    first pick at least `min_gap_s` later. An end less than 0.5 s before a camera cut moves onto
    the cut, so that hold can run up to 0.5 s over `max_hold_s`. An end at the clip's end means
    the clip finishes zoomed in: the out time is then the duration, and no zoom-out is needed.
    """
    # TODO(Brief 8.3): pass the stacked shots in `excluded`; no zoom there
    count = len(starts)
    if count == 0 or cfg.max_hold_s < cfg.min_gap_s:
        return []

    picked = {first} if 0 <= first < count else set()
    ranked = sorted(
        (i for i, s in enumerate(starts) if s.loudness_db is not None),
        key=lambda i: (-(starts[i].loudness_db or 0.0), i),
    )
    picked.update(ranked[: math.ceil(round(cfg.loud_pct * count, 9))])
    picks = sorted(starts[i].t for i in picked)

    stops = sorted({b for b in boundaries if 0.0 < b < duration} | {duration})
    excluded_starts = [a for a, _ in excluded]
    gap = cfg.min_gap_s

    zooms: list[tuple[float, float]] = []
    last_out = -math.inf
    for t in picks:
        if t >= duration - EPS or t < last_out + gap - EPS:
            continue
        if any(abs(t - c) < NEAR_CUT_S - EPS for c in cuts):
            continue
        if any(a - EPS <= t < b - EPS for a, b in excluded):
            continue
        ahead = [s for s in stops + excluded_starts if s > t + EPS]
        next_stop = min(ahead)
        if next_stop - t < gap - EPS:
            continue
        end = min(t + cfg.max_hold_s, next_stop)
        later = [p for p in picks if p >= t + gap - EPS]
        if later:
            end = min(end, later[0])
        for c in cuts:
            if end + EPS < c < end + NEAR_CUT_S - EPS:
                end = c
                break
        end = min(end, duration)
        zooms.append((t, end))
        last_out = end
    return zooms


def zoom_box(box: CropBox, scale: float, face_cy: float, frame_w: int, frame_h: int) -> CropBox:
    """The window `scale` times tighter: horizontally centred on `box` (the smoothed window, so
    the operator smoothing carries on inside a zoom), vertically centred on the face
    (`face_cy`, 0..1 of the frame height). Even pixels, clamped inside the frame."""
    w, h = _zoomed_size(box, scale)
    x = even_round(box.x + (box.w - w) / 2)
    y = even_round(face_cy * frame_h - h / 2)
    # the upper limits are rounded down to even so a clamped box stays even in an odd-sized frame
    return CropBox(
        w=w,
        h=h,
        x=min(max(x, 0), (frame_w - w) // 2 * 2),
        y=min(max(y, 0), (frame_h - h) // 2 * 2),
    )


def zoomed_path(
    path: Sequence[tuple[float, int]],
    base: CropBox,
    spans: Sequence[ZoomSpan],
    scale: float,
    frame_w: int,
    frame_h: int,
    duration: float,
) -> list[tuple[float, CropBox]]:
    """The crop window at every change, as (time, box), times in seconds from the clip start.

    `path` is the crop x as change points (`frame_path`), `base` the unzoomed window (its x is
    replaced by the path's), `spans` the zooms. The box changes where the path moves, where a
    zoom starts and where it ends; inside a span it is the base window zoomed with `zoom_box`, so
    a path step still moves the zoomed window. A zoom-out on a path step is one entry, not two.
    The first entry is at 0.0. An entry is made only where the box differs from the one before.
    A span that ends at or after `duration` makes no entry for its end: the clip ends zoomed in.

    Raises ValueError for an empty path.
    """
    if not path:
        raise ValueError("a zoomed path needs at least one path entry")
    path_times = [t for t, _ in path]
    events = {0.0, *(round(t, TIME_DIGITS) for t in path_times)}
    for span in spans:
        events.add(round(span.start, TIME_DIGITS))
        if span.end < duration - EPS:
            events.add(round(span.end, TIME_DIGITS))

    boxes: list[tuple[float, CropBox]] = []
    for t in sorted(events):
        x = path[max(bisect_right(path_times, t + EPS) - 1, 0)][1]
        window = CropBox(base.w, base.h, x, base.y)
        for span in spans:
            if span.start - EPS <= t < span.end - EPS:
                window = zoom_box(window, scale, span.face_cy, frame_w, frame_h)
                break
        if not boxes or window != boxes[-1][1]:
            boxes.append((t, window))
    return boxes


# ---------------------------------------------------------------------------------------------
# Smooth zooms (Brief R2.1): the same spans, but the window eases to the zoomed size and back.


def ease_in_out(u: float) -> float:
    """Cosine ease-in-out: 0 at u = 0, 0.5 at u = 0.5, 1 at u = 1; u is clamped to 0..1."""
    u = min(max(u, 0.0), 1.0)
    return (1 - math.cos(math.pi * u)) / 2


def smooth_size(base: CropBox, s: float) -> tuple[int, int]:
    """The (w, h) of the base window zoomed by the continuous factor `s` (1 = the base).

    The height is the base's divided by `s`; the width follows from the height with the base
    window's own aspect, so at s = 1 the size equals the base exactly (no command at the first
    frame of a ramp) and the aspect stays within rounding of the base's. "Within 0.5%" in the
    brief is measured against the base window's aspect, which is the closest even 9:16 the
    source allows. Both even, at least 2."""
    h = max(2, even_round(base.h / s))
    w = max(2, even_round(h * base.w / base.h))
    return w, h


def smooth_box(window: CropBox, s: float, face_cy: float, frame_w: int, frame_h: int) -> CropBox:
    """`window` zoomed by `s` (1 = unchanged): horizontally centred on `window`, vertically on
    the face (`face_cy`, 0..1 of the frame height). Even pixels, clamped inside the frame."""
    if s == 1:
        return window
    w, h = smooth_size(window, s)
    x = even_round(window.x + (window.w - w) / 2)
    y = even_round(face_cy * frame_h - h / 2)
    return CropBox(
        w=w,
        h=h,
        x=min(max(x, 0), (frame_w - w) // 2 * 2),
        y=min(max(y, 0), (frame_h - h) // 2 * 2),
    )


@dataclass(frozen=True)
class Ramp:
    """One ease between the base window and the zoomed one, from `start` to `end` seconds."""

    start: float
    end: float
    direction: Literal["in", "out"]


def plan_ramps(
    spans: Sequence[ZoomSpan],
    boundaries: Sequence[float],
    ramp_s: float,
    duration: float,
) -> tuple[list[Ramp], list[str]]:
    """The eases of the zooms, and reason codes.

    A zoom eases in from the base window over `ramp_s` starting at its start, holds, and eases
    back out over `ramp_s` ending at its end. At camera cuts there is no ramp: a hold that ends
    on a cut (or at the clip's end) switches instantly, the cut hides it. A ramp never crosses a
    cut (`boundaries`, which include the clip's end): one that would is shortened, to the next
    boundary for a ramp-in, and so that the two ramps fit in a hold that is shorter than two
    ramps. A ramp of zero length is dropped; `ramp_s == 0` gives none. The note
    `zoom_ramp_shortened` is added once when a ramp is shorter than `ramp_s` for such a reason."""
    ramps: list[Ramp] = []
    shortened = False
    if ramp_s <= 0:
        return ramps, []
    for span in spans:
        ahead = [b for b in boundaries if b > span.start + EPS]
        d_in = min(ramp_s, min(ahead, default=duration) - span.start, (span.end - span.start) / 2)
        on_cut = span.end >= duration - EPS or any(abs(span.end - b) < EPS for b in boundaries)
        d_out = 0.0 if on_cut else min(ramp_s, span.end - span.start - d_in)
        if d_in > EPS:
            ramps.append(Ramp(span.start, span.start + d_in, "in"))
            shortened |= d_in < ramp_s - EPS
        if d_out > EPS:
            ramps.append(Ramp(span.end - d_out, span.end, "out"))
            shortened |= d_out < ramp_s - EPS
    return ramps, ["zoom_ramp_shortened"] if shortened else []


def smooth_path(
    path: Sequence[tuple[float, int]],
    base: CropBox,
    spans: Sequence[ZoomSpan],
    ramps: Sequence[Ramp],
    scale: float,
    frame_w: int,
    frame_h: int,
    fps: float,
    duration: float,
) -> list[tuple[float, CropBox]]:
    """The crop window at every change, as (time, box), with eased zooms.

    Like `zoomed_path`, but the zoom factor `s(t)` is 1 outside the spans, `scale` inside a
    span, and between the two inside a ramp: `1 + (scale - 1) * ease_in_out(u)`, u running 0..1
    over a ramp-in and 1..0 over a ramp-out. During a ramp there is one entry per output frame
    (`n / fps`); elsewhere the box changes where the path moves and where a span starts or ends.
    A span starting at 0 therefore starts at the base window and eases in. The crop x comes from
    `path` (a step function), so a step inside a hold re-centres the zoomed box. An entry is made
    only where the box differs from the one before; the first is at 0.0. A span that ends at or
    after `duration` makes no entry for its end.

    Raises ValueError for an empty path."""
    if not path:
        raise ValueError("a zoomed path needs at least one path entry")
    path_times = [t for t, _ in path]
    events = {0.0, *(round(t, TIME_DIGITS) for t in path_times)}
    for span in spans:
        events.add(round(span.start, TIME_DIGITS))
        if span.end < duration - EPS:
            events.add(round(span.end, TIME_DIGITS))
    for ramp in ramps:
        events.add(round(ramp.start, TIME_DIGITS))
        events.add(round(ramp.end, TIME_DIGITS))
        first, last = math.ceil(ramp.start * fps - EPS), math.floor(ramp.end * fps + EPS)
        events.update(round(n / fps, TIME_DIGITS) for n in range(first, last + 1))

    boxes: list[tuple[float, CropBox]] = []
    for t in sorted(events):
        if t >= duration - EPS and t > 0:
            continue
        x = path[max(bisect_right(path_times, t + EPS) - 1, 0)][1]
        window = CropBox(base.w, base.h, x, base.y)
        s = 1.0
        face_cy = 0.5
        for ramp in ramps:
            if ramp.start - EPS <= t <= ramp.end + EPS:
                u = (t - ramp.start) / (ramp.end - ramp.start)
                s = 1 + (scale - 1) * ease_in_out(u if ramp.direction == "in" else 1 - u)
                break
        else:
            for span in spans:
                if span.start - EPS <= t < span.end - EPS:
                    s = scale
                    break
        if s != 1.0:
            face_cy = next(sp.face_cy for sp in spans if sp.start - EPS <= t <= sp.end + EPS)
        window = smooth_box(window, s, face_cy, frame_w, frame_h)
        if not boxes or window != boxes[-1][1]:
            boxes.append((t, window))
    return boxes
