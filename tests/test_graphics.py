"""Brief 12.1: on-screen graphics detection, the static-overlay rule, the shot rule, the cache."""

import json
from pathlib import Path

import pytest

import graphics_fakes
from clipmachine.models import GraphicsConfig, GraphicsFile, ShotGraphics
from clipmachine.utils import faces, graphics
from clipmachine.utils.graphics import (
    Box,
    GraphicsPlan,
    analyse,
    fit_spans,
    graphics_for_clip,
    learn_static,
    load_valid_graphics,
    position_word,
    shot_graphics,
    shot_ranges,
    window_from_path,
)
from graphics_fakes import LOGO, PANEL, TITLE, overlays_only, panel_between
from test_tracking import video

SHA = "a" * 64
CROP_W = 202  # 9:16 of a 360-high frame, even
WINDOW = Box(0.0, 0.0, 1.0, 1.0)


def cfg(**keys: object) -> GraphicsConfig:
    return GraphicsConfig(enabled=True, **keys)  # type: ignore[arg-type]


def plan(tmp_path: Path, settings: GraphicsConfig | None = None) -> GraphicsPlan:
    model = graphics_fakes.write_text_model(tmp_path)
    (tmp_path / "clips").mkdir(exist_ok=True)
    return GraphicsPlan(
        clips_dir=tmp_path / "clips",
        source=tmp_path / "source.mp4",
        video=video(),
        source_sha256=SHA,
        settings=settings or cfg(),
        model_path=model,
        model_sha256=faces.file_sha256(model),
        opencv_version="5.0.0",
    )


# --- boxes ---------------------------------------------------------------------------------------


def test_box_area_iou_and_inside() -> None:
    a, b = Box(0, 0, 0.5, 0.5), Box(0.25, 0, 0.75, 0.5)
    assert a.area == 0.25 and a.iou(b) == pytest.approx(1 / 3) and a.iou(a) == 1.0
    assert a.inside(WINDOW) and not a.inside(Box(0.1, 0, 1, 1))
    assert Box(0, 0, 0, 0.5).area == 0 and Box(0, 0, 0, 0.5).iou(a) == 0


# --- the static-overlay rule ---------------------------------------------------------------------


def test_a_box_in_every_sample_is_static_and_a_temporary_one_is_not() -> None:
    samples = [[TITLE, LOGO, PANEL] if k < 3 else [TITLE, LOGO] for k in range(10)]
    assert learn_static(samples, 0.8) == [TITLE, LOGO]
    assert learn_static([], 0.8) == []


def test_a_slightly_moving_overlay_still_counts_as_the_same_place() -> None:
    jitter = [[Box(0.05 + 0.002 * k, 0.04, 0.30 + 0.002 * k, 0.07)] for k in range(10)]
    (found,) = learn_static(jitter, 0.8)
    assert found == jitter[0][0]


def test_static_ratio_is_a_share_of_all_samples() -> None:
    samples = [[TITLE] if k < 7 else [] for k in range(10)]
    assert learn_static(samples, 0.8) == []
    assert learn_static(samples, 0.7) == [TITLE]


# --- the shot rule -------------------------------------------------------------------------------


def samples_with(boxes: list[Box], count: int = 10) -> list[tuple[float, list[Box]]]:
    return [(k / 2, boxes) for k in range(count)]


def test_a_panel_inside_the_crop_window_does_not_switch_the_layout() -> None:
    window = Box(0.0, 0.0, 0.5, 1.0)  # the crop covers the whole panel
    shot = shot_graphics(0, 5, samples_with([PANEL]), [], lambda t: window, cfg())
    assert shot.fit is False and shot.outside is False and shot.presence == 1.0
    assert shot.area == pytest.approx(PANEL.area, abs=1e-4)


def test_a_panel_beside_the_crop_window_switches_the_layout() -> None:
    window = Box(0.4, 0.0, 0.9, 1.0)  # the panel at x 0.07-0.38 is outside
    shot = shot_graphics(0, 5, samples_with([PANEL]), [], lambda t: window, cfg())
    assert shot.fit is True and shot.outside is True
    assert shot == ShotGraphics(
        start=0, end=5, fit=True, area=shot.area, presence=1.0, outside=True
    )


