"""On-screen graphics (Brief 12.1): where text panels sit in a clip, and which camera shots show
one outside the speaker crop window, so the render stage can show those shots whole.

Detection only: OpenCV's DB text detector (the PP-OCRv3 model, `faces.TEXT_DETECTOR`) gives the
boxes of text on sampled frames; nothing is read. Boxes at the same place in nearly every sample
of the clip (a chapter title, the show logo) are static overlays and are ignored. A shot "has
graphics" when the other boxes cover enough of the frame in enough of its samples and one of them
reaches outside the crop window in force. The result is cached in clips/<id>.graphics.json and
reused while the clip, source, settings and model are the same, like the track file.

Pure functions apart from the two SEAMs (the detector) and the frame decoding; no transcript text.
"""

import logging
import time
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from clipmachine.models import (
    GRAPHICS_SCHEMA_VERSION,
    GraphicsBox,
    GraphicsConfig,
    GraphicsFile,
    ShotGraphics,
    TrackFrame,
    VideoStreamInfo,
)
from clipmachine.runs import write_json_atomic
from clipmachine.utils import faces, ffmpeg
from clipmachine.utils.crop import display_size, effective_boundaries

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

log = logging.getLogger("clipmachine")

GRAPHICS_JSON_SUFFIX = ".graphics.json"
MATCH_IOU = 0.5  # two boxes this much alike are "the same place"
# The DB detector's input: the OpenCV sample's values for the PP-OCR models
DB_INPUT_SIZE = (736, 736)
DB_MEAN = (122.67891434, 116.66876762, 104.00698793)
DB_SCALE = 1.0 / 255.0
DB_BINARY_THRESHOLD = 0.3
DB_POLYGON_THRESHOLD = 0.5
DB_UNCLIP_RATIO = 2.0
DB_MAX_CANDIDATES = 200
TIME_DIGITS = 3


@dataclass(frozen=True)
class Box:
    """A rectangle as fractions (0..1) of the frame; x2 and y2 are the right and bottom edges."""

    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)

    def iou(self, other: "Box") -> float:
        w = min(self.x2, other.x2) - max(self.x1, other.x1)
        h = min(self.y2, other.y2) - max(self.y1, other.y1)
        inter = max(0.0, w) * max(0.0, h)
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0

    def inside(self, window: "Box") -> bool:
        """True when the box lies fully within `window` (touching its edges is allowed)."""
        return (
            self.x1 >= window.x1
            and self.y1 >= window.y1
            and self.x2 <= window.x2
            and self.y2 <= window.y2
        )

    def to_model(self) -> GraphicsBox:
        return GraphicsBox(
            x1=round(self.x1, 4), y1=round(self.y1, 4), x2=round(self.x2, 4), y2=round(self.y2, 4)
        )


def from_model(box: GraphicsBox) -> Box:
    return Box(box.x1, box.y1, box.x2, box.y2)


@dataclass(frozen=True)
class GraphicsPlan:
    """What the graphics analysis of every clip of a run shares."""

    clips_dir: Path
    source: Path
    video: VideoStreamInfo
    source_sha256: str
    settings: GraphicsConfig
    model_path: Path
    model_sha256: str
    opencv_version: str | None


# --- the detector (SEAMs) ------------------------------------------------------------------------


def open_text_detector(model_path: Path, expected_sha256: str) -> Any:  # SEAM
    """OpenCV's DB text detector loaded with the PP-OCRv3 model. The file is hashed again before
    use; a changed file is a FacesError, never a silent load."""
    actual = faces.file_sha256(model_path)
    if actual != expected_sha256:
        raise faces.FacesError(
            f"{faces.TEXT_DETECTOR.label} at {model_path} has SHA-256 {actual}, expected "
            f"{expected_sha256}; the file changed since the plan was made"
        )
    cv2 = faces._cv2()
    try:
        detector = cv2.dnn.TextDetectionModel_DB(str(model_path))
    except Exception as exc:  # cv2.error or a model the build cannot read
        raise faces.FacesError(
            f"could not load the {faces.TEXT_DETECTOR.label}: {faces._one_line(exc)}"
        ) from exc
    detector.setBinaryThreshold(DB_BINARY_THRESHOLD)
    detector.setPolygonThreshold(DB_POLYGON_THRESHOLD)
    detector.setMaxCandidates(DB_MAX_CANDIDATES)
    detector.setUnclipRatio(DB_UNCLIP_RATIO)
    detector.setInputParams(DB_SCALE, DB_INPUT_SIZE, DB_MEAN, True)
    return detector


