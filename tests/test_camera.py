"""Track linking, speaker choice, shots and the camera path. All data is synthetic."""

from collections.abc import Callable
from dataclasses import replace

import pytest

from clipmachine.models import SpeakerConfig
from clipmachine.utils.camera import (
    FALLBACK_FACES_TOO_SMALL,
    FALLBACK_NO_FACE,
    FALLBACK_SOURCE_NOT_WIDER,
    FaceSample,
    Followed,
    Shot,
    Track,
    _variance,
    build_shots,
    camera_path,
    choose_speakers,
    fallback_reason,
    find_cuts,
    follow_segments,
    link_tracks,
    signature_distance,
    speech_windows,
    split_at_cuts,
    summarise,
)

FPS = 10
WINDOW = 0.5
PER_WINDOW = 5  # samples per window at 10 fps


def face(t: float, cx: float, cy: float = 0.5, h: float = 0.3, mouth: float = 0.0) -> FaceSample:
    return FaceSample(t=t, cx=cx, cy=cy, w=h / 2, h=h, mouth=mouth)


def cfg(**changes: float) -> SpeakerConfig:
    return SpeakerConfig(**changes)  # type: ignore[arg-type]


def time_of(k: int) -> float:
    return round(k / FPS, 3)


def talking(k: int) -> float:
    """A mouth that moves: alternates open and closed every sample."""
    return 0.4 if k % 2 == 0 else 0.0


def two_people(
    windows: int, a_talks: Callable[[int], bool], b_talks: Callable[[int], bool]
) -> list[list[FaceSample]]:
    """Person A at the left, B at the right, one sample per 0.1 s; `*_talks(window)` decides who
    moves their mouth."""
    frames: list[list[FaceSample]] = []
    for k in range(windows * PER_WINDOW):
        w = k // PER_WINDOW
        t = time_of(k)
        frames.append(
            [
                face(t, 0.25, mouth=talking(k) if a_talks(w) else 0.05),
                face(t, 0.75, mouth=talking(k) if b_talks(w) else 0.05),
            ]
        )
    return frames


# --- link_tracks --------------------------------------------------------------------------


def test_one_face_is_one_track() -> None:
    frames = [[face(time_of(k), 0.5 + 0.01 * k)] for k in range(10)]
    tracks = link_tracks(frames, 0.12)
    assert len(tracks) == 1
    assert [i for i, _ in tracks[0].points] == list(range(10))


def test_faces_further_apart_than_the_threshold_are_new_tracks() -> None:
    frames = [[face(0.0, 0.2)], [face(0.1, 0.6)]]
    assert len(link_tracks(frames, 0.12)) == 2


def test_two_faces_of_one_sample_never_share_a_track() -> None:
    frames = [[face(0.0, 0.50), face(0.0, 0.52)], [face(0.1, 0.51), face(0.1, 0.53)]]
    tracks = link_tracks(frames, 0.12)
    assert len(tracks) == 2
    for track in tracks:
        assert len(track.by_frame) == 2  # one face per sample, both samples


def test_two_faces_crossing_keep_their_identities() -> None:
    # A walks left to right at the top, B right to left lower down: they pass at the same x.
    frames = []
    for k in range(11):
        t = time_of(k)
        frames.append([face(t, 0.2 + 0.06 * k, cy=0.3), face(t, 0.8 - 0.06 * k, cy=0.6)])
    tracks = link_tracks(frames, 0.12)
    assert len(tracks) == 2
    top, low = tracks
    assert all(s.cy == 0.3 for s in top.by_frame.values())
    assert all(s.cy == 0.6 for s in low.by_frame.values())
    assert len(top.by_frame) == len(low.by_frame) == 11


def test_a_face_that_vanishes_briefly_returns_to_its_track() -> None:
    frames = [[face(0.0, 0.5)], [face(0.1, 0.51)], [], [], [face(0.4, 0.52)]]
    tracks = link_tracks(frames, 0.12)
    assert len(tracks) == 1
    assert sorted(tracks[0].by_frame) == [0, 1, 4]