def test_static_overlays_are_ignored_by_the_shot_rule() -> None:
    window = Box(0.4, 0.0, 0.9, 1.0)  # the title and logo are both outside it
    shot = shot_graphics(0, 5, samples_with([TITLE, LOGO]), [TITLE, LOGO], lambda t: window, cfg())
    assert shot.fit is False and shot.presence == 0.0 and shot.area == 0.0


def test_the_area_threshold_drops_small_text() -> None:
    small = Box(0.1, 0.1, 0.2, 0.15)  # 0.005 of the frame
    window = Box(0.4, 0.0, 0.9, 1.0)
    assert shot_graphics(0, 5, samples_with([small]), [], lambda t: window, cfg()).fit is False
    loose = cfg(min_area_frac=0.004)
    assert shot_graphics(0, 5, samples_with([small]), [], lambda t: window, loose).fit is True


def test_the_presence_threshold_needs_the_panel_in_enough_samples() -> None:
    window = Box(0.4, 0.0, 0.9, 1.0)
    samples = [(k / 2, [PANEL] if k < 2 else []) for k in range(10)]  # 20% of the samples
    assert shot_graphics(0, 5, samples, [], lambda t: window, cfg()).fit is False
    assert shot_graphics(0, 5, samples, [], lambda t: window, cfg(min_presence=0.2)).fit is True
    assert shot_graphics(0, 5, [], [], lambda t: window, cfg()).fit is False  # no samples


def test_the_window_in_force_comes_from_the_crop_path() -> None:
    window_at = window_from_path([(0.0, 0), (2.0, 400)], crop_w=200, source_w=800)
    assert window_at(0.0) == Box(0.0, 0.0, 0.25, 1.0)
    assert window_at(1.99) == Box(0.0, 0.0, 0.25, 1.0)
    assert window_at(2.0) == Box(0.5, 0.0, 0.75, 1.0)
    assert window_from_path([(0.0, 700)], 200, 800)(9.0) == Box(0.875, 0.0, 1.0, 1.0)


# --- shots and spans -----------------------------------------------------------------------------


def test_shot_ranges_use_the_exact_cut_times_and_one_shot_without_a_track() -> None:
    shots = [(0.0, 4.0), (4.0, 7.0), (7.0, 10.0)]
    assert shot_ranges(shots, [4.0], [4.08], 10.0) == [(0.0, 4.08), (4.08, 7.0), (7.0, 10.0)]
    assert shot_ranges([], [], [], 10.0) == [(0.0, 10.0)]


def test_fit_spans_merge_neighbours_and_skip_the_rest() -> None:
    def shot(start: float, end: float, fit: bool) -> ShotGraphics:
        return ShotGraphics(start=start, end=end, fit=fit, area=0, presence=0, outside=fit)

    results = [
        shot(0, 2, False),
        shot(2, 5, True),
        shot(5, 7, True),
        shot(7, 8, False),
        shot(8, 9, True),
    ]
    assert fit_spans(results) == [(2.0, 7.0), (8.0, 9.0)]
    assert fit_spans([shot(0, 0, True)]) == []


def test_position_words() -> None:
    assert position_word(TITLE.to_model()) == "top-left"
    assert position_word(LOGO.to_model()) == "bottom-right"
    assert position_word(Box(0.4, 0.4, 0.6, 0.6).to_model()) == "centre"
    assert position_word(Box(0.4, 0.0, 0.6, 0.1).to_model()) == "top-centre"


# --- the analysis end to end (fakes) ------------------------------------------------------------


def test_analyse_learns_the_overlays_and_flags_the_shot_with_the_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = graphics_fakes.install(monkeypatch, panel_between(4.0, 8.0))
    p = plan(tmp_path)
    # three shots; the crop sits on the right (x 400 of 640), so the panel on the left is outside
    found = analyse(
        p,
        "c01",
        10.0,
        20.0,
        shots=[(0.0, 4.0), (4.0, 8.0), (8.0, 10.0)],
        cuts=[4.0, 8.0],
        cuts_exact=[4.05, 7.98],
        crop_path=[(0.0, 400)],
        crop_w=CROP_W,
    )
    assert found.samples == 20 and found.frame.w == 640 and found.frame.h == 360
    assert [graphics.from_model(b) for b in found.static] == [TITLE, LOGO]
    assert [s.fit for s in found.shots] == [False, True, False]
    assert found.shots[1].start == 4.05 and found.shots[1].end == 7.98
    assert found.fit_spans == [(4.05, 7.98)]
    assert found.model_sha256 == p.model_sha256 and found.settings == p.settings
    assert fakes.opened == [(p.model_path, p.model_sha256)]
    args = fakes.frame_requests[0]
    assert args[args.index("-ss") + 1] == "10.000" and args[args.index("-t") + 1] == "10.000"
    assert args[args.index("-vf") + 1] == "fps=2,scale=640:-2"  # detect_width capped at the source


