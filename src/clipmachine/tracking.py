"""Speaker-following crop analysis for the kept clips of a run (Brief 8, Part A).

For each kept clip: decode only its time range at speaker.sample_fps and analysis_width, find
faces in every frame (Face Landmarker, through the `faces` seams), then let `utils/camera.py`
link tracks, choose the speaker from mouth movement where the transcript has words, cut shots and
plan the crop. The result is clips/<id>.track.json: numbers only, no transcript text. Optionally a
debug preview, clips/<id>.track.preview.mp4, shows the faces, the chosen speaker and the 9:16
window with the clip's audio.

This is not a pipeline stage and never touches run.json. The render stage ignores these files
until Part B.

Every output goes inside <run folder>/clips/. Logs and the printed lines name clip ids and
counts only.
"""

import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from clipmachine.models import (
    AppConfig,
    SpeakerConfig,
    TrackFile,
    TrackFrame,
    TrackSample,
    TrackShot,
    TrackSource,
    TrackSummary,
    TranscriptSegment,
    VideoStreamInfo,
)
from clipmachine.runs import RunError, write_json_atomic
from clipmachine.stages.candidates import _load_transcript
from clipmachine.stages.render import (
    CLIP_ID_RE,
    CLIPS_DIR,
    TRACK_JSON_SUFFIX,
    TRACK_PREVIEW_SUFFIX,
    _load_meta,
    _load_scored,
)
from clipmachine.stages.score import SCORED_JSON
from clipmachine.utils import camera, faces, ffmpeg
from clipmachine.utils.captions import clip_words
from clipmachine.utils.crop import center_crop, display_size

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

log = logging.getLogger("clipmachine")

PREVIEW_WIDTH = 480
PREVIEW_TEMP_SUFFIX = ".track.preview.tmp.mp4"  # ends in .tmp.mp4: the render sweep clears it
FACE_COLOR = (255, 255, 255)
ACTIVE_COLOR = (255, 40, 40)
WINDOW_COLOR = (40, 255, 40)
FACE_THICKNESS = 1
ACTIVE_THICKNESS = 3
WINDOW_THICKNESS = 2


@dataclass(frozen=True)
class TrackPlan:
    """Everything the clips of one run share."""

    clips_dir: Path
    source: Path
    video: VideoStreamInfo
    source_sha256: str
    target_width: int  # video.width / video.height: the aspect of the crop window
    target_height: int
    segments: list[TranscriptSegment]
    settings: SpeakerConfig
    model_path: Path
    model_sha256: str
    mediapipe_version: str | None


@dataclass(frozen=True)
class ClipTrackResult:
    """What the track command prints for one clip."""

    clip_id: str
    fallback_reason: str | None
    faces_seen: int
    switches: int
    face_time_pct: float
    analysis_seconds: float


def track_path(clips_dir: Path, clip_id: str) -> Path:
    return clips_dir / f"{clip_id}{TRACK_JSON_SUFFIX}"


def preview_path(clips_dir: Path, clip_id: str) -> Path:
    return clips_dir / f"{clip_id}{TRACK_PREVIEW_SUFFIX}"


# --- reuse --------------------------------------------------------------------------------


def _analysis_settings(settings: SpeakerConfig) -> SpeakerConfig:
    """The settings that shape the result; asking for a preview does not."""
    return settings.model_copy(update={"debug_preview": False})


def load_valid_track(
    clips_dir: Path,
    clip_id: str,
    *,
    start: float,
    end: float,
    source_sha256: str,
    settings: SpeakerConfig,
    mediapipe_version: str | None,
    model_sha256: str | None,
) -> TrackFile | None:
    """clips/<id>.track.json if it describes exactly this clip of this source analysed with these
    settings, this mediapipe version and this model file; otherwise None (missing, unreadable or
    stale). Part B's render stage reuses a valid track instead of analysing again."""
    path = track_path(clips_dir, clip_id)
    try:
        track = TrackFile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return None
    same = (
        track.clip_id == clip_id
        and track.clip_start == start
        and track.clip_end == end
        and track.source.sha256 == source_sha256
        and _analysis_settings(track.settings) == _analysis_settings(settings)
        and track.mediapipe_version == mediapipe_version
        and track.model_sha256 == model_sha256
    )
    return track if same else None


