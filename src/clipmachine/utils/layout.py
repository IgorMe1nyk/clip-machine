"""The fit layout (Brief 12.1): a camera shot with on-screen graphics outside the speaker crop is
shown whole, the source frame scaled to the output width on a blurred, darkened copy of itself,
with the captions just below the frame. Pure geometry: no I/O, no ffmpeg.

Placement (D-033): the frame and the caption box below it are centred as a group inside the safe
area, never the frame alone on the canvas, so the captions stay inside the safe area (a frame
centred on a 1080x1920 canvas would push them 48 px below it). The hook keeps its place at the top
of the safe area for its few seconds, over the frame if it must.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from clipmachine.models import TrackFace
from clipmachine.utils.safezone import CAPTION_BOTTOM_GAP_PX, FACE_PAD_FRAC, Rect

FRAME_CAPTION_GAP_PX = 12  # between the fitted frame's bottom edge and the caption box
TIME_SLACK_S = 1e-9


@dataclass(frozen=True)
class FitLayout:
    """Where the whole source frame and the captions go on the output canvas, in output pixels."""

    frame_w: int  # the fitted frame: the output width, an even height
    frame_h: int
    frame_top: int  # y of the frame's top edge
    caption_safe: Rect  # a safe rectangle whose caption anchor puts the captions below the frame

    @property
    def frame_bottom(self) -> int:
        return self.frame_top + self.frame_h

    @property
    def frame(self) -> Rect:
        return Rect(0.0, float(self.frame_top), float(self.frame_w), float(self.frame_bottom))


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def fit_layout(
    source_w: int,
    source_h: int,
    out_w: int,
    out_h: int,
    safe: Rect | None,
    caption_box_h: float,
) -> FitLayout:
    """The layout for an upright source of source_w x source_h on an out_w x out_h canvas.

    `safe` is the safe area (None: no captions geometry is known, so the frame is centred on the
    canvas and `caption_safe` is the whole canvas) and `caption_box_h` the height of a caption box
    at the settings' font size. The group (frame, gap, caption box) is centred vertically in the
    safe area; a group taller than it starts at the safe area's top and the captions may leave it
    (the caller's safe-zone count says so).
    """
    frame_w = out_w
    frame_h = _even(out_w * source_h / source_w)
    if safe is None:
        top = _even((out_h - frame_h) / 2)
        return FitLayout(frame_w, frame_h, top, Rect(0.0, 0.0, float(out_w), float(out_h)))
    group_h = frame_h + FRAME_CAPTION_GAP_PX + caption_box_h
    slack = max(0.0, (safe.y2 - safe.y1) - group_h)
    top = _even(safe.y1 + slack / 2)
    caption_bottom = top + frame_h + FRAME_CAPTION_GAP_PX + caption_box_h
    caption_safe = Rect(safe.x1, safe.y1, safe.x2, caption_bottom + CAPTION_BOTTOM_GAP_PX)
    return FitLayout(frame_w, frame_h, top, caption_safe)


def in_spans(t: float, spans: Sequence[tuple[float, float]]) -> bool:
    """True when `t` (seconds from the clip start) lies in one of the [start, end) spans."""
    return any(start - TIME_SLACK_S <= t < end - TIME_SLACK_S for start, end in spans)


def fit_face_rects(
    faces: Sequence[TrackFace],
    layout: FitLayout,
    spans: Sequence[tuple[float, float]],
    until: float,
) -> list[Rect]:
    """Where the faces seen up to `until` seconds during the fit spans are on the output canvas,
    grown by FACE_PAD_FRAC like the crop's face rectangles: the whole frame is on screen, so a
    face maps straight through the frame's scale and offset."""
    found: list[Rect] = []
    for face in faces:
        if face.t > until or not in_spans(face.t, spans):
            continue
        w, h = face.w * layout.frame_w, face.h * layout.frame_h
        cx, cy = face.cx * layout.frame_w, layout.frame_top + face.cy * layout.frame_h
        pad_w, pad_h = w * FACE_PAD_FRAC, h * FACE_PAD_FRAC
        found.append(
            Rect(
                max(0.0, cx - w / 2 - pad_w),
                max(0.0, cy - h / 2 - pad_h),
                min(float(layout.frame_w), cx + w / 2 + pad_w),
                cy + h / 2 + pad_h,
            )
        )
    return found


def fit_seconds(spans: Sequence[tuple[float, float]]) -> float:
    return round(sum(max(0.0, end - start) for start, end in spans), 3)
