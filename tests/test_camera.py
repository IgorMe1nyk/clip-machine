"""Track linking, speaker choice, shots and the camera path. All data is synthetic."""

from collections.abc import Callable
from dataclasses import replace

import pytest

from clipmachine.models import SpeakerConfig
from clipmachine.utils.camera import (
    FALLBACK_FACES_TOO_SMALL,
    FALLBACK_NO_FACE,
    FALLBACK_SOURCE_NOT_WIDER,
    SIGNAL_MOTION,
    SIGNAL_SYNC,
    FaceSample,
    Followed,
    Shot,
    Track,
    _variance,
    build_shots,
    camera_path,
    centred_mean,
    choose_speakers,
    fallback_reason,
    find_cuts,
    follow_segments,
    link_tracks,
    motion_score,
    shot_targets,
    signature_distance,
    speech_windows,
    split_at_cuts,
    summarise,
    sync_score,
    window_scores,
)

FPS = 10
WINDOW = 0.5
PER_WINDOW = 5  # samples per window at 10 fps


def face(t: float, cx: float, cy: float = 0.5, h: float = 0.3, mouth: float = 0.0) -> FaceSample:
    return FaceSample(t=t, cx=cx, cy=cy, w=h / 2, h=h, mouth=mouth)


def cfg(**changes: float | str) -> SpeakerConfig:
    """Settings for the older tests, which exercise the mouth-variance rule: signal "mouth"."""
    return SpeakerConfig(**{"signal": "mouth", **changes})  # type: ignore[arg-type]


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
    # dead zone is 15% of 200 px = 30 px; the face wobbles by +-20 px (cx 0.48 / 0.52)
    cxs = [0.5 + (0.02 if k % 2 else -0.02) for k in range(20)]
    track = Track(0, {k: face(time_of(k), cxs[k]) for k in range(20)})
    path = camera_path([Shot(0.0, 2.0, 0)], [track], 1000, 200, TIMES, cfg())
    assert len(set(path)) == 1
    # the same input under no dead zone does move
    tiny = camera_path([Shot(0.0, 2.0, 0)], [track], 1000, 200, TIMES, cfg(dead_zone=0))
    assert len(set(tiny)) > 1


def px_track(pxs: list[float], first: int = 0, track_id: int = 0) -> Track:
    """A track whose face centres the crop at these x (frame 1000 px, crop 200 px), from sample
    `first` on."""
    return Track(
        track_id,
        {first + k: face(time_of(first + k), (px + 100) / 1000) for k, px in enumerate(pxs)},
    )


def long_times(count: int) -> list[float]:
    return [time_of(k) for k in range(count)]


def run_path(pxs: list[float], **changes: float) -> list[int]:
    """One shot over len(pxs) samples at 10 fps; smooth_seconds 0 unless given."""
    settings = cfg(**{"smooth_seconds": 0.0, **changes})
    end = len(pxs) / FPS
    return camera_path(
        [Shot(0.0, end, 0)], [px_track(pxs)], 1000, 200, long_times(len(pxs)), settings
    )


def step_moves(path: list[int]) -> list[int]:
    """The per-sample steps from the first to the last sample that changes."""
    changed = [k for k in range(1, len(path)) if path[k] != path[k - 1]]
    return [path[k] - path[k - 1] for k in range(changed[0], changed[-1] + 1)]


def test_a_short_move_is_one_eased_glide_of_glide_seconds() -> None:
    # the face steps 200 -> 260 px at sample 50 (t = 5.0 s); 60 px is outside the 30 px dead zone
    # D = max(0.6, 1.5 * 60 / (0.8 * 200) = 0.5625) = 0.6 s; t_start = 5.0 - 0.3 = 4.7 s,
    # destination = 260. x(t) = 200 + 60 * (3u^2 - 2u^3), u = (t - 4.7) / 0.6, rounded to even:
    # u = 0, 1/6, 1/3, 1/2, 2/3, 5/6, 1 -> 200, 204.4, 215.6, 230, 244.4, 255.6, 260
    path = run_path([200.0] * 50 + [260.0] * 50)
    assert path[:47] == [200] * 47
    assert path[47:54] == [200, 204, 216, 230, 244, 256, 260]
    assert path[53:] == [260] * 47


