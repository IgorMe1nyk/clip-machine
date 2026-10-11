"""MediaPipe Face Landmarker wrapper: model file check, detection, mouth openness, face boxes.

This is the only module that touches mediapipe, and it imports it inside functions, so the CLI
starts fast and unit tests can replace the three seams marked SEAM below (and never need the
library or the model file).

The model file lives in <project>/models/mediapipe/ (git-ignored). The track command never
downloads it: a missing file is an error that names the explicit command, `fetch-face-model`.
"""

import hashlib
import importlib.metadata
import logging
import os
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

from clipmachine.utils.camera import FaceSample

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

log = logging.getLogger("clipmachine")

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/"
    "float16/latest/face_landmarker.task"
)
MODEL_DIR = Path("models") / "mediapipe"  # relative to the project root
MODEL_NAME = "face_landmarker.task"
# SHA-256 of the model file, taken at the first download Igor approved on 2026-10-11 (3,758,596
# bytes; Google publishes none). A different file is an error, never a silent re-download.
EXPECTED_SHA256: str | None = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"

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
    face height."""

    cx: float
    cy: float
    w: float
    h: float
    mouth: float


@dataclass
class LandmarkerHandle:
    """An open landmarker and the last timestamp given to it (VIDEO mode needs strictly
    increasing ones)."""

    landmarker: Any
    last_ms: int = -1

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
    return [FaceSample(t=t, cx=f.cx, cy=f.cy, w=f.w, h=f.h, mouth=f.mouth) for f in ordered]


# --- the model file -----------------------------------------------------------------------


def model_dir(root: Path) -> Path:
    return root / MODEL_DIR


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_model(directory: Path, expected_sha256: str | None = None) -> Path:
    """The model file's path, checked.

    `directory` is the folder that holds the file (model_dir(root)). A missing file is a
    FacesError naming the command that fetches it; this function never downloads. When a hash is
    expected (default: EXPECTED_SHA256) a file with another hash is a FacesError too.
    """
    expected = EXPECTED_SHA256 if expected_sha256 is None else expected_sha256
    path = directory / MODEL_NAME
    if not path.is_file():
        raise FacesError(f"face model not found at {path}; run: {FETCH_COMMAND}")
    if expected is not None:
        try:
            actual = file_sha256(path)
        except OSError as exc:
            raise FacesError(f"could not read the face model at {path}: {_one_line(exc)}") from exc
        if actual != expected:
            raise FacesError(
                f"face model at {path} has SHA-256 {actual}, expected {expected}; the file "
                "changed or is damaged. Delete it and run the fetch command again, or update "
                "EXPECTED_SHA256 after checking the new file"
            )
    return path


def download_model(
    directory: Path,
    *,
    url: str = MODEL_URL,
    expected_sha256: str | None = None,
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
) -> DownloadedModel:
    """Download the model into `directory`; only the explicit fetch-face-model command calls this.

    The file is written to a temp name and moved into place once complete. An existing file is
    never replaced. Returns the size and SHA-256 so the caller can show them.
    """
    expected = EXPECTED_SHA256 if expected_sha256 is None else expected_sha256
    if urlparse(url).scheme != "https":
        raise FacesError("the face model URL must be https")
    target = directory / MODEL_NAME
    if target.exists():
        raise FacesError(f"{target} already exists; delete it first to download it again")
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / f".{MODEL_NAME}.tmp"
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
            raise FacesError("the face model download was empty")
        if expected is not None and sha256 != expected:
            raise FacesError(
                f"downloaded face model has SHA-256 {sha256}, expected {expected}; not saved"
            )
        os.replace(temp, target)
    except FacesError:
        raise
    except (OSError, ValueError) as exc:  # URLError and timeouts are OSErrors
        raise FacesError(f"could not download the face model: {_one_line(exc)}") from exc
    finally:
        temp.unlink(missing_ok=True)
    return DownloadedModel(path=target, size_bytes=size, sha256=sha256)


def mediapipe_version() -> str | None:
    """The installed mediapipe version, read from package metadata (nothing is imported)."""
    try:
        return importlib.metadata.version("mediapipe")
    except importlib.metadata.PackageNotFoundError:
        return None


# --- SEAMS: the only places that touch mediapipe; unit tests replace these ----------------------


def _mediapipe() -> Any:
    try:
        import mediapipe as mp
    except Exception as exc:  # ImportError, or a missing system library
        raise FacesError(
            f"mediapipe could not be imported ({_one_line(exc)}); run: uv sync --locked"
        ) from exc
    return mp


def open_landmarker(model_path: Path, max_faces: int) -> Any:  # SEAM
    """A Face Landmarker in VIDEO mode, wrapped in a LandmarkerHandle. Close it with .close()."""
    mp = _mediapipe()
    try:
        vision = mp.tasks.vision
        options = vision.FaceLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=max_faces,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        return LandmarkerHandle(vision.FaceLandmarker.create_from_options(options))
    except Exception as exc:
        raise FacesError(f"could not start the face landmarker: {_one_line(exc)}") from exc


def detect(handle: Any, frame_rgb: "NDArray[np.uint8]", t_ms: int) -> list[RawFace]:  # SEAM
    """Faces in one RGB frame. Timestamps are made strictly increasing, as VIDEO mode requires."""
    import numpy as np

    mp = _mediapipe()
    timestamp = max(t_ms, handle.last_ms + 1)
    handle.last_ms = timestamp
    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(frame_rgb))
    try:
        result = handle.landmarker.detect_for_video(image, timestamp)
    except Exception as exc:
        raise FacesError(f"face detection failed: {_one_line(exc)}") from exc
    return [raw_face(landmarks) for landmarks in result.face_landmarks]


def close_landmarker(handle: Any) -> None:  # SEAM
    handle.close()