def detect_text(detector: Any, frame_rgb: "NDArray[np.uint8]") -> list[Box]:  # SEAM
    """The text boxes in one RGB frame as fractions of the frame (the axis-aligned bounds of
    each quad the model gives)."""
    h, w = frame_rgb.shape[:2]
    quads, _ = detector.detect(frame_rgb[:, :, ::-1])  # the model expects BGR
    boxes: list[Box] = []
    for quad in quads:
        xs = [float(p[0]) for p in quad]
        ys = [float(p[1]) for p in quad]
        boxes.append(
            Box(
                min(max(min(xs) / w, 0.0), 1.0),
                min(max(min(ys) / h, 0.0), 1.0),
                min(max(max(xs) / w, 0.0), 1.0),
                min(max(max(ys) / h, 0.0), 1.0),
            )
        )
    return [b for b in boxes if b.area > 0]


# --- sampling -------------------------------------------------------------------------------------


def decode_size(plan: GraphicsPlan) -> tuple[int, int]:
    """Frames are decoded at detect_width, never above the upright source's width."""
    video = plan.video
    upright_w, _ = display_size(video.width, video.height, video.rotation)
    return ffmpeg.frame_size_after_scale(
        video.width, video.height, video.rotation, min(plan.settings.detect_width, upright_w)
    )


def sample_text(plan: GraphicsPlan, start: float, duration: float) -> list[list[Box]]:
    """The text boxes of every sampled frame of [start, start + duration): sample k is at
    k / sample_fps seconds from the clip start."""
    frame_w, frame_h = decode_size(plan)
    args = ffmpeg.frame_args(
        plan.source, start=start, duration=duration, fps=plan.settings.sample_fps, width=frame_w
    )
    detector = open_text_detector(plan.model_path, plan.model_sha256)
    samples: list[list[Box]] = []
    for frame in ffmpeg.read_frames(
        args, frame_w, frame_h, ffmpeg.frames_timeout(duration), scratch_dir=plan.clips_dir
    ):
        samples.append(detect_text(detector, frame))
    return samples


# --- the rules (pure) -----------------------------------------------------------------------------


def learn_static(samples: Sequence[Sequence[Box]], static_ratio: float) -> list[Box]:
    """Boxes at the same place (IoU >= MATCH_IOU with the first box seen there) in at least
    `static_ratio` of the samples: the overlays a clip carries throughout."""
    if not samples:
        return []
    clusters: list[tuple[Box, int]] = []
    for boxes in samples:
        seen: set[int] = set()
        for box in boxes:
            for i, (first, _) in enumerate(clusters):
                if i not in seen and first.iou(box) >= MATCH_IOU:
                    clusters[i] = (first, clusters[i][1] + 1)
                    seen.add(i)
                    break
            else:
                clusters.append((box, 1))
                seen.add(len(clusters) - 1)
    needed = static_ratio * len(samples)
    return [first for first, count in clusters if count >= needed]


def is_static(box: Box, static: Sequence[Box]) -> bool:
    return any(box.iou(s) >= MATCH_IOU for s in static)


