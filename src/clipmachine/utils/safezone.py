"""Rectangles and the safe area for on-screen text (Brief R1). Pure functions: no I/O.

The safe area is what is left of the output canvas after the margins that the apps cover (see
`SafeConfig`). Captions and the hook are centred on it, not on the frame.
"""

import math
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from clipmachine.models import TrackFace, TrackSample

CAPTION_BOTTOM_GAP_PX = 12  # between a caption box's bottom edge and the bottom of the safe area
FACE_PAD_FRAC = 0.1  # a face box grows by this fraction of its size on every side
HOOK_FACE_GAP_PX = 12  # between a face box and the hook band moved below it


@dataclass(frozen=True)
class Rect:
    """A rectangle in output pixels; x2 and y2 are the right and bottom edges."""

    x1: float
    y1: float
    x2: float
    y2: float

    def intersects(self, other: "Rect") -> bool:
        """True when the two share some area; rectangles that only touch at an edge do not."""
        return (
            self.x1 < other.x2 and other.x1 < self.x2 and self.y1 < other.y2 and other.y1 < self.y2
        )

    def inside(self, other: "Rect") -> bool:
        """True when this rectangle lies fully within `other` (touching its edges is allowed)."""
        return (
            self.x1 >= other.x1
            and self.y1 >= other.y1
            and self.x2 <= other.x2
            and self.y2 <= other.y2
        )


def safe_rect(width: int, height: int, top: int, bottom: int, left: int, right: int) -> Rect:
    """The canvas without the margins."""
    return Rect(float(left), float(top), float(width - right), float(height - bottom))


def outside_count(rects: Iterable[Rect], safe: Rect) -> int:
    """How many of the rectangles are not fully inside the safe area."""
    return sum(1 for rect in rects if not rect.inside(safe))


def caption_anchor(safe: Rect) -> tuple[float, float]:
    """Where a caption goes in a normal shot: (centre x, y of the box's bottom edge).

    The centre is the safe area's, so with left 120 and right 192 on a 1080 px canvas it is 504.
    """
    # TODO(Brief 8.3): stacked shots place captions on the seam and the hook over the top half
    return (safe.x1 + safe.x2) / 2, safe.y2 - CAPTION_BOTTOM_GAP_PX


def face_rects(
    faces: Sequence[TrackFace],
    samples: Sequence[TrackSample],
    *,
    source_w: float,
    source_h: float,
    crop_w: float,
    crop_h: float,
    crop_y: float,
    out_w: float,
    out_h: float,
    until: float,
) -> list[Rect]:
    """Where the faces up to `until` seconds are in the output frame, grown by FACE_PAD_FRAC.

    A face (0..1 of the analysed frame) becomes source pixels, then output pixels through the crop
    window at that moment: `crop_x` of the sample with the face's own time, else of the latest
    sample before it (a face with no such sample is skipped). Faces that do not reach the output
    frame are dropped. `source_w` and `source_h` are the upright source size.
    """
    ordered = sorted(samples, key=lambda sample: sample.t)
    times = [sample.t for sample in ordered]
    frame = Rect(0.0, 0.0, float(out_w), float(out_h))
    found: list[Rect] = []
    for face in faces:
        if face.t > until:
            continue
        index = bisect_right(times, face.t) - 1
        if index < 0:
            continue
        crop_x = ordered[index].crop_x
        left = (face.cx - face.w / 2) * source_w
        right = (face.cx + face.w / 2) * source_w
        top = (face.cy - face.h / 2) * source_h
        bottom = (face.cy + face.h / 2) * source_h
        x1 = (left - crop_x) * out_w / crop_w
        x2 = (right - crop_x) * out_w / crop_w
        y1 = (top - crop_y) * out_h / crop_h
        y2 = (bottom - crop_y) * out_h / crop_h
        pad_x = FACE_PAD_FRAC * (x2 - x1)
        pad_y = FACE_PAD_FRAC * (y2 - y1)
        rect = Rect(x1 - pad_x, y1 - pad_y, x2 + pad_x, y2 + pad_y)
        if rect.intersects(frame):
            found.append(rect)
    return found


def place_hook(
    band_w: float, band_h: float, safe: Rect, faces: Sequence[Rect], lowest_bottom: float
) -> tuple[float, bool, bool]:
    """Where the hook band goes: (top, moved, over_face).

    Centred on the safe area, starting at its top. While the band overlaps faces it moves down to
    just under the lowest of them (HOOK_FACE_GAP_PX below). If the moved band would end below
    `lowest_bottom` (the caption box minus a gap), or no free place is found, the band stays at
    the top of the safe area and `over_face` is True.
    """
    # TODO(Brief 8.3): stacked shots place the hook over the top half's headroom
    centre = (safe.x1 + safe.x2) / 2
    top = safe.y1
    free = False
    for _ in range(len(faces) + 1):
        band = Rect(centre - band_w / 2, top, centre + band_w / 2, top + band_h)
        hits = [face for face in faces if face.intersects(band)]
        if not hits:
            free = True
            break
        top = float(math.ceil(max(face.y2 for face in hits) + HOOK_FACE_GAP_PX))
    if not free:
        return safe.y1, False, True
    if top == safe.y1:
        return top, False, False
    if top + band_h > lowest_bottom:
        return safe.y1, False, True
    return top, True, False
