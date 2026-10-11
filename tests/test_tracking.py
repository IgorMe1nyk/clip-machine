"""Speaker-crop analysis of one clip with the frame pipe and the landmarker faked."""

import json
from pathlib import Path

import numpy as np
import pytest

import track_fakes
from clipmachine import tracking
from clipmachine.context import RunContext
from clipmachine.models import (
    TRACK_SCHEMA_VERSION,
    SpeakerConfig,
    TrackFile,
    TranscriptSegment,
    TranscriptWord,
    VideoStreamInfo,
)
from clipmachine.runs import write_json_atomic
from clipmachine.stages import render
from clipmachine.tracking import TrackPlan, analyse_clip, load_valid_track
from clipmachine.utils import camera
from test_render import CLIPS, FakeFfmpeg, clip_files, make_ctx, write_inputs


def video(width: int = 640, height: int = 360, rotation: int = 0) -> VideoStreamInfo:
    return VideoStreamInfo(
        codec="h264",
        width=width,
        height=height,
        avg_frame_rate="30/1",
        r_frame_rate="30/1",
        rotation=rotation,
        variable_frame_rate=False,
    )


def segments() -> list[TranscriptSegment]:
    """A word every 2 s (0.4 s long) for 100 s, as the render tests' transcript."""

    words = [
        TranscriptWord(start=float(t), end=t + 0.4, word=f" w{t}", probability=0.9)
        for t in range(0, 100, 2)
    ]
    return [
        TranscriptSegment(
            id=0,
            start=0.0,
            end=100.0,
            text="",
            avg_logprob=0.0,
            no_speech_prob=0.0,
            words=words,
        )
    ]


def make_plan(tmp_path: Path, **changes: object) -> TrackPlan:
    clips = tmp_path / "clips"
    clips.mkdir(exist_ok=True)
    plan = TrackPlan(
        clips_dir=clips,
        source=tmp_path / "source.mp4",
        video=video(),
        source_sha256="a" * 64,
        target_width=1080,
        target_height=1920,
        segments=segments(),
        settings=SpeakerConfig(),
        model_path=tmp_path / "face_landmarker.task",
        model_sha256="b" * 64,
        mediapipe_version="1.1.0",
        detector_path=tmp_path / "face_detection_yunet_2026may.onnx",
        detector_sha256="e" * 64,
        opencv_version="5.0.0",
    )
    return plan if not changes else TrackPlan(**{**plan.__dict__, **changes})


# --- analyse_clip -------------------------------------------------------------------------


def test_two_speakers_give_two_shots_and_a_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = track_fakes.install(monkeypatch, track_fakes.two_people(switch_at=10.0))

    track = analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0)

    assert track.fallback_reason is None
    assert (track.clip_id, track.clip_start, track.clip_end) == ("c01", 10.0, 30.0)
    assert track.crop_w == 202
    assert (track.frame.w, track.frame.h) == (640, 360)
    assert track.source.sha256 == "a" * 64
    assert (track.mediapipe_version, track.model_sha256) == ("1.1.0", "b" * 64)
    assert (track.detector_sha256, track.opencv_version) == ("e" * 64, "5.0.0")
    assert track.cuts == [] and track.summary.cuts == 0
    assert [(s.start, s.end, s.track) for s in track.shots] == [(0.0, 10.0, 0), (10.0, 20.0, 1)]
    assert [s.face_cx for s in track.shots] == [0.25, 0.75]
    summary = track.summary
    assert (summary.faces_seen, summary.switches, summary.samples) == (2, 1, 200)
    assert summary.face_time_pct == 100.0

    xs = [s.crop_x for s in track.samples]
    assert len(xs) == 200
    assert all(x % 2 == 0 and 0 <= x <= 640 - 202 for x in xs)
    assert len(set(xs[:100])) == 1 and len(set(xs[100:])) == 1  # still within each shot
    assert xs[99] < 218 < xs[100]  # the cut at 10 s, no glide between the two
    assert xs[0] == 60 and xs[100] == 380  # 0.25 and 0.75 of 640, minus half the window

    assert fakes.opened == fakes.closed == 1
    assert len(fakes.frame_requests) == 1  # no preview, so one decode pass
    request = fakes.frame_requests[0]
    assert request[request.index("-ss") + 1] == "10.000"
    assert request[request.index("-t") + 1] == "20.000"


