"""The `campaign` command end to end with ffmpeg faked (Brief 11). Invented words only."""

import csv
import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

import track_fakes
from campaign_helpers import (
    black_mark,
    moment,
    sentence_end_cut,
    sentence_start_cut,
    write_campaign,
    write_moments,
    write_run,
)
from clipmachine import cli
from clipmachine.campaign.models import CHECKS, CampaignClip
from clipmachine.campaign.render import META_DIR
from clipmachine.campaign.run import find_run
from clipmachine.campaign.used import USED_FILE, load_used
from clipmachine.utils import claude, ffmpeg
from clipmachine.utils.captions import ass_time
from clipmachine.utils.snapping import START_KEPT_NOTE
from helpers import edit_yaml
from test_render import FakeFfmpeg, invented_lists

HOOK = "AN INVENTED HOOK"
CAPTION = "says something invented."
SECRET_TEXTS = (HOOK, CAPTION, "closer one", "closer two", "closer three", "Zorb1e", "QXV")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeFfmpeg:
    fake = FakeFfmpeg()
    monkeypatch.setattr(ffmpeg, "render_clip", fake.render_clip)
    monkeypatch.setattr(ffmpeg, "probe", fake.probe)
    return fake


@dataclass
class Rig:
    project: Path
    run_dir: Path
    folder: Path

    @property
    def out(self) -> Path:
        return self.project / "runs" / "_campaigns" / "demo"

    @property
    def meta_dir(self) -> Path:
        return self.out / META_DIR

    def mp4(self, name: str) -> Path:
        """The clip of `<id>_<platform>`: `<platform>/<id>.mp4` (Brief 11.2)."""
        moment_id, platform = name.split("_")
        return self.out / platform / f"{moment_id}.mp4"

    def txt(self, name: str) -> Path:
        moment_id, platform = name.split("_")
        return self.out / platform / f"{moment_id}.txt"

    def meta(self, file_name: str) -> Path:
        return self.meta_dir / file_name

    def paths(self, name: str) -> list[Path]:
        """Every file of a clip: its mp4 and text, and its _meta files."""
        return [self.mp4(name), self.txt(name), *sorted(self.meta_dir.glob(f"{name}.*"))]

    def go(self, *extra: str, target: Path | None = None) -> int:
        argv = ["campaign", str(target or self.run_dir), "--campaign", "demo", *extra]
        return cli.main(argv, root=self.project)

    def moments(self, *entries: dict[str, Any]) -> None:
        write_moments(self.folder, list(entries))

    def clip(self, name: str) -> CampaignClip:
        return CampaignClip.model_validate_json(
            self.meta(f"{name}.json").read_text(encoding="utf-8")
        )

    def used(self) -> Any:
        return load_used(self.folder)

    def args_of(self, name: str) -> list[str]:
        return next(args for clip_id, args, _ in self.fake.calls if clip_id == name)

    fake: FakeFfmpeg = None  # type: ignore[assignment]


def make_rig(
    project: Path,
    fake: FakeFfmpeg,
    *,
    mutate: Callable[[dict[str, Any]], None] | None = None,
    png: Any = None,
    overrides: dict[tuple[int, int], str] | None = None,
    width: int = 640,
    height: int = 360,
) -> Rig:
    run_dir = write_run(project, overrides=overrides, width=width, height=height)
    folder = write_campaign(project, mutate=mutate, png=png)
    rig = Rig(project, run_dir.resolve(), folder)
    rig.fake = fake
    rig.moments(moment(id="A01", start="0:24", end="0:58"))
    return rig


@pytest.fixture
def rig(project: Path, fake: FakeFfmpeg) -> Rig:
    return make_rig(project, fake)


