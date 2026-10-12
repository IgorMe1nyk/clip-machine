"""Face finding: model file checks, YuNet face boxes, MediaPipe mouth openness, crop geometry.

This is the only module that touches opencv and mediapipe, and it imports them inside functions,
so the CLI starts fast and unit tests can replace the seams marked SEAM below (and never need the
libraries or the model files).

The model files live in <project>/models/mediapipe/ and <project>/models/yunet/ (git-ignored). The
track command never downloads them: a missing file is an error that names the explicit command,
`fetch-face-model`.
"""

import hashlib
import importlib.metadata
import logging
import math
import os
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

from clipmachine.utils.camera import FaceSample

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

log = logging.getLogger("clipmachine")


@dataclass(frozen=True)
class ModelSpec:
    """One model file the face code needs: where it lives, where it comes from, how to check it."""

    folder: Path  # relative to the project root
    name: str
    url: str
    sha256: str | None  # None: any present file is accepted
    label: str  # for messages, e.g. "face model"


# SHA-256 of the file, taken at the first download Igor approved on 2026-10-11 (3,758,596 bytes;
# Google publishes none). A different file is an error, never a silent re-download.
LANDMARKER = ModelSpec(
    folder=Path("models") / "mediapipe",
    name="face_landmarker.task",
    url=(
        "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/"
        "float16/latest/face_landmarker.task"
    ),
    sha256="64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff",
    label="face model",
)
# YuNet face detector from the OpenCV model zoo (pinned commit). 229,738 bytes, MIT licence. The
# hash is the repository's Git LFS id and matched the bytes fetched on 2026-10-11.
DETECTOR = ModelSpec(
    folder=Path("models") / "yunet",
    name="face_detection_yunet_2026may.onnx",
    url=(
        "https://github.com/opencv/opencv_zoo/raw/26cc381e4d2594bb9f47a26eb8fd96c94a13660d/"
        "models/face_detection_yunet/face_detection_yunet_2026may.onnx"
    ),
    sha256="ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0",
    label="face detector model",
)

DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_CHUNK_BYTES = 1 << 16
MAX_REASON_CHARS = 200
FETCH_COMMAND = "clipmachine fetch-face-model"

# Face Mesh landmark indices (468-point topology; the Face Landmarker model uses the same).
INNER_UPPER_LIP = 13
INNER_LOWER_LIP = 14
FOREHEAD_TOP = 10
CHIN = 152
MIN_LANDMARKS = CHIN + 1
MIN_FACE_HEIGHT = 1e-6  # a face this flat in y has no usable size
MIN_CROP_PIXELS = 64  # the landmarker needs some pixels to work with, even for a tiny face
YUNET_NMS_THRESHOLD = 0.3  # YuNet boxes overlapping more than this are merged
YUNET_TOP_K = 5000  # boxes kept before merging
CROP_LANDMARK_FACES = 2  # a crop may hold a neighbour; the matching step picks the right face
MATCH_FRACTION = 0.5  # a landmarker face is the detector's if its centre is this close (x height)
# Pixel-motion areas, as fractions of the face box round its centre.
MOTION_HALF_WIDTH = 0.30  # both areas span the centre +- this fraction of the box width
MOUTH_TOP = 0.12  # the mouth area runs from centre + this fraction of the box height ...
MOUTH_BOTTOM = 0.50  # ... to centre + this
UPPER_TOP = -0.38  # the upper-face area runs from centre - this fraction of the box height ...
UPPER_BOTTOM = 0.0  # ... to the centre
MIN_MOTION_WIDTH = 8  # pixels; a narrower area is not measured
MIN_MOTION_HEIGHT = 6  # pixels; a shorter area is not measured
MOTION_SIZE = (24, 16)  # (width, height) each area is shrunk to before comparing
MOTION_CONTRAST_FLOOR = 4.0  # added to the picture's contrast so flat areas do not explode


class FacesError(Exception):
    """A face-detection setup problem with a one-line message fit to show the user."""


class Landmark(Protocol):
    """Anything with normalised x and y (mediapipe's NormalizedLandmark; a dataclass in tests)."""

    @property
    def x(self) -> float: ...

    @property
    def y(self) -> float: ...


@dataclass(frozen=True)
class RawFace:
    """One detected face, normalised to the frame: centre, size, and mouth openness divided by the
    face height (None: the face was found but its mouth could not be read). `motion` is the
    movement round the mouth since the previous sample (None: no previous frame, or it could not
    be measured)."""

    cx: float
    cy: float
    w: float
    h: float
    mouth: float | None
    motion: float | None = None


