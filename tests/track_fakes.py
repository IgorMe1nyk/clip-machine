"""Stand-ins for the frame pipe and the Face Landmarker, so tracking tests need neither ffmpeg nor
mediapipe. Everything is synthetic: blank frames and scripted faces."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from clipmachine import tracking
from clipmachine.utils import faces
from clipmachine.utils.faces import MODEL_NAME, RawFace

# t (seconds from the clip start) -> the faces in that frame
Scenario = Callable[[float], list[RawFace]]


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


@dataclass
class TrackFakes:
    scenario: Scenario = nobody
    frame_requests: list[list[str]] = field(default_factory=list)
    previews: list[dict[str, Any]] = field(default_factory=list)
    opened: int = 0
    closed: int = 0

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
        duration = float(args[args.index("-t") + 1])
        fps = float(args[args.index("-vf") + 1].split(",")[0].removeprefix("fps="))
        for _ in range(int(round(duration * fps))):
            yield np.zeros((frame_h, frame_w, 3), dtype=np.uint8)

    def open_landmarker(self, model_path: Path, max_faces: int) -> object:
        self.opened += 1
        assert model_path.name == MODEL_NAME
        return object()

    def detect(self, handle: object, frame: Any, t_ms: int) -> list[RawFace]:
        return self.scenario(t_ms / 1000)

    def close_landmarker(self, handle: object) -> None:
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


def install(monkeypatch: pytest.MonkeyPatch, scenario: Scenario = nobody) -> TrackFakes:
    fakes = TrackFakes(scenario=scenario)
    monkeypatch.setattr(tracking.ffmpeg, "read_frames", fakes.read_frames)
    monkeypatch.setattr(tracking.ffmpeg, "write_preview", fakes.write_preview)
    monkeypatch.setattr(tracking.faces, "open_landmarker", fakes.open_landmarker)
    monkeypatch.setattr(tracking.faces, "detect", fakes.detect)
    monkeypatch.setattr(tracking.faces, "close_landmarker", fakes.close_landmarker)
    return fakes


def write_model(project: Path) -> Path:
    """A tiny stand-in for the model file where the track command looks for it."""
    folder = faces.model_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / MODEL_NAME
    path.write_bytes(b"fake face model")
    return path