def test_the_track_file_holds_numbers_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    track_fakes.install(monkeypatch, track_fakes.two_people(), CUT_LOOK)
    track = analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0)

    raw = json.dumps(track.model_dump(mode="json"))
    assert "w10" not in raw and "synthetic" not in raw  # no transcript words
    assert "source.mp4" not in raw  # no file names
    assert len(track.cuts_exact) == 1 and all(isinstance(t, float) for t in track.cuts_exact)
    assert TrackFile.model_validate_json(raw) == track  # round trip


def test_one_person_is_one_shot_with_no_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def one(t: float) -> list[track_fakes.RawFace]:
        return [track_fakes.RawFace(0.5, 0.5, 0.15, 0.3, track_fakes.talking(t))]

    track_fakes.install(monkeypatch, one)
    track = analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0)

    assert track.fallback_reason is None
    assert len(track.shots) == 1 and track.summary.switches == 0
    assert len({s.crop_x for s in track.samples}) == 1


def test_no_face_falls_back_to_the_centre_crop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track_fakes.install(monkeypatch, track_fakes.nobody)

    track = analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0)

    assert track.fallback_reason == "no_face"
    assert {s.crop_x for s in track.samples} == {218}  # the render stage's centre crop x
    assert [(s.start, s.end, s.track, s.face_cx) for s in track.shots] == [(0.0, 20.0, None, None)]
    assert track.summary.face_time_pct == 0.0 and track.summary.faces_seen == 0


def test_small_faces_fall_back_to_the_centre_crop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track_fakes.install(monkeypatch, track_fakes.two_people(size=0.04))  # webcam boxes

    track = analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0)

    assert track.fallback_reason == "faces_too_small"  # judged from the detector's largest box
    assert {s.crop_x for s in track.samples} == {218}
    assert track.summary.faces_seen == 0  # the small faces were ignored for tracking


def test_a_portrait_source_has_nothing_to_pan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track_fakes.install(monkeypatch, track_fakes.two_people())
    plan = make_plan(tmp_path, video=video(width=1080, height=1920))

    track = analyse_clip(plan, "c01", 10.0, 30.0)

    assert track.fallback_reason == "source_not_wider_than_crop"
    assert {s.crop_x for s in track.samples} == {0}


def test_a_clip_without_transcript_words_never_cuts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track_fakes.install(monkeypatch, track_fakes.two_people(switch_at=10.0))
    plan = make_plan(tmp_path, segments=[])  # no transcript words at all

    track = analyse_clip(plan, "c01", 10.0, 30.0)

    # nobody is "speaking" per the transcript: the largest face is shown, no cut
    assert track.summary.switches == 0 and len(track.shots) == 1


def test_preview_draws_boxes_and_the_window_and_gets_the_clip_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = track_fakes.install(monkeypatch, track_fakes.two_people(switch_at=10.0))
    plan = make_plan(tmp_path)

    analyse_clip(plan, "c01", 10.0, 30.0, preview=True)

    assert len(fakes.frame_requests) == 2  # analysis pass and preview pass
    preview_request = fakes.frame_requests[1]
    assert "scale=480:-2" in preview_request[preview_request.index("-vf") + 1]
    (preview,) = fakes.previews
    assert preview["out"] == plan.clips_dir / "c01.track.preview.mp4"
    assert preview["temp"].name == "c01.track.preview.tmp.mp4"
    args = preview["args"]
    assert args[args.index("-ss") + 1] == "10.000" and args[args.index("-t") + 1] == "20.000"
    assert "1:a:0?" in args
    frames = preview["frames"]
    assert len(frames) == 200
    assert frames[0].shape == (270, 480, 3)
    window = np.array(tracking.WINDOW_COLOR)
    active = np.array(tracking.ACTIVE_COLOR)
    assert (frames[0] == window).all(axis=2).any()  # the 9:16 window
    assert (frames[0] == active).all(axis=2).any()  # the active speaker's box
    # the window moved at the cut: its left edge column differs between the two shots
    left_a = int(np.argmax((frames[0] == window).all(axis=2)[135]))
    left_b = int(np.argmax((frames[150] == window).all(axis=2)[135]))
    assert left_b > left_a


def hop(t: float) -> list[track_fakes.RawFace]:
    """One person who sits at the left for 10 s and at the right afterwards (as a cut would)."""
    return [track_fakes.RawFace(0.25 if t < 10 else 0.75, 0.5, 0.15, 0.3, track_fakes.talking(t))]


