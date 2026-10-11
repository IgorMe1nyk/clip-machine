"""Face geometry, the model file checks and the download, with no mediapipe and no network.
The one `mediapipe` test runs the real landmarker on a generated blank image."""

import hashlib
import io
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import TracebackType
from typing import Any

import numpy as np
import pytest

from clipmachine import cli
from clipmachine.utils import faces
from clipmachine.utils.faces import (
    FaceBox,
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


def box(cx: float, cy: float, w: float, h: float) -> FaceBox:
    return FaceBox(cx=cx, cy=cy, w=w, h=h, score=0.9)


def test_keep_boxes_drops_small_ones_and_returns_left_to_right() -> None:
    boxes = [box(0.8, 0.5, 0.04, 0.08), box(0.2, 0.5, 0.01, 0.02), box(0.5, 0.5, 0.05, 0.1)]
    kept = faces.keep_boxes(boxes, min_height=0.05, max_faces=4)
    assert [b.cx for b in kept] == [0.5, 0.8]


def test_keep_boxes_keeps_the_tallest_when_there_are_too_many() -> None:
    boxes = [box(0.1, 0.5, 0.1, 0.2), box(0.5, 0.5, 0.2, 0.4), box(0.9, 0.5, 0.15, 0.3)]
    kept = faces.keep_boxes(boxes, min_height=0.0, max_faces=2)
    assert [b.cx for b in kept] == [0.5, 0.9]


def test_keep_boxes_breaks_height_ties_by_keeping_the_leftmost() -> None:
    boxes = [box(0.9, 0.5, 0.1, 0.2), box(0.1, 0.5, 0.1, 0.2), box(0.5, 0.5, 0.1, 0.2)]
    kept = faces.keep_boxes(boxes, min_height=0.0, max_faces=2)
    assert [b.cx for b in kept] == [0.1, 0.5]


def test_keep_boxes_of_nothing_is_nothing() -> None:
    assert faces.keep_boxes([], min_height=0.05, max_faces=2) == []


def test_crop_square_is_centred_on_the_face_and_scaled() -> None:
    # a face 100 px wide and 120 px tall in a 1000 x 800 frame, crop 2.5x the longer side
    x0, y0, side = faces.crop_square(box(0.5, 0.5, 0.1, 0.15), 1000, 800, 2.5)
    assert side == 300
    assert (x0, y0) == (350, 250)


@pytest.mark.parametrize(
    ("cx", "cy", "expected"),
    [
        (0.01, 0.5, (0, 250)),  # left edge
        (0.99, 0.5, (700, 250)),  # right edge
        (0.5, 0.01, (350, 0)),  # top edge
        (0.5, 0.99, (350, 500)),  # bottom edge
    ],
)
def test_crop_square_stays_inside_the_frame(
    cx: float, cy: float, expected: tuple[int, int]
) -> None:
    x0, y0, side = faces.crop_square(box(cx, cy, 0.1, 0.15), 1000, 800, 2.5)
    assert side == 300
    assert (x0, y0) == expected


def test_crop_square_is_never_larger_than_the_frame() -> None:
    x0, y0, side = faces.crop_square(box(0.5, 0.5, 0.6, 0.9), 1000, 800, 2.5)
    assert (x0, y0, side) == (100, 0, 800)


def test_crop_square_is_at_least_64_pixels() -> None:
    x0, y0, side = faces.crop_square(box(0.5, 0.5, 0.005, 0.01), 1000, 800, 2.5)
    assert side == 64
    assert (x0, y0) == (468, 368)


def test_to_frame_maps_a_crop_face_back_to_the_frame() -> None:
    raw = RawFace(cx=0.5, cy=0.25, w=0.4, h=0.5, mouth=0.07)
    mapped = faces.to_frame(raw, x0=200, y0=100, side=400, frame_w=1000, frame_h=800)
    assert (mapped.cx, mapped.cy) == pytest.approx((0.4, 0.25))
    assert (mapped.w, mapped.h) == pytest.approx((0.16, 0.25))
    assert mapped.mouth == 0.07


def test_match_mouth_takes_the_face_nearest_the_box() -> None:
    target = box(0.5, 0.5, 0.05, 0.1)
    found = [RawFace(0.58, 0.5, 0.05, 0.1, 0.9), RawFace(0.505, 0.5, 0.05, 0.1, 0.3)]
    assert faces.match_mouth(target, found, 1000, 1000) == 0.3


def test_match_mouth_is_none_when_the_only_face_is_far_away() -> None:
    target = box(0.5, 0.5, 0.05, 0.1)
    assert faces.match_mouth(target, [RawFace(0.7, 0.5, 0.05, 0.1, 0.3)], 1000, 1000) is None


def test_match_mouth_is_none_when_nothing_was_found() -> None:
    assert faces.match_mouth(box(0.5, 0.5, 0.05, 0.1), [], 1000, 1000) is None


def test_match_mouth_measures_horizontal_distance_in_frame_heights() -> None:
    # 0.04 apart in x is 0.071 frame heights in a 1600 x 900 frame: outside half of 0.1
    target = box(0.5, 0.5, 0.05, 0.1)
    far = [RawFace(0.54, 0.5, 0.05, 0.1, 0.3)]
    assert faces.match_mouth(target, far, 1600, 900) is None
    assert faces.match_mouth(target, far, 900, 900) == 0.3  # square frame: 0.04 <= half of 0.1


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
    (tmp_path / faces.LANDMARKER.name).write_bytes(b"tiny fake model")
    with pytest.raises(FacesError, match="SHA-256"):
        ensure_model(tmp_path, expected_sha256="0" * 64)
    assert (tmp_path / faces.LANDMARKER.name).read_bytes() == b"tiny fake model"  # never replaced


def test_model_with_the_right_hash_is_returned(tmp_path: Path) -> None:
    data = b"tiny fake model"
    (tmp_path / faces.LANDMARKER.name).write_bytes(data)
    path = ensure_model(tmp_path, expected_sha256=hashlib.sha256(data).hexdigest())
    assert path == tmp_path / faces.LANDMARKER.name


def test_without_a_pinned_hash_any_present_file_is_accepted(tmp_path: Path) -> None:
    (tmp_path / faces.LANDMARKER.name).write_bytes(b"x")
    assert ensure_model(tmp_path) == tmp_path / faces.LANDMARKER.name


def test_model_dir_is_inside_the_project() -> None:
    assert faces.model_dir(Path("/p")) == Path("/p") / "models" / "mediapipe"
    assert faces.model_dir(Path("/p"), faces.DETECTOR) == Path("/p") / "models" / "yunet"


def test_the_detector_spec_pins_the_zoo_file() -> None:
    assert faces.DETECTOR.name == "face_detection_yunet_2026may.onnx"
    assert faces.DETECTOR.url.startswith("https://")
    assert "26cc381e4d2594bb9f47a26eb8fd96c94a13660d" in faces.DETECTOR.url


def test_missing_detector_is_an_error_that_names_the_fetch_command(tmp_path: Path) -> None:
    with pytest.raises(FacesError, match="detector model not found at .*fetch-face-model"):
        ensure_model(tmp_path, spec=faces.DETECTOR)
    assert list(tmp_path.iterdir()) == []


def test_detector_with_the_wrong_hash_is_an_error(tmp_path: Path) -> None:
    (tmp_path / faces.DETECTOR.name).write_bytes(b"tiny fake detector")
    with pytest.raises(FacesError, match="SHA-256"):
        ensure_model(tmp_path, expected_sha256="0" * 64, spec=faces.DETECTOR)
    assert (tmp_path / faces.DETECTOR.name).read_bytes() == b"tiny fake detector"


def test_detector_with_the_right_hash_is_returned(tmp_path: Path) -> None:
    data = b"tiny fake detector"
    (tmp_path / faces.DETECTOR.name).write_bytes(data)
    path = ensure_model(
        tmp_path, expected_sha256=hashlib.sha256(data).hexdigest(), spec=faces.DETECTOR
    )
    assert path == tmp_path / faces.DETECTOR.name


def test_a_spec_with_a_pinned_hash_is_checked_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(faces, "DETECTOR", replace(faces.DETECTOR, sha256="0" * 64))
    (tmp_path / faces.DETECTOR.name).write_bytes(b"x")
    with pytest.raises(FacesError, match="SHA-256"):
        ensure_model(tmp_path, spec=faces.DETECTOR)


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
    assert served == [faces.LANDMARKER.url]
    assert result.path == target_dir / faces.LANDMARKER.name
    assert result.path.read_bytes() == data
    assert result.size_bytes == len(data)
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert [p.name for p in target_dir.iterdir()] == [faces.LANDMARKER.name]  # no temp file left


def test_download_never_replaces_an_existing_file(tmp_path: Path, served: list[str]) -> None:
    (tmp_path / faces.LANDMARKER.name).write_bytes(b"old")
    with pytest.raises(FacesError, match="already exists"):
        download_model(tmp_path)
    assert served == []
    assert (tmp_path / faces.LANDMARKER.name).read_bytes() == b"old"


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


def test_download_of_the_detector_uses_its_own_url_and_file_name(
    tmp_path: Path, served: list[str]
) -> None:
    result = download_model(tmp_path, spec=faces.DETECTOR)
    assert served == [faces.DETECTOR.url]
    assert result.path == tmp_path / faces.DETECTOR.name


def test_the_fetch_command_prints_path_size_and_hash_for_each_model(
    tmp_path: Path, served: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["fetch-face-model"], root=tmp_path) == 0

    out = capsys.readouterr().out
    data = b"model bytes" * 1000
    assert served == [faces.LANDMARKER.url, faces.DETECTOR.url]
    assert str(tmp_path / "models" / "mediapipe" / faces.LANDMARKER.name) in out
    assert str(tmp_path / "models" / "yunet" / faces.DETECTOR.name) in out
    assert out.count(f"size: {len(data)} bytes") == 2
    assert out.count(f"sha256: {hashlib.sha256(data).hexdigest()}") == 2


def test_the_fetch_command_fetches_only_the_missing_model_and_reports_the_other(
    tmp_path: Path, served: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "models" / "mediapipe"
    folder.mkdir(parents=True)
    (folder / faces.LANDMARKER.name).write_bytes(b"x")

    assert cli.main(["fetch-face-model"], root=tmp_path) == 0

    out = capsys.readouterr().out
    assert served == [faces.DETECTOR.url]
    assert f"already present: {folder / faces.LANDMARKER.name}" in out
    assert (folder / faces.LANDMARKER.name).read_bytes() == b"x"  # left alone
    assert (tmp_path / "models" / "yunet" / faces.DETECTOR.name).is_file()


def test_the_fetch_command_with_both_present_downloads_nothing(
    tmp_path: Path, served: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    for spec in (faces.LANDMARKER, faces.DETECTOR):
        folder = tmp_path / spec.folder
        folder.mkdir(parents=True)
        (folder / spec.name).write_bytes(b"x")
    assert cli.main(["fetch-face-model"], root=tmp_path) == 0
    assert served == []
    assert capsys.readouterr().out.count("already present") == 2


def test_the_fetch_command_reports_a_present_file_with_the_wrong_hash_as_an_error(
    tmp_path: Path,
    served: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(faces, "LANDMARKER", replace(faces.LANDMARKER, sha256="0" * 64))
    (tmp_path / "models" / "mediapipe").mkdir(parents=True)
    (tmp_path / "models" / "mediapipe" / faces.LANDMARKER.name).write_bytes(b"x")
    assert cli.main(["fetch-face-model"], root=tmp_path) == 2
    assert "SHA-256" in capsys.readouterr().err
    assert served == []


def test_mediapipe_version_reads_metadata_without_importing() -> None:
    import sys

    before = "mediapipe" in sys.modules
    version = faces.mediapipe_version()
    assert version is None or version[0].isdigit()
    assert ("mediapipe" in sys.modules) == before


# --- find_faces (detector and landmarker replaced) ----------------------------------------


@dataclass
class FakeFinding:
    """Scripted detector boxes and landmarker answers; records the crops it was asked about."""

    boxes: list[FaceBox]
    crop_faces: Callable[[int, int], list[RawFace]] = lambda w, h: []
    crops: list[tuple[int, int]] = field(default_factory=list)


@pytest.fixture
def finding(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[FaceBox]], FakeFinding]:
    def install(boxes: list[FaceBox]) -> FakeFinding:
        fake = FakeFinding(boxes=boxes)

        def detect_boxes(finder: object, frame: object) -> list[FaceBox]:
            return fake.boxes

        def crop_landmarks(finder: object, crop: "np.ndarray[Any, Any]") -> list[RawFace]:
            assert crop.flags["C_CONTIGUOUS"]
            fake.crops.append((crop.shape[1], crop.shape[0]))
            return fake.crop_faces(crop.shape[1], crop.shape[0])

        monkeypatch.setattr(faces, "_detect_boxes", detect_boxes)
        monkeypatch.setattr(faces, "_crop_landmarks", crop_landmarks)
        return fake

    return install


def finder_for(*, min_face_height: float = 0.02, max_faces: int = 2) -> faces.FaceFinder:
    return faces.FaceFinder(
        detector=None,
        landmarker=None,
        detect_width=640,
        crop_scale=2.5,
        min_face_height=min_face_height,
        max_faces=max_faces,
    )


FRAME = np.zeros((800, 1000, 3), dtype=np.uint8)


def test_find_faces_returns_one_face_per_box_with_the_boxs_own_position(
    finding: Callable[[list[FaceBox]], FakeFinding],
) -> None:
    fake = finding([box(0.7, 0.4, 0.06, 0.1), box(0.3, 0.5, 0.05, 0.08)])
    fake.crop_faces = lambda w, h: [RawFace(0.5, 0.5, 0.3, 0.4, 0.2)]  # centred in each crop

    found = faces.find_faces(finder_for(), FRAME)

    assert [(f.cx, f.cy, f.w, f.h) for f in found.faces] == [
        (0.3, 0.5, 0.05, 0.08),
        (0.7, 0.4, 0.06, 0.1),
    ]
    assert [f.mouth for f in found.faces] == [0.2, 0.2]


def test_find_faces_maps_the_mouth_from_the_crop_back_to_the_frame(
    finding: Callable[[list[FaceBox]], FakeFinding],
) -> None:
    # box centre (500, 400) px, side 2.5 * 120 = 300, so the crop starts at (350, 250); a face at
    # the crop's centre is the box's face
    fake = finding([box(0.5, 0.5, 0.1, 0.15)])
    fake.crop_faces = lambda w, h: [RawFace(0.5, 0.5, 0.3, 0.4, 0.17)]
    assert faces.find_faces(finder_for(), FRAME).faces[0].mouth == 0.17


def test_find_faces_gives_none_when_the_landmarker_finds_nothing(
    finding: Callable[[list[FaceBox]], FakeFinding],
) -> None:
    finding([box(0.5, 0.5, 0.1, 0.15)])
    found = faces.find_faces(finder_for(), FRAME)
    assert len(found.faces) == 1
    assert found.faces[0].mouth is None
    assert found.faces[0].h == 0.15


def test_find_faces_does_not_use_a_neighbours_face_inside_the_crop(
    finding: Callable[[list[FaceBox]], FakeFinding],
) -> None:
    fake = finding([box(0.5, 0.5, 0.1, 0.15)])
    # the crop spans x 350..650, y 250..550; the only face the landmarker finds is at its right
    # edge (x = 640 px, 0.14 frame widths from the box), far from the box
    fake.crop_faces = lambda w, h: [RawFace(0.97, 0.5, 0.2, 0.3, 0.9)]
    assert faces.find_faces(finder_for(), FRAME).faces[0].mouth is None


def test_find_faces_picks_the_face_at_the_box_when_a_neighbour_is_in_the_crop(
    finding: Callable[[list[FaceBox]], FakeFinding],
) -> None:
    fake = finding([box(0.5, 0.5, 0.1, 0.15)])
    fake.crop_faces = lambda w, h: [
        RawFace(0.95, 0.5, 0.2, 0.3, 0.9),  # the neighbour
        RawFace(0.5, 0.5, 0.3, 0.4, 0.1),  # the box's face
    ]
    assert faces.find_faces(finder_for(), FRAME).faces[0].mouth == 0.1


def test_find_faces_drops_small_boxes_but_still_reports_the_tallest_height(
    finding: Callable[[list[FaceBox]], FakeFinding],
) -> None:
    fake = finding([box(0.2, 0.5, 0.01, 0.02), box(0.8, 0.5, 0.015, 0.03)])
    found = faces.find_faces(finder_for(min_face_height=0.05), FRAME)
    assert found.faces == []
    assert found.largest_height == 0.03
    assert fake.crops == []  # no crop was read for a dropped box


def test_find_faces_with_no_boxes_has_zero_largest_height(
    finding: Callable[[list[FaceBox]], FakeFinding],
) -> None:
    finding([])
    found = faces.find_faces(finder_for(), FRAME)
    assert (found.faces, found.largest_height) == ([], 0.0)


def test_find_faces_keeps_only_max_faces_but_reports_the_tallest(
    finding: Callable[[list[FaceBox]], FakeFinding],
) -> None:
    fake = finding([box(0.2, 0.5, 0.05, 0.1), box(0.5, 0.5, 0.1, 0.2), box(0.8, 0.5, 0.06, 0.12)])
    found = faces.find_faces(finder_for(max_faces=2), FRAME)
    assert [f.cx for f in found.faces] == [0.5, 0.8]
    assert len(fake.crops) == 2
    assert found.largest_height == 0.2


def test_find_faces_hands_the_landmarker_the_crop_crop_square_describes(
    finding: Callable[[list[FaceBox]], FakeFinding],
) -> None:
    target = box(0.98, 0.5, 0.06, 0.1)
    fake = finding([target])
    faces.find_faces(finder_for(), FRAME)
    _, _, side = faces.crop_square(target, 1000, 800, 2.5)
    assert fake.crops == [(side, side)]


def test_opencv_version_reads_metadata_without_importing() -> None:
    import sys

    before = "cv2" in sys.modules
    version = faces.opencv_version()
    assert version is None or version[0].isdigit()
    assert ("cv2" in sys.modules) == before


# --- the real finder (opt in) -------------------------------------------------------------


@pytest.mark.mediapipe
def test_real_finder_finds_no_face_in_a_blank_image() -> None:
    landmarker = REPO_ROOT / faces.LANDMARKER.folder / faces.LANDMARKER.name
    detector = REPO_ROOT / faces.DETECTOR.folder / faces.DETECTOR.name
    if not landmarker.is_file() or not detector.is_file():
        pytest.skip(f"face models not present; run: {faces.FETCH_COMMAND}")
    finder = faces.open_finder(
        ensure_model(detector.parent, spec=faces.DETECTOR),
        ensure_model(landmarker.parent),
        detect_width=640,
        detect_score=0.6,
        crop_scale=2.5,
        min_face_height=0.02,
        max_faces=2,
    )
    try:
        found = faces.find_faces(finder, np.zeros((360, 640, 3), dtype=np.uint8))
        assert (found.faces, found.largest_height) == ([], 0.0)
    finally:
        faces.close_finder(finder)
