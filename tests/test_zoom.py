"""Punch-in zooms (Brief R2): the selection rule, the zoomed window and the crop path with zooms.
All synthetic numbers; no ffmpeg."""

import pytest

from clipmachine.models import ZoomConfig
from clipmachine.utils.crop import CropBox
from clipmachine.utils.zoom import (
    SentenceStart,
    ZoomSpan,
    select_zooms,
    upscale_factor,
    zoom_box,
    zoomed_path,
)

CFG = ZoomConfig(enabled=True)  # scale 1.12, loud 30%, gap 4 s, hold 6 s


def sentences(
    times: list[float], loud: dict[int, float] | None = None, default: float | None = None
) -> list[SentenceStart]:
    """Sentence starts at `times`; loudness from `loud` by index, else `default` (None: none)."""
    loud = loud or {}
    return [SentenceStart(t, loud.get(i, default)) for i, t in enumerate(times)]


def select(
    starts: list[SentenceStart],
    first: int = 0,
    boundaries: list[float] | None = None,
    cuts: list[float] | None = None,
    duration: float = 30.0,
    cfg: ZoomConfig = CFG,
    excluded: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float]]:
    return select_zooms(
        starts, first, boundaries or [], cuts or [], duration, cfg, excluded=excluded or ()
    )


# --- selection: which sentences ---------------------------------------------------------------


def test_with_no_audio_only_the_first_line_is_picked() -> None:
    starts = sentences([0.0, 5.0, 10.0, 15.0, 20.0])  # no loudness anywhere
    assert select(starts, first=0) == [(0.0, 6.0)]
    assert select(starts, first=2) == [(10.0, 16.0)]


def test_the_loudest_30_percent_are_picked_besides_the_first_line() -> None:
    starts = sentences(
        [10.0 * i for i in range(10)], loud={3: -10.0, 7: -12.0, 5: -14.0}, default=-30.0
    )
    # n = 10: ceil(0.3 * 10) = 3 loudest are sentences 3, 5 and 7; plus sentence 0
    assert select(starts, first=0, duration=100.0) == [
        (0.0, 6.0),
        (30.0, 36.0),
        (50.0, 56.0),
        (70.0, 76.0),
    ]


def test_the_share_rounds_up() -> None:
    starts = sentences(
        [20.0 * i for i in range(4)], loud={1: -5.0, 2: -6.0, 3: -7.0}, default=-30.0
    )
    # n = 4: ceil(0.3 * 4) = 2 loudest are 1 and 2; sentence 3 is third
    assert select(starts, first=0, duration=100.0) == [(0.0, 6.0), (20.0, 26.0), (40.0, 46.0)]


def test_a_loudness_tie_goes_to_the_earlier_sentence() -> None:
    starts = sentences([10.0 * i for i in range(10)], loud={2: -9.0, 6: -9.0}, default=-30.0)
    cfg = ZoomConfig(enabled=True, loud_pct=0.1)  # one loudest
    assert select(starts, first=0, duration=100.0, cfg=cfg) == [(0.0, 6.0), (20.0, 26.0)]


def test_a_sentence_without_loudness_never_ranks() -> None:
    starts = sentences([10.0 * i for i in range(10)], loud={4: -20.0}, default=None)
    cfg = ZoomConfig(enabled=True, loud_pct=0.5)  # five slots, one sentence can fill one
    assert select(starts, first=0, duration=100.0, cfg=cfg) == [(0.0, 6.0), (40.0, 46.0)]


def test_loud_pct_zero_leaves_the_first_line_alone() -> None:
    starts = sentences([0.0, 10.0, 20.0], loud={1: -5.0}, default=-30.0)
    cfg = ZoomConfig(enabled=True, loud_pct=0.0)
    assert select(starts, first=0, cfg=cfg) == [(0.0, 6.0)]


def test_no_sentences_no_zooms() -> None:
    assert select([]) == []


def test_a_first_index_outside_the_list_picks_nothing_extra() -> None:
    starts = sentences([0.0, 10.0])
    assert select(starts, first=-1) == []
    assert select(starts, first=5) == []


# --- selection: spacing and holds -------------------------------------------------------------


def test_the_hold_ends_at_the_next_pick_when_it_is_far_enough() -> None:
    starts = sentences([0.0, 4.5], loud={1: -5.0}, default=None)
    assert select(starts, first=0) == [(0.0, 4.5)]