def test_a_cut_is_recorded_and_the_crop_jumps_without_counting_a_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track_fakes.install(monkeypatch, hop, track_fakes.cut_at(10.0, levels=(0, 120)))

    track = analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0)

    assert track.cuts == [10.0]
    assert track.summary.cuts == 1
    assert track.summary.switches == 0  # a cut is not a speaker switch
    assert [(s.start, s.end, s.track) for s in track.shots] == [(0.0, 10.0, 0), (10.0, 20.0, 1)]
    xs = [s.crop_x for s in track.samples]
    assert xs[99] == 60 and xs[100] == 380  # already on the new face at the first sample


def test_no_crop_position_is_carried_across_a_cut_to_a_view_without_a_face(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def left_then_nobody(t: float) -> list[track_fakes.RawFace]:
        return hop(t) if t < 10 else []

    track_fakes.install(monkeypatch, left_then_nobody, track_fakes.cut_at(10.0, levels=(0, 120)))

    track = analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0)

    xs = [s.crop_x for s in track.samples]
    assert xs[99] == 60
    assert len(set(xs[100:])) == 1 and xs[100] != 60  # centred, not held at the left face


def test_a_cut_is_still_recorded_when_the_clip_falls_back_to_the_centre(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track_fakes.install(monkeypatch, track_fakes.nobody, track_fakes.cut_at(10.0, levels=(0, 120)))

    track = analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0)

    assert track.fallback_reason == "no_face"
    assert track.cuts == [10.0] and track.summary.cuts == 1
    assert {s.crop_x for s in track.samples} == {218}


# --- frame-exact cut times ----------------------------------------------------------------


CUT_LOOK = track_fakes.cut_at(10.0, levels=(0, 120))


def exact_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **script: object
) -> tuple[TrackFile, track_fakes.TrackFakes]:
    """Analyse a clip with one cut at the sample at 10.0 s, with the timed frames scripted."""
    fakes = track_fakes.install(monkeypatch, hop, CUT_LOOK)
    for name, value in script.items():
        setattr(fakes, name, value)
    return analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0), fakes


def jump_after(frame: int, grey: tuple[int, int] = (0, 120)) -> list[tuple[float, int]]:
    """Seven frames at 30 per second; the picture changes at `frame`."""
    return [(k / 30, grey[0] if k < frame else grey[1]) for k in range(7)]


def test_the_exact_cut_is_the_midpoint_between_the_frames_around_the_jump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track, _ = exact_cut(tmp_path, monkeypatch)  # the default frames change at 10.0 s

    assert track.cuts == [10.0]
    # decoded from 9.9 s; the old shot's last frame is at 2/30 s and the new shot's first at 3/30
    assert track.cuts_exact == [pytest.approx(9.9 + 2.5 / 30, abs=1e-6)]


def test_the_exact_cut_can_be_later_than_the_sample_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track, _ = exact_cut(tmp_path, monkeypatch, timed=jump_after(6))

    assert track.cuts == [10.0]
    assert track.cuts_exact == [pytest.approx(9.9 + 5.5 / 30, abs=1e-6)]
    assert track.cuts_exact[0] > track.cuts[0]


def test_the_exact_cut_falls_back_to_the_sample_time_when_the_jump_is_below_the_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track, _ = exact_cut(tmp_path, monkeypatch, timed=jump_after(3, grey=(0, 8)))  # same bin

    assert track.cuts == [10.0] and track.cuts_exact == [10.0]


def test_the_exact_cut_falls_back_to_the_sample_time_with_a_single_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track, _ = exact_cut(tmp_path, monkeypatch, timed=[(0.0, 120)])

    assert track.cuts_exact == [10.0]


