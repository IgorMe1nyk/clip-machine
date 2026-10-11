"""Face geometry, the model file checks and the download, with no mediapipe and no network.
The one `mediapipe` test runs the real landmarker on a generated blank image."""

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import numpy as np
import pytest

from clipmachine import cli
from clipmachine.utils import faces
from clipmachine.utils.faces import (
    FacesError,
    RawFace,
    download_model,
    ensure_model,
    face_box,
    mouth_openness,
    to_samples,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Pt:
    x: float
    y: float


def mesh(
    *, top: float = 0.2, chin: float = 0.8, upper_lip: float = 0.6, lower_lip: float = 0.66
) -> list[Pt]:
    """468 points in a small cluster, with the four landmarks that matter placed on purpose."""
    points = [Pt(0.5, 0.5) for _ in range(468)]
    points[10] = Pt(0.5, top)
    points[152] = Pt(0.5, chin)
    points[13] = Pt(0.5, upper_lip)
    points[14] = Pt(0.5, lower_lip)
    return points


# --- geometry -----------------------------------------------------------------------------


def test_mouth_openness_is_the_lip_gap_over_the_face_height() -> None:
    assert mouth_openness(mesh()) == pytest.approx(0.06 / 0.6)


def test_mouth_openness_does_not_depend_on_face_size() -> None:
    near = mesh(top=0.1, chin=0.9, upper_lip=0.55, lower_lip=0.65)  # gap 0.1 of height 0.8
    far = mesh(top=0.4, chin=0.5, upper_lip=0.4875, lower_lip=0.5)  # gap 0.0125 of height 0.1
    assert mouth_openness(near) == pytest.approx(mouth_openness(far))


def test_closed_mouth_is_zero() -> None:
    assert mouth_openness(mesh(upper_lip=0.6, lower_lip=0.6)) == 0.0


def test_a_zero_height_face_gives_zero_not_an_error() -> None:
    assert mouth_openness(mesh(top=0.5, chin=0.5)) == 0.0


def test_too_few_landmarks_give_zero() -> None:
    assert mouth_openness([Pt(0.5, 0.5)] * 10) == 0.0


def test_face_box_is_the_bounding_box() -> None:
    points = [Pt(0.2, 0.1), Pt(0.6, 0.5), Pt(0.4, 0.9)]
    cx, cy, w, h = face_box(points)
    assert (cx, cy) == pytest.approx((0.4, 0.5))
    assert (w, h) == pytest.approx((0.4, 0.8))


def test_face_box_clamps_points_outside_the_frame() -> None:
    cx, _, w, _ = face_box([Pt(-0.2, 0.5), Pt(0.4, 0.5)])
    assert (cx, w) == pytest.approx((0.2, 0.4))


def test_face_box_of_nothing_is_an_error() -> None:
    with pytest.raises(ValueError):
        face_box([])


def test_to_samples_orders_faces_left_to_right() -> None:
    raw = [RawFace(0.8, 0.5, 0.1, 0.2, 0.1), RawFace(0.2, 0.5, 0.1, 0.2, 0.3)]
    samples = to_samples(raw, 1.5)
    assert [s.cx for s in samples] == [0.2, 0.8]
    assert all(s.t == 1.5 for s in samples)
    assert samples[0].mouth == 0.3


# --- the model file -----------------------------------------------------------------------


def test_missing_model_is_an_error_that_names_the_fetch_command(tmp_path: Path) -> None:
    with pytest.raises(FacesError, match="not found at .*fetch-face-model"):
        ensure_model(tmp_path)
    assert list(tmp_path.iterdir()) == []  # nothing was downloaded or created


def test_model_with_the_wrong_hash_is_an_error(tmp_path: Path) -> None:
    (tmp_path / faces.MODEL_NAME).write_bytes(b"tiny fake model")
    with pytest.raises(FacesError, match="SHA-256"):
        ensure_model(tmp_path, expected_sha256="0" * 64)
    assert (tmp_path / faces.MODEL_NAME).read_bytes() == b"tiny fake model"  # never replaced


def test_model_with_the_right_hash_is_returned(tmp_path: Path) -> None:
    data = b"tiny fake model"
    (tmp_path / faces.MODEL_NAME).write_bytes(data)
    path = ensure_model(tmp_path, expected_sha256=hashlib.sha256(data).hexdigest())
    assert path == tmp_path / faces.MODEL_NAME


def test_without_a_pinned_hash_any_present_file_is_accepted(tmp_path: Path) -> None:
    (tmp_path / faces.MODEL_NAME).write_bytes(b"x")
    assert ensure_model(tmp_path) == tmp_path / faces.MODEL_NAME


def test_model_dir_is_inside_the_project() -> None:
    assert faces.model_dir(Path("/p")) == Path("/p") / "models" / "mediapipe"


# --- download (urllib faked) --------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace urlopen; the list records the URLs asked for."""
    asked: list[str] = []

    def urlopen(url: str, timeout: float) -> FakeResponse:
        asked.append(url)
        assert timeout > 0
        return FakeResponse(b"model bytes" * 1000)

    monkeypatch.setattr(faces.urllib.request, "urlopen", urlopen)
    return asked


def test_download_writes_the_file_and_returns_size_and_hash(
    tmp_path: Path, served: list[str]
) -> None:
    target_dir = tmp_path / "models" / "mediapipe"
    result = download_model(target_dir)

    data = b"model bytes" * 1000
    assert served == [faces.MODEL_URL]
    assert result.path == target_dir / faces.MODEL_NAME
    assert result.path.read_bytes() == data
    assert result.size_bytes == len(data)
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert [p.name for p in target_dir.iterdir()] == [faces.MODEL_NAME]  # no temp file left


def test_download_never_replaces_an_existing_file(tmp_path: Path, served: list[str]) -> None:
    (tmp_path / faces.MODEL_NAME).write_bytes(b"old")
    with pytest.raises(FacesError, match="already exists"):
        download_model(tmp_path)
    assert served == []
    assert (tmp_path / faces.MODEL_NAME).read_bytes() == b"old"


def test_download_with_a_hash_mismatch_saves_nothing(tmp_path: Path, served: list[str]) -> None:
    with pytest.raises(FacesError, match="not saved"):
        download_model(tmp_path, expected_sha256="0" * 64)
    assert list(tmp_path.iterdir()) == []


def test_download_failure_is_one_line_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(url: str, timeout: float) -> FakeResponse:
        raise OSError("connection refused")

    monkeypatch.setattr(faces.urllib.request, "urlopen", broken)
    with pytest.raises(FacesError, match="could not download.*connection refused"):
        download_model(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_refuses_a_non_https_url(tmp_path: Path) -> None:
    with pytest.raises(FacesError, match="https"):
        download_model(tmp_path, url="http://example.invalid/model.task")


def test_the_fetch_command_prints_path_size_and_hash(
    tmp_path: Path, served: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["fetch-face-model"], root=tmp_path) == 0

    out = capsys.readouterr().out
    data = b"model bytes" * 1000
    assert str(tmp_path / "models" / "mediapipe" / faces.MODEL_NAME) in out
    assert f"size: {len(data)} bytes" in out
    assert f"sha256: {hashlib.sha256(data).hexdigest()}" in out


def test_the_fetch_command_reports_a_failure_as_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "models" / "mediapipe").mkdir(parents=True)
    (tmp_path / "models" / "mediapipe" / faces.MODEL_NAME).write_bytes(b"x")
    assert cli.main(["fetch-face-model"], root=tmp_path) == 2
    assert "already exists" in capsys.readouterr().err


def test_mediapipe_version_reads_metadata_without_importing() -> None:
    import sys

    before = "mediapipe" in sys.modules
    version = faces.mediapipe_version()
    assert version is None or version[0].isdigit()
    assert ("mediapipe" in sys.modules) == before


# --- the real landmarker (opt in) ---------------------------------------------------------


@pytest.mark.mediapipe
def test_real_landmarker_finds_no_face_in_a_blank_image() -> None:
    model = REPO_ROOT / faces.MODEL_DIR / faces.MODEL_NAME
    if not model.is_file():
        pytest.skip(f"face model not present at {faces.MODEL_DIR}; run: {faces.FETCH_COMMAND}")
    path = ensure_model(model.parent)
    handle = faces.open_landmarker(path, 2)
    try:
        blank = np.zeros((360, 640, 3), dtype=np.uint8)
        assert faces.detect(handle, blank, 0) == []
        assert faces.detect(handle, blank, 0) == []  # same timestamp: bumped, not an error
    finally:
        faces.close_landmarker(handle)
