"""Rectangles and the safe area for on-screen text (Brief R1). Pure functions: no I/O.

The safe area is what is left of the output canvas after the margins that the apps cover (see
`SafeConfig`). Captions and the hook are centred on it, not on the frame.
"""

from collections.abc import Iterable
from dataclasses import dataclass

CAPTION_BOTTOM_GAP_PX = 12  # between a caption box's bottom edge and the bottom of the safe area


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