def test_a_face_gone_for_too_long_starts_a_new_track() -> None:
    frames = [[face(0.0, 0.5)], [], [], [], [], [face(0.5, 0.5)]]
    assert len(link_tracks(frames, 0.12)) == 2


def test_no_faces_no_tracks() -> None:
    assert link_tracks([[], [], []], 0.12) == []


# --- speech_windows -----------------------------------------------------------------------


def test_speech_windows_mark_overlap_and_keep_silence_false() -> None:
    words = [(0.1, 0.4), (1.2, 1.6)]
    assert speech_windows(words, 3.0, 0.5) == [True, False, True, True, False, False]


def test_a_word_ending_on_a_window_edge_is_not_in_the_next_window() -> None:
    assert speech_windows([(0.0, 0.5)], 1.0, 0.5) == [True, False]


def test_speech_windows_clamp_words_outside_the_clip() -> None:
    assert speech_windows([(-1.0, 0.2), (9.0, 10.0)], 1.0, 0.5) == [True, False]


def test_speech_windows_need_the_configured_number_of_words() -> None:
    words = [(0.0, 0.2), (0.6, 0.7), (0.8, 0.9)]
    assert speech_windows(words, 1.0, 0.5, min_words=2) == [False, True]


# --- choose_speakers ----------------------------------------------------------------------


def choices(
    frames: list[list[FaceSample]], speech: list[bool], **changes: float
) -> list[int | None]:
    config = cfg(**changes)
    tracks = link_tracks(frames, config.link_max_distance)
    return choose_speakers(tracks, speech, WINDOW, config)


def test_the_track_with_the_moving_mouth_is_the_speaker() -> None:
    frames = two_people(6, a_talks=lambda w: False, b_talks=lambda w: True)
    assert choices(frames, [True] * 6) == [1] * 6


def test_unreadable_mouths_are_left_out_of_the_variance() -> None:
    samples = [face(0.0, 0.5, mouth=0.4), face(0.1, 0.5, mouth=0.0)]
    unreadable = [
        FaceSample(t=0.05, cx=0.5, cy=0.5, w=0.15, h=0.3, mouth=None),
        FaceSample(t=0.15, cx=0.5, cy=0.5, w=0.15, h=0.3, mouth=None),
    ]
    assert _variance(samples + unreadable) == _variance(samples) > 0


def test_fewer_than_two_readable_mouths_have_no_variance() -> None:
    unreadable = FaceSample(t=0.0, cx=0.5, cy=0.5, w=0.15, h=0.3, mouth=None)
    assert _variance([unreadable, unreadable, face(0.1, 0.5, mouth=0.4)]) == 0.0
    assert _variance([unreadable, unreadable]) == 0.0


def test_a_track_with_unreadable_mouths_never_beats_one_that_talks() -> None:
    frames = two_people(6, a_talks=lambda w: False, b_talks=lambda w: True)
    # A is the larger face but its mouth cannot be read; B talks
    frames = [[replace(left, mouth=None, h=0.5), right] for left, right in frames]
    assert choices(frames, [True] * 6) == [1] * 6


def test_the_speaker_changes_after_confirm_windows_and_is_backdated() -> None:
    frames = two_people(8, a_talks=lambda w: w < 4, b_talks=lambda w: w >= 4)
    result = choices(frames, [True] * 8, confirm_windows=2)
    # B starts talking in window 4; two confirming windows (4 and 5), switch backdated to 4
    assert result == [0, 0, 0, 0, 1, 1, 1, 1]


def test_one_noisy_window_does_not_switch() -> None:
    frames = two_people(8, a_talks=lambda w: True, b_talks=lambda w: w == 4)
    # A talks all the time and so does B in window 4: B's variance is not 1.5x A's
    assert choices(frames, [True] * 8) == [0] * 8

    frames = two_people(8, a_talks=lambda w: w < 4 or w > 4, b_talks=lambda w: w == 4)
    assert choices(frames, [True] * 8, confirm_windows=2) == [0] * 8  # one window: no switch