def test_a_glide_is_monotone_without_overshoot_eased_and_speed_capped() -> None:
    # 300 px step: D = max(0.6, 1.5 * 300 / 160) = 2.8125 s (the speed cap decides)
    path = run_path([200.0] * 50 + [500.0] * 100)
    assert path[0] == 200 and path[-1] == 500
    assert path == sorted(path)  # monotone
    assert all(200 <= x <= 500 for x in path)  # no overshoot
    moving = [k for k in range(len(path)) if 200 < path[k] < 500]
    assert 2.6 <= len(moving) / FPS <= 3.0  # the glide lasts about D
    steps = step_moves(path)
    biggest = max(steps)
    third = len(steps) // 3
    assert biggest in steps[third : len(steps) - third]  # fastest in the middle third
    assert steps[0] < biggest and steps[-1] < biggest  # eased in and out
    assert biggest <= 0.8 * 200 / FPS + 2  # speed cap (16 px per sample) plus rounding slack


def test_a_longer_move_takes_longer_than_a_short_one() -> None:
    short = run_path([200.0] * 50 + [260.0] * 50)
    long = run_path([200.0] * 50 + [500.0] * 100)
    count = lambda p: sum(1 for k in range(len(p)) if p[k] not in (p[0], p[-1]))  # noqa: E731
    assert 5 <= count(short) <= 7  # about glide_seconds (0.6 s)
    assert count(long) > 2 * count(short)


def test_the_camera_has_moved_by_the_sample_where_the_target_leaves_the_dead_zone() -> None:
    # default smoothing (0.5 s = +-2 samples). The smoothed target first passes the 30 px dead
    # zone at sample 50 (236 px: three of the five samples in its window are 260). The glide
    # starts D / 2 = 0.3 s earlier, so at sample 50 (u = 0.5) the camera is already at 230 and at
    # sample 49 (u = 1/3) it has begun moving.
    pxs = [200.0] * 50 + [260.0] * 50
    path = camera_path([Shot(0.0, 10.0, 0)], [px_track(pxs)], 1000, 200, long_times(100), cfg())
    assert path[46] == 200
    assert path[47] > 200 or path[48] > 200
    assert path[49] > 200
    assert path[50] == 230
    assert path[-1] == 260


def test_symmetric_jitter_around_a_place_gives_a_constant_path_there() -> None:
    place = 400.0
    pxs = [place + (5.0 if k % 2 else -5.0) for k in range(40)]
    path = camera_path(
        [Shot(0.0, 4.0, 0)], [px_track(pxs)], 1000, 200, long_times(40), cfg(dead_zone=0.05)
    )
    assert len(set(path)) == 1
    assert abs(path[0] - place) <= 2  # the edge window is lopsided by one sample at most


def test_the_centred_mean_has_no_lag_on_a_ramp() -> None:
    times = long_times(20)
    ramp = [10.0 * k for k in range(20)]
    smoothed = centred_mean(times, ramp, 0.25)  # +-2 samples
    for k in range(2, 18):
        assert smoothed[k] == pytest.approx(ramp[k])
    assert smoothed[0] == pytest.approx(10.0)  # clipped at the ends: mean of samples 0..2
    assert centred_mean(times, ramp, 0.0) == ramp


def test_smoothing_off_still_follows_a_step() -> None:
    path = run_path([200.0] * 50 + [260.0] * 50, smooth_seconds=0.0)
    assert path[-1] == 260 and path[49] > 200  # starts before the step even with no smoothing