# --- the preview --------------------------------------------------------------------------


def draw_rect(
    frame: "NDArray[np.uint8]",
    left: float,
    top: float,
    right: float,
    bottom: float,
    color: tuple[int, int, int],
    thickness: int,
) -> None:
    """An unfilled rectangle in a (h, w, 3) frame, drawn in place by slicing. Coordinates are
    pixels and are clamped to the frame."""
    height, width = frame.shape[0], frame.shape[1]
    x0 = min(max(int(round(left)), 0), width - 1)
    x1 = min(max(int(round(right)), 0), width - 1)
    y0 = min(max(int(round(top)), 0), height - 1)
    y1 = min(max(int(round(bottom)), 0), height - 1)
    if x1 < x0 or y1 < y0:
        return
    t = max(thickness, 1)
    frame[y0 : min(y0 + t, y1 + 1), x0 : x1 + 1] = color
    frame[max(y1 - t + 1, y0) : y1 + 1, x0 : x1 + 1] = color
    frame[y0 : y1 + 1, x0 : min(x0 + t, x1 + 1)] = color
    frame[y0 : y1 + 1, max(x1 - t + 1, x0) : x1 + 1] = color


def draw_preview_frame(
    frame: "NDArray[np.uint8]",
    all_faces: Sequence[camera.FaceSample],
    active: camera.FaceSample | None,
    window: tuple[float, float] | None,
) -> None:
    """All face boxes thin, the active speaker's box thick and red, the crop window green.
    `window` is (left, right) as fractions of the frame width. No text, so no font is needed."""
    height, width = frame.shape[0], frame.shape[1]
    for item in all_faces:
        draw_rect(frame, *_box(item, width, height), FACE_COLOR, FACE_THICKNESS)
    if active is not None:
        draw_rect(frame, *_box(active, width, height), ACTIVE_COLOR, ACTIVE_THICKNESS)
    if window is not None:
        left, right = window[0] * width, window[1] * width - 1
        draw_rect(frame, left, 0, right, height - 1, WINDOW_COLOR, WINDOW_THICKNESS)


def _box(item: camera.FaceSample, width: int, height: int) -> tuple[float, float, float, float]:
    return (
        (item.cx - item.w / 2) * width,
        (item.cy - item.h / 2) * height,
        (item.cx + item.w / 2) * width,
        (item.cy + item.h / 2) * height,
    )


# --- one clip -----------------------------------------------------------------------------


def _shot_at(shots: Sequence[camera.Shot], t: float) -> camera.Shot | None:
    for shot in shots:
        if shot.start <= t < shot.end:
            return shot
    return shots[-1] if shots and t >= shots[-1].end else None


def _detect_faces(plan: TrackPlan, start: float, duration: float) -> list[list[camera.FaceSample]]:
    cfg = plan.settings
    video = plan.video
    frame_w, frame_h = ffmpeg.frame_size_after_scale(
        video.width, video.height, video.rotation, cfg.analysis_width
    )
    args = ffmpeg.frame_args(
        plan.source, start=start, duration=duration, fps=cfg.sample_fps, width=cfg.analysis_width
    )
    handle = faces.open_landmarker(plan.model_path, cfg.max_faces)
    frames: list[list[camera.FaceSample]] = []
    try:
        for k, frame in enumerate(
            ffmpeg.read_frames(
                args, frame_w, frame_h, ffmpeg.frames_timeout(duration), scratch_dir=plan.clips_dir
            )
        ):
            t = round(k / cfg.sample_fps, 3)
            raw = faces.detect(handle, frame, int(round(t * 1000)))
            frames.append(faces.to_samples(raw, t))
    finally:
        faces.close_landmarker(handle)
    return frames