def test_confirm_windows_one_switches_at_once() -> None:
    frames = two_people(6, a_talks=lambda w: w < 3, b_talks=lambda w: w >= 3)
    assert choices(frames, [True] * 6, confirm_windows=1) == [0, 0, 0, 1, 1, 1]


def test_silent_windows_keep_the_current_shot() -> None:
    frames = two_people(8, a_talks=lambda w: w < 3, b_talks=lambda w: w >= 3)
    speech = [True, True, True, False, False, False, False, False]
    # B moves their mouth from window 3 but the transcript is silent there
    assert choices(frames, speech) == [0] * 8


def test_leading_windows_take_the_first_speaker() -> None:
    frames = two_people(6, a_talks=lambda w: False, b_talks=lambda w: True)
    speech = [False, False, True, True, True, True]
    assert choices(frames, speech) == [1] * 6


def test_no_speech_shows_the_largest_face() -> None:
    frames = [[face(time_of(k), 0.25, h=0.2), face(time_of(k), 0.75, h=0.4)] for k in range(10)]
    assert choices(frames, [False, False]) == [1, 1]


def test_no_faces_gives_none() -> None:
    assert choices([[], []], [True, True]) == [None, None]


def test_a_speaker_who_leaves_the_frame_hands_over_to_the_one_left() -> None:
    frames = two_people(8, a_talks=lambda w: w < 3, b_talks=lambda w: False)
    for k in range(3 * PER_WINDOW, len(frames)):  # A is gone from window 3 on
        frames[k] = [frames[k][1]]
    result = choices(frames, [True] * 8, confirm_windows=2)
    assert result[:4] == [0, 0, 0, 0]  # held for one window, then handed over
    assert result[-1] == 1


# --- build_shots --------------------------------------------------------------------------


def test_runs_become_shots() -> None:
    shots = build_shots([0, 0, 0, 1, 1, 1], WINDOW, 1.0)
    assert shots == [Shot(0.0, 1.5, 0), Shot(1.5, 3.0, 1)]


def test_a_short_shot_is_merged_into_the_longer_neighbour() -> None:
    # B for one window (0.5 s) between A runs: below the 1.5 s minimum
    shots = build_shots([0, 0, 0, 0, 1, 0, 0, 0], WINDOW, 1.5)
    assert shots == [Shot(0.0, 4.0, 0)]


def test_no_shot_is_shorter_than_the_minimum() -> None:
    pattern = [0] * 5 + [1] * 2 + [0] * 1 + [1] * 6 + [2] * 3
    shots = build_shots(pattern, WINDOW, 1.5)
    assert all(s.end - s.start >= 1.5 for s in shots)
    assert shots[0].start == 0.0 and shots[-1].end == len(pattern) * WINDOW
    assert all(a.end == b.start for a, b in zip(shots, shots[1:], strict=False))
    assert all(a.track != b.track for a, b in zip(shots, shots[1:], strict=False))


def test_a_clip_shorter_than_the_minimum_stays_one_shot() -> None:
    shots = build_shots([0, 1], WINDOW, 1.5)
    assert len(shots) == 1
    assert (shots[0].start, shots[0].end) == (0.0, 1.0)


def test_the_last_shot_ends_at_the_clip_duration() -> None:
    shots = build_shots([0, 0, 1, 1, 1], WINDOW, 0.5, duration=2.2)
    assert shots[-1].end == 2.2


def test_no_windows_no_shots() -> None:
    assert build_shots([], WINDOW, 1.5) == []


# --- camera_path --------------------------------------------------------------------------


def static_track(track_id: int, cx: float, count: int) -> Track:
    return Track(track_id, {k: face(time_of(k), cx) for k in range(count)})


TIMES = [time_of(k) for k in range(20)]