def test_a_hard_cut_between_shots_has_no_glide_and_no_smoothing_across_it() -> None:
    tracks = [px_track([100.0] * 30, track_id=0), px_track([700.0] * 30, track_id=1)]
    shots = [Shot(0.0, 1.5, 0), Shot(1.5, 3.0, 1)]
    path = camera_path(shots, tracks, 1000, 200, long_times(30), cfg())
    assert path[14] == 100 and path[15] == 700  # the two faces' positions exactly
    assert set(path[:15]) == {100} and set(path[15:]) == {700}


def test_a_glide_in_one_shot_never_runs_into_the_next() -> None:
    # shot A: the face moves 100 -> 160 in its last 0.2 s; shot B starts 0.5 s later
    a = px_track([100.0] * 13 + [160.0] * 2, track_id=0)
    b = px_track([700.0] * 15, first=15, track_id=1)
    path = camera_path(
        [Shot(0.0, 1.5, 0), Shot(1.5, 3.0, 1)],
        [a, b],
        1000,
        200,
        long_times(30),
        cfg(smooth_seconds=0),
    )
    assert path[15:] == [700] * 15
    assert all(100 <= x <= 160 for x in path[:15])
    assert path[14] < 160  # cut off by the end of the shot: it just stops there


def test_targets_hold_through_missing_samples_and_start_at_the_first_face() -> None:
    faces = [None, None, face(0.2, 0.3), None, face(0.4, 0.7), None]
    assert shot_targets(faces, 1000, 200) == [200.0, 200.0, 200.0, 200.0, 600.0, 600.0]
    assert shot_targets([None, None], 1000, 200) is None


def test_a_move_across_missing_samples_starts_at_the_last_known_place() -> None:
    # 200 px until sample 4, missing 5..11, 260 px from sample 12 (t = 1.2 s): the target steps
    # at sample 12, so the glide (t_start = 0.9 s) is the only thing that moves the camera
    by_frame = {k: face(time_of(k), 0.3) for k in range(5)}
    by_frame.update({k: face(time_of(k), 0.36) for k in range(12, 40)})
    path = camera_path(
        [Shot(0.0, 4.0, 0)], [Track(0, by_frame)], 1000, 200, long_times(40), cfg(smooth_seconds=0)
    )
    assert path[:9] == [200] * 9  # held while the face is missing
    assert path[-1] == 260


def test_a_shot_without_its_face_holds_the_previous_shots_last_position() -> None:
    track = px_track([300.0] * 10, track_id=0)
    shots = [Shot(0.0, 1.0, 0), Shot(1.0, 2.0, 1), Shot(2.0, 3.0, None)]
    path = camera_path(shots, [track], 1000, 200, long_times(30), cfg())
    assert path == [300] * 30  # shot 1 has no track at all, shot 2 is nobody


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
    by_frame.update({k: face(time_of(k), 0.3) for k in range(12, 20)})
    track = Track(0, by_frame)
    path = camera_path([Shot(0.0, 2.0, 0)], [track], 1000, 200, TIMES, cfg())
    assert path == [200] * 20  # the missing samples take the earlier target: nothing moves


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


def test_largest_heights_decide_when_given() -> None:
    kept: list[list[FaceSample]] = [[] for _ in range(10)]  # every face was dropped as small
    small = [0.05] * 10
    assert fallback_reason(kept, cfg(), 1920, 606, small) == FALLBACK_FACES_TOO_SMALL
    assert fallback_reason(kept, cfg(), 1920, 606, [0.0] * 10) == FALLBACK_NO_FACE
    mixed = [0.0] * 5 + [0.3] * 5
    assert fallback_reason(kept, cfg(), 1920, 606, mixed) is None  # zeros are samples without faces


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


# --- the link gap is a time, not a count --------------------------------------------------


def gap_case(fps: int, missing: int) -> Followed:
    """One face that is missing for some samples in the middle, at fps samples a second."""
    count = 4 + missing + 4
    frames = [
        [] if 4 <= k < 4 + missing else [FaceSample(k / fps, 0.5, 0.5, 0.15, 0.3, 0.0)]
        for k in range(count)
    ]
    times = [round(k / fps, 3) for k in range(count)]
    return follow_segments(frames, times, [], [], count / fps, 1000, 200, cfg(sample_fps=fps))