def test_the_hold_ends_at_the_max_hold_when_the_next_pick_is_too_close() -> None:
    starts = sentences([0.0, 3.0, 20.0], loud={1: -5.0}, default=None)
    # the pick at 3.0 is under min_gap_s after the start: the hold runs to max_hold_s instead
    assert select(starts, first=0) == [(0.0, 6.0)]


def test_a_pick_under_the_gap_after_a_zoom_out_is_skipped() -> None:
    starts = sentences([0.0, 4.0, 7.0, 8.0, 16.0], loud={1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0})
    cfg = ZoomConfig(enabled=True, loud_pct=1.0)
    # zoom 0 -> 4 (ends at the pick at 4.0); the picks at 4.0 and 7.0 are under 4 s after that
    # out; 8.0 is exactly 4 s after it, so it zooms in and ends at max_hold_s (16.0 is too far)
    assert select(starts, first=0, cfg=cfg) == [(0.0, 4.0), (8.0, 14.0)]


def test_a_pick_exactly_min_gap_after_the_out_is_taken() -> None:
    starts = sentences([0.0, 6.0, 10.0], loud={1: 0.0, 2: 0.0})
    cfg = ZoomConfig(enabled=True, loud_pct=1.0)
    # the first zoom ends at the pick at 6.0; 10.0 is exactly 4 s after that out
    assert select(starts, first=0, cfg=cfg) == [(0.0, 6.0), (10.0, 16.0)]


def test_the_clip_can_end_zoomed_in() -> None:
    starts = sentences([14.0, 20.0])
    assert select(starts, first=0, duration=20.0) == [(14.0, 20.0)]
    assert select(sentences([16.0]), first=0, duration=20.0) == [(16.0, 20.0)]


def test_a_pick_with_too_little_clip_left_is_skipped() -> None:
    assert select(sentences([17.0]), first=0, duration=20.0) == []  # 3 s left


def test_a_pick_at_the_end_is_skipped() -> None:
    assert select(sentences([20.0]), first=0, duration=20.0) == []


# --- selection: camera cuts and shots ---------------------------------------------------------


@pytest.mark.parametrize(("t", "zooms"), [(9.6, 0), (9.8, 0), (10.3, 0), (10.4, 0), (10.5, 1)])
def test_no_zoom_in_within_half_a_second_of_a_cut(t: float, zooms: int) -> None:
    result = select(sentences([t]), boundaries=[10.0], cuts=[10.0])
    assert len(result) == zooms


def test_a_pick_just_after_a_cut_is_skipped_and_one_half_a_second_after_zooms() -> None:
    assert select(sentences([10.5]), boundaries=[10.0], cuts=[10.0]) == [(10.5, 16.5)]


def test_a_speaker_switch_is_a_boundary_but_not_a_cut() -> None:
    # no cut near 9.8: the zoom may start there if the shot has min_gap_s left (it has not)
    assert select(sentences([9.8]), boundaries=[10.0], cuts=[]) == []
    assert select(sentences([5.0]), boundaries=[10.0], cuts=[]) == [(5.0, 10.0)]


def test_a_pick_with_too_little_shot_left_is_skipped() -> None:
    assert select(sentences([9.0]), boundaries=[12.0], cuts=[12.0]) == []  # 3 s of shot
    assert select(sentences([8.0]), boundaries=[12.0], cuts=[12.0]) == [(8.0, 12.0)]  # 4 s


def test_the_hold_ends_at_the_shot_boundary() -> None:
    assert select(sentences([0.0]), boundaries=[5.0]) == [(0.0, 5.0)]


def test_a_boundary_at_the_pick_itself_does_not_end_the_hold() -> None:
    # a speaker switch exactly on the sentence start: the zoom belongs to the new shot
    assert select(sentences([5.0]), boundaries=[5.0], duration=30.0) == [(5.0, 11.0)]


def test_the_clip_end_counts_as_a_boundary_listed_or_not() -> None:
    assert select(sentences([16.0]), boundaries=[], duration=20.0) == [(16.0, 20.0)]
    assert select(sentences([16.0]), boundaries=[20.0], duration=20.0) == [(16.0, 20.0)]


def test_an_end_just_before_a_cut_moves_onto_the_cut() -> None:
    # 3.7 + 6.0 = 9.7 is 0.3 s before the cut at 10.0; the shot has no boundary listed
    assert select(sentences([3.7]), cuts=[10.0]) == [(3.7, 10.0)]


def test_an_end_at_the_next_pick_just_before_a_cut_moves_onto_the_cut() -> None:
    starts = sentences([4.5, 9.7], loud={1: -5.0}, default=None)
    assert select(starts, cuts=[10.0], boundaries=[10.0]) == [(4.5, 10.0)]