@dataclass(frozen=True)
class FoundFaces:
    """The faces of one frame, and the height of the tallest face the detector saw before small
    ones were dropped (0.0 when it saw none), so "faces too small" is judged from all of them.
    `boxes` are the kept boxes this call used, left to right, so a caller can reuse them."""

    faces: list[RawFace]
    largest_height: float
    boxes: list["FaceBox"] = field(default_factory=list)


@dataclass
class FaceFinder:
    """The open YuNet detector and landmarker, and the settings find_faces needs."""

    detector: Any
    landmarker: Any
    detect_width: int
    crop_scale: float
    min_face_height: float
    max_faces: int

    def close(self) -> None:
        close = getattr(self.landmarker, "close", None)
        if callable(close):
            close()


@dataclass(frozen=True)
class DownloadedModel:
    path: Path
    size_bytes: int
    sha256: str


def _one_line(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:MAX_REASON_CHARS]


# --- pure geometry ------------------------------------------------------------------------


def mouth_openness(landmarks: Sequence[Landmark]) -> float:
    """The gap between the inner lips divided by the face height (forehead top to chin), so it
    does not depend on how close the person is. 0.0 for a face too flat to measure."""
    if len(landmarks) < MIN_LANDMARKS:
        return 0.0
    face_height = abs(landmarks[FOREHEAD_TOP].y - landmarks[CHIN].y)
    if face_height < MIN_FACE_HEIGHT:
        return 0.0
    return abs(landmarks[INNER_UPPER_LIP].y - landmarks[INNER_LOWER_LIP].y) / face_height


def face_box(landmarks: Sequence[Landmark]) -> tuple[float, float, float, float]:
    """(cx, cy, w, h) of the landmarks' bounding box, clamped to 0..1."""
    if not landmarks:
        raise ValueError("a face needs at least one landmark")
    xs = [min(max(p.x, 0.0), 1.0) for p in landmarks]
    ys = [min(max(p.y, 0.0), 1.0) for p in landmarks]
    left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
    return (left + right) / 2, (top + bottom) / 2, right - left, bottom - top


def raw_face(landmarks: Sequence[Landmark]) -> RawFace:
    cx, cy, w, h = face_box(landmarks)
    return RawFace(cx=cx, cy=cy, w=w, h=h, mouth=mouth_openness(landmarks))


def to_samples(faces: Sequence[RawFace], t: float) -> list[FaceSample]:
    """Faces of one frame as FaceSamples at time t, left to right (a stable order)."""
    ordered = sorted(faces, key=lambda f: (f.cx, f.cy))
    return [
        FaceSample(t=t, cx=f.cx, cy=f.cy, w=f.w, h=f.h, mouth=f.mouth, motion=f.motion)
        for f in ordered
    ]


@dataclass(frozen=True)
class FaceBox:
    """A face found by the detector: centre and size as fractions of the frame, and its score."""

    cx: float
    cy: float
    w: float
    h: float
    score: float


def keep_boxes(boxes: Sequence[FaceBox], min_height: float, max_faces: int) -> list[FaceBox]:
    """Drop boxes shorter than min_height, keep the max_faces tallest (ties: leftmost first), and
    return them left to right."""
    big = [b for b in boxes if b.h >= min_height]
    tallest = sorted(big, key=lambda b: (-b.h, b.cx))[: max(max_faces, 0)]
    return sorted(tallest, key=lambda b: b.cx)


Rect = tuple[int, int, int, int]  # (x0, y0, x1, y1) in pixels


def motion_rects(box: FaceBox, frame_w: int, frame_h: int) -> tuple[Rect, Rect] | None:
    """The pixel rectangles (mouth area, upper-face area) to compare between two frames, clamped to
    the frame. None when either is narrower than MIN_MOTION_WIDTH or shorter than
    MIN_MOTION_HEIGHT pixels."""

    def rect(top: float, bottom: float) -> Rect:
        xa = int((box.cx - MOTION_HALF_WIDTH * box.w) * frame_w)
        xb = int((box.cx + MOTION_HALF_WIDTH * box.w) * frame_w)
        ya = int((box.cy + top * box.h) * frame_h)
        yb = int((box.cy + bottom * box.h) * frame_h)
        return (
            min(max(xa, 0), frame_w),
            min(max(ya, 0), frame_h),
            min(max(xb, 0), frame_w),
            min(max(yb, 0), frame_h),
        )

    mouth = rect(MOUTH_TOP, MOUTH_BOTTOM)
    upper = rect(UPPER_TOP, UPPER_BOTTOM)
    for x0, y0, x1, y1 in (mouth, upper):
        if x1 - x0 < MIN_MOTION_WIDTH or y1 - y0 < MIN_MOTION_HEIGHT:
            return None
    return mouth, upper