def out_of(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


# --- snapping and lengths ------------------------------------------------------------------------


def test_a_moment_near_clean_edges_moves_to_them(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rig.go() == 0

    clip = rig.clip("A01_tiktok")
    start, end = sentence_start_cut(5), sentence_end_cut(11)  # 24.25 and 58.3
    assert (clip.requested_start, clip.requested_end) == (24.0, 58.0)
    assert (clip.start, clip.end) == (start, end)
    assert clip.duration == pytest.approx(end - start)
    assert clip.snap_notes == [
        "start moved +0.25 s to a clean sentence edge",
        "end moved +0.30 s to a clean sentence edge",
    ]
    assert clip.status == "rendered" and clip.end_mode == "given" and clip.api_calls == 0
    for path in (rig.mp4("A01_tiktok"), rig.txt("A01_tiktok"), *rig.paths("A01_tiktok")[2:]):
        assert path.is_file()
    assert {p.suffix for p in rig.meta_dir.glob("A01_tiktok.*")} == {".json", ".ass"}
    ffmpeg_start = rig.args_of("A01_tiktok")[rig.args_of("A01_tiktok").index("-ss") + 1]
    assert float(ffmpeg_start) == pytest.approx(start)
    text = out_of(capsys)
    assert "A01 tiktok: rendered 24.25-58.30 s (34.05 s), start +0.25 s, end +0.30 s" in text


def test_a_time_with_no_clean_edge_in_reach_is_kept_with_a_warning_note(rig: Rig) -> None:
    rig.moments(moment(start="0:22", end="0:58"))  # the nearest start edges are 2.25 s away

    assert rig.go() == 0

    clip = rig.clip("A01_tiktok")
    assert clip.start == 22.0 and clip.end == sentence_end_cut(11)
    assert clip.snap_notes[0].startswith(START_KEPT_NOTE)
    assert "1.5 s" in clip.snap_notes[0]


def test_one_moment_fails_the_length_check_while_the_others_still_render(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.moments(
        moment(id="A01", start="0:24", end="0:58"),
        moment(id="A02", start="0:24", end="0:30", platform="instagram"),  # 4.65 s: too short
        moment(id="A03", start="0:34", end="1:04", platform="youtube"),
    )

    assert rig.go() == 1

    assert (rig.mp4("A01_tiktok")).is_file() and (rig.mp4("A03_youtube")).is_file()
    for name in ("A01_tiktok", "A03_youtube"):
        assert rig.clip(name).status == "rendered"
        assert (rig.txt(name)).is_file()
    failed = rig.clip("A02_instagram")
    assert failed.status == "failed" and failed.checks["length_in_range"] == "fail"
    assert "outside the allowed 20-60 s" in (failed.error or "")
    assert not (rig.mp4("A02_instagram")).exists()
    assert not (rig.txt("A02_instagram")).exists()
    text = out_of(capsys)
    assert "A02 instagram: FAILED: 4.6 s is outside the allowed 20-60 s" in text
    assert "campaign: 2 rendered, 1 failed, 0 refused; API calls: 0;" in text


def test_a_failed_moment_removes_what_an_earlier_run_left(rig: Rig) -> None:
    assert rig.go() == 0
    assert (rig.mp4("A01_tiktok")).is_file()
    rig.moments(moment(start="0:24", end="0:30"))  # now too short

    assert rig.go() == 1

    assert rig.clip("A01_tiktok").status == "failed"
    for path in (rig.mp4("A01_tiktok"), rig.txt("A01_tiktok"), rig.meta("A01_tiktok.ass")):
        assert not path.exists()


def test_a_moment_outside_the_source_fails(rig: Rig) -> None:
    rig.moments(moment(start="5:00", end="5:30"))  # the source is 100 s long
    assert rig.go() == 1
    assert "outside the source" in (rig.clip("A01_tiktok").error or "")


def test_a_stretch_with_no_words_fails(project: Path, fake: FakeFfmpeg) -> None:
    rig = make_rig(project, fake)
    run_dir = write_run(project, sentences=3)  # 14.7 s of speech in a 100 s source
    rig.moments(moment(start="0:30", end="1:10"))
    assert rig.go(target=run_dir) == 1
    assert "no words" in (rig.clip("A01_tiktok").error or "")


# --- auto end ------------------------------------------------------------------------------------


def test_end_auto_picks_the_nearest_clean_end_to_the_target(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.moments(moment(start="0:24", end="auto"))

    assert rig.go() == 0

    clip = rig.clip("A01_tiktok")
    start, end = sentence_start_cut(5), sentence_end_cut(12)  # target end 64.25: 63.2 is nearest
    assert (clip.start, clip.end) == (start, end)
    assert clip.end_mode == "auto" and clip.auto_end_target == 40.0
    assert clip.requested_end is None
    assert clip.duration == pytest.approx(end - start, abs=1e-3)
    assert clip.snap_notes[-1] == "end chosen automatically: target 40 s, actual 38.95 s"
    assert "end auto (target 40 s, actual 38.95 s)" in out_of(capsys)


def test_end_auto_fails_cleanly_when_no_edge_gives_an_allowed_length(rig: Rig) -> None:
    rig.moments(moment(start="1:30", end="auto"), moment(id="A02", start="0:24", end="0:58"))

    assert rig.go() == 1

    failed = rig.clip("A01_tiktok")
    assert failed.status == "failed" and failed.end_mode == "auto"
    assert "no clean sentence end gives a clip of 20-60 s" in (failed.error or "")
    assert rig.clip("A02_tiktok").status == "rendered"


def test_a_missing_end_is_the_same_as_auto(rig: Rig) -> None:
    entry = moment(start="0:24")
    del entry["end"]
    rig.moments(entry)
    assert rig.go() == 0
    assert rig.clip("A01_tiktok").end_mode == "auto"


# --- no API --------------------------------------------------------------------------------------


def test_a_campaign_run_never_touches_the_api(
    rig: Rig, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[str] = []
    monkeypatch.setattr(claude, "new_client", lambda key: created.append(key))
    monkeypatch.setattr(claude, "load_api_key", lambda root: pytest.fail("key read"))
    monkeypatch.delenv(claude.KILL_SWITCH_ENV, raising=False)
    seen: list[str | None] = []
    original = fake.render_clip

    def spy(args: list[str], cwd: Path, duration_seconds: float) -> None:
        seen.append(os.environ.get(claude.KILL_SWITCH_ENV))
        original(args, cwd, duration_seconds)

    monkeypatch.setattr(ffmpeg, "render_clip", spy)

    assert rig.go() == 0

    assert created == [] and seen == ["1"]
    assert claude.KILL_SWITCH_ENV not in os.environ  # put back
    assert rig.clip("A01_tiktok").api_calls == 0
    assert not (rig.run_dir / "costs.json").exists()


def test_a_changed_ledger_stops_the_command(
    rig: Rig, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = fake.render_clip

    def spend(args: list[str], cwd: Path, duration_seconds: float) -> None:
        ledger = rig.project / "runs" / "_ledger" / "costs.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("{}\n", encoding="utf-8")
        original(args, cwd, duration_seconds)

    monkeypatch.setattr(ffmpeg, "render_clip", spend)

    assert rig.go() == 2
    assert "campaign mode made an API call" in capsys.readouterr().err


def test_a_changed_run_cost_file_stops_the_command(
    rig: Rig, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = fake.render_clip

    def spend(args: list[str], cwd: Path, duration_seconds: float) -> None:
        (rig.run_dir / "costs.json").write_text("{}", encoding="utf-8")
        original(args, cwd, duration_seconds)

    monkeypatch.setattr(ffmpeg, "render_clip", spend)

    assert rig.go() == 2
    assert "campaign mode made an API call" in capsys.readouterr().err


# --- the watermark and the hook ------------------------------------------------------------------


def test_the_hook_goes_below_the_watermark(rig: Rig) -> None:
    assert rig.go() == 0

    clip = rig.clip("A01_tiktok")
    wm_bottom = clip.watermark.rect[3]
    ass = (rig.meta("A01_tiktok.ass")).read_text(encoding="utf-8")
    tops = [
        int(m.group(1))
        for line in ass.split("\n")
        if line.startswith("Dialogue: 3,")
        for m in [re.search(r"pos\(\d+,(\d+)\)", line)]
        if m
    ]
    assert tops and all(top > wm_bottom for top in tops)
    assert "hook_moved_for_overlay" in clip.notes


def test_a_caption_over_the_watermark_fails_the_moment(project: Path, fake: FakeFfmpeg) -> None:
    def low(d: dict[str, Any]) -> None:
        d["watermark"]["position"] = {"x_center": 0.5, "y_center": 0.68}

    rig = make_rig(project, fake, mutate=low)
    assert rig.go() == 1
    failed = rig.clip("A01_tiktok")
    assert failed.status == "failed" and "a caption would cover the watermark" in (
        failed.error or ""
    )
    assert failed.checks["watermark_full_and_clear"] == "fail"
    assert not (rig.mp4("A01_tiktok")).exists()


def test_a_hook_that_cannot_fit_fails_the_moment(project: Path, fake: FakeFfmpeg) -> None:
    def big(d: dict[str, Any]) -> None:
        d["hook"]["font_size"] = 200

    rig = make_rig(project, fake, mutate=big)
    rig.moments(moment(start="0:24", end="0:58", hook="AAAA BBBB CCCC DDDD EEEE FFFF"))
    assert rig.go() == 1
    error = rig.clip("A01_tiktok").error or ""
    assert "the hook does not fit at font size 200; shorten it" in error


def test_centre_crop_says_the_faces_are_unknown(rig: Rig) -> None:
    assert rig.go() == 0
    clip = rig.clip("A01_tiktok")
    assert clip.crop_mode == "center" and "watermark_faces_unknown" in clip.notes
    assert "watermark_over_face" not in clip.notes


def speaker_rig(project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch, cy: float) -> Rig:
    edit_yaml(project / "config" / "default.yaml", lambda d: d["video"].update(crop="speaker"))
    track_fakes.write_model(project)
    track_fakes.install(
        monkeypatch, lambda t: [track_fakes.RawFace(0.5, cy, 0.1, 0.1, track_fakes.talking(t))]
    )
    return make_rig(project, fake)


def test_a_watermark_over_a_face_is_a_note_and_the_clip_still_renders(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = speaker_rig(project, fake, monkeypatch, 0.18)  # the face is where the watermark is

    assert rig.go() == 0

    clip = rig.clip("A01_tiktok")
    assert clip.crop_mode == "speaker" and "watermark_over_face" in clip.notes
    assert (rig.meta("A01_tiktok.track.json")).is_file()  # tracks live with the campaign clips
    assert not list((rig.run_dir / "clips").glob("*"))  # the track plan makes an empty folder


def test_a_face_away_from_the_watermark_adds_no_note(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = speaker_rig(project, fake, monkeypatch, 0.5)
    assert rig.go() == 0
    clip = rig.clip("A01_tiktok")
    assert "watermark_over_face" not in clip.notes and "watermark_faces_unknown" not in clip.notes


# --- the ffmpeg arguments and the .ass file ------------------------------------------------------


def graph_of(args: list[str]) -> str:
    return args[args.index("-filter_complex") + 1]


def test_the_ffmpeg_arguments_overlay_the_watermark_for_the_whole_clip(rig: Rig) -> None:
    assert rig.go() == 0
    args = rig.args_of("A01_tiktok")
    graph = graph_of(args)

    assert graph.index("subtitles=") < graph.index("overlay=")
    assert "enable=" not in graph and "eof_action=repeat" in graph
    assert "blend=" not in graph and "scale=" in graph.split("overlay=")[0].split("subtitles=")[0]
    assert "-vf" not in args
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert maps == ["[v]", "0:a:0"]
    inputs = [args[i + 1] for i, a in enumerate(args) if a == "-i"]
    wm_file = rig.folder / "assets" / "mark.png"
    assert inputs[1] == ffmpeg.input_url(wm_file) and len(inputs) == 2
    assert not re.search(r"[A-Za-z]:[\/]|\\", graph)  # no drive letter, no backslash
    cwd = next(cwd for clip_id, _, cwd in rig.fake.calls if clip_id == "A01_tiktok")
    assert cwd == rig.run_dir


def test_blend_screen_uses_a_screen_blend_and_the_visible_box(
    project: Path, fake: FakeFfmpeg
) -> None:
    def screen(d: dict[str, Any]) -> None:
        d["watermark"]["blend"] = "screen"

    rig = make_rig(project, fake, mutate=screen, png=black_mark())
    assert rig.go() == 0
    graph = graph_of(rig.args_of("A01_tiktok"))
    assert "blend=all_mode=screen" in graph and "enable=" not in graph
    assert rig.clip("A01_tiktok").watermark.blend == "screen"


def test_the_ass_file_has_captions_on_layers_0_and_1_and_the_hook_for_the_whole_clip(
    rig: Rig,
) -> None:
    assert rig.go() == 0
    clip = rig.clip("A01_tiktok")
    assert clip.duration is not None
    ass = (rig.meta("A01_tiktok.ass")).read_text(encoding="utf-8")
    lines = [line for line in ass.split("\n") if line.startswith("Dialogue:")]
    layers = {int(line.split(",")[0].split(": ")[1]) for line in lines}
    assert layers == {0, 1, 2, 3}
    end = ass_time(clip.duration)
    hook_lines = [line for line in lines if line.startswith(("Dialogue: 2,", "Dialogue: 3,"))]
    assert len(hook_lines) >= 2
    for line in hook_lines:
        assert f",0:00:00.00,{end}," in line
    assert "\\fad" not in "".join(hook_lines)
    assert any(line.startswith("Dialogue: 0,") for line in lines)


def test_hook_mode_intro_shows_the_hook_only_at_the_start(project: Path, fake: FakeFfmpeg) -> None:
    def intro(d: dict[str, Any]) -> None:
        d["hook"]["mode"] = "intro"

    rig = make_rig(project, fake, mutate=intro)
    assert rig.go() == 0
    ass = (rig.meta("A01_tiktok.ass")).read_text(encoding="utf-8")
    hooks = [line for line in ass.split("\n") if line.startswith("Dialogue: 2,")]
    assert hooks and all(",0:00:00.00,0:00:03.00," in line for line in hooks)
    assert "\\fad(0,150)" in ass
    assert rig.clip("A01_tiktok").hook_mode == "intro"


def test_a_campaign_can_set_its_own_hook_seconds_in_intro_mode(
    project: Path, fake: FakeFfmpeg
) -> None:
    def intro(d: dict[str, Any]) -> None:
        d["hook"]["mode"] = "intro"
        d["hook"]["seconds"] = 3

    rig = make_rig(project, fake, mutate=intro)
    assert rig.go() == 0
    ass = (rig.meta("A01_tiktok.ass")).read_text(encoding="utf-8")
    hooks = [line for line in ass.splitlines() if line.startswith("Dialogue: 2,")]
    assert hooks and all(",0:00:00.00,0:00:03.00," in line for line in hooks)


def test_hook_seconds_is_ignored_in_full_mode(project: Path, fake: FakeFfmpeg) -> None:
    def full(d: dict[str, Any]) -> None:
        d["hook"]["seconds"] = 3

    rig = make_rig(project, fake, mutate=full)
    assert rig.go() == 0
    ass = (rig.meta("A01_tiktok.ass")).read_text(encoding="utf-8")
    hooks = [line for line in ass.splitlines() if line.startswith("Dialogue: 2,")]
    assert hooks and not any(",0:00:03.00," in line for line in hooks)


def test_hook_position_top_puts_the_band_at_the_top_of_the_safe_area(
    project: Path, fake: FakeFfmpeg
) -> None:
    def top(d: dict[str, Any]) -> None:
        d["hook"]["position"] = "top"
        d["watermark"]["position"]["y_center"] = 0.62  # out of the hook's way

    rig = make_rig(project, fake, mutate=top)
    assert rig.go() == 0
    clip = rig.clip("A01_tiktok")
    assert clip.hook_position == "top" and "hook_faces_unknown" not in clip.notes
    assert set(clip.checks.values()) == {"pass"}
    ass = (rig.meta("A01_tiktok.ass")).read_text(encoding="utf-8")
    band = next(line for line in ass.splitlines() if line.startswith("Dialogue: 2,"))
    assert band.split("p1}m ")[1].split(" ")[1] == "290"


def test_hook_position_top_fails_the_moment_when_the_watermark_is_in_the_way(
    project: Path, fake: FakeFfmpeg
) -> None:
    rig = make_rig(project, fake, mutate=lambda d: d["hook"].update(position="top"))
    assert rig.go() != 0
    failed = rig.clip("A01_tiktok")
    assert failed.status == "failed" and "the hook overlaps the watermark" in (failed.error or "")
    assert failed.checks["watermark_full_and_clear"] == "fail"


def test_hook_position_avoid_faces_still_notes_unknown_faces(
    project: Path, fake: FakeFfmpeg
) -> None:
    rig = make_rig(project, fake)  # the rig's campaign says avoid_faces
    assert rig.go() == 0
    clip = rig.clip("A01_tiktok")
    assert clip.hook_position == "avoid_faces" and "hook_faces_unknown" in clip.notes


def test_the_clip_json_records_the_watermark_and_the_settings(rig: Rig) -> None:
    assert rig.go() == 0
    clip = rig.clip("A01_tiktok")
    wm = clip.watermark
    assert (wm.x, wm.y, wm.width, wm.height) == (360, 298, 360, 96)
    assert wm.rect == (490.0, 336.0, 590.0, 356.0) and wm.background == "transparent"
    assert wm.color_type == "RGBA" and len(wm.sha256) == 64
    assert clip.hook_font_size == 80 and clip.hook_mode == "full"
    assert clip.settings is not None and clip.settings.hook_font_size == 80
    assert clip.output is not None and clip.output.audio_codec == "aac"
    assert list(clip.checks) == list(CHECKS)
    assert set(clip.checks.values()) == {"pass"}
    assert clip.source_sha256 == "b" * 64


# --- spelling ------------------------------------------------------------------------------------


def test_spelling_fixes_reach_the_captions_and_the_transcript_stays_as_it_was(
    project: Path, fake: FakeFfmpeg
) -> None:
    overrides = {(5, 2): "zorble", (6, 0): "q", (6, 1): "x", (6, 2): "v"}
    rig = make_rig(project, fake, overrides=overrides)
    transcript = rig.run_dir / "transcript.json"
    before = transcript.read_bytes()

    assert rig.go() == 0

    clip = rig.clip("A01_tiktok")
    ass = (rig.meta("A01_tiktok.ass")).read_text(encoding="utf-8")
    assert "Zorb1e" in ass and "QXV" in ass
    assert "zorble" not in ass.lower() and "s6w3" in ass
    assert clip.spelling_replacements == 2
    assert clip.names_to_check == {"Zorb1e": [1.25], "QXV": [5.15]}
    assert transcript.read_bytes() == before


# --- post text and closers -----------------------------------------------------------------------


def three(rig: Rig) -> None:
    rig.moments(
        moment(id="A01", start="0:24", end="0:58", platform="tiktok"),
        moment(id="A02", start="0:10", end="0:40", platform="instagram", caption="a second one."),
        moment(id="A03", start="0:34", end="1:04", platform="youtube", caption="a third one."),
    )


def post(rig: Rig, name: str) -> list[str]:
    text = (rig.txt(name)).read_text(encoding="utf-8")
    assert text.endswith("\n") and "\r" not in text
    return text.rstrip("\n").split("\n")


def test_post_text_is_prefix_caption_then_the_closer(rig: Rig) -> None:
    three(rig)
    assert rig.go() == 0
    assert post(rig, "A01_tiktok") == [f"@demo {CAPTION}", "closer one"]
    assert post(rig, "A02_instagram") == ["a second one.", "closer two"]  # no prefix for it
    # YouTube (Brief 11.2): a title line from the hook, a blank line, then the description
    assert post(rig, "A03_youtube") == [
        "TITLE: An invented hook",
        "",
        "a third one.",
        "closer three",
    ]


def test_hashtags_are_a_line_of_their_own_only_when_given(project: Path, fake: FakeFfmpeg) -> None:
    def tags(d: dict[str, Any]) -> None:
        d["post"]["hashtags"] = ["#one", "#two"]

    rig = make_rig(project, fake, mutate=tags)
    assert rig.go() == 0
    assert post(rig, "A01_tiktok") == [f"@demo {CAPTION}", "#one #two", "closer one"]


def test_a_second_run_continues_the_platforms_rotation_and_never_repeats_its_last_closer(
    rig: Rig,
) -> None:
    three(rig)
    assert rig.go() == 0
    assert rig.go("--only", "A01") == 0  # tiktok's next closer after "closer one"
    assert post(rig, "A01_tiktok")[-1] == "closer two"
    assert rig.used().closers["tiktok"].last == "closer two"
    assert rig.used().closers["instagram"].last == "closer two"  # its own rotation, untouched

    # the list changes so that tiktok's next position points at the closer it just used
    def longer(d: dict[str, Any]) -> None:
        d["post"]["closers"] = ["x", "y", "closer two", "w"]

    edit_yaml(rig.folder / "campaign.yaml", longer)
    assert rig.go("--only", "A01") == 0
    assert post(rig, "A01_tiktok")[-1] == "w"


def test_each_platform_starts_its_rotation_at_its_own_place_in_the_list(rig: Rig) -> None:
    three(rig)
    assert rig.go() == 0
    closers = [rig.clip(n).closer for n in ("A01_tiktok", "A02_instagram", "A03_youtube")]
    assert closers == ["closer one", "closer two", "closer three"]


def test_a_platforms_posts_never_repeat_a_closer_back_to_back(rig: Rig) -> None:
    rig.moments(
        *(moment(id=f"A{n:02d}", start="0:24", end="0:58", platform="youtube") for n in range(1, 8))
    )
    assert rig.go() == 0
    closers = [rig.clip(f"A{n:02d}_youtube").closer for n in range(1, 8)]
    assert all(a != b for a, b in zip(closers, closers[1:], strict=False))
    assert set(closers) == {"closer one", "closer two", "closer three"}


# --- the used log --------------------------------------------------------------------------------


def snapshot(rig: Rig, name: str) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in rig.paths(name) if p.is_file()
    }


def test_a_rendered_moment_is_recorded_unposted(rig: Rig) -> None:
    assert rig.go() == 0
    (entry,) = rig.used().entries
    assert (entry.id, entry.platform, entry.posted_at) == ("A01", "tiktok", None)
    assert entry.output.startswith("runs/_campaigns/demo/") and entry.output.endswith(".mp4")
    assert entry.closer == "closer one" and entry.source_sha256 == "b" * 64
    assert (entry.start, entry.end) == (sentence_start_cut(5), sentence_end_cut(11))


def test_a_posted_moment_is_refused_and_its_files_are_not_touched(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    three(rig)
    assert rig.go() == 0
    assert (
        cli.main(
            ["campaign-posted", "--campaign", "demo", "--id", "A01", "--platform", "tiktok"],
            root=rig.project,
        )
        == 0
    )
    before = snapshot(rig, "A01_tiktok")
    stamps = {p: p.stat().st_mtime_ns for p in rig.paths("A01_tiktok")}
    assert len(stamps) == 4  # mp4, txt, json, ass
    rig.fake.calls.clear()
    capsys.readouterr()

    assert rig.go() == 0  # a full run: refused, not an error

    assert snapshot(rig, "A01_tiktok") == before
    assert {p: p.stat().st_mtime_ns for p in rig.paths("A01_tiktok")} == stamps
    assert rig.fake.ids == ["A02_instagram", "A03_youtube"]
    text = out_of(capsys)
    assert "A01 tiktok: refused: already posted (use --force-reuse to render it again)" in text
    assert "2 rendered, 0 failed, 1 refused" in text


def test_a_refused_moment_named_in_only_is_exit_1(rig: Rig) -> None:
    assert rig.go() == 0
    cli.main(
        ["campaign-posted", "--campaign", "demo", "--id", "A01", "--platform", "tiktok"],
        root=rig.project,
    )
    assert rig.go("--only", "A01") == 1


def test_force_reuse_renders_a_posted_moment_with_a_warning_check(rig: Rig) -> None:
    assert rig.go() == 0
    cli.main(
        ["campaign-posted", "--campaign", "demo", "--id", "A01", "--platform", "tiktok"],
        root=rig.project,
    )

    assert rig.go("--force-reuse") == 0

    clip = rig.clip("A01_tiktok")
    assert clip.checks["not_previously_used"] == "warn"
    entries = rig.used().entries
    assert len(entries) == 2 and entries[0].posted_at is not None and entries[1].posted_at is None


def test_a_moment_overlapping_a_posted_one_renders_with_a_warning(rig: Rig) -> None:
    assert rig.go() == 0
    cli.main(
        ["campaign-posted", "--campaign", "demo", "--id", "A01", "--platform", "tiktok"],
        root=rig.project,
    )
    rig.moments(moment(id="A09", start="0:30", end="1:00", platform="youtube"))  # most of it is A01

    assert rig.go() == 0

    clip = rig.clip("A09_youtube")
    assert clip.status == "rendered" and clip.checks["not_previously_used"] == "warn"
    assert clip.warnings == ["overlaps a posted moment (A01 tiktok)"]


def test_a_moment_that_barely_overlaps_a_posted_one_has_no_warning(rig: Rig) -> None:
    assert rig.go() == 0
    cli.main(
        ["campaign-posted", "--campaign", "demo", "--id", "A01", "--platform", "tiktok"],
        root=rig.project,
    )
    rig.moments(moment(id="A09", start="0:55", end="1:20", platform="youtube"))  # 3 s of 25

    assert rig.go() == 0
    assert rig.clip("A09_youtube").checks["not_previously_used"] == "pass"


# --- finding the run -----------------------------------------------------------------------------


def make_source(project: Path, name: str = "talk.mp4", data: bytes = b"video bytes") -> Path:
    path = project / name
    path.write_bytes(data)
    return path


def test_a_video_file_matching_path_size_and_time_reuses_the_run(
    project: Path, fake: FakeFfmpeg, capsys: pytest.CaptureFixture[str]
) -> None:
    source = make_source(project)
    rig = make_rig(project, fake)
    run_dir = write_run(project, name="earlier", source=source)

    assert rig.go(target=source) == 0

    assert find_run(project.resolve(), source) == (run_dir, False)
    assert f"reusing run folder: {run_dir.resolve()}" in capsys.readouterr().out
    assert (project / "runs" / "_campaigns" / "demo" / "tiktok" / "A01.mp4").is_file()


@pytest.mark.parametrize("what", ["size", "mtime"])
def test_a_changed_size_or_time_is_not_the_same_source(project: Path, what: str) -> None:
    source = make_source(project)
    write_run(project, source=source, **{what: "other"})  # type: ignore[arg-type]
    assert find_run(project.resolve(), source) is None


def test_another_path_is_not_the_same_source(project: Path) -> None:
    source = make_source(project)
    write_run(project, source=source)
    other = make_source(project, "copy.mp4")
    os.utime(other, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns))
    assert find_run(project.resolve(), other) is None


def test_a_run_with_no_recorded_time_matches_on_path_and_size_with_a_warning(
    project: Path, fake: FakeFfmpeg, capsys: pytest.CaptureFixture[str]
) -> None:
    source = make_source(project)
    rig = make_rig(project, fake)
    run_dir = write_run(project, name="old", source=source, mtime="none")
    assert find_run(project.resolve(), source) == (run_dir, True)

    assert rig.go(target=source) == 0

    out = capsys.readouterr().out
    assert "reusing run folder:" in out
    assert "warning: that run has no recorded modified time for its source" in out


def test_a_run_matched_with_the_time_wins_over_a_newer_one_without(project: Path) -> None:
    source = make_source(project)
    with_time = write_run(project, name="a", source=source, created_at="2026-10-01T00:00:00+00:00")
    write_run(
        project, name="b", source=source, mtime="none", created_at="2026-10-09T00:00:00+00:00"
    )
    assert find_run(project.resolve(), source) == (with_time, False)


def test_the_newest_run_wins_among_equals(project: Path) -> None:
    source = make_source(project)
    write_run(project, name="a", source=source, created_at="2026-10-01T00:00:00+00:00")
    newer = write_run(project, name="b", source=source, created_at="2026-10-09T00:00:00+00:00")
    assert find_run(project.resolve(), source) == (newer, False)


def test_a_run_whose_transcribe_is_not_done_is_not_matched(project: Path) -> None:
    source = make_source(project)
    write_run(project, source=source, transcribe_done=False)
    assert find_run(project.resolve(), source) is None


def test_unreadable_runs_are_skipped_quietly(project: Path) -> None:
    source = make_source(project)
    broken = project / "runs" / "broken"
    broken.mkdir()
    (broken / "run.json").write_text("not json", encoding="utf-8")
    good = write_run(project, name="good", source=source)
    assert find_run(project.resolve(), source) == (good, False)


def test_a_run_folder_is_used_as_given(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    assert rig.go() == 0
    assert f"run folder: {rig.run_dir}" in capsys.readouterr().out


def test_a_new_source_makes_a_new_run_with_the_general_client(
    project: Path, fake: FakeFfmpeg, capsys: pytest.CaptureFixture[str]
) -> None:
    source = make_source(project)
    rig = make_rig(project, fake)
    # fetch and transcribe are stubbed in unit tests, so the new run has no inputs to render:
    # the command stops with a clear error, but the run was made for the general client
    assert rig.go(target=source) == 2
    (created,) = (p for p in (project / "runs").iterdir() if p.name not in ("run", "_campaigns"))
    state = json.loads((created / "run.json").read_text(encoding="utf-8"))
    assert state["client"] == "general" and state["input"] == str(source.resolve())
    assert [s["status"] for s in state["stages"].values()][:2] == ["done", "done"]
    assert state["stages"]["candidates"]["status"] == "pending"
    assert f"run folder: {created}" in capsys.readouterr().out


# --- options and errors --------------------------------------------------------------------------


def test_only_selects_the_named_moments(rig: Rig) -> None:
    three(rig)
    assert rig.go("--only", "A02,A03") == 0
    assert not (rig.mp4("A01_tiktok")).exists()
    assert (rig.mp4("A02_instagram")).is_file() and (rig.mp4("A03_youtube")).is_file()


def test_only_keeps_the_source_column_of_the_other_unposted_rows(rig: Rig) -> None:
    three(rig)
    assert rig.go() == 0
    assert rig.go("--only", "A02") == 0  # Brief 12.2: A01 and A03 keep their source name
    text = (rig.out / "posting_plan.csv").read_text(encoding="utf-8")
    rows = list(csv.DictReader(text.splitlines()))
    assert sorted(r["id"] for r in rows) == ["A01", "A02", "A03"]
    assert len({r["source"] for r in rows}) == 1 and rows[0]["source"]


def test_an_unknown_id_in_only_is_exit_2(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    assert rig.go("--only", "Z9") == 2
    assert "no moment with id Z9" in capsys.readouterr().err
    assert not rig.out.exists()


def test_an_unknown_campaign_is_exit_2(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["campaign", str(rig.run_dir), "--campaign", "nothing"], root=rig.project)
    assert code == 2 and "unknown campaign" in capsys.readouterr().err


def test_legacy_caption_style_is_exit_2(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    def legacy(d: dict[str, Any]) -> None:
        d["captions"]["style"] = "legacy"
        d["hook"]["enabled"] = False

    edit_yaml(rig.project / "config" / "default.yaml", legacy)
    assert rig.go() == 2
    assert "box_karaoke" in capsys.readouterr().err
    assert not rig.out.exists()


def test_a_bad_campaign_file_stops_everything_before_a_run_is_made(
    project: Path, fake: FakeFfmpeg, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(d: dict[str, Any]) -> None:
        d["watermark"]["position"] = {"x_center": 0.05, "y_center": 0.05}  # a corner

    rig = make_rig(project, fake, mutate=broken)
    source = make_source(project)
    assert rig.go(target=source) == 2
    assert "corner zone" in capsys.readouterr().err
    assert [p.name for p in (project / "runs").iterdir()] == ["run"]  # no new run folder


def test_the_current_config_is_used_not_the_runs_copy(rig: Rig) -> None:
    edit_yaml(
        rig.project / "config" / "default.yaml", lambda d: d["campaign"].update(max_snap_seconds=0)
    )
    assert rig.go() == 0
    clip = rig.clip("A01_tiktok")
    assert clip.start == 24.0 and clip.snap_notes[0].startswith(START_KEPT_NOTE)


# --- what is printed -----------------------------------------------------------------------------


def test_the_console_never_shows_texts(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    three(rig)
    rig.moments(
        moment(id="A01", start="0:24", end="0:58"),
        moment(id="A02", start="0:24", end="0:30", platform="instagram"),  # fails
        moment(id="A03", start="0:34", end="1:04", platform="youtube", hook="AAAA BBBB CCCC DDDD"),
    )
    rig.go()
    cli.main(
        ["campaign-posted", "--campaign", "demo", "--id", "A01", "--platform", "tiktok"],
        root=rig.project,
    )
    rig.go()
    text = out_of(capsys)
    assert "A01 tiktok" in text
    for secret in (*SECRET_TEXTS, "AAAA BBBB", "Example Campaign"):
        assert secret not in text


def test_the_json_and_the_post_file_hold_no_hook_or_caption_text(rig: Rig) -> None:
    assert rig.go() == 0
    raw = (rig.meta("A01_tiktok.json")).read_text(encoding="utf-8")
    assert HOOK not in raw and CAPTION not in raw and "s5w" not in raw
    assert "closer one" in raw  # the one text it keeps
    assert json.loads(raw)["schema_version"] == 1


def test_used_json_is_written_in_the_campaign_folder(rig: Rig) -> None:
    assert rig.go() == 0
    assert (rig.folder / USED_FILE).is_file()


# --- masking and the sensitive-word check (Brief 11.1) -------------------------------------------


def review(rig: Rig, name: str) -> list[str]:
    path = rig.meta(f"{name}.review.txt")
    return path.read_text(encoding="utf-8").rstrip("\n").split("\n") if path.is_file() else []


def test_a_sensitive_word_in_the_hook_or_caption_is_a_warning_never_a_rewrite(
    project: Path, fake: FakeFfmpeg, capsys: pytest.CaptureFixture[str]
) -> None:
    invented_lists(project)
    rig = make_rig(project, fake)
    rig.moments(
        moment(start="0:24", end="0:58", hook="WHY WUMP MATTERS", caption="says zib invented.")
    )

    assert rig.go() == 0

    clip = rig.clip("A01_tiktok")
    assert clip.status == "rendered" and clip.sensitive_words == 2 and clip.masked_words == 0
    assert "hook_sensitive" in clip.notes and "caption_sensitive" in clip.notes
    assert review(rig, "A01_tiktok") == [
        "sensitive\thook\tWUMP\tflag",
        "sensitive\tpost\tzib\tcontext",
    ]
    assert "WUMP" in (rig.meta("A01_tiktok.ass")).read_text(encoding="utf-8")  # not rewritten
    assert post(rig, "A01_tiktok")[0] == "@demo says zib invented."
    raw = (rig.meta("A01_tiktok.json")).read_text(encoding="utf-8")
    text = out_of(capsys)
    for secret in ("WUMP", "zib"):
        assert secret not in raw and secret not in text


def test_hook_sensitive_fail_fails_the_moment_alone(project: Path, fake: FakeFfmpeg) -> None:
    invented_lists(project)

    def strict(d: dict[str, Any]) -> None:
        d["hook"]["sensitive"] = "fail"

    rig = make_rig(project, fake, mutate=strict)
    rig.moments(
        moment(start="0:24", end="0:58", hook="WHY WUMP MATTERS"),
        moment(id="A02", start="0:10", end="0:40", platform="instagram", caption="zib here."),
    )

    assert rig.go() == 1

    failed = rig.clip("A01_tiktok")
    assert failed.status == "failed" and failed.checks["hook_from_start"] == "fail"
    assert "1 sensitive word(s) (hook.sensitive: fail)" in (failed.error or "")
    assert not (rig.mp4("A01_tiktok")).exists() and review(rig, "A01_tiktok") == []
    other = rig.clip("A02_instagram")  # a caption hit is a warning even with fail
    assert other.status == "rendered" and "caption_sensitive" in other.notes


def test_the_post_caption_is_masked_but_the_closer_and_hashtags_are_not(
    project: Path, fake: FakeFfmpeg
) -> None:
    invented_lists(project)

    def closers(d: dict[str, Any]) -> None:
        d["post"]["closers"] = ["blorp closer", "closer two"]
        d["post"]["hashtags"] = ["#blorp"]

    rig = make_rig(project, fake, mutate=closers)
    rig.moments(moment(start="0:24", end="0:58", caption="says Blorp, then blorping."))

    assert rig.go() == 0

    assert post(rig, "A01_tiktok") == ["@demo says B***, then b***.", "#blorp", "blorp closer"]
    clip = rig.clip("A01_tiktok")
    assert clip.masked_words == 2 and clip.checks["closer_present"] == "pass"
    assert review(rig, "A01_tiktok") == [
        "masked\tpost\tBlorp\tmask",
        "masked\tpost\tblorping\tmask",
    ]


def test_caption_words_are_masked_and_counted_and_a_slur_is_never_written(
    project: Path, fake: FakeFfmpeg
) -> None:
    invented_lists(project)
    rig = make_rig(project, fake, overrides={(5, 2): "blorp", (6, 1): "vexor"})
    rig.moments(moment(start="0:24", end="0:58", hook="WHY BLORP MATTERS"))

    assert rig.go() == 0

    ass = (rig.meta("A01_tiktok.ass")).read_text(encoding="utf-8")
    assert "b***" in ass and "v***" in ass and "B***" in ass
    assert "blorp" not in ass.lower() and "vexor" not in ass.lower()
    clip = rig.clip("A01_tiktok")
    assert clip.masked_words == 3
    assert review(rig, "A01_tiktok") == [
        "masked\tcaptions\tblorp\tmask",
        "masked\tcaptions\t-\tslur",
        "masked\thook\tBLORP\tmask",
    ]
    raw = (rig.meta("A01_tiktok.json")).read_text(encoding="utf-8")
    assert "blorp" not in raw.lower() and "vexor" not in raw.lower()


def test_allowed_extra_masked_and_the_campaigns_own_names_are_respected(
    project: Path, fake: FakeFfmpeg
) -> None:
    invented_lists(project)

    def own(d: dict[str, Any]) -> None:
        d["allowed"] = ["Blorp", "wump"]
        d["extra_masked"] = ["invented", "Zorb1e"]  # Zorb1e is a spelling target: never masked

    rig = make_rig(project, fake, mutate=own, overrides={(5, 2): "blorp", (5, 3): "zorble"})
    rig.moments(moment(start="0:24", end="0:58", hook="WHY WUMP MATTERS"))

    assert rig.go() == 0

    ass = (rig.meta("A01_tiktok.ass")).read_text(encoding="utf-8")
    assert "blorp" in ass and "Zorb1e" in ass
    assert post(rig, "A01_tiktok")[0] == "@demo says something i***."
    clip = rig.clip("A01_tiktok")
    assert clip.masked_words == 1 and clip.sensitive_words == 0
    assert "hook_sensitive" not in clip.notes


def test_masking_off_leaves_the_campaign_texts_alone(project: Path, fake: FakeFfmpeg) -> None:
    invented_lists(project)
    edit_yaml(
        project / "config" / "default.yaml", lambda d: d["captions"].update(mask_profanity=False)
    )
    rig = make_rig(project, fake, overrides={(5, 2): "blorp"})
    rig.moments(moment(start="0:24", end="0:58", hook="WHY WUMP MATTERS", caption="says blorp."))

    assert rig.go() == 0

    assert "blorp" in (rig.meta("A01_tiktok.ass")).read_text(encoding="utf-8")
    assert post(rig, "A01_tiktok")[0] == "@demo says blorp."
    clip = rig.clip("A01_tiktok")
    assert (clip.masked_words, clip.sensitive_words) == (0, 0)
    assert review(rig, "A01_tiktok") == []


# --- names and automatic spelling rules (Brief 11.1) ---------------------------------------------


def with_names(*names: str) -> Callable[[dict[str, Any]], None]:
    def mutate(d: dict[str, Any]) -> None:
        d["names"] = list(names)

    return mutate


def auto_spelling(rig: Rig) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (rig.folder / "auto_spelling.json").read_text(encoding="utf-8")
    )
    return data


def test_name_variants_become_spelling_rules_and_are_recorded(
    project: Path, fake: FakeFfmpeg, capsys: pytest.CaptureFixture[str]
) -> None:
    overrides = {(5, 2): "z0rble", (6, 0): "zor", (6, 1): "ble", (6, 3): "Z0rble,"}
    rig = make_rig(project, fake, mutate=with_names("Zorb1e", "QXV"), overrides=overrides)
    transcript = rig.run_dir / "transcript.json"
    before = transcript.read_bytes()

    assert rig.go() == 0

    ass = (rig.meta("A01_tiktok.ass")).read_text(encoding="utf-8")
    assert ass.count("Zorb1e") >= 3 and "z0rble" not in ass.lower() and "ble" not in ass.split()
    clip = rig.clip("A01_tiktok")
    assert clip.spelling_replacements == 3
    assert clip.names_to_check == {"Zorb1e": [1.25, 5.15, 6.65], "QXV": []}
    assert clip.checks["names_listed"] == "pass"
    assert transcript.read_bytes() == before

    data = auto_spelling(rig)
    by_text = {v["text"]: v for v in data["variants"]}
    assert set(by_text) == {"z0rble", "zor ble"}
    assert by_text["z0rble"]["count"] == 2 and by_text["z0rble"]["status"] == "apply"
    assert by_text["z0rble"]["rule_to"] == "Zorb1e"
    assert by_text["z0rble"]["seen"] == [
        {"clip": "A01_tiktok", "at": 1.25},
        {"clip": "A01_tiktok", "at": 6.65},
    ]
    assert by_text["zor ble"]["seen"] == [{"clip": "A01_tiktok", "at": 5.15}]
    assert data["names"] == ["Zorb1e", "QXV"] and data["source_sha256"] == "b" * 64
    text = out_of(capsys)
    assert "names: 2 automatic spelling rules, 0 suggestions, 0 already in spelling" in text
    assert "z0rble" not in text and "zor ble" not in text
    html = (rig.out / "campaign_report.html").read_text(encoding="utf-8")
    assert "<b>z0rble</b> &rarr; Zorb1e: 2 times" in html and "heard at A01_tiktok 0:01.2" in html


def test_an_explicit_rule_wins_and_an_identity_rule_vetoes(project: Path, fake: FakeFfmpeg) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["names"] = ["Zorb1e"]
        d["spelling"].append({"from": "z0rble", "to": "z0rble"})

    rig = make_rig(project, fake, mutate=mutate, overrides={(5, 2): "z0rble", (6, 0): "zorbie"})

    assert rig.go() == 0

    ass = (rig.meta("A01_tiktok.ass")).read_text(encoding="utf-8")
    assert "z0rble" in ass and "zorbie" not in ass and "Zorb1e" in ass
    by_text = {v["text"]: v for v in auto_spelling(rig)["variants"]}
    assert by_text["z0rble"]["status"] == "explicit" and by_text["z0rble"]["rule_to"] is None
    assert by_text["zorbie"]["status"] == "apply"
    html = (rig.out / "campaign_report.html").read_text(encoding="utf-8")
    assert "Already in your spelling list" in html


def test_a_suggestion_is_listed_but_not_applied(
    project: Path, fake: FakeFfmpeg, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = make_rig(project, fake, mutate=with_names("Frob"), overrides={(5, 2): "frab"})

    assert rig.go() == 0

    assert "frab" in (rig.meta("A01_tiktok.ass")).read_text(encoding="utf-8")
    assert rig.clip("A01_tiktok").spelling_replacements == 0
    by_text = {v["text"]: v for v in auto_spelling(rig)["variants"]}
    assert by_text["frab"]["status"] == "suggest" and by_text["frab"]["seen"] == [
        {"clip": "A01_tiktok", "at": 1.25}
    ]
    assert "names: 0 automatic spelling rules, 1 suggestions" in out_of(capsys)
    html = (rig.out / "campaign_report.html").read_text(encoding="utf-8")
    assert "<b>frab</b> (for Frob): 1 time" in html


def test_without_names_nothing_is_scanned_or_written(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rig.go() == 0
    assert not (rig.folder / "auto_spelling.json").exists()
    assert "names:" not in out_of(capsys)
    assert "Automatic spelling rules" not in (rig.out / "campaign_report.html").read_text("utf-8")


# --- punch-in zooms (Brief R2) -------------------------------------------------------------------
#
# A 2560x1440 source: the standard 640x360 one can never zoom (the upscale guard skips it).


def zoom_rig(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_zoom: dict[str, Any] | None = None,
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> Rig:
    edit_yaml(project / "config" / "default.yaml", lambda d: d["video"].update(crop="speaker"))
    if app_zoom is not None:
        edit_yaml(
            project / "config" / "default.yaml", lambda d: d["video"]["zoom"].update(app_zoom)
        )
    track_fakes.write_model(project)
    track_fakes.install(
        monkeypatch, lambda t: [track_fakes.RawFace(0.5, 0.5, 0.1, 0.1, track_fakes.talking(t))]
    )
    return make_rig(project, fake, mutate=mutate, width=2560, height=1440)


def crop_w_values(rig: Rig) -> list[str]:
    text = (rig.meta("A01_tiktok.crop.cmd")).read_text(encoding="utf-8")
    return re.findall(r"crop w (\d+)", text)


def test_a_zoom_in_the_app_config_shows_in_the_json_the_line_the_graph_and_the_cmd_file(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig = zoom_rig(project, fake, monkeypatch, app_zoom={"enabled": True})

    assert rig.go() == 0

    clip = rig.clip("A01_tiktok")
    assert clip.zoom_count >= 1 and len(clip.zoom_spans) == clip.zoom_count
    assert clip.settings is not None and clip.settings.zoom_enabled
    assert clip.settings.zoom_scale == 1.15
    assert "zooms" in out_of(capsys)
    assert graph_of(rig.args_of("A01_tiktok")).startswith("[0:v:0]sendcmd=")
    assert crop_w_values(rig)


def test_a_campaign_zoom_scale_overrides_the_app_scale_and_changes_the_crop(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = zoom_rig(project, fake, monkeypatch, app_zoom={"enabled": True})
    assert rig.go() == 0
    plain = crop_w_values(rig)
    plain_graph = graph_of(rig.args_of("A01_tiktok"))

    def scale(d: dict[str, Any]) -> None:
        d["video"] = {"zoom": {"scale": 1.5}}

    other = zoom_rig(project, fake, monkeypatch, app_zoom={"enabled": True}, mutate=scale)
    assert other.go() == 0
    clip = other.clip("A01_tiktok")
    assert clip.settings is not None and clip.settings.zoom_enabled
    assert clip.settings.zoom_scale == 1.5  # merged: the campaign's scale, the app's other keys
    assert clip.settings.zoom_loud_pct == 0.30
    assert crop_w_values(other) != plain or graph_of(other.args_of("A01_tiktok")) != plain_graph


def test_the_campaign_can_switch_zoom_on_while_the_app_config_has_it_off(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def on(d: dict[str, Any]) -> None:
        d["video"] = {"zoom": {"enabled": True}}

    rig = zoom_rig(project, fake, monkeypatch, mutate=on)  # video.zoom.enabled is false in the app

    assert rig.go() == 0

    clip = rig.clip("A01_tiktok")
    assert clip.zoom_count >= 1 and clip.zoom_spans
    assert clip.settings is not None and clip.settings.zoom_enabled
    assert "zooms" in out_of(capsys)
    assert graph_of(rig.args_of("A01_tiktok")).startswith("[0:v:0]sendcmd=")


def test_with_zoom_off_everywhere_the_line_and_the_json_say_nothing_about_zooms(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig = zoom_rig(project, fake, monkeypatch)

    assert rig.go() == 0

    clip = rig.clip("A01_tiktok")
    assert clip.zoom_count == 0 and clip.zoom_spans == []
    assert clip.settings is not None and not clip.settings.zoom_enabled
    assert "zooms" not in out_of(capsys)


def test_a_moment_without_a_hook_fails_with_a_clear_reason(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.moments(moment(id="A01", start="0:24", end="0:58", hook=""))
    assert rig.go() == 1
    assert "A01 tiktok: FAILED: no hook in moments.yaml; write one" in out_of(capsys)


# --- Brief 12.1: the graphics-aware fit layout ---------------------------------------------------


def fit_rig(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutate: Callable[[dict[str, Any]], None] | None = None,
    scenario: Any = None,
) -> Rig:
    import graphics_fakes

    edit_yaml(project / "config" / "default.yaml", lambda d: d["video"].update(crop="speaker"))
    edit_yaml(
        project / "config" / "default.yaml",
        lambda d: d["video"]["graphics"].update(enabled=True),
    )
    track_fakes.write_model(project)
    graphics_fakes.write_text_model(project)
    fakes = track_fakes.install(
        monkeypatch, lambda t: [track_fakes.RawFace(0.75, 0.5, 0.1, 0.1, track_fakes.talking(t))]
    )
    graphics_fakes.install(
        monkeypatch,
        scenario or graphics_fakes.panel_between(0.0, 20.0),
        track_fakes=fakes,
    )
    return make_rig(project, fake, mutate=mutate, width=2560, height=1440)


def test_a_fit_shot_renders_through_the_fit_graph_with_the_watermark_on_top(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig = fit_rig(project, fake, monkeypatch)

    assert rig.go() == 0

    clip = rig.clip("A01_tiktok")
    assert clip.fit_spans and clip.fit_seconds > 0 and "graphics_fit" in clip.notes
    assert [b.y1 for b in clip.static_overlays] == [0.04, 0.91]  # the title and the logo
    assert clip.settings is not None and clip.settings.graphics_enabled
    graph = graph_of(rig.args_of("A01_tiktok"))
    assert graph.startswith("[0:v:0]split=3[fit_src][fit_frame][fit_fill];")
    assert "[base];" in graph and graph.endswith("[v]")  # the watermark wraps the fit graph
    assert rig.meta("A01_tiktok.graphics.json").is_file()
    out = out_of(capsys)
    assert "fit " in out and "static overlays" in out
    assert "fit layout" in (rig.out / "campaign_report.html").read_text(encoding="utf-8")


def test_a_campaign_may_switch_the_fit_layout_off(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    def off(d: dict[str, Any]) -> None:
        d["video"] = {"graphics": {"enabled": False}}

    rig = fit_rig(project, fake, monkeypatch, mutate=off)
    assert rig.go() == 0
    clip = rig.clip("A01_tiktok")
    assert clip.fit_spans == [] and clip.settings is not None
    assert clip.settings.graphics_enabled is False
    assert not rig.meta("A01_tiktok.graphics.json").exists()
    assert "fit_src" not in graph_of(rig.args_of("A01_tiktok"))


def test_a_smooth_zoom_puts_its_ramps_in_the_clip_json(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = zoom_rig(project, fake, monkeypatch, app_zoom={"enabled": True})

    assert rig.go() == 0

    clip = rig.clip("A01_tiktok")
    assert clip.settings is not None
    assert (clip.settings.zoom_style, clip.settings.zoom_ramp_s) == ("smooth", 0.4)
    assert clip.zoom_ramps and all(0 <= a < b for a, b in clip.zoom_ramps)
    assert len(crop_w_values(rig)) > 10  # one line per frame in a ramp


def test_a_campaign_can_choose_the_punch_style_and_its_own_ramp_length(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    def punch(d: dict[str, Any]) -> None:
        d["video"] = {"zoom": {"style": "punch"}}

    rig = zoom_rig(project, fake, monkeypatch, app_zoom={"enabled": True}, mutate=punch)
    assert rig.go() == 0
    clip = rig.clip("A01_tiktok")
    assert clip.settings is not None and clip.settings.zoom_style == "punch"
    assert clip.zoom_ramps == [] and clip.zoom_count >= 1

    def longer(d: dict[str, Any]) -> None:
        d["video"] = {"zoom": {"ramp_s": 0.8}}

    other = zoom_rig(project, fake, monkeypatch, app_zoom={"enabled": True}, mutate=longer)
    assert other.go() == 0
    ramps = other.clip("A01_tiktok").zoom_ramps
    assert ramps and ramps[0][1] - ramps[0][0] == pytest.approx(0.8)


# --- per-platform outputs, auto assignment and the posting plan (Brief 11.2) ---------------------


def auto(**fields: Any) -> dict[str, Any]:
    entry = moment(**fields)
    del entry["platform"]  # left out: auto
    return entry


def test_platform_folders_hold_only_the_mp4_and_the_text_and_meta_holds_the_rest(rig: Rig) -> None:
    three(rig)
    assert rig.go() == 0
    for platform, moment_id in (("tiktok", "A01"), ("instagram", "A02"), ("youtube", "A03")):
        names = sorted(p.name for p in (rig.out / platform).iterdir())
        assert names == [f"{moment_id}.mp4", f"{moment_id}.txt"]
    meta = sorted(p.name for p in rig.meta_dir.iterdir())
    assert meta == [
        "A01_tiktok.ass",
        "A01_tiktok.json",
        "A02_instagram.ass",
        "A02_instagram.json",
        "A03_youtube.ass",
        "A03_youtube.json",
    ]
    top = sorted(p.name for p in rig.out.iterdir())
    assert top == [
        "_meta",
        "campaign_report.html",
        "instagram",
        "posting_plan.csv",
        "tiktok",
        "youtube",
    ]
    assert rig.used().entries[1].output == "runs/_campaigns/demo/instagram/A02.mp4"


def test_auto_moments_are_spread_over_the_platforms_and_locked(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.moments(
        auto(id="A01", start="0:24", end="0:58"),
        auto(id="A02", start="0:10", end="0:40"),
        auto(id="A03", start="0:34", end="1:04"),
        auto(id="A04", start="0:05", end="0:35"),
    )

    assert rig.go() == 0

    assert rig.fake.ids == ["A01_tiktok", "A02_instagram", "A03_youtube", "A04_tiktok"]
    lock = json.loads((rig.folder / "assignments.json").read_text(encoding="utf-8"))
    assert lock["assigned"] == {
        "A01": "tiktok",
        "A02": "instagram",
        "A03": "youtube",
        "A04": "tiktok",
    }
    text = out_of(capsys)
    assert (
        "platforms: tiktok 2, instagram 1, youtube 1 (4 auto; locked in assignments.json)" in text
    )
    assert "A02 instagram: rendered" in text
    html = (rig.out / "campaign_report.html").read_text(encoding="utf-8")
    assert "Platforms: tiktok 2 &middot; instagram 1 &middot; youtube 1 moments" in html

    # a second run with the moments reordered changes nothing
    rig.moments(
        auto(id="A04", start="0:05", end="0:35"),
        auto(id="A03", start="0:34", end="1:04"),
        auto(id="A02", start="0:10", end="0:40"),
        auto(id="A01", start="0:24", end="0:58"),
    )
    rig.fake.calls.clear()
    assert rig.go() == 0
    assert sorted(rig.fake.ids) == ["A01_tiktok", "A02_instagram", "A03_youtube", "A04_tiktok"]


def test_a_posted_auto_moment_keeps_its_platform_and_is_refused(rig: Rig) -> None:
    rig.moments(auto(id="A01", start="0:24", end="0:58"), auto(id="A02", start="0:10", end="0:40"))
    assert rig.go() == 0
    assert cli.main(["campaign-posted", "--campaign", "demo", "--id", "A02"], root=rig.project) == 0
    (rig.folder / "assignments.json").unlink()  # even without the lock
    rig.fake.calls.clear()

    assert rig.go() == 0

    assert rig.fake.ids == ["A01_tiktok"]
    (entry,) = (e for e in rig.used().entries if e.posted_at)
    assert (entry.id, entry.platform) == ("A02", "instagram")
    lock = json.loads((rig.folder / "assignments.json").read_text(encoding="utf-8"))
    assert lock["assigned"]["A02"] == "instagram"


def test_a_failed_moment_leaves_nothing_in_its_platform_folder(rig: Rig) -> None:
    rig.moments(moment(id="A01", start="0:24", end="0:30", platform="youtube"))  # too short
    assert rig.go() == 1
    assert list((rig.out / "youtube").iterdir()) == []
    assert rig.clip("A01_youtube").status == "failed"


def test_the_posting_plan_is_written_with_the_rendered_unposted_clips(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    three(rig)
    assert rig.go() == 0
    assert cli.main(["campaign-posted", "--campaign", "demo", "--id", "A02"], root=rig.project) == 0
    assert rig.go() == 0

    path = rig.out / "posting_plan.csv"
    assert f"posting plan: {path}" in out_of(capsys)
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    assert [(r["id"], r["platform"], r["posted"]) for r in rows] == [
        ("A02", "instagram", "yes"),
        ("A01", "tiktok", "no"),
        ("A03", "youtube", "no"),
    ]
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert rows[1]["date"] == tomorrow and rows[1]["time_et"] == "12:30 PM"
    assert rows[1]["file"] == "tiktok/A01.mp4" and rows[1]["text_file"] == "tiktok/A01.txt"
    # the posted row: campaign-posted recorded A02 at its slot (tomorrow's first time), and the
    # row keeps that slot until Igor corrects it
    assert rows[0]["date"] == tomorrow and rows[0]["time_et"] == "12:30 PM"
    assert "views_48h" in rows[0] and "notes" in rows[0]
    for secret in SECRET_TEXTS:
        assert secret not in path.read_text(encoding="utf-8")


def test_the_youtube_text_has_a_title_and_the_closer_check_still_passes(rig: Rig) -> None:
    rig.moments(moment(id="A03", start="0:34", end="1:04", platform="youtube", caption="a third."))
    assert rig.go() == 0
    assert post(rig, "A03_youtube") == ["TITLE: An invented hook", "", "a third.", "closer three"]
    assert rig.clip("A03_youtube").checks["closer_present"] == "pass"


def test_old_flat_files_are_left_alone_and_listed_in_the_report(rig: Rig) -> None:
    rig.out.mkdir(parents=True)
    old = rig.out / "A01_tiktok.mp4"
    old.write_bytes(b"old render")
    (rig.out / "A01_tiktok.post.txt").write_text("old text\n", encoding="utf-8")
    assert rig.go() == 0
    assert old.read_bytes() == b"old render" and rig.mp4("A01_tiktok").is_file()
    html = (rig.out / "campaign_report.html").read_text(encoding="utf-8")
    assert "Old files from before the per-platform folders" in html
    assert "<li>A01_tiktok.mp4</li><li>A01_tiktok.post.txt</li>" in html


# --- no watermark, no closers (Brief 12) ---------------------------------------------------------


def bare(d: dict[str, Any]) -> None:
    """A campaign that requires neither a watermark nor a closer line."""
    del d["watermark"]
    d["post"]["closers"] = []
    d["post"]["hashtags"] = ["#one"]


def test_a_campaign_without_a_watermark_or_closers_renders_with_one_input_and_no_overlay(
    project: Path, fake: FakeFfmpeg, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = make_rig(project, fake, mutate=bare)
    three(rig)

    assert rig.go() == 0

    args = rig.args_of("A01_tiktok")
    assert [args[i + 1] for i, a in enumerate(args) if a == "-i"] == [
        ffmpeg.input_url(rig.run_dir / "source.mp4")
    ]
    assert "-vf" in args and "-filter_complex" not in args and "overlay=" not in " ".join(args)
    clip = rig.clip("A01_tiktok")
    assert clip.watermark is None and clip.closer is None
    assert clip.checks["watermark_full_and_clear"] == "n/a"
    assert clip.checks["closer_present"] == "n/a"
    assert (
        "watermark_faces_unknown" not in clip.notes and "hook_moved_for_overlay" not in clip.notes
    )
    assert post(rig, "A01_tiktok") == [f"@demo {CAPTION}", "#one"]
    assert post(rig, "A03_youtube") == ["TITLE: An invented hook", "", "a third one.", "#one"]
    assert all(e.closer is None for e in rig.used().entries)
    assert rig.used().closers == {}
    text = out_of(capsys)
    assert "watermark: none required by this campaign" in text
    assert "closer" not in text.split("A01 tiktok: rendered")[1].split("\n")[0]
    html = (rig.out / "campaign_report.html").read_text(encoding="utf-8")
    assert "No watermark required by this campaign" in html
    assert html.count("&ndash; not required</span>") == 6  # two checks on three clips
    assert "Closer:" not in html
    assert not (rig.folder / "auto_position.json").exists()


def test_a_campaign_with_a_watermark_but_no_closers_keeps_the_overlay(
    project: Path, fake: FakeFfmpeg
) -> None:
    rig = make_rig(project, fake, mutate=lambda d: d["post"].update(closers=[]))
    assert rig.go() == 0
    args = rig.args_of("A01_tiktok")
    assert "-filter_complex" in args and len([a for a in args if a == "-i"]) == 2
    clip = rig.clip("A01_tiktok")
    assert clip.watermark is not None and clip.checks["watermark_full_and_clear"] == "pass"
    assert clip.checks["closer_present"] == "n/a" and post(rig, "A01_tiktok") == [
        f"@demo {CAPTION}"
    ]


def test_a_campaign_without_a_watermark_still_fails_a_hook_outside_the_safe_area(
    project: Path, fake: FakeFfmpeg
) -> None:
    def big(d: dict[str, Any]) -> None:
        bare(d)
        d["hook"]["font_size"] = 200

    rig = make_rig(project, fake, mutate=big)
    rig.moments(moment(start="0:24", end="0:58", hook="AAAA BBBB CCCC DDDD EEEE FFFF"))
    assert rig.go() == 1
    assert "the hook does not fit at font size 200" in (rig.clip("A01_tiktok").error or "")


# --- several source videos in one campaign (Brief 12) --------------------------------------------


def second_source(project: Path, rig: Rig) -> Path:
    """A second source file with its own finished run folder (its words are t<k>w<j>)."""
    source = make_source(project, "second.mp4", b"other video bytes")
    write_run(project, name="second", source=source, overrides={(5, 2): "glimmer"})
    return source


def test_moments_from_two_sources_render_into_the_same_campaign_folder(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    source = second_source(rig.project, rig)
    rig.moments(
        moment(id="A01", start="0:24", end="0:58"),
        moment(id="A02", start="0:24", end="0:58", platform="instagram", source="second.mp4"),
        moment(id="A03", start="0:34", end="1:04", platform="youtube", source=str(source)),
    )

    assert rig.go() == 0

    assert rig.fake.ids == ["A01_tiktok", "A02_instagram", "A03_youtube"]
    assert rig.mp4("A01_tiktok").is_file() and rig.mp4("A02_instagram").is_file()
    assert rig.mp4("A03_youtube").is_file()
    text = out_of(capsys)
    assert "sources: 2 videos" in text
    assert f"run folder: {rig.run_dir}" in text and "reusing run folder:" in text
    assert text.index("A01 tiktok: rendered") < text.index("A02 instagram: rendered")
    cwds = {clip_id: cwd for clip_id, _, cwd in rig.fake.calls}
    assert cwds["A01_tiktok"] == rig.run_dir and cwds["A02_instagram"].name == "second"
    entries = {e.id: e for e in rig.used().entries}
    assert entries["A01"].source_name == "original.mp4" and entries["A02"].source_sha256 == "b" * 64
    rows = list(
        csv.DictReader((rig.out / "posting_plan.csv").read_text(encoding="utf-8").splitlines())
    )
    assert [(r["id"], r["source"]) for r in rows] == [
        ("A01", "original.mp4"),
        ("A02", "original.mp4"),
        ("A03", "original.mp4"),
    ]  # both runs were fetched from a file called original.mp4 in this rig
    assert "run, second" in (rig.out / "campaign_report.html").read_text(encoding="utf-8")


def test_a_missing_source_names_the_moment_and_renders_nothing(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.moments(moment(id="A01", start="0:24", end="0:58", source="nowhere/missing.mp4"))
    assert rig.go() == 2
    assert "moment A01: source: input file not found" in capsys.readouterr().err
    assert rig.fake.calls == []


def test_an_unsupported_source_type_is_refused(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    (rig.project / "notes.txt").write_text("x", encoding="utf-8")
    rig.moments(moment(id="A01", start="0:24", end="0:58", source="notes.txt"))
    assert rig.go() == 2
    assert "moment A01: source: unsupported input type" in capsys.readouterr().err


# --- Brief 12.2: a clip whose entry did not change is kept by `produce` ------------------------


def pass_again(rig: Rig) -> dict[str, int]:
    from clipmachine.campaign.run import open_campaign, render_moments

    opened = open_campaign(rig.project, "demo", quiet=True)
    outcome = render_moments(
        rig.project, opened, opened.moments, rig.run_dir, force_reuse=False, skip_unchanged=True
    )
    return outcome.counts


def test_an_unchanged_moment_is_kept_and_a_changed_one_is_rendered_again(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    three(rig)
    assert rig.go() == 0
    assert all(e.spec_sha256 for e in rig.used().entries)
    calls = len(rig.fake.calls)

    counts = pass_again(rig)
    assert counts["unchanged"] == 3 and counts["rendered"] == 0
    assert len(rig.fake.calls) == calls
    assert "A01 tiktok: unchanged since the last render, kept" in out_of(capsys)

    rig.moments(
        moment(id="A01", start="0:24", end="0:58", platform="tiktok", hook="ANOTHER HOOK"),
        moment(id="A02", start="0:12", end="0:40", platform="instagram", caption="a second one."),
        moment(id="A03", start="0:34", end="1:04", platform="youtube", caption="a third one."),
    )
    counts = pass_again(rig)
    assert counts["rendered"] == 2 and counts["unchanged"] == 1  # the hook and the start changed
    assert len(rig.fake.calls) == calls + 2


def test_a_missing_file_or_an_entry_from_before_12_2_is_rendered_again(rig: Rig) -> None:
    three(rig)
    assert rig.go() == 0
    rig.mp4("A01_tiktok").unlink()
    data = json.loads((rig.folder / USED_FILE).read_text(encoding="utf-8"))
    for entry in data["entries"]:
        if entry["id"] == "A02":
            del entry["spec_sha256"]  # a used.json written before Brief 12.2
    (rig.folder / USED_FILE).write_text(json.dumps(data), encoding="utf-8")

    counts = pass_again(rig)
    assert counts["rendered"] == 2 and counts["unchanged"] == 1
    assert rig.mp4("A01_tiktok").is_file()


def test_a_posted_moment_is_refused_not_kept_as_unchanged(rig: Rig) -> None:
    three(rig)
    assert rig.go() == 0
    assert cli.main(["campaign-posted", "--campaign", "demo", "--id", "A02"], root=rig.project) == 0
    counts = pass_again(rig)
    assert counts == {"rendered": 0, "failed": 0, "refused": 1, "rejected": 0, "unchanged": 2}