def test_an_end_half_a_second_or_more_before_a_cut_stays() -> None:
    assert select(sentences([3.7]), cuts=[10.2]) == [(3.7, 9.7)]


# --- selection: excluded spans ----------------------------------------------------------------


def test_a_pick_inside_an_excluded_span_is_skipped() -> None:
    assert select(sentences([0.0]), excluded=[(0.0, 8.0)]) == []
    assert select(sentences([3.0]), excluded=[(0.0, 8.0)]) == []


def test_a_pick_after_an_excluded_span_zooms() -> None:
    assert select(sentences([8.0]), excluded=[(0.0, 8.0)]) == [(8.0, 14.0)]


def test_a_hold_stops_at_an_excluded_span() -> None:
    assert select(sentences([6.0]), excluded=[(10.0, 20.0)]) == [(6.0, 10.0)]
    assert select(sentences([8.0]), excluded=[(10.0, 20.0)]) == []  # only 2 s before it


# --- upscale guard ----------------------------------------------------------------------------


def test_upscale_factor_of_a_wide_source() -> None:
    # 810x1440 zooms to 724x1286: 1920 / 1286 = 1.493
    assert upscale_factor(CropBox(810, 1440, 0, 0), 1.12, 1080, 1920) == pytest.approx(
        1.493, abs=1e-3
    )


def test_upscale_factor_of_a_small_source_exceeds_the_guard() -> None:
    # the 640x360 test video: 202x360 zooms to 180x322; 1080 / 180 = 6
    assert upscale_factor(CropBox(202, 360, 218, 0), 1.12, 1080, 1920) > 2.0


def test_upscale_factor_on_1080p_sits_just_under_the_guard() -> None:
    # 606x1080 zooms to 542x964 (even): 1080 / 542 = 1.99, only just under the guard
    factor = upscale_factor(CropBox(606, 1080, 656, 0), 1.12, 1080, 1920)
    assert 1.9 < factor < 2.0


# --- zoom_box ---------------------------------------------------------------------------------

WIDE = CropBox(w=810, h=1440, x=800, y=0)  # a 9:16 window in a 2560x1440 frame


def test_zoom_box_is_even_and_tighter() -> None:
    box = zoom_box(WIDE, 1.12, 0.5, 2560, 1440)
    assert (box.w, box.h) == (724, 1286)
    assert all(v % 2 == 0 for v in (box.w, box.h, box.x, box.y))


def test_zoom_box_is_centred_horizontally_on_the_window() -> None:
    box = zoom_box(WIDE, 1.12, 0.5, 2560, 1440)
    # the window's centre is 800 + 405 = 1205; the zoomed centre is within a pixel of it
    assert box.x == 844
    assert abs((box.x + box.w / 2) - 1205) <= 1


def test_zoom_box_is_centred_vertically_on_the_face() -> None:
    box = zoom_box(WIDE, 1.12, 0.5, 2560, 1440)
    assert box.y == 76
    assert abs((box.y + box.h / 2) - 0.5 * 1440) <= 1
    high = zoom_box(WIDE, 1.12, 0.45, 2560, 1440)
    assert high.y == 4  # 648 - 643 = 5, rounded to even


def test_zoom_box_is_clamped_at_the_top_and_bottom() -> None:
    assert zoom_box(WIDE, 1.12, 0.0, 2560, 1440).y == 0
    assert zoom_box(WIDE, 1.12, 1.0, 2560, 1440).y == 154  # 1440 - 1286


def test_zoom_box_is_clamped_at_the_left_and_right() -> None:
    off_left = CropBox(w=810, h=1440, x=-100, y=0)
    assert zoom_box(off_left, 1.12, 0.5, 2560, 1440).x == 0
    off_right = CropBox(w=810, h=1440, x=2400, y=0)
    assert zoom_box(off_right, 1.12, 0.5, 2560, 1440).x == 1836  # 2560 - 724


def test_zoom_box_stays_inside_an_odd_sized_frame_and_even() -> None:
    box = zoom_box(CropBox(w=810, h=1440, x=1750, y=0), 1.12, 1.0, 2561, 1441)
    assert box.x % 2 == 0 and box.y % 2 == 0
    assert box.x + box.w <= 2561 and box.y + box.h <= 1441


def test_zoom_box_at_a_bigger_scale() -> None:
    box = zoom_box(WIDE, 1.5, 0.5, 2560, 1440)
    assert (box.w, box.h) == (540, 960)