def shot_graphics(
    start: float,
    end: float,
    samples: Sequence[tuple[float, Sequence[Box]]],
    static: Sequence[Box],
    window_at: Callable[[float], Box],
    cfg: GraphicsConfig,
) -> ShotGraphics:
    """One shot's verdict from its samples (time, boxes): `fit` when non-static text covers at
    least min_area_frac of the frame in at least min_presence of the samples and, in one of those
    samples, a box reaches outside the crop window in force at that time."""
    present_areas: list[float] = []
    outside = False
    for t, boxes in samples:
        live = [b for b in boxes if not is_static(b, static)]
        area = sum(b.area for b in live)
        if area < cfg.min_area_frac:
            continue
        present_areas.append(area)
        window = window_at(t)
        if any(not b.inside(window) for b in live):
            outside = True
    presence = len(present_areas) / len(samples) if samples else 0.0
    area = sum(present_areas) / len(present_areas) if present_areas else 0.0
    fit = presence >= cfg.min_presence and outside
    return ShotGraphics(
        start=round(start, TIME_DIGITS),
        end=round(end, TIME_DIGITS),
        fit=fit,
        area=round(area, 4),
        presence=round(presence, 4),
        outside=outside,
    )


def shot_ranges(
    shots: Sequence[tuple[float, float]],
    cuts: Sequence[float],
    cuts_exact: Sequence[float],
    duration: float,
) -> list[tuple[float, float]]:
    """Each shot's real [start, end): the exact cut times where the track has them (the same
    boundaries the crop path and the zooms use). No shots means one shot over the clip."""
    all_shots = list(shots) or [(0.0, duration)]
    bounds = effective_boundaries(all_shots, cuts, cuts_exact, duration)
    edges = [0.0, *bounds, duration]
    return [(edges[i], edges[i + 1]) for i in range(len(all_shots))]


def fit_spans(results: Sequence[ShotGraphics]) -> list[tuple[float, float]]:
    """The fit shots as (start, end) spans, neighbours merged, empty spans dropped."""
    spans: list[tuple[float, float]] = []
    for shot in results:
        if not shot.fit or shot.end <= shot.start:
            continue
        if spans and abs(spans[-1][1] - shot.start) < 1e-6:
            spans[-1] = (spans[-1][0], shot.end)
        else:
            spans.append((shot.start, shot.end))
    return spans


def window_from_path(
    crop_path: Sequence[tuple[float, int]], crop_w: int, source_w: int
) -> Callable[[float], Box]:
    """The crop window in force at a time, as fractions of the upright source frame: the x of the
    latest path entry not after t (the first entry before that), full height."""
    ordered = sorted(crop_path)
    times = [t for t, _ in ordered]

    def window_at(t: float) -> Box:
        i = max(bisect_right(times, t + 1e-9) - 1, 0)
        x = ordered[i][1]
        return Box(x / source_w, 0.0, min((x + crop_w) / source_w, 1.0), 1.0)

    return window_at


# --- the file -------------------------------------------------------------------------------------


def graphics_path(clips_dir: Path, clip_id: str) -> Path:
    return clips_dir / f"{clip_id}{GRAPHICS_JSON_SUFFIX}"


def load_valid_graphics(
    clips_dir: Path,
    clip_id: str,
    *,
    start: float,
    end: float,
    source_sha256: str,
    settings: GraphicsConfig,
    model_sha256: str,
    opencv_version: str | None,
) -> GraphicsFile | None:
    """clips/<id>.graphics.json if it describes exactly this clip of this source analysed with
    these settings, this model and this OpenCV; otherwise None."""
    path = graphics_path(clips_dir, clip_id)
    try:
        found = GraphicsFile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return None
    same = (
        found.schema_version == GRAPHICS_SCHEMA_VERSION
        and found.clip_id == clip_id
        and found.clip_start == start
        and found.clip_end == end
        and found.source_sha256 == source_sha256
        and found.settings == settings
        and found.model_sha256 == model_sha256
        and found.opencv_version == opencv_version
    )
    return found if same else None