def crop_square(box: FaceBox, frame_w: int, frame_h: int, scale: float) -> tuple[int, int, int]:
    """(x0, y0, side) in pixels of the square to read this face's landmarks from.

    The side is `scale` times the box's longer side, at least MIN_CROP_PIXELS and at most the
    frame's shorter side. The square is centred on the box and shifted to stay inside the frame.
    """
    wanted = scale * max(box.w * frame_w, box.h * frame_h)
    side = int(round(min(max(wanted, MIN_CROP_PIXELS), min(frame_w, frame_h))))
    x0 = int(round(box.cx * frame_w - side / 2))
    y0 = int(round(box.cy * frame_h - side / 2))
    return min(max(x0, 0), frame_w - side), min(max(y0, 0), frame_h - side), side


def to_frame(raw: RawFace, x0: int, y0: int, side: int, frame_w: int, frame_h: int) -> RawFace:
    """A face measured as fractions of a crop, as fractions of the whole frame (mouth unchanged)."""
    return RawFace(
        cx=(x0 + raw.cx * side) / frame_w,
        cy=(y0 + raw.cy * side) / frame_h,
        w=raw.w * side / frame_w,
        h=raw.h * side / frame_h,
        mouth=raw.mouth,
    )


def match_mouth(box: FaceBox, found: Sequence[RawFace], frame_w: int, frame_h: int) -> float | None:
    """The mouth of the landmarker face (in full-frame fractions) that is this box's face.

    That is the one whose centre is nearest the box centre, if it is within half the larger of the
    two face heights (distance in units of frame height). Otherwise None, so a neighbour's face
    that happens to be inside the crop is never used.
    """
    aspect = frame_w / frame_h
    best: RawFace | None = None
    best_distance = 0.0
    for face in found:
        distance = math.hypot((face.cx - box.cx) * aspect, face.cy - box.cy)
        if best is None or distance < best_distance:
            best, best_distance = face, distance
    if best is None or best_distance > MATCH_FRACTION * max(box.h, best.h):
        return None
    return best.mouth


# --- the model file -----------------------------------------------------------------------


def model_dir(root: Path, spec: ModelSpec | None = None) -> Path:
    """The folder that holds the model file (default: the landmarker's)."""
    return root / (LANDMARKER if spec is None else spec).folder


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_model(
    directory: Path, expected_sha256: str | None = None, *, spec: ModelSpec | None = None
) -> Path:
    """The model file's path, checked.

    `directory` is the folder that holds the file (model_dir(root, spec)). A missing file is a
    FacesError naming the command that fetches it; this function never downloads. When a hash is
    expected (default: the spec's) a file with another hash is a FacesError too. `spec` defaults
    to the landmarker.
    """
    spec = LANDMARKER if spec is None else spec
    expected = spec.sha256 if expected_sha256 is None else expected_sha256
    path = directory / spec.name
    if not path.is_file():
        raise FacesError(f"{spec.label} not found at {path}; run: {FETCH_COMMAND}")
    if expected is not None:
        try:
            actual = file_sha256(path)
        except OSError as exc:
            raise FacesError(
                f"could not read the {spec.label} at {path}: {_one_line(exc)}"
            ) from exc
        if actual != expected:
            raise FacesError(
                f"{spec.label} at {path} has SHA-256 {actual}, expected {expected}; the file "
                "changed or is damaged. Delete it and run the fetch command again, or update "
                "the pinned hash in utils/faces.py after checking the new file"
            )
    return path


def download_model(
    directory: Path,
    *,
    spec: ModelSpec | None = None,
    url: str | None = None,
    expected_sha256: str | None = None,
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
) -> DownloadedModel:
    """Download a model into `directory`; only the explicit fetch-face-model command calls this.

    `spec` defaults to the landmarker; `url` and `expected_sha256` default to the spec's. The file
    is written to a temp name and moved into place once complete. An existing file is never
    replaced. Returns the size and SHA-256 so the caller can show them.
    """
    spec = LANDMARKER if spec is None else spec
    expected = spec.sha256 if expected_sha256 is None else expected_sha256
    url = spec.url if url is None else url
    if urlparse(url).scheme != "https":
        raise FacesError(f"the {spec.label} URL must be https")
    target = directory / spec.name
    if target.exists():
        raise FacesError(f"{target} already exists; delete it first to download it again")
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / f".{spec.name}.tmp"
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response, temp.open("wb") as out:  # noqa: S310
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
        sha256 = digest.hexdigest()
        if size == 0:
            raise FacesError(f"the {spec.label} download was empty")
        if expected is not None and sha256 != expected:
            raise FacesError(
                f"downloaded {spec.label} has SHA-256 {sha256}, expected {expected}; not saved"
            )
        os.replace(temp, target)
    except FacesError:
        raise
    except (OSError, ValueError) as exc:  # URLError and timeouts are OSErrors
        raise FacesError(f"could not download the {spec.label}: {_one_line(exc)}") from exc
    finally:
        temp.unlink(missing_ok=True)
    return DownloadedModel(path=target, size_bytes=size, sha256=sha256)


