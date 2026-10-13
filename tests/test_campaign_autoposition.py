"""`watermark.position: auto` (Brief 11.1) through the campaign command with ffmpeg faked: flat or
half-dark frames, scripted faces, the lock file. Invented words only."""

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import track_fakes
from campaign_helpers import moment, transparent_mark, write_png
from clipmachine import cli
from clipmachine.campaign.autoposition import AUTO_POSITION_FILE
from clipmachine.campaign.models import AutoPosition
from clipmachine.config import load_app_config
from clipmachine.stages import render as stage
from clipmachine.utils import ffmpeg
from clipmachine.utils.captions import HOOK_CAPTION_GAP_PX, caption_top
from clipmachine.utils.safezone import Rect
from helpers import edit_yaml
from test_campaign_run import Rig, make_rig, out_of
from test_render import FakeFfmpeg

Mutate = Callable[[dict[str, Any]], None]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeFfmpeg:
    fake = FakeFfmpeg()
    monkeypatch.setattr(ffmpeg, "render_clip", fake.render_clip)
    monkeypatch.setattr(ffmpeg, "probe", fake.probe)
    return fake


def auto(d: dict[str, Any]) -> None:
    d["watermark"]["position"] = "auto"


def both(*mutates: Mutate) -> Mutate:
    def mutate(d: dict[str, Any]) -> None:
        for m in mutates:
            m(d)

    return mutate


def auto_rig(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutate: Mutate | None = None,
    png: Any = None,
    scenario: track_fakes.Scenario | None = None,
) -> tuple[Rig, track_fakes.TrackFakes]:
    """A campaign with `position: auto`; with a scenario the crop is the speaker crop and the
    fakes script the faces. Frames are flat grey 0 unless a test replaces read_frames."""
    if scenario is not None:
        edit_yaml(project / "config" / "default.yaml", lambda d: d["video"].update(crop="speaker"))
        track_fakes.write_model(project)
    fakes = track_fakes.install(monkeypatch, scenario or track_fakes.nobody)
    rig = make_rig(project, fake, mutate=both(auto, mutate) if mutate else auto, png=png)
    rig.moments(moment(id="A01", start="0:24", end="0:58"))
    return rig, fakes


def lock_of(rig: Rig) -> AutoPosition:
    return AutoPosition.model_validate_json(
        (rig.folder / AUTO_POSITION_FILE).read_text(encoding="utf-8")
    )


def rect_of(rig: Rig, name: str = "A01_tiktok") -> Rect:
    return Rect(*rig.clip(name).watermark.rect)


def envelope_top(rig: Rig) -> float:
    """The highest edge a caption box can have, minus the hook gap: no candidate goes below it."""
    app = load_app_config(rig.project)
    inputs = stage.load_source_inputs(rig.run_dir, rig.project, app)
    plan = stage._make_plan(rig.run_dir, rig.out, app, inputs)
    assert plan.karaoke is not None and plan.metrics is not None and plan.safe is not None
    return caption_top(plan.karaoke, plan.metrics, plan.safe) - HOOK_CAPTION_GAP_PX