def analyse(
    plan: GraphicsPlan,
    clip_id: str,
    start: float,
    end: float,
    *,
    shots: Sequence[tuple[float, float]],
    cuts: Sequence[float],
    cuts_exact: Sequence[float],
    crop_path: Sequence[tuple[float, int]],
    crop_w: int,
) -> GraphicsFile:
    """Sample the clip, learn its static overlays, judge every shot. `shots`, `cuts` and
    `cuts_exact` come from the track (none: one shot), `crop_path` is the crop x over time as the
    render stage uses it ((0.0, x) alone for a fixed crop)."""
    started = time.perf_counter()
    duration = end - start
    cfg = plan.settings
    samples = sample_text(plan, start, duration)
    timed = [(round(k / cfg.sample_fps, TIME_DIGITS), boxes) for k, boxes in enumerate(samples)]
    static = learn_static(samples, cfg.static_ratio)
    source_w, _ = display_size(plan.video.width, plan.video.height, plan.video.rotation)
    window_at = window_from_path(crop_path, crop_w, source_w)
    ranges = shot_ranges(shots, cuts, cuts_exact, duration)
    starts = [s for s, _ in ranges[1:]]
    per_shot: list[list[tuple[float, Sequence[Box]]]] = [[] for _ in ranges]
    for t, boxes in timed:
        per_shot[bisect_right(starts, t)].append((t, boxes))
    results = [
        shot_graphics(s, e, per_shot[i], static, window_at, cfg) for i, (s, e) in enumerate(ranges)
    ]
    frame_w, frame_h = decode_size(plan)
    return GraphicsFile(
        clip_id=clip_id,
        clip_start=start,
        clip_end=end,
        source_sha256=plan.source_sha256,
        model_sha256=plan.model_sha256,
        opencv_version=plan.opencv_version,
        settings=cfg,
        frame=TrackFrame(w=frame_w, h=frame_h),
        samples=len(samples),
        static=[b.to_model() for b in static],
        shots=results,
        fit_spans=fit_spans(results),
        analysis_seconds=round(time.perf_counter() - started, 3),
    )


def graphics_for_clip(
    plan: GraphicsPlan,
    clip_id: str,
    start: float,
    end: float,
    *,
    shots: Sequence[tuple[float, float]],
    cuts: Sequence[float],
    cuts_exact: Sequence[float],
    crop_path: Sequence[tuple[float, int]],
    crop_w: int,
) -> tuple[GraphicsFile, bool]:
    """A valid clips/<id>.graphics.json if there is one (second value True: reused), else a
    fresh analysis written to that path."""
    found = load_valid_graphics(
        plan.clips_dir,
        clip_id,
        start=start,
        end=end,
        source_sha256=plan.source_sha256,
        settings=plan.settings,
        model_sha256=plan.model_sha256,
        opencv_version=plan.opencv_version,
    )
    if found is not None:
        return found, True
    found = analyse(
        plan,
        clip_id,
        start,
        end,
        shots=shots,
        cuts=cuts,
        cuts_exact=cuts_exact,
        crop_path=crop_path,
        crop_w=crop_w,
    )
    write_json_atomic(graphics_path(plan.clips_dir, clip_id), found.model_dump(mode="json"))
    return found, False


def build_plan(
    clips_dir: Path,
    root: Path,
    settings: GraphicsConfig,
    *,
    video: VideoStreamInfo,
    source: Path,
    source_sha256: str,
) -> GraphicsPlan:
    """Checks the text detector file (a missing or wrong one is a FacesError naming the fetch
    command; nothing is downloaded) and hashes it."""
    model = faces.ensure_model(faces.model_dir(root, faces.TEXT_DETECTOR), spec=faces.TEXT_DETECTOR)
    return GraphicsPlan(
        clips_dir=clips_dir,
        source=source,
        video=video,
        source_sha256=source_sha256,
        settings=settings,
        model_path=model,
        model_sha256=faces.file_sha256(model),
        opencv_version=faces.opencv_version(),
    )


def position_word(box: GraphicsBox) -> str:
    """Where a box sits, in words, for the report: its centre's ninth of the frame."""
    cx, cy = (box.x1 + box.x2) / 2, (box.y1 + box.y2) / 2
    row = "top" if cy < 1 / 3 else "bottom" if cy > 2 / 3 else "middle"
    col = "left" if cx < 1 / 3 else "right" if cx > 2 / 3 else "centre"
    return f"{row}-{col}" if row != "middle" or col != "centre" else "centre"