def test_analyse_without_a_track_judges_the_whole_clip_as_one_shot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphics_fakes.install(monkeypatch, panel_between(2.0, 8.0))  # 60% of the samples
    found = analyse(
        plan(tmp_path),
        "c01",
        0.0,
        10.0,
        shots=[],
        cuts=[],
        cuts_exact=[],
        crop_path=[(0.0, 400)],
        crop_w=CROP_W,
    )
    assert len(found.shots) == 1 and found.fit_spans == [(0.0, 10.0)]
    assert [graphics.from_model(b) for b in found.static] == [TITLE, LOGO]
    assert found.shots[0].presence == 0.6


def test_a_panel_present_throughout_counts_as_an_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphics_fakes.install(monkeypatch, panel_between(0.0, 10.0))
    found = analyse(
        plan(tmp_path),
        "c01",
        0.0,
        10.0,
        shots=[],
        cuts=[],
        cuts_exact=[],
        crop_path=[(0.0, 400)],
        crop_w=CROP_W,
    )
    assert [graphics.from_model(b) for b in found.static] == [TITLE, LOGO, PANEL]
    assert found.fit_spans == []


def test_the_result_is_cached_and_reused_only_while_everything_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = graphics_fakes.install(monkeypatch, overlays_only)
    p = plan(tmp_path)
    common = {"shots": [], "cuts": [], "cuts_exact": [], "crop_path": [(0.0, 0)], "crop_w": CROP_W}
    first, reused = graphics_for_clip(p, "c01", 0.0, 5.0, **common)  # type: ignore[arg-type]
    assert reused is False and len(fakes.frame_requests) == 1
    path = graphics.graphics_path(p.clips_dir, "c01")
    assert GraphicsFile.model_validate_json(path.read_text(encoding="utf-8")) == first
    again, reused = graphics_for_clip(p, "c01", 0.0, 5.0, **common)  # type: ignore[arg-type]
    assert reused is True and again == first and len(fakes.frame_requests) == 1

    def valid(**changes: object) -> bool:
        keys = {
            "start": 0.0,
            "end": 5.0,
            "source_sha256": SHA,
            "settings": p.settings,
            "model_sha256": p.model_sha256,
            "opencv_version": "5.0.0",
        }
        keys.update(changes)
        return load_valid_graphics(p.clips_dir, "c01", **keys) is not None  # type: ignore[arg-type]

    assert valid()
    assert not valid(end=6.0)
    assert not valid(source_sha256="b" * 64)
    assert not valid(settings=cfg(min_presence=0.5))
    assert not valid(model_sha256="c" * 64)
    assert not valid(opencv_version="4.9.0")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")
    assert not valid()
    # 1: built on the shots of the colour-only cut test, before fix/low-contrast-cuts
    data["schema_version"] = 1
    path.write_text(json.dumps(data), encoding="utf-8")
    assert not valid()
    path.write_text("{not json", encoding="utf-8")
    assert not valid()


def test_build_plan_needs_the_text_model_and_names_the_command(tmp_path: Path) -> None:
    with pytest.raises(faces.FacesError, match="clipmachine fetch-text-model"):
        graphics.build_plan(
            tmp_path, tmp_path, cfg(), video=video(), source=tmp_path / "s.mp4", source_sha256=SHA
        )
    model = graphics_fakes.write_text_model(tmp_path)
    built = graphics.build_plan(
        tmp_path, tmp_path, cfg(), video=video(), source=tmp_path / "s.mp4", source_sha256=SHA
    )
    assert built.model_path == model and built.model_sha256 == faces.file_sha256(model)


def test_the_real_detector_seam_rejects_a_changed_file(tmp_path: Path) -> None:
    model = graphics_fakes.write_text_model(tmp_path)
    with pytest.raises(faces.FacesError, match="SHA-256"):
        graphics.open_text_detector(model, "0" * 64)