def mediapipe_version() -> str | None:
    """The installed mediapipe version, read from package metadata (nothing is imported)."""
    try:
        return importlib.metadata.version("mediapipe")
    except importlib.metadata.PackageNotFoundError:
        return None


def opencv_version() -> str | None:
    """The installed OpenCV version, read from package metadata (nothing is imported)."""
    try:
        return importlib.metadata.version("opencv-contrib-python")
    except importlib.metadata.PackageNotFoundError:
        return None


# --- the libraries, imported on first use ---------------------------------------------------


def _mediapipe() -> Any:
    try:
        import mediapipe as mp
    except Exception as exc:  # ImportError, or a missing system library
        raise FacesError(
            f"mediapipe could not be imported ({_one_line(exc)}); run: uv sync --locked"
        ) from exc
    return mp


# --- SEAMS for the finder: YuNet finds faces, the landmarker reads mouths on crops ---------------


def _cv2() -> Any:
    try:
        import cv2
    except Exception as exc:  # ImportError, or a missing system library
        raise FacesError(
            f"opencv could not be imported ({_one_line(exc)}); run: uv sync --locked"
        ) from exc
    return cv2


def open_finder(  # SEAM
    detector_path: Path,
    landmarker_path: Path,
    *,
    detect_width: int,
    detect_score: float,
    crop_scale: float,
    min_face_height: float,
    max_faces: int,
) -> FaceFinder:
    """Open the YuNet detector and an IMAGE-mode Face Landmarker. Close with close_finder."""
    import numpy as np

    cv2 = _cv2()
    mp = _mediapipe()
    try:
        data = detector_path.read_bytes()
    except OSError as exc:
        raise FacesError(
            f"could not read the {DETECTOR.label} at {detector_path}: {_one_line(exc)}"
        ) from exc
    if DETECTOR.sha256 is not None:
        actual = hashlib.sha256(data).hexdigest()
        if actual != DETECTOR.sha256:
            raise FacesError(
                f"{DETECTOR.label} at {detector_path} has SHA-256 {actual}, expected "
                f"{DETECTOR.sha256}; the file changed or is damaged"
            )
    try:
        detector = cv2.FaceDetectorYN.create(
            "onnx",
            np.frombuffer(data, np.uint8),
            np.empty(0, np.uint8),
            (detect_width, detect_width),  # replaced per frame by setInputSize
            detect_score,
            YUNET_NMS_THRESHOLD,
            YUNET_TOP_K,
        )
    except Exception as exc:
        raise FacesError(f"could not start the face detector: {_one_line(exc)}") from exc
    try:
        vision = mp.tasks.vision
        options = vision.FaceLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(landmarker_path)),
            running_mode=vision.RunningMode.IMAGE,
            num_faces=CROP_LANDMARK_FACES,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        landmarker = vision.FaceLandmarker.create_from_options(options)
    except Exception as exc:
        raise FacesError(f"could not start the face landmarker: {_one_line(exc)}") from exc
    return FaceFinder(
        detector=detector,
        landmarker=landmarker,
        detect_width=detect_width,
        crop_scale=crop_scale,
        min_face_height=min_face_height,
        max_faces=max_faces,
    )


def _detect_boxes(finder: FaceFinder, frame_rgb: "NDArray[np.uint8]") -> list[FaceBox]:
    """Every face YuNet finds in the frame, as fractions of the frame (cv2 is touched here)."""
    import numpy as np

    cv2 = _cv2()
    height, width = frame_rgb.shape[:2]
    small_w = min(finder.detect_width, width)
    small_h = max(2, int(round(small_w * height / width / 2)) * 2)  # even, like ffmpeg's -2
    try:
        small = cv2.resize(frame_rgb, (small_w, small_h), interpolation=cv2.INTER_AREA)
        bgr = np.ascontiguousarray(small[..., ::-1])  # the detector wants BGR
        finder.detector.setInputSize((small_w, small_h))
        _, rows = finder.detector.detect(bgr)
    except Exception as exc:
        raise FacesError(f"face detection failed: {_one_line(exc)}") from exc
    boxes: list[FaceBox] = []
    for row in [] if rows is None else rows:
        left, top = max(float(row[0]), 0.0), max(float(row[1]), 0.0)
        right = min(float(row[0]) + float(row[2]), float(small_w))
        bottom = min(float(row[1]) + float(row[3]), float(small_h))
        if right <= left or bottom <= top:
            continue
        boxes.append(
            FaceBox(
                cx=(left + right) / 2 / small_w,
                cy=(top + bottom) / 2 / small_h,
                w=(right - left) / small_w,
                h=(bottom - top) / small_h,
                score=float(row[14]),
            )
        )
    return boxes