def test_the_exact_cut_falls_back_to_the_sample_time_when_it_strays_too_far(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timed = [(0.0, 0), (0.5, 0), (1.0, 120)]  # 0.75 s after the decode started: 0.65 s late
    track, _ = exact_cut(tmp_path, monkeypatch, timed=timed)

    assert track.cuts_exact == [10.0]


def test_the_exact_cut_falls_back_to_the_sample_time_when_ffmpeg_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    error = tracking.ffmpeg.MediaError("frame extraction failed: details")
    track, _ = exact_cut(tmp_path, monkeypatch, timed_error=error)

    assert track.cuts == [10.0] and track.cuts_exact == [10.0]
    assert [r.getMessage() for r in caplog.records if "cut" in r.getMessage()]
    assert "details" not in caplog.text and "source.mp4" not in caplog.text


def test_there_is_one_exact_time_per_cut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = track_fakes.install(
        monkeypatch, track_fakes.nobody, track_fakes.cut_at(10.0, 15.0, levels=(0, 120))
    )

    track = analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0)

    assert track.cuts == [10.0, 15.0]  # a centre fallback still records its cuts
    assert track.fallback_reason == "no_face"
    assert len(track.cuts_exact) == 2 and len(fakes.timed_requests) == 2
    assert track.cuts_exact == [
        pytest.approx(9.9 + 2.5 / 30, abs=1e-6),
        pytest.approx(14.9 + 2.5 / 30, abs=1e-6),
    ]


def test_there_are_no_exact_times_without_cuts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = track_fakes.install(monkeypatch, track_fakes.two_people())

    track = analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0)

    assert track.cuts == [] and track.cuts_exact == []
    assert fakes.timed_requests == []  # nothing to look up


def test_the_source_is_decoded_from_the_previous_sample_with_a_margin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fakes = exact_cut(tmp_path, monkeypatch)

    assert len(fakes.timed_requests) == 1
    args = fakes.timed_requests[0]
    assert args[args.index("-ss") + 1] == "19.900"  # clip start 10.0 + previous sample 9.9
    assert args[args.index("-t") + 1] == "0.200"  # one sample step + 0.1 s
    assert args[args.index("-vf") + 1] == "showinfo,scale=320:-2"
    assert fakes.timed_sizes == [(320, 180)]


def test_faces_below_the_minimum_height_are_not_tracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def big_and_small(t: float) -> list[track_fakes.RawFace]:
        return [
            track_fakes.RawFace(0.25, 0.5, 0.15, 0.3, track_fakes.talking(t)),
            track_fakes.RawFace(0.75, 0.5, 0.02, 0.04, 0.1),  # a webcam box
        ]

    track_fakes.install(monkeypatch, big_and_small)

    track = analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0)

    assert track.fallback_reason is None  # the big face decides
    assert track.summary.faces_seen == 1
    assert track.summary.faces_per_sample == 1.0
    assert [s.face_cx for s in track.shots] == [0.25]


def test_faces_with_an_unreadable_mouth_are_tracked_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def one_unreadable(t: float) -> list[track_fakes.RawFace]:
        return [
            track_fakes.RawFace(0.25, 0.5, 0.15, 0.3, track_fakes.talking(t)),
            track_fakes.RawFace(0.75, 0.5, 0.15, 0.3, None),
        ]

    track_fakes.install(monkeypatch, one_unreadable)

    track = analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0)

    assert track.summary.faces_seen == 2
    assert track.summary.faces_per_sample == 2.0
    assert track.summary.mouth_read_pct == 50.0
    assert [s.track for s in track.shots] == [0]  # the one that talks


def test_frames_are_decoded_at_the_landmark_width_but_never_above_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = track_fakes.install(monkeypatch, track_fakes.nobody)

    small = analyse_clip(make_plan(tmp_path), "c01", 10.0, 12.0)  # a 640 px source
    assert "scale=640:-2" in fakes.frame_requests[-1][fakes.frame_requests[-1].index("-vf") + 1]
    assert (small.frame.w, small.frame.h) == (640, 360)

    big_plan = make_plan(tmp_path, video=video(width=3840, height=2160))
    big = analyse_clip(big_plan, "c01", 10.0, 11.0)
    assert "scale=1920:-2" in fakes.frame_requests[-1][fakes.frame_requests[-1].index("-vf") + 1]
    assert (big.frame.w, big.frame.h) == (1920, 1080)
    assert fakes.frame_sizes[-1] == (1920, 1080)

    narrow = make_plan(tmp_path, settings=SpeakerConfig(landmark_width=320))
    analyse_clip(narrow, "c01", 10.0, 11.0)
    assert fakes.frame_sizes[-1] == (320, 180)


def test_the_finder_gets_the_configured_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = track_fakes.install(monkeypatch, track_fakes.nobody)
    analyse_clip(make_plan(tmp_path), "c01", 10.0, 11.0)
    assert fakes.finder_args == [
        {
            "detect_width": 960,
            "detect_score": 0.7,
            "crop_scale": 2.0,
            "min_face_height": 0.08,
            "max_faces": 4,
        }
    ]


