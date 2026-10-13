"""Crop arithmetic for the render stage: the centre crop, and the crop position at every frame
of a clip that follows a track. Pure integer/float math, no filter expressions."""

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

AUTO_ROTATED = frozenset({90, 270})


@dataclass(frozen=True)
class CropBox:
    """A crop rectangle in pixels of the upright (auto-rotated) frame."""

    w: int
    h: int
    x: int
    y: int


def display_size(width: int, height: int, rotation: int) -> tuple[int, int]:
    """Frame size after ffmpeg's auto-rotation: width and height swap at 90 and 270 degrees."""
    if rotation % 360 in AUTO_ROTATED:
        return height, width
    return width, height


def _even(value: int) -> int:
    return value - value % 2


def center_crop(
    width: int, height: int, rotation: int, target_width: int, target_height: int
) -> CropBox:
    """The largest centred rectangle with the target's aspect ratio, in even integers.

    width, height and rotation are the stored (ffprobe) values of the source. A source that
    already has the target ratio gets a full-frame crop. Raises ValueError for a frame too small
    to hold a 2x2 crop.
    """
    frame_w, frame_h = display_size(width, height, rotation)
    if frame_w * target_height > frame_h * target_width:  # wider than the target
        crop_h = frame_h
        crop_w = frame_h * target_width // target_height
    else:
        crop_w = frame_w
        crop_h = frame_w * target_height // target_width
    crop_w, crop_h = _even(crop_w), _even(crop_h)
    if crop_w < 2 or crop_h < 2:
        raise ValueError(f"frame {frame_w}x{frame_h} is too small to crop")
    return CropBox(
        w=crop_w,
        h=crop_h,
        x=_even((frame_w - crop_w) // 2),
        y=_even((frame_h - crop_h) // 2),
    )


CUT_MATCH_SECONDS = 1e-6  # a shot start this close to a cut is that cut
TIME_DIGITS = 6


def even_round(value: float) -> int:
    """The nearest even integer (ties to the even multiple of two, as round does)."""
    return 2 * round(value / 2)


def effective_boundaries(
    shots: Sequence[tuple[float, float]],
    cuts: Sequence[float],
    cuts_exact: Sequence[float],
    duration: float,
) -> list[float]:
    """Where each shot after the first really starts: a camera cut at its exact time (when the
    track has one), a speaker switch at its nominal time. Clamped to the clip, non-decreasing."""
    exact_known = len(cuts_exact) == len(cuts)
    bounds: list[float] = []
    for start, _ in shots[1:]:
        at = start
        if exact_known:
            for cut, exact in zip(cuts, cuts_exact, strict=True):
                if abs(cut - start) <= CUT_MATCH_SECONDS:
                    at = exact
                    break
        at = min(max(at, 0.0), duration)
        if bounds:
            at = max(at, bounds[-1])
        bounds.append(round(at, TIME_DIGITS))
    return bounds


def _shot_anchors(
    samples: Sequence[tuple[float, float]], shots: Sequence[tuple[float, float]]
) -> list[list[tuple[float, float]]]:
    """The samples of each shot, by the shot's nominal [start, end). A shot without samples takes
    the one nearest in time to its start."""
    starts = [start - CUT_MATCH_SECONDS for start, _ in shots[1:]]
    anchors: list[list[tuple[float, float]]] = [[] for _ in shots]
    for t, x in samples:
        anchors[bisect_right(starts, t)].append((t, x))
    for i, group in enumerate(anchors):
        if not group:
            group.append(min(samples, key=lambda s: abs(s[0] - shots[i][0])))
    return anchors


def _interpolate(anchors: Sequence[tuple[float, float]], t: float) -> float:
    if t <= anchors[0][0]:
        return anchors[0][1]
    if t >= anchors[-1][0]:
        return anchors[-1][1]
    right = bisect_right([a[0] for a in anchors], t)
    (t0, x0), (t1, x1) = anchors[right - 1], anchors[right]
    return x0 + (x1 - x0) * (t - t0) / (t1 - t0)


def frame_path(
    samples: Sequence[tuple[float, float]],
    shots: Sequence[tuple[float, float]],
    cuts: Sequence[float],
    cuts_exact: Sequence[float],
    fps: float,
    duration: float,
) -> list[tuple[float, int]]:
    """The crop x as change points (time, x), times in seconds from the clip start.

    `samples` are (t, crop_x) pairs in time order, `shots` the (start, end) pairs in order, `cuts`
    and `cuts_exact` the track's parallel lists, `fps` the source frame rate. x is worked out at
    every frame time n / fps and at every shot boundary, from the samples of the frame's own shot
    only (linear between them, held before the first and after the last), so every boundary is a
    hard jump: a speaker switch at the shot's start, a camera cut at its exact time when
    `cuts_exact` matches `cuts`. The first entry is always (0.0, x at 0); after that an entry is
    made only where x differs from the one before. x is an even integer.

    Raises ValueError for no samples or an fps that is not positive. No shots means one shot.
    """
    if not samples:
        raise ValueError("a crop path needs at least one sample")
    if fps <= 0:
        raise ValueError("fps must be positive")
    all_shots = list(shots) or [(0.0, duration)]
    bounds = effective_boundaries(all_shots, cuts, cuts_exact, duration)
    anchors = _shot_anchors(samples, all_shots)

    times = {0.0}
    n = 1
    while n / fps < duration:
        times.add(round(n / fps, TIME_DIGITS))
        n += 1
    times.update(b for b in bounds if 0.0 < b < duration)

    path: list[tuple[float, int]] = []
    for t in sorted(times):
        x = even_round(_interpolate(anchors[bisect_right(bounds, t)], t))
        if not path or x != path[-1][1]:
            path.append((t, x))
    return path