def test_cut_between_shots_is_a_hard_jump() -> None:
    tracks = [static_track(0, 0.2, 20), static_track(1, 0.8, 20)]
    shots = [Shot(0.0, 1.0, 0), Shot(1.0, 2.0, 1)]
    path = camera_path(shots, tracks, 1000, 200, TIMES, cfg())
    assert path[:10] == [100] * 10
    assert path[10:] == [700] * 10  # no glide between the two


def test_small_movements_inside_the_dead_zone_do_not_move_the_crop() -> None:
    # dead zone is 8% of 200 px = 16 px; the face wobbles by +-5 px
    cxs = [0.5 + (0.005 if k % 2 else -0.005) for k in range(20)]
    track = Track(0, {k: face(time_of(k), cxs[k]) for k in range(20)})
    path = camera_path([Shot(0.0, 2.0, 0)], [track], 1000, 200, TIMES, cfg())
    assert len(set(path)) == 1


def test_a_big_move_is_followed_smoothly() -> None:
    cxs = [0.3] * 5 + [0.7] * 15
    track = Track(0, {k: face(time_of(k), cxs[k]) for k in range(20)})
    path = camera_path([Shot(0.0, 2.0, 0)], [track], 1000, 200, TIMES, cfg(smoothing=0.25))
    assert path[4] == 200
    rising = path[5:]
    assert rising == sorted(rising)
    assert 200 < rising[0] < 600  # a step toward the target, not a jump
    assert rising[0] == 300  # 200 + 0.25 * (600 - 200)
    assert rising[-1] > 560 and rising[-1] <= 600


def test_smoothing_one_follows_exactly() -> None:
    cxs = [0.3] * 5 + [0.7] * 15
    track = Track(0, {k: face(time_of(k), cxs[k]) for k in range(20)})
    path = camera_path([Shot(0.0, 2.0, 0)], [track], 1000, 200, TIMES, cfg(smoothing=1.0))
    assert path[5] == 600


def test_the_crop_is_clamped_to_the_frame_at_both_edges() -> None:
    left = Track(0, {k: face(time_of(k), 0.0) for k in range(20)})
    right = Track(1, {k: face(time_of(k), 1.0) for k in range(20)})
    assert set(camera_path([Shot(0.0, 2.0, 0)], [left], 1000, 200, TIMES, cfg())) == {0}
    assert set(camera_path([Shot(0.0, 2.0, 1)], [right], 1000, 200, TIMES, cfg())) == {800}


def test_positions_are_even_integers() -> None:
    cxs = [0.31 + 0.013 * k for k in range(20)]
    track = Track(0, {k: face(time_of(k), cxs[k]) for k in range(20)})
    path = camera_path([Shot(0.0, 2.0, 0)], [track], 1001, 203, TIMES, cfg())
    assert all(isinstance(x, int) and x % 2 == 0 for x in path)
    assert all(0 <= x <= 798 for x in path)


def test_a_sample_without_a_face_holds_the_last_position() -> None:
    by_frame = {k: face(time_of(k), 0.3) for k in range(5)}
    by_frame.update({k: face(time_of(k), 0.7) for k in range(12, 20)})
    track = Track(0, by_frame)
    path = camera_path([Shot(0.0, 2.0, 0)], [track], 1000, 200, TIMES, cfg(smoothing=1.0))
    assert path[4] == 200
    assert path[5:12] == [200] * 7  # held while the face is missing
    assert path[12] == 600


def test_samples_before_the_first_face_use_the_first_position() -> None:
    track = Track(0, {k: face(time_of(k), 0.8) for k in range(8, 20)})
    path = camera_path([Shot(0.0, 2.0, 0)], [track], 1000, 200, TIMES, cfg())
    assert set(path) == {700}


def test_a_clip_that_never_has_a_face_stays_centred() -> None:
    assert camera_path([Shot(0.0, 2.0, None)], [], 1000, 200, TIMES, cfg()) == [400] * 20
    assert camera_path([], [], 1000, 200, TIMES, cfg()) == [400] * 20


# --- fallback_reason and summarise --------------------------------------------------------


def test_no_face_in_the_whole_clip_is_a_fallback() -> None:
    assert fallback_reason([[], []], cfg(), 1920, 606) == FALLBACK_NO_FACE