def _preview_frames(
    plan: TrackPlan,
    start: float,
    duration: float,
    frame_size: tuple[int, int],
    frames: Sequence[Sequence[camera.FaceSample]],
    tracks: Sequence[camera.Track],
    shots: Sequence[camera.Shot],
    window_per_sample: Sequence[tuple[float, float] | None],
) -> Iterator["NDArray[np.uint8]"]:
    """Decode the clip again at preview size and draw the analysis on every frame."""
    cfg = plan.settings
    args = ffmpeg.frame_args(
        plan.source, start=start, duration=duration, fps=cfg.sample_fps, width=PREVIEW_WIDTH
    )
    by_id = {t.id: t for t in tracks}
    for k, frame in enumerate(
        ffmpeg.read_frames(
            args,
            frame_size[0],
            frame_size[1],
            ffmpeg.frames_timeout(duration),
            scratch_dir=plan.clips_dir,
        )
    ):
        if k < len(frames):
            shot = _shot_at(shots, round(k / cfg.sample_fps, 3))
            active = None
            if shot is not None and shot.track is not None and shot.track in by_id:
                active = by_id[shot.track].by_frame.get(k)
            window = window_per_sample[k] if k < len(window_per_sample) else None
            draw_preview_frame(frame, frames[k], active, window)
        yield frame


def analyse_clip(
    plan: TrackPlan, clip_id: str, start: float, end: float, *, preview: bool = False
) -> TrackFile:
    """Analyse one clip and return its TrackFile; with preview, also write the debug preview."""
    started = time.perf_counter()
    cfg = plan.settings
    video = plan.video
    duration = end - start
    try:
        box = center_crop(
            video.width, video.height, video.rotation, plan.target_width, plan.target_height
        )
    except ValueError as exc:
        raise RunError(f"cannot crop the source: {exc}") from exc
    source_w, _ = display_size(video.width, video.height, video.rotation)
    frame_w, frame_h = ffmpeg.frame_size_after_scale(
        video.width, video.height, video.rotation, cfg.analysis_width
    )

    frames = _detect_faces(plan, start, duration)
    times = [round(k / cfg.sample_fps, 3) for k in range(len(frames))]
    tracks = camera.link_tracks(frames, cfg.link_max_distance)
    reason = camera.fallback_reason(frames, cfg, source_w, box.w)

    words = [(w.start, w.end) for w in clip_words(plan.segments, start, end)]
    speech = camera.speech_windows(
        words, duration, cfg.window_seconds, cfg.min_speech_words_per_window
    )
    choice = camera.choose_speakers(tracks, speech, cfg.window_seconds, cfg)
    followed = camera.build_shots(choice, cfg.window_seconds, cfg.min_shot_seconds, duration)
    summary = camera.summarise(frames, tracks, followed)

    if reason is not None:  # centre crop, as render does today
        crop_xs = [box.x] * len(frames)
        shots = [camera.Shot(0.0, round(duration, 3), None)]
    else:
        crop_xs = camera.camera_path(followed, tracks, source_w, box.w, times, cfg)
        shots = list(followed)

    if preview:
        _write_preview(
            plan,
            clip_id,
            start,
            duration,
            (PREVIEW_WIDTH, _preview_height(plan)),
            frames,
            tracks,
            [] if reason is not None else followed,
            [(x / source_w, (x + box.w) / source_w) for x in crop_xs],
        )

    track_by_id = {t.id: t for t in tracks}
    track_shots: list[TrackShot] = []
    for shot in shots:
        face_cx: float | None = None
        if shot.track is not None:
            xs = [
                s.cx for k, s in track_by_id[shot.track].points if _shot_at(shots, times[k]) is shot
            ]
            face_cx = round(sum(xs) / len(xs), 4) if xs else None
        track_shots.append(
            TrackShot(
                start=round(shot.start, 3),
                end=round(shot.end, 3),
                track=shot.track,
                face_cx=face_cx,
            )
        )
    seconds = round(time.perf_counter() - started, 2)
    return TrackFile(
        clip_id=clip_id,
        clip_start=start,
        clip_end=end,
        source=TrackSource(
            width=video.width,
            height=video.height,
            rotation=video.rotation,
            sha256=plan.source_sha256,
        ),
        settings=cfg,
        mediapipe_version=plan.mediapipe_version,
        model_sha256=plan.model_sha256,
        frame=TrackFrame(w=frame_w, h=frame_h),
        crop_w=box.w,
        shots=track_shots,
        samples=[TrackSample(t=t, crop_x=x) for t, x in zip(times, crop_xs, strict=True)],
        fallback_reason=reason,
        summary=TrackSummary(
            faces_seen=summary.faces_seen,
            switches=summary.switches,
            face_time_pct=summary.face_time_pct,
            samples=len(frames),
            analysis_seconds=seconds,
        ),
    )