def test_a_face_missing_for_six_samples_relinks_at_30_fps_but_not_at_10() -> None:
    assert len(gap_case(30, 6).tracks) == 1  # 0.2 s, within 0.3 s
    assert len(gap_case(10, 6).tracks) == 2  # 0.6 s, too long


def test_the_link_gap_at_10_fps_is_still_three_samples() -> None:
    assert len(gap_case(10, 3).tracks) == 1
    assert len(gap_case(10, 4).tracks) == 2


def test_face_sample_motion_defaults_to_none() -> None:
    assert face(0.0, 0.5).motion is None


# --- Brief 8.2: the mix signal -------------------------------------------------------------


def test_sync_score_is_high_for_a_mouth_that_follows_the_sound() -> None:
    loud = [0.0, 10.0] * 5
    mouth: list[float | None] = [0.0, 0.4] * 5
    score = sync_score(mouth, loud)
    assert score == pytest.approx(0.2)  # covariance 1.0 / loudness spread 5.0


def test_sync_score_is_zero_for_a_mouth_that_moves_regardless_or_against() -> None:
    loud = [0.0, 10.0] * 4
    unrelated: list[float | None] = [0.0, 0.0, 0.4, 0.4] * 2  # zero covariance with the loudness
    against: list[float | None] = [0.4, 0.0] * 4
    assert sync_score(unrelated, loud) == pytest.approx(0.0, abs=1e-12)
    assert sync_score(against, loud) == 0.0


def test_sync_score_needs_enough_readable_mouths() -> None:
    loud = [0.0, 10.0] * 5
    assert sync_score([0.0, 0.4, 0.0], [0.0, 10.0, 0.0]) is None  # fewer than 4 samples
    mouth: list[float | None] = [0.0, 0.4, None, None, 0.0, 0.4, None, None, None, None]
    assert sync_score(mouth, loud) is None  # 4 of 10 is under 60%
    mouth = [0.0, 0.4, None, None, 0.0, 0.4, 0.0, 0.4, None, None]
    assert sync_score(mouth, loud) is not None  # 6 of 10


def test_sync_score_is_zero_for_a_flat_mouth_or_flat_sound() -> None:
    assert sync_score([0.2] * 8, [0.0, 10.0] * 4) == 0.0
    assert sync_score([0.0, 0.4] * 4, [5.0] * 8) == 0.0


def test_sync_score_rejects_sequences_of_different_length() -> None:
    with pytest.raises(ValueError, match="same length"):
        sync_score([0.0, 0.4, 0.0, 0.4], [0.0, 1.0, 0.0])


def test_motion_score_is_the_mean_of_the_readable_values() -> None:
    assert motion_score([1.0, None, 3.0]) == pytest.approx(2.0)
    assert motion_score([0.5]) == pytest.approx(0.5)


def test_motion_score_needs_enough_readable_values() -> None:
    assert motion_score([1.0, None, None]) is None
    assert motion_score([1.0, 2.0, None, None, None]) is None  # 2 of 5 is under 60%
    assert motion_score([]) is None


def px(t: float, cx: float, mouth: float | None, motion: float | None) -> FaceSample:
    return FaceSample(t=t, cx=cx, cy=0.5, w=0.15, h=0.3, mouth=mouth, motion=motion)


MIX_SAMPLES = 40  # four seconds at 10 fps: eight windows
MIX_TIMES = [time_of(k) for k in range(MIX_SAMPLES)]
MIX_LOUD = [10.0 if k % 2 == 0 else 0.0 for k in range(MIX_SAMPLES)]


def make_tracks(
    a_mouth: Callable[[int], float | None],
    b_mouth: Callable[[int], float | None],
    a_motion: float | None = 0.1,
    b_motion: float | None = 0.1,
) -> list[Track]:
    frames = [
        [px(MIX_TIMES[k], 0.25, a_mouth(k), a_motion), px(MIX_TIMES[k], 0.75, b_mouth(k), b_motion)]
        for k in range(MIX_SAMPLES)
    ]
    return link_tracks(frames, 0.12)


