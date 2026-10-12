"""Stand-ins for the frame pipe and the face finder, so tracking tests need neither ffmpeg, opencv
nor mediapipe. Everything is synthetic: flat grey frames and scripted faces."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from clipmachine import tracking
from clipmachine.utils import faces
from clipmachine.utils.faces import (
    DETECTOR,
    LANDMARKER,
    FaceBox,
    FaceFinder,
    FoundFaces,
    RawFace,
)

MOTION = 0.25  # what the fake finder reports as motion whenever there is a previous frame

# t (seconds from the clip start) -> the faces in that frame, before small ones are dropped
Scenario = Callable[[float], list[RawFace]]
# t -> the grey level (0-255) the frame is painted with; a jump is a cut in the "source's edit"
Look = Callable[[float], int]


def talking(t: float) -> float:
    """A moving mouth: alternates open and closed every sample (0.1 s)."""
    return 0.4 if int(round(t * 10)) % 2 else 0.0


def two_people(switch_at: float = 10.0, size: float = 0.3) -> Scenario:
    """Person A (left) talks before switch_at, person B (right) after."""

    def scenario(t: float) -> list[RawFace]:
        a_talks = t < switch_at
        return [
            RawFace(0.25, 0.5, size / 2, size, talking(t) if a_talks else 0.05),
            RawFace(0.75, 0.5, size / 2, size, 0.05 if a_talks else talking(t)),
        ]

    return scenario


def nobody(t: float) -> list[RawFace]:
    return []


def steady(t: float) -> int:
    return 0


def cut_at(*times: float, levels: tuple[int, int] = (0, 255)) -> Look:
    """The picture alternates between two grey levels at each of the given times."""

    def look(t: float) -> int:
        return levels[sum(1 for c in times if t >= c - 1e-9) % 2]

    return look


@dataclass
class TrackFakes:
    scenario: Scenario = nobody
    look: Look = steady
    frame_requests: list[list[str]] = field(default_factory=list)
    frame_sizes: list[tuple[int, int]] = field(default_factory=list)
    previews: list[dict[str, Any]] = field(default_factory=list)
    finder_args: list[dict[str, Any]] = field(default_factory=list)
    opened: int = 0
    closed: int = 0
    fps: float = 10.0
    index: int = 0  # frames handed to find_faces since read_frames was last called
    detected: list[int] = field(default_factory=list)  # samples on which the detector ran
    previous_seen: list[bool] = field(default_factory=list)  # find_faces got a previous frame
    boxes_given: list[list[FaceBox] | None] = field(default_factory=list)
    # read_timed_frames: the source's own frames around a cut. Unless scripted, they come from
    # `look` at timed_fps, with timestamps from the -ss point like ffmpeg's showinfo.
    timed: list[tuple[float, int]] | None = None  # scripted (timestamp, grey level) pairs
    timed_error: Exception | None = None  # raised instead of returning frames
    timed_fps: float = 30.0
    timed_requests: list[list[str]] = field(default_factory=list)
    timed_sizes: list[tuple[int, int]] = field(default_factory=list)

    def read_frames(
        self,
        args: list[str],
        frame_w: int,
        frame_h: int,
        timeout: float,
        *,
        scratch_dir: Path | None = None,
    ) -> Iterator["np.ndarray[Any, Any]"]:
        self.frame_requests.append(args)
        self.frame_sizes.append((frame_w, frame_h))
        duration = float(args[args.index("-t") + 1])
        self.fps = float(args[args.index("-vf") + 1].split(",")[0].removeprefix("fps="))
        self.index = 0
        for k in range(int(round(duration * self.fps))):
            grey = self.look(k / self.fps)
            yield np.full((frame_h, frame_w, 3), grey, dtype=np.uint8)

    def read_timed_frames(
        self, args: list[str], frame_w: int, frame_h: int, timeout: float
    ) -> list[tuple[float, "np.ndarray[Any, Any]"]]:
        self.timed_requests.append(args)
        self.timed_sizes.append((frame_w, frame_h))
        if self.timed_error is not None:
            raise self.timed_error
        if self.timed is not None:
            return [(t, np.full((frame_h, frame_w, 3), g, dtype=np.uint8)) for t, g in self.timed]
        # `look` is in clip time: the first analysis decode started at the clip start
        origin = float(self.frame_requests[0][self.frame_requests[0].index("-ss") + 1])
        offset = float(args[args.index("-ss") + 1]) - origin
        duration = float(args[args.index("-t") + 1])
        stamps = [k / self.timed_fps for k in range(int(round(duration * self.timed_fps)))]
        return [
            (t, np.full((frame_h, frame_w, 3), self.look(offset + t), dtype=np.uint8))
            for t in stamps
        ]

    def open_finder(
        self, detector_path: Path, landmarker_path: Path, **settings: Any
    ) -> FaceFinder:
        self.opened += 1
        assert detector_path.name == DETECTOR.name
        assert landmarker_path.name == LANDMARKER.name
        self.finder_args.append(settings)
        return FaceFinder(
            detector=None,
            landmarker=None,
            detect_width=settings["detect_width"],
            crop_scale=settings["crop_scale"],
            min_face_height=settings["min_face_height"],
            max_faces=settings["max_faces"],
        )

    def find_faces(
        self,
        finder: FaceFinder,
        frame: Any,
        previous_rgb: Any = None,
        boxes: list[FaceBox] | None = None,
    ) -> FoundFaces:
        """What the real finder returns: boxes under min_face_height dropped but still counted in
        largest_height, and at most max_faces kept. The detector "runs" (and is recorded in
        `detected`) only when no boxes are handed in; mouths are scripted for every sample, and
        motion is MOTION when there is a previous frame, else None."""
        sample = self.index
        t = sample / self.fps
        self.index += 1
        self.previous_seen.append(previous_rgb is not None)
        self.boxes_given.append(boxes)
        seen = self.scenario(t)
        kept = [f for f in seen if f.h >= finder.min_face_height][: finder.max_faces]
        if boxes is None:
            self.detected.append(sample)
            largest = max((f.h for f in seen), default=0.0)
            kept_boxes = [FaceBox(f.cx, f.cy, f.w, f.h, 1.0) for f in kept]
        else:
            largest = max((b.h for b in boxes), default=0.0)
            kept_boxes = list(boxes)
        motion = None if previous_rgb is None else MOTION
        faces_out = [replace(f, motion=motion) for f in kept]
        return FoundFaces(faces=faces_out, largest_height=largest, boxes=kept_boxes)

    def close_finder(self, finder: FaceFinder) -> None:
        self.closed += 1

    def write_preview(
        self,
        frames: Any,
        args: list[str],
        temp_out: Path,
        out: Path,
        timeout: float,
        *,
        scratch_dir: Path | None = None,
    ) -> None:
        drawn = [frame.copy() for frame in frames]
        self.previews.append({"args": args, "frames": drawn, "out": out, "temp": temp_out})
        out.write_bytes(b"fake preview")


def install(
    monkeypatch: pytest.MonkeyPatch, scenario: Scenario = nobody, look: Look = steady
) -> TrackFakes:
    fakes = TrackFakes(scenario=scenario, look=look)
    monkeypatch.setattr(tracking.ffmpeg, "read_frames", fakes.read_frames)
    monkeypatch.setattr(tracking.ffmpeg, "read_timed_frames", fakes.read_timed_frames)
    monkeypatch.setattr(tracking.ffmpeg, "write_preview", fakes.write_preview)
    monkeypatch.setattr(tracking.faces, "open_finder", fakes.open_finder)
    monkeypatch.setattr(tracking.faces, "find_faces", fakes.find_faces)
    monkeypatch.setattr(tracking.faces, "close_finder", fakes.close_finder)
    return fakes


def write_model(project: Path) -> Path:
    """Tiny stand-ins for both model files where the track command looks for them. Returns the
    landmarker's path."""
    for spec in (DETECTOR, LANDMARKER):
        folder = faces.model_dir(project, spec)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / spec.name).write_bytes(b"fake " + spec.name.encode())
    return faces.model_dir(project) / LANDMARKER.name