def _preview_height(plan: TrackPlan) -> int:
    video = plan.video
    return ffmpeg.frame_size_after_scale(video.width, video.height, video.rotation, PREVIEW_WIDTH)[
        1
    ]


def _write_preview(
    plan: TrackPlan,
    clip_id: str,
    start: float,
    duration: float,
    frame_size: tuple[int, int],
    frames: Sequence[Sequence[camera.FaceSample]],
    tracks: Sequence[camera.Track],
    shots: Sequence[camera.Shot],
    windows: Sequence[tuple[float, float] | None],
) -> None:
    cfg = plan.settings
    out = preview_path(plan.clips_dir, clip_id)
    temp = plan.clips_dir / f"{clip_id}{PREVIEW_TEMP_SUFFIX}"
    args = ffmpeg.preview_args(
        plan.source,
        temp,
        start=start,
        duration=duration,
        fps=cfg.sample_fps,
        width=frame_size[0],
        height=frame_size[1],
    )
    ffmpeg.write_preview(
        _preview_frames(plan, start, duration, frame_size, frames, tracks, shots, windows),
        args,
        temp,
        out,
        ffmpeg.frames_timeout(duration),
        scratch_dir=plan.clips_dir,
    )


# --- the run ------------------------------------------------------------------------------


def run_track(
    run_dir: Path, root: Path, app: AppConfig, *, preview: bool = False
) -> list[ClipTrackResult]:
    """Analyse every kept clip of a run and write clips/<id>.track.json (and the preview).

    Needs scored.json, meta.json and transcript.json from earlier stages and the face model file
    (never downloaded here). Returns one result per kept clip, in rank order; an empty list when
    no clip was kept.
    """
    run_dir = run_dir.resolve()
    scored = _load_scored(run_dir)
    kept = sorted((c for c in scored.clips if c.kept), key=lambda c: c.rank or 0)
    if any(not CLIP_ID_RE.fullmatch(c.id) for c in kept):
        raise RunError(f"{SCORED_JSON} has a clip id that is not a safe file name")
    if not kept:
        return []

    meta = _load_meta(run_dir)
    video = meta.media.video
    if video is None:
        raise RunError("meta.json has no video stream")
    source = run_dir / meta.source_file
    if not source.is_file():
        raise RunError(f"source video not found in the run folder: {meta.source_file}")
    segments = _load_transcript(run_dir).segments
    model = faces.ensure_model(faces.model_dir(root))

    clips_dir = run_dir / CLIPS_DIR
    clips_dir.mkdir(exist_ok=True)
    plan = TrackPlan(
        clips_dir=clips_dir,
        source=source,
        video=video,
        source_sha256=meta.sha256,
        target_width=app.video.width,
        target_height=app.video.height,
        segments=segments,
        settings=app.speaker,
        model_path=model,
        model_sha256=faces.file_sha256(model),
        mediapipe_version=faces.mediapipe_version(),
    )
    results: list[ClipTrackResult] = []
    for clip in kept:
        track = analyse_clip(plan, clip.id, clip.start, clip.end, preview=preview)
        write_json_atomic(track_path(clips_dir, clip.id), track.model_dump(mode="json"))
        results.append(
            ClipTrackResult(
                clip_id=clip.id,
                fallback_reason=track.fallback_reason,
                faces_seen=track.summary.faces_seen,
                switches=track.summary.switches,
                face_time_pct=track.summary.face_time_pct,
                analysis_seconds=track.summary.analysis_seconds,
            )
        )
    return results
