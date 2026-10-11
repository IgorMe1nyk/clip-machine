"""Speaker-crop analysis of one clip with the frame pipe and the landmarker faked."""

import json
from pathlib import Path

import numpy as np
import pytest

import track_fakes
from clipmachine import tracking
from clipmachine.context import RunContext
from clipmachine.models import (
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
    track_fakes.install(monkeypatch, track_fakes.two_people())
    track = analyse_clip(make_plan(tmp_path), "c01", 10.0, 30.0)

    raw = json.dumps(track.model_dump(mode="json"))
    assert "w10" not in raw and "synthetic" not in raw  # no transcript words
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

    assert track.fallback_reason == "faces_too_small"
    assert {s.crop_x for s in track.samples} == {218}
    assert track.summary.faces_seen == 2  # what was seen is still reported


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
    ],
)
def test_a_changed_input_invalidates_the_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object]
) -> None:
    plan = make_plan(tmp_path)
    saved_track(plan, monkeypatch)
    assert load(plan, **changes) is None


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