def half_dark(
    args: list[str], frame_w: int, frame_h: int, timeout: float, *, scratch_dir: Path | None = None
) -> Iterator[Any]:
    """Frames whose top half is black and bottom half white."""
    duration = float(args[args.index("-t") + 1])
    fps = float(args[args.index("-vf") + 1].split(",")[0].removeprefix("fps="))
    for _ in range(int(round(duration * fps))):
        frame = np.full((frame_h, frame_w, 3), 255, dtype=np.uint8)
        frame[: frame_h // 2] = 0
        yield frame


# --- the choice ------------------------------------------------------------------------------


def test_with_nothing_to_tell_them_apart_the_spot_nearest_the_captions_wins(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig, _ = auto_rig(project, fake, monkeypatch)

    assert rig.go() == 0

    lock = lock_of(rig)
    rect = rect_of(rig)
    top = envelope_top(rig)
    assert lock.x_center == 0.5
    assert rect.y2 <= top and top - rect.y2 < 0.02 * 1920 + 1  # the last step above the captions
    assert lock.face_cost == 0.0 and lock.luminance == 0.0 and lock.score == 0.0
    runner = lock.runner_up
    assert runner is not None and (runner.x_center, runner.y_center) != (0.5, lock.y_center)
    assert runner.score == 0.0 and runner.y_center <= lock.y_center
    assert lock.sampled == ["A01_tiktok"] and lock.scored <= lock.placeable <= lock.candidates
    assert any(note.startswith("faces unknown") for note in lock.notes)
    clip = rig.clip("A01_tiktok")
    assert clip.watermark.position_mode == "auto" and set(clip.checks.values()) == {"pass"}
    text = out_of(capsys)
    assert "watermark position: auto; choosing from the first 5 moments" in text
    assert "watermark: auto position x 0.50, y " in text and "runner-up x 0.45, y " in text
    html = (rig.out / "campaign_report.html").read_text(encoding="utf-8")
    assert "<b>Watermark position:</b> auto &rarr; x 0.50" in html and "runner-up" in html
    assert "Locked in auto_position.json" in html


def test_a_dark_background_beats_a_light_one(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, _ = auto_rig(project, fake, monkeypatch)
    monkeypatch.setattr(ffmpeg, "read_frames", half_dark)

    assert rig.go() == 0

    lock = lock_of(rig)
    rect = rect_of(rig)
    # the source top half is the output top half (y < 960): the mark sits there, as low as it can
    assert lock.luminance == 0.0 and rect.y2 <= 960 and rect.y2 > 960 - 0.02 * 1920 - 1
    assert lock.runner_up is not None and lock.runner_up.luminance == 0.0


def test_a_face_free_spot_beats_one_over_the_face(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a wide face low in the frame, across the spots nearest the captions
    face = track_fakes.RawFace(0.5, 0.6, 0.9, 0.1, 0.0)
    rig, fakes = auto_rig(project, fake, monkeypatch, scenario=lambda t: [face])

    assert rig.go() == 0

    lock = lock_of(rig)
    rect = rect_of(rig)
    face_top = (0.6 - 0.05) * 1920 - 0.1 * 0.1 * 1920  # the padded face box, in output pixels
    assert lock.face_cost == 0.0 and rect.y2 <= face_top
    assert rect.y2 > face_top - 0.02 * 1920 - 1  # just above it
    assert "watermark_over_face" not in rig.clip("A01_tiktok").notes
    assert fakes.opened == 1  # the track from the scoring was reused by the render
    assert (rig.out / "A01_tiktok.track.json").is_file()


def test_the_face_cost_counts_the_share_of_samples_with_a_face_in_the_way(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    # faces everywhere a candidate could go: the mark must sit over one, and the cost says how often
    def scenario(t: float) -> list[track_fakes.RawFace]:
        if t < 10:
            return [track_fakes.RawFace(0.5, 0.3, 0.9, 0.6, 0.0)]  # wide and tall: covers all
        return []

    rig, _ = auto_rig(project, fake, monkeypatch, scenario=scenario)

    assert rig.go() == 0

    lock = lock_of(rig)
    assert 0 < lock.face_cost < 1 and "watermark_over_face" in rig.clip("A01_tiktok").notes


def test_hook_position_top_keeps_the_band_free(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    def top(d: dict[str, Any]) -> None:
        d["hook"]["position"] = "top"

    rig, _ = auto_rig(project, fake, monkeypatch, mutate=top)
    monkeypatch.setattr(ffmpeg, "read_frames", half_dark)  # pulls the mark up, under the band

    assert rig.go() == 0

    lock = lock_of(rig)
    assert rect_of(rig).y1 > 290 + 60  # below a one-line band at the top of the safe area
    assert any("hook.position top" in note for note in lock.notes)
    assert rig.clip("A01_tiktok").checks["hook_from_start"] == "pass"


# --- the lock --------------------------------------------------------------------------------


def test_the_lock_is_reused_until_the_watermark_changes_or_it_is_deleted(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig, fakes = auto_rig(project, fake, monkeypatch)
    assert rig.go() == 0
    first = lock_of(rig)
    reads = len(fakes.frame_requests)
    out_of(capsys)

    assert rig.go() == 0  # the lock is used: no frame is read
    assert len(fakes.frame_requests) == reads and lock_of(rig) == first
    text = out_of(capsys)
    assert "(locked in auto_position.json)" in text and "choosing from" not in text

    write_png(rig.folder / "assets" / "mark.png", transparent_mark(bar=(100, 30, 260, 66)))
    assert rig.go() == 0
    second = lock_of(rig)
    assert second.watermark_sha256 != first.watermark_sha256 and second.chosen_at != first.chosen_at
    assert "warning: auto_position.json is stale" in out_of(capsys)
    assert len(fakes.frame_requests) > reads

    (rig.folder / AUTO_POSITION_FILE).unlink()
    assert rig.go() == 0
    assert lock_of(rig).chosen_at != second.chosen_at


def test_a_lock_that_no_longer_fits_is_a_config_error(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig, _ = auto_rig(project, fake, monkeypatch)
    assert rig.go() == 0
    path = rig.folder / AUTO_POSITION_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data["x_center"] = 0.08  # outside the safe area
    path.write_text(json.dumps(data), encoding="utf-8")

    assert rig.go() == 2
    text = out_of(capsys)
    assert "error: auto_position.json: the locked position puts the visible watermark" in text
    assert "delete the file to choose again" in text


def test_an_unreadable_lock_is_a_warning_and_the_position_is_chosen_again(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig, _ = auto_rig(project, fake, monkeypatch)
    (rig.folder / AUTO_POSITION_FILE).write_text("{not json", encoding="utf-8")

    assert rig.go() == 0
    assert "warning: auto_position.json cannot be read" in out_of(capsys)
    assert lock_of(rig).x_center == 0.5


def test_the_lock_records_the_geometry_it_was_chosen_for(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, _ = auto_rig(project, fake, monkeypatch)
    assert rig.go() == 0
    lock = lock_of(rig)
    assert lock.frame == (1080, 1920) and lock.safe == (290, 560, 120, 192)
    assert lock.corner_zone == 0.15 and lock.hook_position == "avoid_faces"
    assert lock.scale == 1.0 and lock.blend == "normal" and lock.schema_version == 1


# --- what is sampled -------------------------------------------------------------------------


def test_the_sample_is_the_selected_unposted_moments(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, _ = auto_rig(project, fake, monkeypatch)
    rig.moments(
        moment(id="A01", start="0:24", end="0:58"),
        moment(id="A02", start="0:10", end="0:40", platform="instagram"),
        moment(id="A03", start="0:34", end="1:04", platform="youtube"),
    )
    assert rig.go() == 0
    assert lock_of(rig).sampled == ["A01_tiktok", "A02_instagram", "A03_youtube"]
    assert (
        cli.main(
            ["campaign-posted", "--campaign", "demo", "--id", "A03", "--platform", "youtube"],
            root=rig.project,
        )
        == 0
    )
    (rig.folder / AUTO_POSITION_FILE).unlink()

    assert rig.go("--only", "A01,A03") == 1  # A03 is named and refused: not sampled either
    assert lock_of(rig).sampled == ["A01_tiktok"]


def test_the_sample_is_capped_and_skips_moments_that_fail_the_time_checks(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    edit_yaml(
        project / "config" / "default.yaml",
        lambda d: d["campaign"].update(auto_watermark_sample=2),
    )
    rig, _ = auto_rig(project, fake, monkeypatch)
    rig.moments(
        moment(id="A01", start="0:24", end="0:30"),  # too short: not sampled, fails later
        moment(id="A02", start="0:10", end="0:40", platform="instagram"),
        moment(id="A03", start="0:34", end="1:04", platform="youtube"),
        moment(id="A04", start="0:24", end="0:58", platform="tiktok"),
    )

    assert rig.go() == 1
    assert lock_of(rig).sampled == ["A02_instagram", "A03_youtube"]


# --- when there is no place ------------------------------------------------------------------


def test_a_watermark_too_wide_for_the_safe_area_is_a_clear_error(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wide = transparent_mark(1000, 96, bar=(0, 38, 1000, 58))
    rig, _ = auto_rig(project, fake, monkeypatch, png=wide)

    assert rig.go() == 2
    text = out_of(capsys)
    assert "error: watermark.position: auto found no place for the visible watermark" in text
    assert "1000x20 px at scale 1" in text and "lower watermark.scale" in text
    assert not (rig.folder / AUTO_POSITION_FILE).exists()


def test_no_renderable_moment_is_a_clear_error(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig, _ = auto_rig(project, fake, monkeypatch)
    rig.moments(moment(id="A01", start="0:24", end="0:30"))  # too short

    assert rig.go() == 2
    assert "needs at least one moment that can be rendered" in out_of(capsys)


# --- an explicit position is untouched -------------------------------------------------------


def test_an_explicit_position_bypasses_auto(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    track_fakes.install(monkeypatch)
    rig = make_rig(project, fake)
    rig.moments(moment(id="A01", start="0:24", end="0:58"))

    assert rig.go() == 0
    assert not (rig.folder / AUTO_POSITION_FILE).exists()
    assert rig.clip("A01_tiktok").watermark.position_mode == "explicit"
    assert "watermark: auto position" not in out_of(capsys)
    assert "Watermark position:" not in (rig.out / "campaign_report.html").read_text("utf-8")
