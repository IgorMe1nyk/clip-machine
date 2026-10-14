"""Fakes for the graphics analysis (Brief 12.1): scripted text boxes per sample time instead of
OpenCV, flat frames instead of ffmpeg. The frames come from `track_fakes` when a test also runs
the speaker tracking, so both see the same decode."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from clipmachine.utils import faces, graphics
from clipmachine.utils.graphics import Box

Scenario = Callable[[float], list[Box]]  # the text boxes at a time (seconds from the clip start)

TITLE = Box(0.05, 0.04, 0.30, 0.07)  # a chapter title, top left
LOGO = Box(0.87, 0.91, 0.96, 0.97)  # the show logo, bottom right
PANEL = Box(0.07, 0.25, 0.38, 0.42)  # a definition card, left of centre


def nothing(t: float) -> list[Box]:
    return []


def overlays_only(t: float) -> list[Box]:
    return [TITLE, LOGO]


def panel_between(start: float, end: float) -> Scenario:
    """The overlays always, plus the panel during [start, end)."""

    def scenario(t: float) -> list[Box]:
        return [TITLE, LOGO, PANEL] if start - 1e-9 <= t < end - 1e-9 else [TITLE, LOGO]

    return scenario


@dataclass
class GraphicsFakes:
    scenario: Scenario = nothing
    frame_requests: list[list[str]] = field(default_factory=list)
    opened: list[tuple[Path, str]] = field(default_factory=list)
    fps: float = 2.0
    index: int = 0
    delegate: Callable[..., Iterator[Any]] | None = None  # track_fakes.read_frames, if any

    def read_frames(
        self,
        args: list[str],
        frame_w: int,
        frame_h: int,
        timeout: float,
        *,
        scratch_dir: Path | None = None,
    ) -> Iterator[Any]:
        self.frame_requests.append(args)
        self.fps = float(args[args.index("-vf") + 1].split(",")[0].removeprefix("fps="))
        self.index = 0
        if self.delegate is not None:
            yield from self.delegate(args, frame_w, frame_h, timeout, scratch_dir=scratch_dir)
            return
        duration = float(args[args.index("-t") + 1])
        for _ in range(int(round(duration * self.fps))):
            yield np.zeros((frame_h, frame_w, 3), dtype=np.uint8)

    def open_text_detector(self, model_path: Path, expected_sha256: str) -> Any:
        assert model_path.name == faces.TEXT_DETECTOR.name
        self.opened.append((model_path, expected_sha256))
        return object()

    def detect_text(self, detector: Any, frame: Any) -> list[Box]:
        t = self.index / self.fps
        self.index += 1
        return list(self.scenario(t))


def install(
    monkeypatch: pytest.MonkeyPatch,
    scenario: Scenario = nothing,
    *,
    track_fakes: Any = None,
) -> GraphicsFakes:
    """Patch the seams. With `track_fakes` (a TrackFakes), frames for both analyses come from it,
    so the tracking fake's own sample counter keeps working."""
    fakes = GraphicsFakes(scenario=scenario)
    if track_fakes is not None:
        fakes.delegate = track_fakes.read_frames
    monkeypatch.setattr(graphics.ffmpeg, "read_frames", fakes.read_frames)
    monkeypatch.setattr(graphics, "open_text_detector", fakes.open_text_detector)
    monkeypatch.setattr(graphics, "detect_text", fakes.detect_text)
    return fakes


def write_text_model(project: Path) -> Path:
    """A tiny stand-in for the text detector file where the code looks for it."""
    folder = faces.model_dir(project, faces.TEXT_DETECTOR)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / faces.TEXT_DETECTOR.name
    path.write_bytes(b"fake text detector")
    return path