def test_the_preview_marks_the_first_samples_after_a_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = track_fakes.install(
        monkeypatch, track_fakes.two_people(), track_fakes.cut_at(10.0, levels=(0, 120))
    )

    analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0, preview=True)

    frames = fakes.previews[0]["frames"]
    border = np.array(tracking.CUT_COLOR)

    def has_border(k: int) -> bool:
        return bool((frames[k] == border).all(axis=2).any())

    assert [has_border(k) for k in (0, 99, 100, 101, 102, 103, 150)] == [
        False,
        False,
        True,
        True,
        True,
        False,
        False,
    ]


def test_the_preview_is_not_written_unless_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = track_fakes.install(monkeypatch, track_fakes.two_people())
    analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0)
    assert fakes.previews == []


# --- drawing ------------------------------------------------------------------------------


def blank(width: int = 20, height: int = 10) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


def test_draw_rect_draws_an_outline_only() -> None:
    frame = blank()
    tracking.draw_rect(frame, 2, 2, 8, 6, (255, 0, 0), 1)
    red = (frame == (255, 0, 0)).all(axis=2)
    assert red[2, 2:9].all() and red[6, 2:9].all()  # top and bottom edges
    assert red[2:7, 2].all() and red[2:7, 8].all()  # left and right edges
    assert not red[4, 5]  # the inside is untouched
    assert red.sum() == 2 * 7 + 2 * 3


def test_draw_rect_clamps_to_the_frame() -> None:
    frame = blank()
    tracking.draw_rect(frame, -5, -5, 100, 100, (0, 255, 0), 2)
    assert frame.shape == (10, 20, 3)
    assert (frame[0] == (0, 255, 0)).all() and (frame[:, 0] == (0, 255, 0)).all()


def test_draw_preview_frame_marks_the_active_face_apart() -> None:
    frame = blank(100, 100)
    left = camera.FaceSample(0.0, 0.25, 0.5, 0.2, 0.2, 0.0)
    right = camera.FaceSample(0.0, 0.75, 0.5, 0.2, 0.2, 0.0)
    tracking.draw_preview_frame(frame, [left, right], right, (0.0, 0.5))
    assert (frame == tracking.FACE_COLOR).all(axis=2).any()
    assert (frame == tracking.ACTIVE_COLOR).all(axis=2).any()
    assert (frame == tracking.WINDOW_COLOR).all(axis=2).any()
    assert tuple(frame[50, 65]) == tracking.ACTIVE_COLOR  # left edge of the right-hand box
    assert tuple(frame[50, 15]) == tracking.FACE_COLOR  # left edge of the left-hand box


def test_a_frame_with_a_cut_border_has_it_and_one_without_does_not() -> None:
    plain, marked = blank(100, 100), blank(100, 100)
    tracking.draw_preview_frame(plain, [], None, None)
    tracking.draw_preview_frame(marked, [], None, None, cut=True)
    border = np.array(tracking.CUT_COLOR)
    assert not (plain == border).all(axis=2).any()
    assert (marked == border).all(axis=2)[0].all() and (marked == border).all(axis=2)[:, 99].all()
    assert not (marked == border).all(axis=2)[50, 50]  # the inside is untouched


# --- frame_signature ----------------------------------------------------------------------


def test_a_signature_has_48_numbers_that_sum_to_one() -> None:
    signature = tracking.frame_signature(np.full((90, 160, 3), 128, dtype=np.uint8))
    assert len(signature) == 48
    assert sum(signature) == pytest.approx(1.0)


def test_identical_frames_have_distance_zero_and_black_against_white_has_one() -> None:
    black = tracking.frame_signature(np.zeros((90, 160, 3), dtype=np.uint8))
    white = tracking.frame_signature(np.full((90, 160, 3), 255, dtype=np.uint8))
    assert camera.signature_distance(black, black) == 0.0
    assert camera.signature_distance(black, white) == pytest.approx(1.0)


def test_a_big_frame_is_subsampled_and_still_sums_to_one() -> None:
    signature = tracking.frame_signature(np.full((1080, 1920, 3), 200, dtype=np.uint8))
    assert sum(signature) == pytest.approx(1.0)


# --- old track files ----------------------------------------------------------------------