def follows_sound(k: int) -> float:
    return 0.4 if k % 2 == 0 else 0.0


def ignores_sound(k: int) -> float:
    return 0.4 if k % 4 in (2, 3) else 0.0  # period four against the sound's period two


def no_mouth(k: int) -> None:
    return None


def test_window_scores_use_sync_when_every_main_mouth_is_readable() -> None:
    tracks = make_tracks(follows_sound, ignores_sound)
    scores, kinds = window_scores(tracks, MIX_LOUD, MIX_TIMES, WINDOW, 8)
    assert kinds == [SIGNAL_SYNC] * 8
    assert all(a > b for a, b in zip(scores[0], scores[1], strict=True))
    assert all(b < a / 2 for a, b in zip(scores[0], scores[1], strict=True))


def test_window_scores_use_motion_when_a_main_mouth_is_never_readable() -> None:
    tracks = make_tracks(follows_sound, no_mouth, a_motion=0.1, b_motion=0.5)
    scores, kinds = window_scores(tracks, MIX_LOUD, MIX_TIMES, WINDOW, 8)
    assert kinds == [SIGNAL_MOTION] * 8
    assert all(b > a > 0 for a, b in zip(scores[0], scores[1], strict=True))


def test_window_scores_use_motion_without_audio() -> None:
    tracks = make_tracks(follows_sound, ignores_sound, a_motion=0.5, b_motion=0.1)
    scores, kinds = window_scores(tracks, None, MIX_TIMES, WINDOW, 8)
    assert kinds == [SIGNAL_MOTION] * 8
    assert all(a > b for a, b in zip(scores[0], scores[1], strict=True))


def test_window_scores_have_no_signal_where_nobody_scores() -> None:
    tracks = make_tracks(no_mouth, no_mouth, a_motion=None, b_motion=None)
    scores, kinds = window_scores(tracks, MIX_LOUD, MIX_TIMES, WINDOW, 8)
    assert kinds == [None] * 8
    assert scores == {0: [0.0] * 8, 1: [0.0] * 8}


def test_a_mouth_that_moves_more_but_not_with_the_sound_loses_to_one_that_follows_it() -> None:
    """A laugh: B's mouth moves a lot, but not with the speech."""

    def a_mouth(k: int) -> float:
        return 0.2 if k % 2 == 0 else 0.0

    def b_mouth(k: int) -> float:
        return 0.5 if k % 4 in (2, 3) else 0.0

    tracks = make_tracks(a_mouth, b_mouth)
    scores, kinds = window_scores(tracks, MIX_LOUD, MIX_TIMES, WINDOW, 8)
    assert kinds == [SIGNAL_SYNC] * 8
    assert all(a > b for a, b in zip(scores[0], scores[1], strict=True))

    # the older rule goes by the size of the movement, and picks the laugh
    speech = [True] * 8
    assert set(choose_speakers(tracks, speech, WINDOW, cfg())) == {1}
    assert set(choose_speakers(tracks, speech, WINDOW, cfg(), scores)) == {0}


def two_tracks() -> list[Track]:
    return link_tracks([[face(time_of(k), 0.25), face(time_of(k), 0.75)] for k in range(40)], 0.12)


def test_choose_speakers_with_scores_backdates_a_switch_to_the_turn_start() -> None:
    scores = {0: [2.0, 2, 2, 0.5, 0.5, 2, 2, 2], 1: [0.0, 0, 0, 2, 2, 0, 0, 0]}
    choice = choose_speakers(two_tracks(), [True] * 8, WINDOW, cfg(), scores)
    assert choice == [0, 0, 0, 1, 1, 0, 0, 0]  # B from its first leading window, A from its


