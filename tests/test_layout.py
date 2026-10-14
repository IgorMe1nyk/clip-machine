"""Brief 12.1: the fit layout geometry."""

import pytest

from clipmachine.models import TrackFace
from clipmachine.utils.layout import (
    FRAME_CAPTION_GAP_PX,
    FitLayout,
    fit_face_rects,
    fit_layout,
    fit_seconds,
    in_spans,
)
from clipmachine.utils.safezone import Rect, caption_anchor, safe_rect

SAFE = safe_rect(1080, 1920, 290, 560, 120, 192)  # the shipped safe area: y 290..1360
CAPTION_H = 132.0  # about a one-line caption box at font size 96


def test_a_1080p_source_fits_1080_wide_and_the_group_is_centred_in_the_safe_area() -> None:
    layout = fit_layout(1920, 1080, 1080, 1920, SAFE, CAPTION_H)
    assert (layout.frame_w, layout.frame_h) == (1080, 608)
    group = 608 + FRAME_CAPTION_GAP_PX + CAPTION_H
    expected_top = 290 + (1070 - group) / 2
    assert abs(layout.frame_top - expected_top) <= 1 and layout.frame_top % 2 == 0
    assert layout.frame_bottom == layout.frame_top + 608
    # the caption anchor of caption_safe puts the box bottom just under the frame plus the gap
    _, bottom = caption_anchor(layout.caption_safe)
    assert bottom == pytest.approx(layout.frame_bottom + FRAME_CAPTION_GAP_PX + CAPTION_H)
    assert layout.caption_safe.y2 <= SAFE.y2 and layout.caption_safe.y1 == SAFE.y1
    assert (layout.caption_safe.x1, layout.caption_safe.x2) == (SAFE.x1, SAFE.x2)
    assert layout.frame == Rect(0, layout.frame_top, 1080, layout.frame_bottom)


def test_other_source_sizes_and_a_rotated_source() -> None:
    assert fit_layout(1280, 720, 1080, 1920, SAFE, CAPTION_H).frame_h == 608
    assert fit_layout(1920, 1200, 1080, 1920, SAFE, CAPTION_H).frame_h == 676  # 675 made even
    tall = fit_layout(1080, 1920, 1080, 1920, SAFE, CAPTION_H)  # a portrait source (rotated)
    assert tall.frame_h == 1920 and tall.frame_top == 290  # taller than the safe area: top


def test_a_group_taller_than_the_safe_area_starts_at_its_top_and_may_leave_it() -> None:
    layout = fit_layout(1920, 1080, 1080, 1920, SAFE, 600.0)
    assert layout.frame_top == 290
    assert layout.caption_safe.y2 > SAFE.y2  # the caller's safe-zone count reports it


def test_without_a_safe_area_the_frame_is_centred_on_the_canvas() -> None:
    layout = fit_layout(1920, 1080, 1080, 1920, None, 0.0)
    assert layout.frame_top == 656 and layout.caption_safe == Rect(0, 0, 1080, 1920)


def test_in_spans_and_fit_seconds() -> None:
    spans = [(4.05, 7.98), (12.0, 15.5)]
    assert in_spans(4.05, spans) and in_spans(7.97, spans) and not in_spans(7.98, spans)
    assert not in_spans(0.0, spans) and in_spans(12.0, spans)
    assert fit_seconds(spans) == 7.43 and fit_seconds([]) == 0.0


def test_faces_in_fit_spans_map_through_the_frame_and_the_rest_are_left_out() -> None:
    layout = FitLayout(1080, 608, 400, Rect(0, 0, 1080, 1920))
    faces = [
        TrackFace(t=5.0, cx=0.5, cy=0.5, w=0.1, h=0.2),  # in the span
        TrackFace(t=1.0, cx=0.5, cy=0.5, w=0.1, h=0.2),  # before it
        TrackFace(t=9.0, cx=0.5, cy=0.5, w=0.1, h=0.2),  # after `until`
    ]
    (rect,) = fit_face_rects(faces, layout, [(4.0, 8.0)], until=8.0)
    w, h = 0.1 * 1080, 0.2 * 608
    assert rect.x1 == pytest.approx(540 - w / 2 - w * 0.1)
    assert rect.x2 == pytest.approx(540 + w / 2 + w * 0.1)
    assert rect.y1 == pytest.approx(400 + 304 - h / 2 - h * 0.1)
    assert rect.y2 == pytest.approx(400 + 304 + h / 2 + h * 0.1)
    edge = fit_face_rects([TrackFace(t=5.0, cx=0.0, cy=0.0, w=0.2, h=0.2)], layout, [(4, 8)], 8)
    assert edge[0].x1 == 0.0 and edge[0].y1 < 400  # clamped to the canvas's left edge