def test_a_track_file_from_before_brief_8_1_still_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track_fakes.install(monkeypatch, track_fakes.two_people())
    data = analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0).model_dump(mode="json")
    for key in ("detector_sha256", "opencv_version", "cuts", "cuts_exact"):
        del data[key]
    data["schema_version"] = 1
    for key in ("cuts", "faces_per_sample", "mouth_read_pct"):
        del data["summary"][key]
    data["settings"] = {"analysis_width": 640, "sample_fps": 10}

    old = TrackFile.model_validate(data)

    assert (old.detector_sha256, old.opencv_version, old.cuts) == (None, None, [])
    assert old.schema_version == 1 and old.cuts_exact == []
    assert (old.summary.cuts, old.summary.faces_per_sample, old.summary.mouth_read_pct) == (
        0,
        0.0,
        0.0,
    )
    assert old.settings.analysis_width == 640 and old.settings.detect_width == 960


# --- load_valid_track ---------------------------------------------------------------------


def saved_track(plan: TrackPlan, monkeypatch: pytest.MonkeyPatch) -> TrackFile:
    track_fakes.install(monkeypatch, track_fakes.two_people())
    track = analyse_clip(plan, "c01", 10.0, 30.0)
    write_json_atomic(tracking.track_path(plan.clips_dir, "c01"), track.model_dump(mode="json"))
    return track


def load(plan: TrackPlan, **changes: object) -> TrackFile | None:
    args: dict[str, object] = {
        "start": 10.0,
        "end": 30.0,
        "source_sha256": plan.source_sha256,
        "settings": plan.settings,
        "mediapipe_version": plan.mediapipe_version,
        "model_sha256": plan.model_sha256,
        "detector_sha256": plan.detector_sha256,
        "opencv_version": plan.opencv_version,
    }
    args.update(changes)
    return load_valid_track(plan.clips_dir, "c01", **args)  # type: ignore[arg-type]


def test_a_matching_track_is_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = make_plan(tmp_path)
    track = saved_track(plan, monkeypatch)
    assert load(plan) == track


def test_asking_for_a_preview_does_not_invalidate_a_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = make_plan(tmp_path)
    saved_track(plan, monkeypatch)
    assert load(plan, settings=SpeakerConfig(debug_preview=True)) is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"start": 11.0},
        {"end": 31.0},
        {"source_sha256": "c" * 64},
        {"settings": SpeakerConfig(smoothing=0.5)},
        {"settings": SpeakerConfig(sample_fps=5)},
        {"mediapipe_version": "1.0.0"},
        {"mediapipe_version": None},
        {"model_sha256": "d" * 64},
        {"detector_sha256": "f" * 64},
        {"detector_sha256": None},
        {"opencv_version": "5.1.0"},
        {"opencv_version": None},
    ],
)
def test_a_changed_input_invalidates_the_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object]
) -> None:
    plan = make_plan(tmp_path)
    saved_track(plan, monkeypatch)
    assert load(plan, **changes) is None


def test_a_schema_1_track_file_parses_but_is_not_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = make_plan(tmp_path)
    data = saved_track(plan, monkeypatch).model_dump(mode="json")
    assert data["schema_version"] == TRACK_SCHEMA_VERSION == 2
    data["schema_version"] = 1
    del data["cuts_exact"]
    write_json_atomic(tracking.track_path(plan.clips_dir, "c01"), data)

    path = tracking.track_path(plan.clips_dir, "c01")
    assert TrackFile.model_validate_json(path.read_text(encoding="utf-8")).cuts_exact == []
    assert load(plan) is None  # recomputed