def test_choose_speakers_with_scores_does_not_switch_while_one_track_keeps_leading() -> None:
    scores = {0: [2.0] * 8, 1: [1.0] * 8}  # B is never 1.5 times better
    assert choose_speakers(two_tracks(), [True] * 8, WINDOW, cfg(), scores) == [0] * 8
    one_window = {0: [2.0, 2, 2, 2, 0.5, 2, 2, 2], 1: [0.0, 0, 0, 0, 2, 0, 0, 0]}
    assert choose_speakers(two_tracks(), [True] * 8, WINDOW, cfg(), one_window) == [0] * 8


def test_choose_speakers_without_scores_is_the_mouth_variance() -> None:
    frames = two_people(8, a_talks=lambda w: w < 4, b_talks=lambda w: w >= 4)
    tracks = link_tracks(frames, 0.12)
    speech = [True] * 8
    plain = choose_speakers(tracks, speech, WINDOW, cfg())
    by_hand = {
        t.id: [
            _variance([s for _, s in t.points if int(s.t / WINDOW + 1e-9) == w]) for w in range(8)
        ]
        for t in tracks
    }
    assert choose_speakers(tracks, speech, WINDOW, cfg(), by_hand) == plain
    assert plain == [0, 0, 0, 0, 1, 1, 1, 1]


VISIT: list[int | None] = [0, 0, 0, 0, 1, 1, 0, 0, 0, 0]  # a one second visit of track 1


def test_build_shots_minimums_keep_a_visit_that_is_long_enough() -> None:
    shots = build_shots(VISIT, WINDOW, 9.0, 5.0, minimums=[1.0] * 10)
    assert shots == [Shot(0.0, 2.0, 0), Shot(2.0, 3.0, 1), Shot(3.0, 5.0, 0)]


def test_build_shots_minimums_merge_a_visit_that_is_too_short() -> None:
    assert build_shots(VISIT, WINDOW, 0.5, 5.0, minimums=[1.5] * 10) == [Shot(0.0, 5.0, 0)]


def test_build_shots_minimums_take_the_largest_of_the_windows_a_shot_covers() -> None:
    mixed = [1.0, 1.0, 1.0, 1.0, 1.0, 1.5, 1.0, 1.0, 1.0, 1.0]  # one window of the visit is noisy
    assert build_shots(VISIT, WINDOW, 0.5, 5.0, minimums=mixed) == [Shot(0.0, 5.0, 0)]
    elsewhere = [1.5, 1.5, 1.5, 1.5, 1.0, 1.0, 1.5, 1.5, 1.5, 1.5]  # only the long shots are noisy
    assert len(build_shots(VISIT, WINDOW, 0.5, 5.0, minimums=elsewhere)) == 3


def test_build_shots_without_minimums_is_unchanged() -> None:
    assert len(build_shots(VISIT, WINDOW, 1.0, 5.0)) == 3
    assert build_shots(VISIT, WINDOW, 1.0, 5.0) == build_shots(
        VISIT, WINDOW, 1.0, 5.0, minimums=None
    )
    assert build_shots(VISIT, WINDOW, 1.5, 5.0) == [Shot(0.0, 5.0, 0)]


def test_build_shots_minimums_must_cover_every_window() -> None:
    with pytest.raises(ValueError, match="one value per window"):
        build_shots([0, 0, 1], WINDOW, 1.0, minimums=[1.0, 1.0])


def mix_cfg(**changes: float | str) -> SpeakerConfig:
    return SpeakerConfig(**{"signal": "mix", **changes})  # type: ignore[arg-type]


def follow_mix(
    frames: list[list[FaceSample]],
    cuts: list[int] | None = None,
    words: list[tuple[float, float]] | None = None,
    loudness: list[float] | None = None,
    config: SpeakerConfig | None = None,
) -> Followed:
    return follow_segments(
        frames,
        clip_times(len(frames)),
        words or [],
        cuts or [],
        len(frames) / FPS,
        1000,
        200,
        config or mix_cfg(),
        loudness,
    )