def test_small_faces_are_a_fallback() -> None:
    frames = [[face(time_of(k), 0.8, h=0.05)] for k in range(10)]  # a webcam box
    assert fallback_reason(frames, cfg(), 1920, 606) == FALLBACK_FACES_TOO_SMALL


def test_a_few_small_samples_are_not_a_fallback() -> None:
    frames = [[face(time_of(k), 0.5, h=0.05 if k < 2 else 0.3)] for k in range(10)]
    assert fallback_reason(frames, cfg(), 1920, 606) is None


def test_the_largest_face_decides_not_the_smallest() -> None:
    frames = [[face(0.0, 0.2, h=0.04), face(0.0, 0.7, h=0.3)] for _ in range(10)]
    assert fallback_reason(frames, cfg(), 1920, 606) is None


def test_a_source_not_wider_than_the_crop_is_a_fallback() -> None:
    frames = [[face(0.0, 0.5)]]
    assert fallback_reason(frames, cfg(), 606, 606) == FALLBACK_SOURCE_NOT_WIDER


def test_summary_counts() -> None:
    frames = two_people(6, a_talks=lambda w: w < 3, b_talks=lambda w: w >= 3)
    frames[0] = []
    tracks = link_tracks(frames, 0.12)
    shots = [Shot(0.0, 1.5, 0), Shot(1.5, 3.0, 1)]
    summary = summarise(frames, tracks, shots)
    assert summary.faces_seen == 2
    assert summary.switches == 1
    assert summary.face_time_pct == pytest.approx(100.0 * 29 / 30, abs=0.1)


def test_summary_counts_faces_per_sample_and_readable_mouths() -> None:
    unreadable = FaceSample(t=0.0, cx=0.2, cy=0.5, w=0.15, h=0.3, mouth=None)
    frames = [
        [unreadable, face(0.0, 0.7)],
        [],
        [face(0.2, 0.7, mouth=0.3)],
        [],
    ]
    summary = summarise(frames, link_tracks(frames, 0.12), [])
    assert summary.faces_per_sample == 0.75
    assert summary.mouth_read_pct == 66.7


def test_summary_with_no_faces_has_zero_mouth_percentage() -> None:
    summary = summarise([[], []], [], [])
    assert (summary.faces_per_sample, summary.mouth_read_pct) == (0.0, 0.0)


def test_summary_does_not_count_a_boundary_on_a_cut_as_a_switch() -> None:
    shots = [Shot(0.0, 1.0, 0), Shot(1.0, 2.0, 1), Shot(2.0, 3.0, 0)]
    assert summarise([], [], shots).switches == 2
    assert summarise([], [], shots, cut_times=[1.0]).switches == 1
    assert summarise([], [], shots, cut_times=[1.0000004]).switches == 1
    assert summarise([], [], shots, cut_times=[1.5]).switches == 2


# --- cuts and segments --------------------------------------------------------------------

A = [1.0, 0.0, 0.0]
B = [0.0, 1.0, 0.0]
C = [0.0, 0.0, 1.0]


def test_signature_distance_is_half_the_absolute_differences() -> None:
    assert signature_distance(A, A) == 0.0
    assert signature_distance(A, B) == 1.0
    assert signature_distance([0.5, 0.5, 0.0], A) == pytest.approx(0.5)


def test_a_hard_change_is_a_cut() -> None:
    assert find_cuts([A, A, A, B, B, B], 0.5) == [3]


def test_a_slow_drift_is_never_a_cut() -> None:
    drift = [[1 - 0.1 * i, 0.1 * i, 0.0] for i in range(11)]  # A to B in steps of 0.1
    assert signature_distance(drift[0], drift[-1]) == pytest.approx(1.0)
    assert find_cuts(drift, 0.3) == []


def test_a_one_sample_flash_is_not_a_cut() -> None:
    assert find_cuts([A, A, B, A, A], 0.5) == []