# --- zoomed_path ------------------------------------------------------------------------------

BASE = CropBox(w=810, h=1440, x=0, y=0)  # x is replaced by the path's
FRAME = (2560, 1440)


def base_at(x: int) -> CropBox:
    return CropBox(810, 1440, x, 0)


def zoomed_at(x: int, face_cy: float = 0.5) -> CropBox:
    return zoom_box(base_at(x), 1.12, face_cy, *FRAME)


def path_with(
    path: list[tuple[float, int]], spans: list[ZoomSpan], duration: float = 30.0
) -> list[tuple[float, CropBox]]:
    return zoomed_path(path, BASE, spans, 1.12, *FRAME, duration)


def test_without_spans_the_boxes_are_the_base_window_with_the_paths_x() -> None:
    path = [(0.0, 800), (3.0, 900), (7.5, 700)]
    assert path_with(path, []) == [(0.0, base_at(800)), (3.0, base_at(900)), (7.5, base_at(700))]


def test_a_single_path_entry_without_spans_is_one_box() -> None:
    assert path_with([(0.0, 800)], []) == [(0.0, base_at(800))]


def test_a_zoom_in_and_out_adds_two_entries() -> None:
    boxes = path_with([(0.0, 800)], [ZoomSpan(2.0, 6.0, 0.5)])
    assert boxes == [(0.0, base_at(800)), (2.0, zoomed_at(800)), (6.0, base_at(800))]


def test_a_zoom_from_the_start_makes_the_first_entry_zoomed() -> None:
    boxes = path_with([(0.0, 800)], [ZoomSpan(0.0, 6.0, 0.5)])
    assert boxes == [(0.0, zoomed_at(800)), (6.0, base_at(800))]


def test_an_x_change_inside_a_span_moves_the_zoomed_window() -> None:
    boxes = path_with([(0.0, 800), (3.0, 900)], [ZoomSpan(0.0, 6.0, 0.5)])
    assert boxes == [(0.0, zoomed_at(800)), (3.0, zoomed_at(900)), (6.0, base_at(900))]
    assert (boxes[0][1].w, boxes[0][1].h) == (boxes[1][1].w, boxes[1][1].h)


def test_a_path_step_that_leaves_the_zoomed_box_unchanged_makes_no_entry() -> None:
    # 800 -> 802 rounds to the same zoomed x; only the zoom-out shows the new x
    assert zoomed_at(800) == zoomed_at(802)
    boxes = path_with([(0.0, 800), (3.0, 802)], [ZoomSpan(0.0, 6.0, 0.5)])
    assert boxes == [(0.0, zoomed_at(800)), (6.0, base_at(802))]


def test_a_span_starting_on_a_path_time_is_one_entry() -> None:
    boxes = path_with([(0.0, 800), (4.0, 900)], [ZoomSpan(4.0, 10.0, 0.5)])
    assert boxes == [(0.0, base_at(800)), (4.0, zoomed_at(900)), (10.0, base_at(900))]


def test_a_zoom_out_on_a_boundary_jump_is_one_entry() -> None:
    boxes = path_with([(0.0, 800), (6.0, 1000)], [ZoomSpan(0.0, 6.0, 0.5)])
    assert boxes == [(0.0, zoomed_at(800)), (6.0, base_at(1000))]


def test_a_clip_that_ends_zoomed_has_no_zoom_out_entry() -> None:
    boxes = path_with([(0.0, 800)], [ZoomSpan(24.0, 30.0, 0.5)], duration=30.0)
    assert boxes == [(0.0, base_at(800)), (24.0, zoomed_at(800))]


def test_each_span_uses_its_own_face_height() -> None:
    spans = [ZoomSpan(2.0, 6.0, 0.4), ZoomSpan(12.0, 16.0, 0.6)]
    boxes = path_with([(0.0, 800)], spans)
    assert [b for _, b in boxes] == [
        base_at(800),
        zoomed_at(800, 0.4),
        base_at(800),
        zoomed_at(800, 0.6),
        base_at(800),
    ]
    assert zoomed_at(800, 0.4).y != zoomed_at(800, 0.6).y


def test_the_first_entry_is_at_zero_even_if_the_path_starts_later() -> None:
    boxes = path_with([(0.0, 800)], [ZoomSpan(1.0, 5.0, 0.5)])
    assert boxes[0][0] == 0.0


def test_an_empty_path_is_refused() -> None:
    with pytest.raises(ValueError, match="path"):
        path_with([], [])