def hidden_mouth_frames(count: int = 80) -> list[list[FaceSample]]:
    """A talks for 2 s, B (whose mouth is never readable) for 3 s, then A again. A's mouth keeps
    moving throughout; each speaker moves more in the pixels round the mouth."""
    frames = []
    for k in range(count):
        t = time_of(k)
        b_speaks = 20 <= k < 50
        frames.append(
            [
                px(t, 0.25, talking(k), 0.1 if b_speaks else 1.0),
                px(t, 0.75, None, 1.0 if b_speaks else 0.1),
            ]
        )
    return frames


def test_mix_follows_a_speaker_whose_mouth_is_never_readable_by_pixel_motion() -> None:
    done = follow_mix(hidden_mouth_frames(), words=[(0.0, 8.0)])
    assert done.shots == [Shot(0.0, 2.0, 0), Shot(2.0, 5.0, 1), Shot(5.0, 8.0, 0)]
    assert done.switches == 2
    assert (done.sync_windows, done.motion_windows, done.unscored_windows) == (0, 16, 0)
    assert set(done.crop_xs[:20]) == {150} and set(done.crop_xs[20:50]) == {650}


def test_the_mouth_rule_never_shows_that_speaker() -> None:
    done = follow_segments(
        hidden_mouth_frames(), clip_times(80), [(0.0, 8.0)], [], 8.0, 1000, 200, cfg()
    )
    assert done.shots == [Shot(0.0, 8.0, 0)]
    assert (done.sync_windows, done.motion_windows, done.unscored_windows) == (0, 0, 0)


def test_mix_falls_back_to_motion_when_audio_cannot_score_a_main_face() -> None:
    loudness = [10.0 if k % 2 == 0 else 0.0 for k in range(80)]
    done = follow_mix(hidden_mouth_frames(), words=[(0.0, 8.0)], loudness=loudness)
    assert [s.track for s in done.shots] == [0, 1, 0]
    assert (done.sync_windows, done.motion_windows) == (0, 16)


def test_mix_counts_speech_windows_decided_by_each_signal() -> None:
    frames = [
        [px(time_of(k), 0.25, follows_sound(k), None), px(time_of(k), 0.75, 0.05, None)]
        for k in range(20)
    ]
    loudness = [10.0 if k % 2 == 0 else 0.0 for k in range(20)]
    done = follow_mix(frames, words=[(0.0, 1.0)], loudness=loudness)  # windows 0 and 1 have speech
    assert (done.sync_windows, done.motion_windows, done.unscored_windows) == (2, 0, 0)

    blank = [
        [px(time_of(k), 0.25, None, None), px(time_of(k), 0.75, None, None)] for k in range(20)
    ]
    nothing = follow_mix(blank, words=[(0.0, 1.0)], loudness=loudness)
    assert (nothing.sync_windows, nothing.motion_windows, nothing.unscored_windows) == (0, 0, 2)
    assert nothing.shots == [Shot(0.0, 2.0, 0)]  # the fallback: the first of two equal faces


def test_mix_without_audio_counts_motion_windows() -> None:
    frames = [[px(time_of(k), 0.25, 0.0, 0.3)] for k in range(20)]
    done = follow_mix(frames, words=[(0.0, 1.0)])
    assert (done.sync_windows, done.motion_windows, done.unscored_windows) == (0, 2, 0)


def test_mix_carries_nothing_across_a_cut() -> None:
    loudness = [10.0 if k % 2 == 0 else 0.0 for k in range(80)]
    done = follow_mix(hidden_mouth_frames(), cuts=[40], words=[(0.0, 8.0)], loudness=loudness)
    assert all(max(t.by_frame) < 40 or min(t.by_frame) >= 40 for t in done.tracks)
    assert any(s.start == 4.0 for s in done.shots)
    assert_gap_free(done.shots, 8.0)
    assert done.motion_windows == 16  # eight windows in each shot
    inside = sum(
        1
        for a, b in zip(done.shots, done.shots[1:], strict=False)
        if a.track != b.track and a.end != 4.0
    )
    assert done.switches == inside