def _crop_landmarks(finder: FaceFinder, crop_rgb: "NDArray[np.uint8]") -> list[RawFace]:
    """Faces the landmarker finds in a crop, as fractions of the crop (mediapipe is touched
    here)."""
    mp = _mediapipe()
    try:
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=crop_rgb)
        result = finder.landmarker.detect(image)
    except Exception as exc:
        raise FacesError(f"face landmarks failed: {_one_line(exc)}") from exc
    return [raw_face(landmarks) for landmarks in result.face_landmarks]


def face_motion(
    gray: "NDArray[np.uint8]", previous_gray: "NDArray[np.uint8]", box: FaceBox
) -> float | None:
    """How much the picture round the face's mouth changed between two grey frames, less how much
    the upper face changed (so a moving head cancels out). None when the areas are too small or
    the frames differ in shape.

    Each area is cut from both frames, shrunk to MOTION_SIZE, and scored as the mean absolute
    difference divided by (the contrast of the newer one + MOTION_CONTRAST_FLOOR).
    """
    import numpy as np

    if gray.shape != previous_gray.shape:
        return None
    height, width = gray.shape[:2]
    rects = motion_rects(box, width, height)
    if rects is None:
        return None
    cv2 = _cv2()

    def score(rect: Rect) -> float:
        x0, y0, x1, y1 = rect
        now = cv2.resize(gray[y0:y1, x0:x1], MOTION_SIZE, interpolation=cv2.INTER_AREA)
        before = cv2.resize(previous_gray[y0:y1, x0:x1], MOTION_SIZE, interpolation=cv2.INTER_AREA)
        now = now.astype(np.float32)
        before = before.astype(np.float32)
        return float(np.mean(np.abs(now - before)) / (np.std(now) + MOTION_CONTRAST_FLOOR))

    mouth, upper = rects
    return max(0.0, score(mouth) - score(upper))


def find_faces(
    finder: FaceFinder,
    frame_rgb: "NDArray[np.uint8]",
    previous_rgb: "NDArray[np.uint8] | None" = None,
    boxes: list[FaceBox] | None = None,
) -> FoundFaces:
    """The faces in one RGB frame: one RawFace per kept YuNet box, with the box's own position and
    size and the mouth read from a crop around it (None if the landmarker found no matching face).

    With `previous_rgb`, each face also gets its `motion` since that frame. With `boxes` (the
    `boxes` of an earlier result) the detector is not run and those boxes are used as the kept
    ones.
    """
    import numpy as np

    height, width = frame_rgb.shape[:2]
    if boxes is None:
        detected = _detect_boxes(finder, frame_rgb)
        largest = max((box.h for box in detected), default=0.0)
        kept = keep_boxes(detected, finder.min_face_height, finder.max_faces)
    else:
        kept = list(boxes)
        largest = max((box.h for box in kept), default=0.0)
    grays: tuple[NDArray[np.uint8], NDArray[np.uint8]] | None = None
    if previous_rgb is not None:
        cv2 = _cv2()
        grays = (
            cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY),
            cv2.cvtColor(previous_rgb, cv2.COLOR_RGB2GRAY),
        )
    out: list[RawFace] = []
    for box in kept:
        x0, y0, side = crop_square(box, width, height, finder.crop_scale)
        crop = np.ascontiguousarray(frame_rgb[y0 : y0 + side, x0 : x0 + side])
        found = [
            to_frame(raw, x0, y0, side, width, height) for raw in _crop_landmarks(finder, crop)
        ]
        mouth = match_mouth(box, found, width, height)
        motion = None if grays is None else face_motion(grays[0], grays[1], box)
        out.append(RawFace(cx=box.cx, cy=box.cy, w=box.w, h=box.h, mouth=mouth, motion=motion))
    return FoundFaces(faces=out, largest_height=largest, boxes=kept)


def close_finder(finder: FaceFinder) -> None:  # SEAM
    finder.close()