def test_two_real_cuts() -> None:
    assert find_cuts([A, A, B, B, C, C], 0.5) == [2, 4]


def test_a_cut_on_the_last_sample() -> None:
    assert find_cuts([A, A, B], 0.5) == [2]


def test_no_cuts_in_empty_or_single_sample_input() -> None:
    assert find_cuts([], 0.5) == []
    assert find_cuts([A], 0.5) == []


def test_no_cuts_is_one_segment() -> None:
    assert split_at_cuts(10, []) == [(0, 10)]


def test_several_cuts_make_several_segments() -> None:
    assert split_at_cuts(10, [3, 7]) == [(0, 3), (3, 7), (7, 10)]


def test_out_of_range_and_duplicate_cuts_are_ignored() -> None:
    assert split_at_cuts(10, [0, 10, 12, -1, 3, 3]) == [(0, 3), (3, 10)]
    assert split_at_cuts(0, [1]) == []


def clip_times(count: int) -> list[float]:
    return [time_of(k) for k in range(count)]


def follow(
    frames: list[list[FaceSample]],
    cuts: list[int] | None = None,
    words: list[tuple[float, float]] | None = None,
    **changes: float,
) -> Followed:
    return follow_segments(
        frames,
        clip_times(len(frames)),
        words or [],
        cuts or [],
        len(frames) / FPS,
        1000,
        200,
        cfg(**changes),
    )


def assert_gap_free(shots: list[Shot], duration: float) -> None:
    assert shots[0].start == 0.0
    assert shots[-1].end == pytest.approx(duration)
    assert all(a.end == pytest.approx(b.start) for a, b in zip(shots, shots[1:], strict=False))


def test_tracks_never_link_across_a_cut_even_at_the_same_position() -> None:
    frames = [[face(time_of(k), 0.5)] for k in range(20)]
    done = follow(frames, cuts=[10])
    assert [t.id for t in done.tracks] == [0, 1]
    assert sorted(done.tracks[0].by_frame) == list(range(10))
    assert sorted(done.tracks[1].by_frame) == list(range(10, 20))
    assert done.tracks[1].by_frame[12] is frames[12][0]  # the original sample, clip-relative t


def test_no_crop_position_is_carried_across_a_cut_to_a_segment_without_a_face() -> None:
    frames = [[face(time_of(k), 0.2)] if k < 10 else [] for k in range(20)]
    done = follow(frames, cuts=[10])
    assert done.crop_xs[:10] == [100] * 10
    assert done.crop_xs[10:] == [400] * 10  # centred, not held at 100
    assert follow(frames).crop_xs[10:] == [100] * 10  # without the cut it would be held


def test_the_first_sample_after_a_cut_is_already_on_the_new_face() -> None:
    frames = [[face(time_of(k), 0.2 if k < 10 else 0.8)] for k in range(20)]
    done = follow(frames, cuts=[10], smoothing=0.25)
    assert done.crop_xs[9] == 100
    assert done.crop_xs[10] == 700  # a jump, not a glide


def test_the_speaker_is_followed_in_a_fresh_segment() -> None:
    frames = [
        [face(time_of(k), 0.25, h=0.4, mouth=0.05), face(time_of(k), 0.75, h=0.2, mouth=talking(k))]
        for k in range(20)
    ]
    done = follow(frames, words=[(0.0, 2.0)])
    assert done.shots == [Shot(0.0, 2.0, 1)]  # the smaller face talks
    assert set(done.crop_xs) == {650}


def test_without_a_speaker_the_largest_face_is_followed() -> None:
    frames = [[face(time_of(k), 0.25, h=0.2), face(time_of(k), 0.75, h=0.4)] for k in range(20)]
    done = follow(frames)
    assert done.shots == [Shot(0.0, 2.0, 1)]
    assert set(done.crop_xs) == {650}


def test_among_equal_faces_the_most_central_is_followed() -> None:
    frames = [[face(time_of(k), 0.2), face(time_of(k), 0.55)] for k in range(20)]
    done = follow(frames)
    assert done.shots == [Shot(0.0, 2.0, 1)]  # not track 0, which has the lower id


