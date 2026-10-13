import pytest

from clipmachine.models import TrackFace, TrackSample
from clipmachine.utils.crop import CropBox
from clipmachine.utils.safezone import (
    CAPTION_BOTTOM_GAP_PX,
    FACE_PAD_FRAC,
    Rect,
    caption_anchor,
    face_rects,
    hook_base_top,
    outside_count,
    place_hook,
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


# --- face_rects (Brief R1) ----------------------------------------------------------------------

# a 1920x1080 source cropped to a 608x1080 window at x 656, shown at 1080x1920
MAPPING = {
    "source_w": 1920,
    "source_h": 1080,
    "crop_w": 608,
    "crop_h": 1080,
    "crop_y": 0,
    "out_w": 1080,
    "out_h": 1920,
}


def face(
    t: float = 0.0, cx: float = 0.5, cy: float = 0.4, w: float = 0.1, h: float = 0.2
) -> TrackFace:
    return TrackFace(t=t, cx=cx, cy=cy, w=w, h=h)


def test_a_face_is_mapped_through_the_crop_and_grown_by_ten_percent() -> None:
    samples = [TrackSample(t=0.0, crop_x=656)]
    (rect,) = face_rects([face()], samples, until=2.0, **MAPPING)
    # source pixels: x 864..1056, y 324..540; then (px - 656) * 1080 / 608 and py * 1920 / 1080
    x1, x2 = (864 - 656) * 1080 / 608, (1056 - 656) * 1080 / 608
    y1, y2 = 324 * 1920 / 1080, 540 * 1920 / 1080
    assert rect.x1 == pytest.approx(x1 - 0.1 * (x2 - x1))
    assert rect.x2 == pytest.approx(x2 + 0.1 * (x2 - x1))
    assert rect.y1 == pytest.approx(y1 - 0.1 * (y2 - y1))
    assert rect.y2 == pytest.approx(y2 + 0.1 * (y2 - y1))
    assert FACE_PAD_FRAC == 0.1


def test_a_crop_offset_in_y_moves_the_face_up() -> None:
    samples = [TrackSample(t=0.0, crop_x=656)]
    shifted = {**MAPPING, "crop_y": 100, "crop_h": 980}
    (rect,) = face_rects([face()], samples, until=2.0, **shifted)
    (plain,) = face_rects([face()], samples, until=2.0, **MAPPING)
    assert rect.y1 < plain.y1 and rect.y2 < plain.y2


def test_the_crop_of_the_sample_with_the_same_time_is_used_else_the_latest_before() -> None:
    samples = [TrackSample(t=0.0, crop_x=656), TrackSample(t=0.1, crop_x=700)]
    exact, between, later = face_rects(
        [face(t=0.1), face(t=0.05), face(t=0.0)], samples, until=2.0, **MAPPING
    )
    (first,) = face_rects([face(t=0.0)], samples, until=2.0, **MAPPING)
    assert between == first and later == first  # t=0.05 uses the sample at 0.0
    assert exact.x1 < first.x1  # the window moved right, so the face moved left in the frame


def test_a_face_before_the_first_sample_is_skipped() -> None:
    samples = [TrackSample(t=0.1, crop_x=656)]
    assert face_rects([face(t=0.0)], samples, until=2.0, **MAPPING) == []
    assert len(face_rects([face(t=0.1)], samples, until=2.0, **MAPPING)) == 1


def test_only_faces_up_to_until_are_kept() -> None:
    samples = [TrackSample(t=0.0, crop_x=656)]
    faces = [face(t=0.0), face(t=1.0), face(t=2.0), face(t=2.1)]
    assert len(face_rects(faces, samples, until=2.0, **MAPPING)) == 3
    assert len(face_rects(faces, samples, until=0.5, **MAPPING)) == 1


def test_faces_outside_the_crop_window_are_dropped() -> None:
    samples = [TrackSample(t=0.0, crop_x=656)]
    left = face(cx=0.1)  # source x 96..288, left of the window starting at 656
    right = face(cx=0.95)  # source x 1824..2016 (past the window ending at 1264)
    assert face_rects([left, right], samples, until=2.0, **MAPPING) == []
    assert len(face_rects([left, face()], samples, until=2.0, **MAPPING)) == 1


# --- place_hook ---------------------------------------------------------------------------------


def test_the_hook_stays_at_the_top_without_faces() -> None:
    assert place_hook(400, 100, SAFE, [], 1100) == (290.0, False, False)


def test_the_hook_moves_just_below_an_overlapping_face() -> None:
    face_box = Rect(400, 250, 600, 400)
    assert place_hook(400, 100, SAFE, [face_box], 1100) == (412.0, True, False)
    assert place_hook(400, 100, SAFE, [Rect(400, 250, 600, 400.2)], 1100)[0] == 413.0  # ceil


def test_the_hook_moves_below_every_face_it_would_then_hit() -> None:
    faces = [Rect(400, 250, 600, 400), Rect(400, 420, 600, 600)]
    assert place_hook(400, 100, SAFE, faces, 1100) == (612.0, True, False)


def test_faces_beside_or_below_or_only_touching_do_not_move_the_hook() -> None:
    beside = Rect(0, 290, 100, 500)  # band spans x 304..704
    below = Rect(400, 390, 600, 600)  # band ends at y 390: edges touch only
    assert place_hook(400, 100, SAFE, [beside, below], 1100) == (290.0, False, False)


def test_a_hook_that_cannot_clear_the_faces_stays_at_the_top_over_one() -> None:
    low = Rect(0, 250, 1080, 1100)
    assert place_hook(400, 100, SAFE, [low], 1100) == (290.0, False, True)
    assert place_hook(400, 100, SAFE, [low], 1300) == (1112.0, True, False)  # room: fits below


# --- place_hook with fixed rectangles (Brief 11: the campaign watermark) -------------------------


def test_fixed_rectangles_none_change_nothing() -> None:
    cases = [
        ([], 1100),
        ([Rect(400, 250, 600, 400)], 1100),
        ([Rect(400, 250, 600, 400), Rect(400, 420, 600, 600)], 1100),
        ([Rect(0, 250, 1080, 1100)], 1100),
        ([Rect(0, 250, 1080, 1100)], 1300),
    ]
    for faces, lowest in cases:
        assert place_hook(400, 100, SAFE, faces, lowest, fixed=()) == place_hook(
            400, 100, SAFE, faces, lowest
        )


def test_the_band_starts_under_a_fixed_rectangle_at_the_top() -> None:
    mark = Rect(300, 280, 700, 400)  # band spans x 304..704, y 290..390
    assert hook_base_top(400, 100, SAFE, [mark]) == 412.0  # ceil(400 + 12)
    assert place_hook(400, 100, SAFE, [], 1100, fixed=[mark]) == (412.0, False, False)
    assert place_hook(400, 100, SAFE, [], 1100, fixed=[Rect(300, 280, 700, 400.2)])[0] == 413.0


def test_a_fixed_rectangle_elsewhere_changes_nothing() -> None:
    beside = Rect(0, 290, 100, 500)
    below = Rect(400, 390, 600, 600)  # only touches the band's bottom edge
    assert hook_base_top(400, 100, SAFE, [beside, below]) == 290.0
    assert place_hook(400, 100, SAFE, [], 1100, fixed=[beside, below]) == (290.0, False, False)


def test_faces_under_the_base_position_move_the_band_further() -> None:
    mark = Rect(300, 280, 700, 400)
    face_box = Rect(400, 420, 600, 600)  # the band at 412 would hit it
    assert place_hook(400, 100, SAFE, [face_box], 1100, fixed=[mark]) == (612.0, True, False)


def test_a_fixed_rectangle_is_an_obstacle_below_the_base_position_too() -> None:
    first, second = Rect(300, 280, 700, 400), Rect(300, 420, 700, 500)
    assert place_hook(400, 100, SAFE, [], 1100, fixed=[first, second]) == (512.0, False, False)


def test_with_no_free_place_the_band_returns_the_base_position_over_a_face() -> None:
    mark = Rect(300, 280, 700, 400)  # base 412
    low = Rect(0, 400, 1080, 1100)  # everything below is a face: the band would end at 1212
    assert place_hook(400, 100, SAFE, [low], 1100, fixed=[mark]) == (412.0, False, True)
    assert place_hook(400, 100, SAFE, [low], 1300, fixed=[mark]) == (1112.0, True, False)


# --- face_rects with the crop windows of a zoomed clip (Brief R2) -------------------------------

BASE_BOX = CropBox(w=608, h=1080, x=656, y=0)  # MAPPING's window
ZOOMED_BOX = CropBox(w=544, h=972, x=688, y=50)  # about 1.12x tighter, lower and to the right


def test_a_face_after_a_zoom_starts_is_mapped_through_the_zoomed_window() -> None:
    samples = [TrackSample(t=0.0, crop_x=656)]  # ignored: the windows decide
    boxes = [(0.0, BASE_BOX), (0.5, ZOOMED_BOX)]
    (rect,) = face_rects([face(t=1.0)], samples, until=2.0, boxes=boxes, **MAPPING)
    # source pixels x 864..1056, y 324..540; then (px - 688) * 1080 / 544, (py - 50) * 1920 / 972
    x1, x2 = (864 - 688) * 1080 / 544, (1056 - 688) * 1080 / 544
    y1, y2 = (324 - 50) * 1920 / 972, (540 - 50) * 1920 / 972
    assert rect.x1 == pytest.approx(x1 - 0.1 * (x2 - x1))
    assert rect.x2 == pytest.approx(x2 + 0.1 * (x2 - x1))
    assert rect.y1 == pytest.approx(y1 - 0.1 * (y2 - y1))
    assert rect.y2 == pytest.approx(y2 + 0.1 * (y2 - y1))


def test_a_face_before_the_zoom_starts_uses_the_base_window() -> None:
    samples = [TrackSample(t=0.0, crop_x=656)]
    boxes = [(0.0, BASE_BOX), (0.5, ZOOMED_BOX)]
    (early,) = face_rects([face(t=0.2)], samples, until=2.0, boxes=boxes, **MAPPING)
    (plain,) = face_rects([face(t=0.2)], samples, until=2.0, **MAPPING)
    assert early == plain
    (at_start,) = face_rects([face(t=0.5)], samples, until=2.0, boxes=boxes, **MAPPING)
    (late,) = face_rects([face(t=1.0)], samples, until=2.0, boxes=boxes, **MAPPING)
    assert at_start == late  # a window written at the face's own time is in force


def test_a_face_before_the_first_window_is_skipped() -> None:
    samples = [TrackSample(t=0.0, crop_x=656)]
    boxes = [(0.3, BASE_BOX)]
    assert face_rects([face(t=0.2)], samples, until=2.0, boxes=boxes, **MAPPING) == []


def test_without_boxes_or_with_none_the_samples_decide_as_before() -> None:
    samples = [TrackSample(t=0.0, crop_x=656)]
    assert face_rects([face()], samples, until=2.0, boxes=None, **MAPPING) == face_rects(
        [face()], samples, until=2.0, **MAPPING
    )