def test_missing_or_broken_track_files_are_not_valid(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    assert load(plan) is None
    tracking.track_path(plan.clips_dir, "c01").write_text("{not json", encoding="utf-8")
    assert load(plan) is None
    tracking.track_path(plan.clips_dir, "c01").write_text('{"clip_id": "c01"}', encoding="utf-8")
    assert load(plan) is None


# --- the render stage's sweep -------------------------------------------------------------


@pytest.fixture
def fake_render(monkeypatch: pytest.MonkeyPatch) -> FakeFfmpeg:
    fake = FakeFfmpeg()
    monkeypatch.setattr(render.ffmpeg, "render_clip", fake.render_clip)
    monkeypatch.setattr(render.ffmpeg, "probe", fake.probe)
    return fake


def test_render_keeps_track_files_of_kept_clips_and_removes_the_others(
    project: Path, fake_render: FakeFfmpeg
) -> None:
    ctx: RunContext = make_ctx(project)
    write_inputs(ctx.run_dir)
    clips = ctx.run_dir / "clips"
    clips.mkdir()
    for clip_id in ("c01", "c02", "c03", "gone"):  # c03 is rejected, "gone" is not in scored.json
        (clips / f"{clip_id}.track.json").write_text("{}", encoding="utf-8")
        (clips / f"{clip_id}.track.preview.mp4").write_bytes(b"x")
    (clips / "c01.track.preview.tmp.mp4").write_bytes(b"x")  # an interrupted preview
    (clips / ".c01.track.json.abc.tmp").write_text("{}", encoding="utf-8")  # atomic-write leftover

    render.run(ctx)  # a forced redo: every render output is rebuilt, track files of kept clips stay

    assert [c[0] for c in CLIPS if c[1] is not None] == ["c01", "c02"]
    assert clip_files(ctx.run_dir) == [
        "c01.ass",
        "c01.json",
        "c01.mp4",
        "c01.track.json",
        "c01.track.preview.mp4",
        "c02.ass",
        "c02.json",
        "c02.mp4",
        "c02.track.json",
        "c02.track.preview.mp4",
    ]


def test_render_resume_also_keeps_track_files(project: Path, fake_render: FakeFfmpeg) -> None:
    ctx = make_ctx(project)
    write_inputs(ctx.run_dir)
    render.run(ctx)
    (ctx.run_dir / "clips" / "c01.track.json").write_text("{}", encoding="utf-8")

    resumed = RunContext(
        run_dir=ctx.run_dir,
        config=ctx.config,
        input_path=ctx.input_path,
        project_root=ctx.project_root,
        resuming=True,
    )
    render.run(resumed)

    assert (ctx.run_dir / "clips" / "c01.track.json").is_file()


# --- track_for_clip and build_plan --------------------------------------------------------


def test_track_for_clip_analyses_and_saves_then_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = track_fakes.install(monkeypatch, track_fakes.two_people())
    plan = make_plan(tmp_path)

    first, reused_first = tracking.track_for_clip(plan, "c01", 10.0, 30.0)
    second, reused_second = tracking.track_for_clip(plan, "c01", 10.0, 30.0)

    assert (reused_first, reused_second) == (False, True)
    assert first == second and fakes.opened == 1
    saved = tracking.track_path(plan.clips_dir, "c01").read_text(encoding="utf-8")
    assert TrackFile.model_validate_json(saved) == first


def test_track_for_clip_analyses_again_for_another_window_width(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = track_fakes.install(monkeypatch, track_fakes.two_people())
    plan = make_plan(tmp_path)
    tracking.track_for_clip(plan, "c01", 10.0, 30.0)

    _, reused = tracking.track_for_clip(plan, "c01", 10.0, 30.0, crop_w=100)
    _, reused_same = tracking.track_for_clip(plan, "c01", 10.0, 30.0, crop_w=202)

    assert (reused, reused_same) == (False, True) and fakes.opened == 2


def test_build_plan_checks_both_model_files(project: Path) -> None:
    from clipmachine.utils import faces

    app = make_ctx(project).config.app
    run_dir = project / "runs" / "run"
    args = {"video": video(), "source": run_dir / "source.mp4", "source_sha256": "a" * 64}

    with pytest.raises(faces.FacesError, match="fetch-face-model"):
        tracking.build_plan(run_dir, project, app, segments=[], **args)  # type: ignore[arg-type]
    track_fakes.write_model(project)
    (faces.model_dir(project, faces.DETECTOR) / faces.DETECTOR.name).unlink()
    with pytest.raises(faces.FacesError, match="fetch-face-model"):  # the detector is missing
        tracking.build_plan(run_dir, project, app, segments=[], **args)  # type: ignore[arg-type]
    track_fakes.write_model(project)

    plan = tracking.build_plan(run_dir, project, app, segments=[], **args)  # type: ignore[arg-type]

    assert plan.clips_dir == run_dir / "clips" and plan.clips_dir.is_dir()
    assert plan.settings == app.speaker
    assert (plan.target_width, plan.target_height) == (1080, 1920)
    assert len(plan.model_sha256) == 64 and len(plan.detector_sha256) == 64
