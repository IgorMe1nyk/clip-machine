import pytest

from clipmachine.utils.safezone import (
    CAPTION_BOTTOM_GAP_PX,
    Rect,
    caption_anchor,
    outside_count,
    safe_rect,
)

SAFE = safe_rect(1080, 1920, 290, 672, 120, 192)


def test_safe_rect_is_the_canvas_without_the_margins() -> None:
    assert Rect(120.0, 290.0, 888.0, 1248.0) == SAFE


def test_caption_anchor_is_centred_on_the_safe_area_not_the_frame() -> None:
    cx, bottom = caption_anchor(SAFE)
    assert cx == 504.0  # not 540
    assert bottom == 1248.0 - CAPTION_BOTTOM_GAP_PX == 1236.0


# --- inside and intersects ----------------------------------------------------------------------


def test_inside_allows_touching_the_edges() -> None:
    assert SAFE.inside(SAFE)
    assert Rect(120, 290, 200, 300).inside(SAFE)
    assert Rect(500, 1200, 888, 1248).inside(SAFE)


@pytest.mark.parametrize(
    "rect",
    [
        Rect(119.9, 400, 300, 500),  # left
        Rect(400, 289.9, 500, 500),  # top
        Rect(400, 400, 888.1, 500),  # right
        Rect(400, 400, 500, 1248.1),  # bottom
        Rect(0, 0, 1080, 1920),  # contains the safe area
    ],
)
def test_inside_is_false_when_any_edge_is_outside(rect: Rect) -> None:
    assert not rect.inside(SAFE)


def test_intersects_needs_shared_area() -> None:
    base = Rect(0, 0, 10, 10)
    assert base.intersects(Rect(5, 5, 15, 15))
    assert base.intersects(Rect(2, 2, 3, 3))  # one inside the other
    assert base.intersects(base)
    assert not base.intersects(Rect(10, 0, 20, 10))  # touching edges only
    assert not base.intersects(Rect(0, 10, 10, 20))
    assert not base.intersects(Rect(11, 11, 20, 20))
    assert Rect(5, 5, 15, 15).intersects(base)  # symmetric


def test_outside_count_counts_rects_not_fully_inside() -> None:
    good = Rect(130, 1100, 880, 1240)
    too_low = Rect(130, 1100, 880, 1300)
    too_wide = Rect(100, 1100, 880, 1240)
    assert outside_count([good, good], SAFE) == 0
    assert outside_count([good, too_low, too_wide], SAFE) == 2
    assert outside_count([], SAFE) == 0