def test_without_any_face_the_crop_is_centred() -> None:
    done = follow([[] for _ in range(20)])
    assert done.shots == [Shot(0.0, 2.0, None)]
    assert done.crop_xs == [400] * 20
    assert done.tracks == []


def test_a_cut_soon_after_a_speaker_change_still_moves_the_crop_at_once() -> None:
    first = two_people(3, a_talks=lambda w: w < 2, b_talks=lambda w: w >= 2)
    frames = first + [[face(time_of(k), 0.9)] for k in range(15, 30)]
    done = follow(frames, cuts=[15], words=[(0.0, 3.0)], confirm_windows=1)
    assert done.crop_xs[15:] == [800] * 15  # the minimum shot length does not hold it back
    assert any(s.start == 1.5 for s in done.shots)
    assert_gap_free(done.shots, 3.0)


def test_a_segment_shorter_than_the_minimum_shot_stays_one_shot() -> None:
    frames = [[face(time_of(k), 0.5)] for k in range(20)]
    done = follow(frames, cuts=[15], min_shot_seconds=1.5)
    assert [(s.start, s.end) for s in done.shots] == [(0.0, 1.5), (1.5, 2.0)]
    assert_gap_free(done.shots, 2.0)


def test_a_pending_challenger_does_not_complete_after_a_cut() -> None:
    frames = two_people(8, a_talks=lambda w: w < 3, b_talks=lambda w: w >= 3)
    words = [(0.0, 4.0)]
    joined = follow(frames, words=words)
    assert [(s.start, s.track) for s in joined.shots] == [(0.0, 0), (1.5, 1)]  # backdated

    split = follow(frames, cuts=[20], words=words)
    # B leads window 3 only once before the cut; the second window it would need is after it
    assert split.shots[0] == Shot(0.0, 2.0, 0)
    assert split.crop_xs[:20] == [150] * 20
    assert split.shots[1].start == 2.0


def test_the_speaker_changes_inside_a_segment_that_starts_after_a_cut() -> None:
    first = [[face(time_of(k), 0.5)] for k in range(20)]
    second = two_people(8, a_talks=lambda w: w < 4, b_talks=lambda w: w >= 4)
    second = [[replace(f, t=round(f.t + 2.0, 3)) for f in faces] for faces in second]
    done = follow(first + second, cuts=[20], words=[(2.0, 6.0)])
    # the windows of the second segment are counted from the cut, not from the clip start
    assert done.shots == [Shot(0.0, 2.0, 0), Shot(2.0, 4.0, 1), Shot(4.0, 6.0, 2)]
    assert done.switches == 1
    assert done.crop_xs[20:40] == [150] * 20
    assert done.crop_xs[40:] == [650] * 20


def test_switches_count_changes_inside_segments_only() -> None:
    frames = two_people(8, a_talks=lambda w: w < 3, b_talks=lambda w: w >= 3)
    words = [(0.0, 4.0)]
    assert follow(frames, words=words).switches == 1
    split = follow(frames, cuts=[20], words=words)
    assert split.switches == 0  # A to B across the cut is a cut, not a switch
    assert split.shots[0].track != split.shots[1].track


def test_without_cuts_the_result_is_what_the_five_functions_give() -> None:
    frames = two_people(8, a_talks=lambda w: w < 3, b_talks=lambda w: w >= 3)
    words = [(0.0, 1.2), (1.6, 3.9)]
    config = cfg()
    done = follow(frames, words=words)

    tracks = link_tracks(frames, config.link_max_distance)
    speech = speech_windows(words, 4.0, config.window_seconds, config.min_speech_words_per_window)
    choice = choose_speakers(tracks, speech, config.window_seconds, config)
    shots = build_shots(choice, config.window_seconds, config.min_shot_seconds, 4.0)
    assert done.tracks == tracks
    assert done.shots == shots
    assert done.crop_xs == camera_path(shots, tracks, 1000, 200, clip_times(40), config)
