"""The render stage with ffmpeg and ffprobe faked. All data is synthetic."""

import hashlib
import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import track_fakes
from clipmachine import pipeline, tracking
from clipmachine.config import (
    ResolvedConfig,
    dump_resolved,
    load_app_config,
    load_resolved,
    load_resolved_file,
)
from clipmachine.context import RunContext
from clipmachine.models import (
    TRACK_SCHEMA_VERSION,
    AudioStreamInfo,
    FetchMeta,
    MediaInfo,
    RenderedClip,
    ScoreConfig,
    ScoredClip,
    ScoredFile,
    ScoredSummary,
    TrackFile,
    TranscribeConfig,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
    VideoStreamInfo,
    ZoomConfig,
)
from clipmachine.pipeline import load_state, run_pipeline, start_new_run
from clipmachine.runs import RunError, write_json_atomic
from clipmachine.stages import render
from clipmachine.utils import ffmpeg
from clipmachine.utils.crop import CropBox, frame_path
from clipmachine.utils.ffmpeg import MediaError
from clipmachine.utils.zoom import ZoomSpan, zoom_box, zoomed_path
from helpers import edit_yaml

DRIVE_OR_BACKSLASH = re.compile(r"(?<![A-Za-z0-9_.])[A-Za-z]:|\\")
SOURCE_SECONDS = 120.0
# id, rank (None = not kept), start, end
CLIPS = [("c01", 1, 10.0, 30.0), ("c02", 2, 40.0, 62.5), ("c03", None, 70.0, 95.0)]


def scored_clip(clip_id: str, rank: int | None, start: float, end: float) -> ScoredClip:
    kept = rank is not None
    return ScoredClip(
        id=clip_id,
        rank=rank,
        kept=kept,
        score=8 if kept else 3,
        verdict="keep" if kept else "reject",
        reject_reason=None if kept else "weak_hook",
        dropped_by=None,
        hook_line="synthetic hook",
        suggested_title="synthetic title",
        reason="synthetic reason",
        start=start,
        end=end,
        candidate_start=start,
        candidate_end=end,
        trimmed=False,
        notes=[],
    )


def write_scored(run_dir: Path, clips: list[tuple[str, int | None, float, float]]) -> None:
    items = [scored_clip(*c) for c in clips]
    summary = ScoredSummary(
        candidates=len(items),
        kept=sum(1 for c in items if c.kept),
        rejected=sum(1 for c in items if not c.kept),
        dropped_min_score=0,
        dropped_top_n=0,
        trimmed=0,
        reject_reasons={},
        model="m",
        effort="medium",
        thinking="between_tools",
        min_score=6,
        keep_top=8,
        chunks=1,
        calls=1,
        input_tokens=0,
        output_tokens=0,
        thinking_tokens=0,
        estimated_usd=0.0,
        actual_usd=0.0,
    )
    scored = ScoredFile(settings=ScoreConfig(), summary=summary, clips=items, warnings=[])
    write_json_atomic(run_dir / "scored.json", scored.model_dump(mode="json"))


def write_inputs(
    run_dir: Path,
    clips: list[tuple[str, int | None, float, float]] = CLIPS,
    *,
    width: int = 640,
    height: int = 360,
    rotation: int = 0,
) -> None:
    video = VideoStreamInfo(
        codec="h264",
        width=width,
        height=height,
        avg_frame_rate="30/1",
        r_frame_rate="30/1",
        rotation=rotation,
        variable_frame_rate=False,
    )
    meta = FetchMeta(
        original_filename="original.mp4",
        source_file="source.mp4",
        sha256="0" * 64,
        size_bytes=4,
        media=MediaInfo(
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            duration_seconds=SOURCE_SECONDS,
            size_bytes=4,
            video=video,
            audio=AudioStreamInfo(codec="aac", sample_rate=48000, channels=2),
        ),
        audio_file="audio.wav",
        ffmpeg_version="ffmpeg version test",
        fetched_at="2026-10-10T00:00:00+00:00",
        fetch_seconds=0.1,
    )
    write_json_atomic(run_dir / "meta.json", meta.model_dump(mode="json"))
    (run_dir / "source.mp4").write_bytes(b"fake")
    words = [
        TranscriptWord(start=float(t), end=t + 0.4, word=f" w{t}", probability=0.9)
        for t in range(0, 100, 2)
    ]
    transcript = Transcript(
        model="small",
        device="cpu",
        compute_type="int8",
        settings=TranscribeConfig(model="small"),
        language="en",
        language_probability=0.99,
        audio_duration_seconds=SOURCE_SECONDS,
        transcribe_seconds=1.0,
        realtime_factor=100.0,
        segments=[
            TranscriptSegment(
                id=0,
                start=0.0,
                end=100.0,
                text="",
                avg_logprob=0.0,
                no_speech_prob=0.0,
                words=words,
            )
        ],
        warnings=[],
    )
    write_json_atomic(run_dir / "transcript.json", transcript.model_dump(mode="json"))
    write_scored(run_dir, clips)


@dataclass
class FakeFfmpeg:
    """Stands in for ffmpeg.render_clip and ffmpeg.probe."""

    calls: list[tuple[str, list[str], Path]] = field(default_factory=list)
    fail_on: set[str] = field(default_factory=set)
    durations: dict[str, float] = field(default_factory=dict)

    def render_clip(self, args: list[str], cwd: Path, duration_seconds: float) -> None:
        out = Path(args[-1])
        clip_id = out.name.removesuffix(".tmp.mp4")
        self.calls.append((clip_id, args, cwd))
        out.write_bytes(b"fake mp4")  # a temp file exists even if the encode then fails
        if clip_id in self.fail_on:
            raise MediaError("ffmpeg render failed: boom")
        self.durations[clip_id] = float(args[args.index("-t") + 1])

    def probe(self, path: Path) -> MediaInfo:
        clip_id = path.name.removesuffix(".tmp.mp4").removesuffix(".mp4")
        return MediaInfo(
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            duration_seconds=self.durations[clip_id],
            size_bytes=8,
            video=VideoStreamInfo(
                codec="h264",
                width=1080,
                height=1920,
                avg_frame_rate="30/1",
                r_frame_rate="30/1",
                rotation=0,
                variable_frame_rate=False,
            ),
            audio=AudioStreamInfo(codec="aac", sample_rate=48000, channels=2),
        )

    @property
    def ids(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeFfmpeg:
    fake = FakeFfmpeg()
    monkeypatch.setattr(render.ffmpeg, "render_clip", fake.render_clip)
    monkeypatch.setattr(render.ffmpeg, "probe", fake.probe)
    return fake


def make_ctx(
    project: Path, *, resuming: bool = False, run_name: str = "run", style: str = "legacy"
) -> RunContext:
    run_dir = project / "runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    resolved = load_resolved(project, "example-client")
    # The shipped default is the box_karaoke style with the hook on (Brief R1). The tests that
    # assert the older caption text and settings are pinned to `legacy` here; those of the new
    # style pass style="box_karaoke" and get the shipped values.
    # The shipped default offset is 0.15; these tests assume captions at their spoken time.
    updates: dict[str, Any] = {"offset_seconds": 0.0}
    update: dict[str, Any] = {}
    if style == "legacy":
        updates.update(style="legacy", font_size=80)
        update["hook"] = resolved.app.hook.model_copy(update={"enabled": False})
    captions = resolved.app.captions.model_copy(update=updates)
    app = resolved.app.model_copy(update={"captions": captions, **update})
    return RunContext(
        run_dir=run_dir,
        config=ResolvedConfig(app=app, client_name=resolved.client_name, rules=resolved.rules),
        input_path=run_dir / "x.mp4",
        project_root=project,
        resuming=resuming,
    )


@pytest.fixture
def ctx(project: Path) -> RunContext:
    context = make_ctx(project)
    write_inputs(context.run_dir)
    return context


def clip_files(run_dir: Path) -> list[str]:
    folder = run_dir / "clips"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def resume(ctx: RunContext) -> RunContext:
    return RunContext(
        run_dir=ctx.run_dir,
        config=ctx.config,
        input_path=ctx.input_path,
        project_root=ctx.project_root,
        resuming=True,
    )


# --- what is rendered ---------------------------------------------------------------------


def test_only_kept_clips_are_rendered(ctx: RunContext, fake: FakeFfmpeg) -> None:
    render.run(ctx)

    assert fake.ids == ["c01", "c02"]  # rank order; c03 was not kept
    assert clip_files(ctx.run_dir) == [
        "c01.ass",
        "c01.json",
        "c01.mp4",
        "c02.ass",
        "c02.json",
        "c02.mp4",
    ]


def test_clip_json_records_the_facts_and_no_text(ctx: RunContext, fake: FakeFfmpeg) -> None:
    render.run(ctx)

    raw = (ctx.run_dir / "clips" / "c02.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert (data["id"], data["rank"], data["score"]) == ("c02", 2, 8)
    assert (data["start"], data["end"], data["duration"]) == (40.0, 62.5, 22.5)
    assert data["source"] == {
        "width": 640,
        "height": 360,
        "rotation": 0,
        "r_frame_rate": "30/1",
        "avg_frame_rate": "30/1",
        "variable_frame_rate": False,
    }
    assert data["crop"] == {"w": 202, "h": 360, "x": 218, "y": 0}
    assert data["output"]["width"] == 1080 and data["output"]["height"] == 1920
    assert data["output"]["video_codec"] == "h264" and data["output"]["audio_codec"] == "aac"
    assert data["caption_events"] > 0
    assert "synthetic" not in raw  # no hook, title or reason
    assert "w40" not in raw  # no transcript words


def test_ass_file_holds_the_clip_words_shifted_to_zero(ctx: RunContext, fake: FakeFfmpeg) -> None:
    render.run(ctx)

    text = (ctx.run_dir / "clips" / "c02.ass").read_text(encoding="utf-8")
    assert "PlayResX: 1080" in text and "PlayResY: 1920" in text
    assert "Dialogue: 0,0:00:00.00," in text  # w40 starts the clip
    assert "w40" in text and "w62" in text  # words of 40-62.5 s
    assert "w64" not in text and "w38" not in text


GOLDEN_LEGACY_ASS = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "PlayResX: 1080\n"
    "PlayResY: 1920\n"
    "WrapStyle: 0\n"
    "ScaledBorderAndShadow: yes\n"
    "\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
    "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: Clip,Montserrat,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
    "-1,0,0,0,100,100,0,0,1,4,0,2,60,60,350,1\n"
    "\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "Dialogue: 0,0:00:00.00,0:00:00.40,Clip,,0,0,0,,{\\c&H00D4FF&}w10{\\r} w12 w14\n"
    "Dialogue: 0,0:00:02.00,0:00:02.40,Clip,,0,0,0,,w10 {\\c&H00D4FF&}w12{\\r} w14\n"
    "Dialogue: 0,0:00:04.00,0:00:04.40,Clip,,0,0,0,,w10 w12 {\\c&H00D4FF&}w14{\\r}\n"
    "Dialogue: 0,0:00:06.00,0:00:06.40,Clip,,0,0,0,,{\\c&H00D4FF&}w16{\\r} w18 w20\n"
    "Dialogue: 0,0:00:08.00,0:00:08.40,Clip,,0,0,0,,w16 {\\c&H00D4FF&}w18{\\r} w20\n"
    "Dialogue: 0,0:00:10.00,0:00:10.40,Clip,,0,0,0,,w16 w18 {\\c&H00D4FF&}w20{\\r}\n"
    "Dialogue: 0,0:00:12.00,0:00:12.40,Clip,,0,0,0,,{\\c&H00D4FF&}w22{\\r} w24 w26\n"
    "Dialogue: 0,0:00:14.00,0:00:14.40,Clip,,0,0,0,,w22 {\\c&H00D4FF&}w24{\\r} w26\n"
    "Dialogue: 0,0:00:16.00,0:00:16.40,Clip,,0,0,0,,w22 w24 {\\c&H00D4FF&}w26{\\r}\n"
    "Dialogue: 0,0:00:18.00,0:00:18.40,Clip,,0,0,0,,{\\c&H00D4FF&}w28{\\r}\n"
)  # the .ass the stage wrote for c01 before Brief R1; it must never change


def _legacy_yaml(explicit: bool) -> Callable[[dict[str, Any]], None]:
    """Edit default.yaml to the pre-R1 shape: none of the new keys, or the legacy values spelled
    out. Works whatever the shipped default is."""

    def mutate(data: dict[str, Any]) -> None:
        captions = data["captions"]
        for key in (
            "style",
            "max_words",
            "max_width_px",
            "word_gap_em",
            "letter_spacing_px",
            "line_gap_em",
            "spoken_color",
            "upcoming_color",
            "box_opacity",
            "box_padding_px",
        ):
            captions.pop(key, None)
        captions.update(font_size=80, margin_v=350, words_per_line=3, highlight_color="#FFD400")
        data.pop("hook", None)
        data.pop("safe", None)
        if explicit:
            captions["style"] = "legacy"
            data["hook"] = {"enabled": False}

    return mutate


@pytest.mark.parametrize("explicit", [False, True], ids=["no_new_keys", "explicit_legacy"])
def test_legacy_ass_is_byte_for_byte_what_it_was_before_brief_r1(
    project: Path, fake: FakeFfmpeg, explicit: bool
) -> None:
    edit_yaml(project / "config" / "default.yaml", _legacy_yaml(explicit))
    ctx = make_ctx(project)
    write_inputs(ctx.run_dir)

    render.run(ctx)

    raw = (ctx.run_dir / "clips" / "c01.ass").read_bytes()
    assert raw.decode("utf-8") == GOLDEN_LEGACY_ASS
    assert b"\r" not in raw


def test_ffmpeg_gets_the_run_folder_and_relative_paths(ctx: RunContext, fake: FakeFfmpeg) -> None:
    render.run(ctx)

    clip_id, args, cwd = fake.calls[0]
    assert clip_id == "c01"
    assert cwd == ctx.run_dir.resolve()
    vf = args[args.index("-vf") + 1]
    assert vf == (
        "crop=202:360:218:0,scale=1080:1920,setsar=1,"
        "subtitles=filename=clips/c01.ass:fontsdir=../../assets/fonts"
    )
    assert not DRIVE_OR_BACKSLASH.search(vf)
    assert args[args.index("-ss") + 1] == "10.000"
    assert args[args.index("-t") + 1] == "20.000"


def test_rotated_source_crops_the_upright_frame(project: Path, fake: FakeFfmpeg) -> None:
    ctx = make_ctx(project)
    write_inputs(ctx.run_dir, width=360, height=640, rotation=90)  # plays as 640x360

    render.run(ctx)

    vf = fake.calls[0][1][fake.calls[0][1].index("-vf") + 1]
    assert vf.startswith("crop=202:360:218:0,")


def test_missing_font_is_a_clear_error_and_renders_nothing(
    project: Path, ctx: RunContext, fake: FakeFfmpeg
) -> None:
    (project / "assets" / "fonts" / "Montserrat-Bold.ttf").unlink()

    with pytest.raises(RunError, match="caption font not found.*Montserrat-Bold.ttf"):
        render.run(ctx)

    assert fake.calls == []
    assert clip_files(ctx.run_dir) == []


def test_no_kept_clips_is_not_an_error(project: Path, fake: FakeFfmpeg) -> None:
    ctx = make_ctx(project)
    write_inputs(ctx.run_dir, [("c03", None, 70.0, 95.0)])

    render.run(ctx)

    assert fake.calls == []
    assert clip_files(ctx.run_dir) == []


def test_missing_inputs_name_the_stage_to_run(project: Path, fake: FakeFfmpeg) -> None:
    ctx = make_ctx(project)
    with pytest.raises(RunError, match="scored.json not found.*score stage"):
        render.run(ctx)


def test_unsafe_clip_id_is_refused(project: Path, fake: FakeFfmpeg) -> None:
    ctx = make_ctx(project)
    write_inputs(ctx.run_dir, [("..\\evil", 1, 10.0, 30.0)])
    with pytest.raises(RunError, match="not a safe file name"):
        render.run(ctx)
    assert fake.calls == []


# --- forced redo vs resume ----------------------------------------------------------------


def test_forced_redo_rerenders_every_kept_clip(ctx: RunContext, fake: FakeFfmpeg) -> None:
    render.run(ctx)
    assert fake.ids == ["c01", "c02"]

    render.run(ctx)  # resuming is False: this is `--from-stage render`

    assert fake.ids == ["c01", "c02", "c01", "c02"]
    assert not [n for n in clip_files(ctx.run_dir) if n.endswith(".tmp.mp4")]


def test_resume_after_a_failure_renders_only_the_missing_clip(
    ctx: RunContext, fake: FakeFfmpeg
) -> None:
    fake.fail_on = {"c02"}
    with pytest.raises(RunError, match="render failed for c02"):
        render.run(ctx)
    assert fake.ids == ["c01", "c02"]
    # c01 is finished and kept; the failed clip left no temp file and no JSON
    assert clip_files(ctx.run_dir) == ["c01.ass", "c01.json", "c01.mp4", "c02.ass"]
    fake.fail_on = set()

    render.run(resume(ctx))

    assert fake.ids == ["c01", "c02", "c02"]
    assert clip_files(ctx.run_dir) == [
        "c01.ass",
        "c01.json",
        "c01.mp4",
        "c02.ass",
        "c02.json",
        "c02.mp4",
    ]


def test_resume_rerenders_an_output_that_no_longer_validates(
    ctx: RunContext, fake: FakeFfmpeg
) -> None:
    render.run(ctx)
    fake.durations["c01"] = 12.0  # ffprobe now sees a truncated file

    render.run(resume(ctx))

    assert fake.ids == ["c01", "c02", "c01"]


def test_resume_rerenders_when_the_json_is_missing_or_unreadable(
    ctx: RunContext, fake: FakeFfmpeg
) -> None:
    render.run(ctx)
    (ctx.run_dir / "clips" / "c01.json").unlink()
    (ctx.run_dir / "clips" / "c02.json").write_text("{not json", encoding="utf-8")

    render.run(resume(ctx))

    assert fake.ids == ["c01", "c02", "c01", "c02"]


def test_resume_rerenders_a_clip_whose_times_changed(ctx: RunContext, fake: FakeFfmpeg) -> None:
    render.run(ctx)
    write_scored(
        ctx.run_dir, [("c01", 1, 10.0, 30.0), ("c02", 2, 41.0, 62.5), ("c03", None, 70, 95)]
    )

    render.run(resume(ctx))

    assert fake.ids == ["c01", "c02", "c02"]


def test_resume_rerenders_everything_when_settings_changed(
    project: Path, ctx: RunContext, fake: FakeFfmpeg
) -> None:
    render.run(ctx)
    other = RunContext(
        run_dir=ctx.run_dir,
        config=ctx.config.__class__(
            app=ctx.config.app.model_copy(
                update={"captions": ctx.config.app.captions.model_copy(update={"font_size": 90})}
            ),
            client_name=ctx.config.client_name,
            rules=ctx.config.rules,
        ),
        input_path=ctx.input_path,
        project_root=project,
        resuming=True,
    )

    render.run(other)

    assert fake.ids == ["c01", "c02", "c01", "c02"]


def test_outputs_of_clips_that_are_no_longer_kept_are_removed(
    ctx: RunContext, fake: FakeFfmpeg
) -> None:
    render.run(ctx)
    write_scored(ctx.run_dir, [("c01", 1, 10.0, 30.0)])

    render.run(resume(ctx))

    assert clip_files(ctx.run_dir) == ["c01.ass", "c01.json", "c01.mp4"]
    assert fake.ids == ["c01", "c02"]  # c01 was kept as it was


def test_foreign_files_in_clips_are_left_alone(ctx: RunContext, fake: FakeFfmpeg) -> None:
    folder = ctx.run_dir / "clips"
    folder.mkdir()
    (folder / "notes.txt").write_text("keep", encoding="utf-8")

    render.run(ctx)

    assert (folder / "notes.txt").read_text(encoding="utf-8") == "keep"


# --- the pipeline decides forced vs resume --------------------------------------------------


def test_pipeline_marks_only_a_failed_stage_as_resuming(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    def recording_render(c: RunContext) -> None:
        seen.append(c.resuming)
        if len(seen) == 1:
            raise RunError("boom")

    monkeypatch.setitem(pipeline.STAGES, "render", recording_render)
    media = project / "sample.mp4"
    media.write_bytes(b"")
    run_dir = start_new_run(project, media, "example-client", date(2026, 10, 12))

    first = run_pipeline(run_dir)
    assert first.failed_stage == "render"
    assert seen == [False]  # first run: nothing to resume

    second = run_pipeline(run_dir)  # the render stage failed last time: resume
    assert second.ok
    assert seen == [False, True]

    third = run_pipeline(run_dir, from_stage="render")  # forced redo
    assert third.ok
    assert seen == [False, True, False]
    assert load_state(run_dir).stages["render"].status == "done"


def test_pipeline_treats_an_interrupted_render_as_resuming(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []
    monkeypatch.setitem(pipeline.STAGES, "render", lambda c: seen.append(c.resuming))
    media = project / "sample.mp4"
    media.write_bytes(b"")
    run_dir = start_new_run(project, media, "example-client", date(2026, 10, 12))
    state = load_state(run_dir)
    state.stages["render"].status = "running"  # the process died here
    pipeline.save_state(run_dir, state)

    run_pipeline(run_dir)

    assert seen == [True]


# --- caption offset (Brief 6.1) -------------------------------------------------------------


def with_offset(ctx: RunContext, offset: float, *, resuming: bool = True) -> RunContext:
    app = ctx.config.app.model_copy(
        update={"captions": ctx.config.app.captions.model_copy(update={"offset_seconds": offset})}
    )
    return RunContext(
        run_dir=ctx.run_dir,
        config=ResolvedConfig(app=app, client_name=ctx.config.client_name, rules=ctx.config.rules),
        input_path=ctx.input_path,
        project_root=ctx.project_root,
        resuming=resuming,
    )


def test_offset_is_recorded_and_moves_the_captions(ctx: RunContext, fake: FakeFfmpeg) -> None:
    render.run(with_offset(ctx, 0.15, resuming=False))

    data = json.loads((ctx.run_dir / "clips" / "c01.json").read_text(encoding="utf-8"))
    assert data["settings"]["caption_offset_seconds"] == 0.15
    text = (ctx.run_dir / "clips" / "c01.ass").read_text(encoding="utf-8")
    assert "Dialogue: 0,0:00:00.15," in text  # the first word starts at 0 s, now 0.15 s


def test_resume_rerenders_when_only_the_offset_changed(ctx: RunContext, fake: FakeFfmpeg) -> None:
    render.run(ctx)
    assert fake.ids == ["c01", "c02"]

    render.run(with_offset(ctx, 0.15))  # resuming, same everything but the offset

    assert fake.ids == ["c01", "c02", "c01", "c02"]


def test_resume_keeps_clips_when_the_offset_is_unchanged(ctx: RunContext, fake: FakeFfmpeg) -> None:
    render.run(with_offset(ctx, 0.15, resuming=False))

    render.run(with_offset(ctx, 0.15))

    assert fake.ids == ["c01", "c02"]


def test_json_from_before_the_offset_existed_is_still_valid_at_zero(
    ctx: RunContext, fake: FakeFfmpeg
) -> None:
    render.run(ctx)
    path = ctx.run_dir / "clips" / "c01.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["settings"]["caption_offset_seconds"]  # a Brief 6 file
    path.write_text(json.dumps(data), encoding="utf-8")

    render.run(resume(ctx))

    assert fake.ids == ["c01", "c02"]


# --- audio fade (Brief 10) ------------------------------------------------------------------


def with_fade(ctx: RunContext, fade: float, *, resuming: bool = True) -> RunContext:
    app = ctx.config.app.model_copy(
        update={"render": ctx.config.app.render.model_copy(update={"audio_fade_seconds": fade})}
    )
    return RunContext(
        run_dir=ctx.run_dir,
        config=ResolvedConfig(app=app, client_name=ctx.config.client_name, rules=ctx.config.rules),
        input_path=ctx.input_path,
        project_root=ctx.project_root,
        resuming=resuming,
    )


def test_fade_is_passed_to_ffmpeg_and_recorded(ctx: RunContext, fake: FakeFfmpeg) -> None:
    render.run(with_fade(ctx, 0.1, resuming=False))

    args = fake.calls[0][1]
    assert args[args.index("-af") + 1] == "afade=t=in:st=0:d=0.100,afade=t=out:st=19.900:d=0.100"
    data = json.loads((ctx.run_dir / "clips" / "c01.json").read_text(encoding="utf-8"))
    assert data["settings"]["audio_fade_seconds"] == 0.1


def test_default_fade_is_applied_and_zero_turns_it_off(ctx: RunContext, fake: FakeFfmpeg) -> None:
    render.run(ctx)
    assert "-af" in fake.calls[0][1]

    render.run(with_fade(ctx, 0.0, resuming=False))
    assert "-af" not in fake.calls[-1][1]


def test_json_from_before_the_fade_existed_loads_as_zero_and_rerenders(
    ctx: RunContext, fake: FakeFfmpeg
) -> None:
    render.run(ctx)
    path = ctx.run_dir / "clips" / "c01.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["settings"]["audio_fade_seconds"]  # a file from before Brief 10
    path.write_text(json.dumps(data), encoding="utf-8")
    assert (
        RenderedClip.model_validate_json(
            path.read_text(encoding="utf-8")
        ).settings.audio_fade_seconds
        == 0.0
    )

    render.run(resume(ctx))  # the config now has a fade, so c01 is stale; c02 is untouched

    assert fake.ids == ["c01", "c02", "c01"]


def test_resume_rerenders_when_only_the_fade_changed(ctx: RunContext, fake: FakeFfmpeg) -> None:
    render.run(ctx)

    render.run(with_fade(ctx, 0.2))

    assert fake.ids == ["c01", "c02", "c01", "c02"]


# --- speaker crop --------------------------------------------------------------------------

CENTRE_VF = (
    "crop=202:360:218:0,scale=1080:1920,setsar=1,"
    "subtitles=filename=clips/c01.ass:fontsdir=../../assets/fonts"
)
CUT_LOOK = track_fakes.cut_at(10.0, levels=(0, 120))
TWO_PEOPLE = track_fakes.two_people(switch_at=10.0)


def speaker_ctx(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: track_fakes.Scenario = TWO_PEOPLE,
    look: track_fakes.Look = track_fakes.steady,
    *,
    models: bool = True,
    style: str = "legacy",
) -> tuple[RunContext, track_fakes.TrackFakes]:
    """A run folder with the render stage set to speaker mode and the analysis faked."""
    ctx = make_ctx(project, style=style)
    write_inputs(ctx.run_dir)
    ctx.config.app.video.crop = "speaker"
    # these tests pin the mouth-variance rule; the mix signal has its own tests
    ctx.config.app.speaker = ctx.config.app.speaker.model_copy(update={"signal": "mouth"})
    if models:
        track_fakes.write_model(project)
    return ctx, track_fakes.install(monkeypatch, scenario, look)


def read_json(path: Path) -> dict[str, object]:
    data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return data


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def vf_of(args: list[str]) -> str:
    return args[args.index("-vf") + 1]


def expected_commands(ctx: RunContext, clip_id: str, duration: float) -> str:
    """The command file text worked out from the clip's track file."""
    track = TrackFile.model_validate_json(
        tracking.track_path(ctx.run_dir / "clips", clip_id).read_text(encoding="utf-8")
    )
    path = frame_path(
        [(s.t, s.crop_x) for s in track.samples],
        [(s.start, s.end) for s in track.shots],
        track.cuts,
        track.cuts_exact,
        30.0,
        duration,
    )
    return ffmpeg.crop_commands(path)


def test_a_valid_track_is_reused_and_becomes_a_command_file(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, fakes = speaker_ctx(project, monkeypatch)
    render.run(ctx)  # analyses both clips and leaves their track files
    assert fakes.opened == 2
    fake.calls.clear()

    render.run(ctx)  # a forced redo: the tracks are still valid

    assert fakes.opened == 2  # no new analysis
    clips = ctx.run_dir / "clips"
    _, args, _ = fake.calls[0]
    # a shot change at 10 s of the clip, from the face at 0.25 to the one at 0.75
    assert vf_of(args) == (
        "sendcmd=f=clips/c01.crop.cmd,crop=202:360:60:0,scale=1080:1920,setsar=1,"
        "subtitles=filename=clips/c01.ass:fontsdir=../../assets/fonts"
    )
    text = (clips / "c01.crop.cmd").read_text(encoding="utf-8")
    assert text == expected_commands(ctx, "c01", 20.0)
    assert "10.000000 crop x 380;\n" in text
    data = read_json(clips / "c01.json")
    assert data["crop_mode"] == "speaker"
    assert data["track_sha256"] == file_hash(clips / "c01.track.json")
    assert data["track_fallback"] is None
    assert data["crop_changes"] == len(text.splitlines()) >= 1
    assert data["crop"] == {"w": 202, "h": 360, "x": 60, "y": 0}
    assert "synthetic" not in json.dumps(data)


def stale_by(change: str, ctx: RunContext) -> None:
    track_file = ctx.run_dir / "clips" / "c01.track.json"
    if change == "missing":
        track_file.unlink()
    elif change == "settings":
        speaker = ctx.config.app.speaker
        ctx.config.app.speaker = speaker.model_copy(update={"smoothing": 0.5})
    else:
        data = read_json(track_file)
        if change == "times":
            data["clip_start"] = 11.0
        elif change == "schema":
            data["schema_version"] = 1
        elif change == "crop_w":
            data["crop_w"] = 100
        track_file.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("change", ["missing", "times", "schema", "settings", "crop_w"])
def test_a_missing_or_stale_track_is_analysed_again_and_saved(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    ctx, fakes = speaker_ctx(project, monkeypatch)
    render.run(ctx)
    opened = fakes.opened
    stale_by(change, ctx)

    render.run(ctx)

    # c01's track was spoiled; a settings change spoils both
    assert fakes.opened == opened + (2 if change == "settings" else 1)
    clips = ctx.run_dir / "clips"
    track = TrackFile.model_validate_json((clips / "c01.track.json").read_text(encoding="utf-8"))
    assert (track.clip_start, track.clip_end, track.schema_version) == (
        10.0,
        30.0,
        TRACK_SCHEMA_VERSION,
    )
    assert track.crop_w == 202
    data = read_json(clips / "c01.json")
    assert data["track_sha256"] == file_hash(clips / "c01.track.json")  # rendered from the new one
    assert (clips / "c01.crop.cmd").read_text(encoding="utf-8") == expected_commands(
        ctx, "c01", 20.0
    )


def test_a_track_with_a_fallback_reason_renders_the_centre_crop(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = speaker_ctx(project, monkeypatch, track_fakes.nobody)

    render.run(ctx)

    assert vf_of(fake.calls[0][1]) == CENTRE_VF
    assert not [n for n in clip_files(ctx.run_dir) if n.endswith(".crop.cmd")]
    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert (data["crop_mode"], data["track_fallback"], data["crop_changes"]) == (
        "speaker",
        "no_face",
        0,
    )
    assert data["crop"] == {"w": 202, "h": 360, "x": 218, "y": 0}
    speaker_args = fake.calls[0][1]

    ctx.config.app.video.crop = "center"  # the very same command as centre mode
    fake.calls.clear()
    render.run(ctx)
    assert fake.calls[0][1] == speaker_args


def test_a_crop_that_never_moves_is_a_plain_crop(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    def one(t: float) -> list[track_fakes.RawFace]:
        return [track_fakes.RawFace(0.25, 0.5, 0.15, 0.3, track_fakes.talking(t))]

    ctx, _ = speaker_ctx(project, monkeypatch, one)

    render.run(ctx)

    vf = vf_of(fake.calls[0][1])
    assert vf.startswith("crop=202:360:60:0,scale=")  # 0.25 of 640, minus half the window
    assert not [n for n in clip_files(ctx.run_dir) if n.endswith(".crop.cmd")]
    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert (data["crop_mode"], data["crop_changes"], data["track_fallback"]) == ("speaker", 0, None)


def hop(t: float) -> list[track_fakes.RawFace]:
    """One person who sits at the left for 10 s and at the right after a camera cut."""
    return [track_fakes.RawFace(0.25 if t < 10 else 0.75, 0.5, 0.15, 0.3, track_fakes.talking(t))]


def test_a_camera_cut_moves_the_crop_at_its_exact_time(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = speaker_ctx(project, monkeypatch, hop, CUT_LOOK)

    render.run(ctx)

    track = TrackFile.model_validate_json(
        (ctx.run_dir / "clips" / "c01.track.json").read_text(encoding="utf-8")
    )
    assert track.cuts == [10.0] and track.cuts_exact == [pytest.approx(9.9 + 2.5 / 30, abs=1e-6)]
    text = (ctx.run_dir / "clips" / "c01.crop.cmd").read_text(encoding="utf-8")
    assert text == "9.983333 crop x 380;\n"  # the exact cut, not the sample time 10.0


def test_speaker_mode_logs_numbers_only(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx, _ = speaker_ctx(project, monkeypatch, hop, CUT_LOOK)

    with caplog.at_level("INFO", logger="clipmachine"):
        render.run(ctx)

    line = next(r.getMessage() for r in caplog.records if "render: c01" in r.getMessage())
    assert "speaker, track analysed, 1 cuts, 0 switches, 1 crop changes" in line
    assert "source.mp4" not in caplog.text and ".json" not in caplog.text


# --- speaker mode: resume ------------------------------------------------------------------


def test_resume_keeps_clips_with_the_same_mode_and_track(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, fakes = speaker_ctx(project, monkeypatch)
    render.run(ctx)

    render.run(resume(ctx))

    assert fake.ids == ["c01", "c02"]  # nothing was rendered again
    assert fakes.opened == 2


def test_resume_rerenders_when_the_track_file_changed(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, fakes = speaker_ctx(project, monkeypatch)
    render.run(ctx)
    track_file = ctx.run_dir / "clips" / "c01.track.json"
    track_file.write_text(json.dumps(read_json(track_file), indent=1), encoding="utf-8")

    render.run(resume(ctx))

    assert fake.ids == ["c01", "c02", "c01"]  # still a valid track, so no new analysis
    assert fakes.opened == 2


def test_resume_rerenders_when_the_track_is_missing(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, fakes = speaker_ctx(project, monkeypatch)
    render.run(ctx)
    (ctx.run_dir / "clips" / "c02.track.json").unlink()

    render.run(resume(ctx))

    assert fake.ids == ["c01", "c02", "c02"]
    assert fakes.opened == 3  # c02 was analysed again


def test_resume_rerenders_when_the_command_file_is_gone(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = speaker_ctx(project, monkeypatch)
    render.run(ctx)
    (ctx.run_dir / "clips" / "c01.crop.cmd").unlink()

    render.run(resume(ctx))

    assert fake.ids == ["c01", "c02", "c01"]
    assert (ctx.run_dir / "clips" / "c01.crop.cmd").is_file()


@pytest.mark.parametrize(("first", "second"), [("center", "speaker"), ("speaker", "center")])
def test_resume_rerenders_when_the_crop_mode_changed(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch, first: str, second: str
) -> None:
    ctx, _ = speaker_ctx(project, monkeypatch)
    ctx.config.app.video.crop = first
    render.run(ctx)

    ctx.config.app.video.crop = second
    render.run(resume(ctx))

    assert fake.ids == ["c01", "c02", "c01", "c02"]
    assert read_json(ctx.run_dir / "clips" / "c01.json")["crop_mode"] == second


# --- speaker mode: the sweep ---------------------------------------------------------------


def test_a_forced_redo_removes_old_command_files(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = speaker_ctx(project, monkeypatch, track_fakes.nobody)  # nothing will write one
    folder = ctx.run_dir / "clips"
    folder.mkdir()
    (folder / "c01.crop.cmd").write_text("1.000000 crop x 2;\n", encoding="utf-8")
    (folder / "c01.track.json").write_text("{}", encoding="utf-8")  # a track file is not ours

    render.run(ctx)

    assert not (folder / "c01.crop.cmd").exists()
    assert (folder / "c01.track.json").is_file()


def test_command_files_of_clips_no_longer_kept_are_removed_and_kept_ones_survive_a_resume(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = speaker_ctx(project, monkeypatch)
    render.run(ctx)
    folder = ctx.run_dir / "clips"
    assert (folder / "c01.crop.cmd").is_file() and (folder / "c02.crop.cmd").is_file()
    write_scored(ctx.run_dir, [("c01", 1, 10.0, 30.0)])

    render.run(resume(ctx))

    assert fake.ids == ["c01", "c02"]  # c01 was kept as it was
    assert (folder / "c01.crop.cmd").is_file()
    assert not (folder / "c02.crop.cmd").exists()
    assert (folder / "c01.track.json").is_file()
    assert not (folder / "c02.track.json").exists()  # as before: gone with its clip


# --- speaker mode: errors and centre mode --------------------------------------------------


def test_a_missing_model_file_names_the_fetch_command_and_renders_nothing(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, fakes = speaker_ctx(project, monkeypatch, models=False)

    with pytest.raises(RunError, match="fetch-face-model"):
        render.run(ctx)

    assert fake.calls == [] and fakes.opened == 0
    assert clip_files(ctx.run_dir) == []


def test_a_failed_analysis_is_a_run_error_naming_the_clip(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = speaker_ctx(project, monkeypatch)

    def broken(*args: object, **kwargs: object) -> object:
        raise MediaError("frame extraction failed: boom")

    monkeypatch.setattr(tracking.ffmpeg, "read_frames", broken)

    with pytest.raises(RunError, match="speaker tracking failed for c01: frame extraction failed"):
        render.run(ctx)

    assert fake.calls == []


def test_centre_mode_needs_no_model_files_and_never_imports_tracking(
    ctx: RunContext, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clipmachine

    monkeypatch.delitem(sys.modules, "clipmachine.tracking", raising=False)
    monkeypatch.delattr(clipmachine, "tracking", raising=False)

    render.run(ctx)  # no model files exist in this project

    assert "clipmachine.tracking" not in sys.modules
    assert vf_of(fake.calls[0][1]) == CENTRE_VF
    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert (data["crop_mode"], data["track_sha256"], data["crop_changes"]) == ("center", None, 0)


# --- old run folders keep their frozen config (D-019 default: speaker) ----------------------


def test_a_run_folder_frozen_on_center_still_renders_centre_after_the_default_changed(
    project: Path, fake: FakeFfmpeg
) -> None:
    ctx = make_ctx(project)  # the fixture's config pins crop: center
    write_inputs(ctx.run_dir)
    dump_resolved(ctx.config, ctx.run_dir / "config.resolved.yaml")
    # The project's default now says speaker, and no model file exists anywhere.
    edit_yaml(project / "config" / "default.yaml", lambda d: d["video"].update(crop="speaker"))
    assert load_app_config(project).video.crop == "speaker"
    assert not (project / "models").exists()

    frozen = load_resolved_file(ctx.run_dir / "config.resolved.yaml")
    render.run(
        RunContext(
            run_dir=ctx.run_dir,
            config=frozen,
            input_path=ctx.input_path,
            project_root=project,
        )
    )

    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert (data["crop_mode"], data["crop_changes"], data["track_fallback"]) == ("center", 0, None)
    assert data["crop"] == {"w": 202, "h": 360, "x": 218, "y": 0}


# --- box_karaoke captions and the hook (Brief R1) -----------------------------------------------

HOOK_TEXT = "synthetic hook"
HOOK_LAYERS = ("Dialogue: 2,", "Dialogue: 3,")


def v2_ctx(project: Path) -> RunContext:
    ctx = make_ctx(project, style="box_karaoke")
    write_inputs(ctx.run_dir)
    return ctx


def dialogues(run_dir: Path, clip_id: str = "c01") -> list[str]:
    text = (run_dir / "clips" / f"{clip_id}.ass").read_text(encoding="utf-8")
    return [line for line in text.split("\n") if line.startswith("Dialogue:")]


def hook_lines(run_dir: Path) -> list[str]:
    return [d for d in dialogues(run_dir) if d.startswith(HOOK_LAYERS)]


def test_the_box_karaoke_file_has_the_three_styles_and_karaoke_text(
    project: Path, fake: FakeFfmpeg
) -> None:
    ctx = v2_ctx(project)

    render.run(ctx)

    path = ctx.run_dir / "clips" / "c01.ass"
    text = path.read_text(encoding="utf-8")
    assert "WrapStyle: 2" in text and "PlayResX: 1080" in text
    styles = [line.split(",")[0] for line in text.split("\n") if line.startswith("Style:")]
    assert styles == ["Style: Cap", "Style: Hook", "Style: Box"]
    caps = [d for d in dialogues(ctx.run_dir) if ",Cap,," in d]
    assert caps and all("\\k" in d and "\\pos(504," in d for d in caps)
    assert any(",Box,," in d and "\\p1}" in d for d in dialogues(ctx.run_dir))
    assert "w10" in text and "w28" in text  # the clip's words
    assert b"\r" not in path.read_bytes()


def test_the_hook_events_are_written_when_enabled(project: Path, fake: FakeFfmpeg) -> None:
    ctx = v2_ctx(project)

    render.run(ctx)

    hooks = hook_lines(ctx.run_dir)
    assert len(hooks) == 2  # the band and one line of text
    assert all(",0:00:00.00,0:00:03.00," in d for d in hooks)
    assert "\\fad(0,150)" in hooks[0] and HOOK_TEXT.split()[0] in hooks[1]


def test_no_hook_events_when_the_hook_is_disabled(project: Path, fake: FakeFfmpeg) -> None:
    ctx = v2_ctx(project)
    ctx.config.app.hook = ctx.config.app.hook.model_copy(update={"enabled": False})

    render.run(ctx)

    assert hook_lines(ctx.run_dir) == []
    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["hook_shown"] is False and data["notes"] == []


def test_a_blank_hook_line_shows_no_hook_and_says_so(project: Path, fake: FakeFfmpeg) -> None:
    ctx = v2_ctx(project)
    scored = ScoredFile.model_validate_json((ctx.run_dir / "scored.json").read_text("utf-8"))
    for clip in scored.clips:
        clip.hook_line = " {} "
    write_json_atomic(ctx.run_dir / "scored.json", scored.model_dump(mode="json"))

    render.run(ctx)

    assert hook_lines(ctx.run_dir) == []
    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["hook_shown"] is False and data["notes"] == ["hook_missing"]


def test_the_clip_json_and_log_hold_counts_and_codes_but_never_the_hook(
    project: Path, fake: FakeFfmpeg, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = v2_ctx(project)

    with caplog.at_level(logging.INFO, logger="clipmachine"):
        render.run(ctx)

    raw = (ctx.run_dir / "clips" / "c01.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert "synthetic" not in raw and "w10" not in raw
    assert "synthetic" not in caplog.text and "w10" not in caplog.text
    # a word every 2 s is a pause of more than half a second each time: one word per line
    assert data["caption_lines"] == 10 and data["caption_max_words"] == 1
    assert data["caption_events"] == 2 * data["caption_lines"]  # a box and a text line each
    assert (data["hook_shown"], data["hook_moved"], data["outside_safe_zone"]) == (True, False, 0)
    assert data["notes"] == []  # hook.position top: faces are not looked at
    settings = data["settings"]
    assert settings["caption_style"] == "box_karaoke" and settings["hook_enabled"] is True
    assert (settings["safe_top"], settings["safe_bottom"]) == (290, 560)
    assert (settings["spoken_color"], settings["box_opacity"]) == ("#FFD400", 0.75)
    assert "captions 10 lines, hook shown" in caplog.text


def test_speaker_mode_moves_the_hook_below_a_face_in_its_way(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    def high_face(t: float) -> list[track_fakes.RawFace]:
        return [track_fakes.RawFace(0.5, 0.1, 0.1, 0.15, track_fakes.talking(t))]

    ctx, _ = speaker_ctx(project, monkeypatch, high_face, style="box_karaoke")
    ctx.config.app.hook = ctx.config.app.hook.model_copy(update={"position": "avoid_faces"})

    render.run(ctx)

    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["hook_shown"] is True and data["hook_moved"] is True
    assert data["notes"] == ["hook_moved_for_faces"]
    assert data["outside_safe_zone"] == 0
    band = next(d for d in dialogues(ctx.run_dir) if d.startswith("Dialogue: 2,"))
    match = re.search(r"\\p1\}m \d+ (\d+)", band)  # the drawing starts at the top edge
    assert match is not None and int(match.group(1)) > 290


def test_hook_position_top_ignores_a_face_in_its_way(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    def high_face(t: float) -> list[track_fakes.RawFace]:
        return [track_fakes.RawFace(0.5, 0.1, 0.1, 0.15, track_fakes.talking(t))]

    ctx, _ = speaker_ctx(project, monkeypatch, high_face, style="box_karaoke")
    assert ctx.config.app.hook.position == "top"  # the shipped default

    render.run(ctx)

    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["hook_shown"] is True and data["hook_moved"] is False
    assert data["notes"] == [] and data["outside_safe_zone"] == 0
    band = next(d for d in dialogues(ctx.run_dir) if d.startswith("Dialogue: 2,"))
    assert band.split("p1}m ")[1].split(" ")[1] == "290"  # the drawing starts at the top
    assert data["settings"]["hook_position"] == "top"


def test_speaker_mode_without_a_face_in_the_way_keeps_the_hook_at_the_top(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = speaker_ctx(project, monkeypatch, style="box_karaoke")  # faces are mid-frame

    render.run(ctx)

    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["hook_shown"] is True and data["hook_moved"] is False
    assert data["notes"] == [] and data["outside_safe_zone"] == 0


def test_a_changed_caption_setting_makes_the_old_output_stale(
    project: Path, fake: FakeFfmpeg
) -> None:
    ctx = v2_ctx(project)
    render.run(ctx)
    fake.calls.clear()

    render.run(resume(ctx))
    assert fake.ids == []  # nothing changed: both clips kept

    ctx.config.app.captions = ctx.config.app.captions.model_copy(update={"word_gap_em": 0.6})
    render.run(resume(ctx))
    assert fake.ids == ["c01", "c02"]
    assert read_json(ctx.run_dir / "clips" / "c01.json")["settings"]["word_gap_em"] == 0.6


def test_the_hook_is_drawn_at_its_own_configured_size_not_the_caption_size(
    project: Path, fake: FakeFfmpeg
) -> None:
    ctx = v2_ctx(project)
    assert ctx.config.app.captions.font_size == 96  # the caption size, unchanged by the hook

    render.run(ctx)

    text = hook_lines(ctx.run_dir)[1]
    assert "\\fs80}" in text  # the shipped hook size; the Hook style itself is the caption size
    assert read_json(ctx.run_dir / "clips" / "c01.json")["settings"]["hook_font_size"] == 80


def test_a_changed_hook_font_size_reaches_the_ass_file_and_makes_the_output_stale(
    project: Path, fake: FakeFfmpeg
) -> None:
    ctx = v2_ctx(project)
    render.run(ctx)
    fake.calls.clear()

    ctx.config.app.hook = ctx.config.app.hook.model_copy(update={"font_size": 60})
    render.run(resume(ctx))

    assert fake.ids == ["c01", "c02"]
    assert "\\fs60}" in hook_lines(ctx.run_dir)[1]
    assert read_json(ctx.run_dir / "clips" / "c01.json")["settings"]["hook_font_size"] == 60


def test_a_hook_font_size_equal_to_the_caption_size_needs_no_size_tag(
    project: Path, fake: FakeFfmpeg
) -> None:
    ctx = v2_ctx(project)
    ctx.config.app.hook = ctx.config.app.hook.model_copy(update={"font_size": 96})

    render.run(ctx)

    assert not re.search(r"\\fs\d", hook_lines(ctx.run_dir)[1])  # \fsp is letter spacing


def test_an_unreadable_font_file_fails_before_anything_is_deleted(
    project: Path, fake: FakeFfmpeg
) -> None:
    ctx = v2_ctx(project)
    clips = ctx.run_dir / "clips"
    clips.mkdir()
    (clips / "c01.mp4").write_bytes(b"earlier output")
    (project / "assets" / "fonts" / "Montserrat-Bold.ttf").write_bytes(b"not a font" * 50)

    with pytest.raises(RunError, match=r"captions\.font_file"):
        render.run(ctx)

    assert fake.calls == []
    assert (clips / "c01.mp4").read_bytes() == b"earlier output"


def test_the_legacy_style_does_not_read_the_font_file(project: Path, fake: FakeFfmpeg) -> None:
    ctx = make_ctx(project)  # legacy
    write_inputs(ctx.run_dir)
    (project / "assets" / "fonts" / "Montserrat-Bold.ttf").write_bytes(b"not a font" * 50)

    render.run(ctx)

    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["settings"]["caption_style"] == "legacy" and data["notes"] == []
    assert data["caption_lines"] == 4 and data["caption_max_words"] == 3


# --- seams for campaign mode (Brief 11) ---------------------------------------------------------


def take_id(clip: render.ClipLike) -> str:
    return f"{clip.id}:{clip.start}:{clip.end}:{clip.hook_line}"


def test_a_scored_clip_is_a_clip_like() -> None:
    assert take_id(scored_clip("c01", 1, 10.0, 30.0)) == "c01:10.0:30.0:synthetic hook"


def v2_plan(project: Path) -> tuple[render.Plan, ScoredClip, list[Any]]:
    from clipmachine.utils.captions import clip_words

    ctx = v2_ctx(project)
    run_dir = ctx.run_dir.resolve()
    inputs = render._load_inputs(run_dir, project.resolve(), ctx.config.app)
    plan = render._make_plan(run_dir, run_dir / "clips", ctx.config.app, inputs)
    clip = inputs.kept[0]
    return plan, clip, clip_words(plan.segments, clip.start, clip.end)


def test_a_hook_spec_for_the_whole_clip_has_no_fade_and_lasts_the_clip(project: Path) -> None:
    plan, clip, words = v2_plan(project)
    duration = clip.end - clip.start

    whole = render._karaoke_captions(
        plan, clip, words, None, duration, render.HookSpec(duration, 0, 80)
    )

    hooks = [d for d in whole.text.split("\n") if d.startswith(HOOK_LAYERS)]
    assert len(hooks) == 2
    assert all(",0:00:00.00,0:00:20.00," in d for d in hooks)
    assert "\fad" not in "".join(hooks)
    assert whole.hook_shown and whole.hook_band is not None


def test_the_default_hook_spec_is_the_one_built_from_the_settings(project: Path) -> None:
    plan, clip, words = v2_plan(project)
    duration = clip.end - clip.start
    settings = plan.settings

    default = render._karaoke_captions(plan, clip, words, None, duration)
    explicit = render._karaoke_captions(
        plan,
        clip,
        words,
        None,
        duration,
        render.HookSpec(
            settings.hook_seconds,
            settings.hook_fade_ms,
            settings.hook_font_size,
            position=settings.hook_position,
        ),
    )

    assert default == explicit


def test_a_given_hook_spec_shows_the_hook_even_when_the_settings_turn_it_off(
    project: Path,
) -> None:
    ctx = v2_ctx(project)
    ctx.config.app.hook = ctx.config.app.hook.model_copy(update={"enabled": False})
    run_dir = ctx.run_dir.resolve()
    inputs = render._load_inputs(run_dir, project.resolve(), ctx.config.app)
    plan = render._make_plan(run_dir, run_dir / "clips", ctx.config.app, inputs)
    clip = inputs.kept[0]
    words = render.clip_words(plan.segments, clip.start, clip.end)

    off = render._karaoke_captions(plan, clip, words, None, 20.0)
    on = render._karaoke_captions(plan, clip, words, None, 20.0, render.HookSpec(2.0, 150, 80))

    assert not off.hook_shown and off.hook_band is None
    assert on.hook_shown and on.hook_band is not None


def test_captions_report_the_caption_boxes_and_the_hook_band(project: Path) -> None:
    plan, clip, words = v2_plan(project)

    captions = render._karaoke_captions(plan, clip, words, None, clip.end - clip.start)

    assert len(captions.caption_boxes) == captions.lines == 10
    assert all(isinstance(box, render.Rect) for box in captions.caption_boxes)
    assert captions.hook_band is not None
    assert not any(captions.hook_band.intersects(box) for box in captions.caption_boxes)


def test_the_hook_spec_avoid_moves_the_hook_below_the_rectangle(project: Path) -> None:
    plan, clip, words = v2_plan(project)
    assert plan.safe is not None
    mark = render.Rect(0.0, plan.safe.y1 - 10, 1080.0, plan.safe.y1 + 100)

    captions = render._karaoke_captions(
        plan, clip, words, None, 20.0, render.HookSpec(20.0, 0, 80, (mark,))
    )

    assert captions.hook_band is not None and captions.hook_band.y1 == plan.safe.y1 + 112
    assert "hook_moved_for_overlay" in captions.notes
    assert not captions.hook_band.intersects(mark)


def test_the_legacy_captions_leave_the_new_fields_empty(project: Path) -> None:
    ctx = make_ctx(project)  # legacy
    write_inputs(ctx.run_dir)
    run_dir = ctx.run_dir.resolve()
    inputs = render._load_inputs(run_dir, project.resolve(), ctx.config.app)
    plan = render._make_plan(run_dir, run_dir / "clips", ctx.config.app, inputs)

    captions = render._legacy_captions(plan, [], 20.0)

    assert captions.caption_boxes == () and captions.hook_band is None


def test_source_inputs_load_without_scored_json_in_centre_mode(project: Path) -> None:
    ctx = v2_ctx(project)
    (ctx.run_dir / "scored.json").unlink()
    run_dir, root = ctx.run_dir.resolve(), project.resolve()

    inputs = render.load_source_inputs(run_dir, root, ctx.config.app)

    assert inputs.kept == [] and inputs.track_plan is None and inputs.segments is None
    assert inputs.source == run_dir / "source.mp4" and inputs.video.width == 640
    assert inputs.metrics is not None
    with pytest.raises(RunError, match=r"scored\.json not found"):
        render._load_inputs(run_dir, root, ctx.config.app)


def test_source_inputs_in_speaker_mode_always_build_the_track_plan(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = speaker_ctx(project, monkeypatch, style="box_karaoke")
    (ctx.run_dir / "scored.json").unlink()

    inputs = render.load_source_inputs(ctx.run_dir.resolve(), project.resolve(), ctx.config.app)

    assert inputs.kept == [] and inputs.track_plan is not None
    assert inputs.segments is not None and inputs.segments


def test_source_inputs_in_speaker_mode_still_need_the_models(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = speaker_ctx(project, monkeypatch, style="box_karaoke", models=False)
    with pytest.raises(RunError):
        render.load_source_inputs(ctx.run_dir.resolve(), project.resolve(), ctx.config.app)


def test_source_inputs_keep_the_errors_of_a_missing_font_and_a_missing_source(
    project: Path,
) -> None:
    ctx = v2_ctx(project)
    run_dir, root = ctx.run_dir.resolve(), project.resolve()
    (run_dir / "source.mp4").unlink()
    with pytest.raises(RunError, match="source video not found"):
        render.load_source_inputs(run_dir, root, ctx.config.app)
    (project / "assets" / "fonts" / "Montserrat-Bold.ttf").unlink()
    with pytest.raises(RunError, match="caption font not found"):
        render.load_source_inputs(run_dir, root, ctx.config.app)


# --- profanity masking (Brief 11.1) -------------------------------------------------------------


def invented_lists(project: Path) -> None:
    """Replace the shipped word lists in the project copy with invented words."""
    folder = project / "config" / "wordlists"
    (folder / "mask.txt").write_text("blorp\ncontext: zib\nslur: vexor\n", encoding="utf-8")
    (folder / "flag.txt").write_text("wump\n", encoding="utf-8")
    (folder / "never.txt").write_text("glimmer\n", encoding="utf-8")


def plant_words(run_dir: Path, changes: dict[str, str]) -> None:
    """Replace transcript words (" w10" -> " blorp") in the run's transcript.json."""
    path = run_dir / "transcript.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for segment in data["segments"]:
        for word in segment["words"]:
            word["word"] = changes.get(word["word"].strip(), word["word"].strip()).join((" ", ""))
    write_json_atomic(path, data)


def set_hook(run_dir: Path, text: str) -> None:
    scored = ScoredFile.model_validate_json((run_dir / "scored.json").read_text("utf-8"))
    for clip in scored.clips:
        clip.hook_line = text
    write_json_atomic(run_dir / "scored.json", scored.model_dump(mode="json"))


def test_masked_words_render_as_first_letter_and_stars_in_captions_and_the_hook(
    project: Path, fake: FakeFfmpeg, caplog: pytest.LogCaptureFixture
) -> None:
    invented_lists(project)
    ctx = v2_ctx(project)
    # w16 (a context word) is two words after w12, so it is masked too; w24 is alone
    plant_words(
        ctx.run_dir,
        {"w10": "Blorp,", "w12": "blorping", "w14": "blorpish", "w16": "zib", "w24": "zib"},
    )
    set_hook(ctx.run_dir, "WHY BLORP MATTERS")

    with caplog.at_level(logging.INFO):
        render.run(ctx)

    caps = [d for d in dialogues(ctx.run_dir) if ",Cap,," in d]
    shown = [d.rsplit("}", 1)[1] for d in caps]
    assert shown[:4] == ["B***,", "b***", "blorpish", "z***"] and "zib" in shown
    assert not [w for w in shown if w.lower().startswith("blorp") and w != "blorpish"]
    hooks = hook_lines(ctx.run_dir)
    assert "WHY{" in hooks[1] and "B***{" in hooks[1] and "BLORP" not in hooks[1]
    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["masked_words"] == 4 and data["settings"]["mask_profanity"] is True
    assert "blorp" not in json.dumps(data).lower()  # the json holds a count, never the words
    assert ", 4 masked" in caplog.text and "blorp" not in caplog.text.lower()


def test_masking_off_renders_the_words_as_spoken(project: Path, fake: FakeFfmpeg) -> None:
    invented_lists(project)
    ctx = v2_ctx(project)
    ctx.config.app.captions = ctx.config.app.captions.model_copy(update={"mask_profanity": False})
    plant_words(ctx.run_dir, {"w10": "blorp"})

    render.run(ctx)

    assert "blorp" in (ctx.run_dir / "clips" / "c01.ass").read_text(encoding="utf-8")
    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["masked_words"] == 0 and data["settings"]["mask_profanity"] is False


def test_the_legacy_style_masks_too(project: Path, fake: FakeFfmpeg) -> None:
    invented_lists(project)
    ctx = make_ctx(project)  # legacy style
    write_inputs(ctx.run_dir)
    plant_words(ctx.run_dir, {"w10": "blorp"})

    render.run(ctx)

    text = (ctx.run_dir / "clips" / "c01.ass").read_text(encoding="utf-8")
    assert "b***" in text and "blorp" not in text
    assert read_json(ctx.run_dir / "clips" / "c01.json")["masked_words"] == 1


def test_a_missing_word_list_fails_the_render_only_when_masking_is_on(
    project: Path, fake: FakeFfmpeg
) -> None:
    (project / "config" / "wordlists" / "flag.txt").unlink()
    ctx = v2_ctx(project)
    with pytest.raises(RunError, match="word list not found: config/wordlists/flag.txt"):
        render.run(ctx)
    ctx.config.app.captions = ctx.config.app.captions.model_copy(update={"mask_profanity": False})
    render.run(ctx)
    assert (ctx.run_dir / "clips" / "c01.mp4").is_file()


def test_a_clip_rendered_before_masking_existed_is_not_stale(
    project: Path, fake: FakeFfmpeg
) -> None:
    ctx = v2_ctx(project)
    render.run(ctx)
    path = ctx.run_dir / "clips" / "c01.json"
    data = read_json(path)
    settings = data["settings"]
    assert isinstance(settings, dict)
    del settings["mask_profanity"]  # as a json from before Brief 11.1
    write_json_atomic(path, data)
    before = len(fake.calls)

    render.run(resume(ctx))

    assert len(fake.calls) == before  # nothing re-rendered


# --- punch-in zooms (Brief R2) -------------------------------------------------------------
#
# The default rig is a 640x360 source, whose 202x360 window can never pass the upscale guard
# (the 1.12x window would be drawn about 6x larger): that is the zoom_skipped_upscale case. The
# other zoom tests use a 2560x1440 source (a 810x1440 window, drawn 1.5x larger when zoomed).
# The rig's transcript has one word every 2 s and no punctuation, so every word is a sentence of
# its own: clip c01 (10-30 s) has sentences starting at 0, 2, ... 18 s of the clip.

BIG = {"width": 2560, "height": 1440}
BIG_WINDOW = CropBox(w=810, h=1440, x=0, y=0)  # the centre crop of the 2560x1440 source
# Brief R2's zoom: the punch tests below keep it explicit, as they assume a hard cut at 1.12
PUNCH = ZoomConfig(enabled=True, style="punch", scale=1.12, max_upscale=2.0)
SMOOTH = ZoomConfig(enabled=True)  # Brief R2.1: smooth, 1.15, ramps of 0.4 s


def zoom_rig(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: track_fakes.Scenario = TWO_PEOPLE,
    *,
    big: bool = True,
    zoom: ZoomConfig | None = None,
    audio_file: bool = False,
    style: str = "legacy",
) -> tuple[RunContext, track_fakes.TrackFakes]:
    """The speaker rig with the zoom switched on (the project fixture ships it off)."""
    ctx, fakes = speaker_ctx(project, monkeypatch, scenario, style=style)
    if big:
        write_inputs(ctx.run_dir, **BIG)
    ctx.config.app.video.zoom = zoom if zoom is not None else PUNCH
    if audio_file:
        (ctx.run_dir / "audio.wav").write_bytes(b"fake")  # loudness_envelope is faked
    return ctx, fakes


def expected_zoom_commands(
    ctx: RunContext, clip_id: str, duration: float, spans: list[ZoomSpan]
) -> str:
    """The command file text worked out from the clip's track file and the expected spans."""
    track = TrackFile.model_validate_json(
        tracking.track_path(ctx.run_dir / "clips", clip_id).read_text(encoding="utf-8")
    )
    path = frame_path(
        [(s.t, s.crop_x) for s in track.samples],
        [(s.start, s.end) for s in track.shots],
        track.cuts,
        track.cuts_exact,
        30.0,
        duration,
    )
    zoom = ctx.config.app.video.zoom
    boxes = zoomed_path(path, BIG_WINDOW, spans, zoom.scale, 2560, 1440, duration)
    return ffmpeg.crop_box_commands(boxes)


def punctuate(run_dir: Path, word_times: set[int]) -> None:
    """End a sentence after each rig word that starts at one of `word_times` (seconds)."""
    path = run_dir / "transcript.json"
    transcript = Transcript.model_validate_json(path.read_text(encoding="utf-8"))
    for word in transcript.segments[0].words:
        if int(word.start) in word_times:
            word.word += "."
    write_json_atomic(path, transcript.model_dump(mode="json"))


def fake_loudness(
    monkeypatch: pytest.MonkeyPatch, loud: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Replace audio.loudness_envelope: -30 dB everywhere, -10 dB inside the `loud` windows
    (seconds of the source). Returns the (start, end) of every call."""
    calls: list[tuple[float, float]] = []

    def envelope(path: Path, start: float, end: float) -> list[float]:
        calls.append((start, end))
        count = int(round((end - start) / 0.01))
        return [
            -10.0 if any(a <= start + i * 0.01 < b for a, b in loud) else -30.0
            for i in range(count)
        ]

    monkeypatch.setattr(render.audio, "loudness_envelope", envelope)
    return calls


def test_the_first_spoken_line_zooms_in_and_back_out_after_the_max_hold(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx, _ = zoom_rig(project, monkeypatch)

    with caplog.at_level("INFO", logger="clipmachine"):
        render.run(ctx)

    clips = ctx.run_dir / "clips"
    track = TrackFile.model_validate_json((clips / "c01.track.json").read_text(encoding="utf-8"))
    cy = track.shots[0].face_cy
    assert cy == pytest.approx(0.5)
    zoomed = zoom_box(CropBox(810, 1440, track.samples[0].crop_x, 0), 1.12, 0.5, 2560, 1440)
    assert (zoomed.w, zoomed.h) == (724, 1286)
    # the opening zoom is in the filter's own crop; the sendcmd file brings the window back
    assert vf_of(fake.calls[0][1]).startswith(
        "sendcmd=f=clips/c01.crop.cmd,"
        f"crop={zoomed.w}:{zoomed.h}:{zoomed.x}:{zoomed.y},scale=1080:1920,setsar=1,"
    )
    text = (clips / "c01.crop.cmd").read_text(encoding="utf-8")
    spans = [ZoomSpan(0.0, 6.0, 0.5)]  # no audio: the first line alone, held for max_hold_s
    assert text == expected_zoom_commands(ctx, "c01", 20.0, spans)
    out_line = next(line for line in text.splitlines() if line.startswith("6.000000 "))
    assert out_line.endswith("crop y 0, crop w 810, crop h 1440;")  # growing: x, y, w, h
    assert "10.000000 crop x" in text  # the speaker switch still moves the (zoomed) window
    data = read_json(clips / "c01.json")
    assert data["zoom_count"] == 1 and data["zoom_spans"] == [[0.0, 6.0]]
    assert data["notes"] == ["zoom_no_audio"]
    assert data["crop_changes"] == len(text.splitlines())
    assert data["crop"] == {"w": 724, "h": 1286, "x": zoomed.x, "y": zoomed.y}
    settings = data["settings"]
    assert isinstance(settings, dict)
    assert settings["zoom_enabled"] is True and settings["zoom_scale"] == 1.12
    assert (settings["zoom_max_upscale"], settings["zoom_min_gap_s"]) == (2.0, 4.0)
    line = next(r.getMessage() for r in caplog.records if "render: c01" in r.getMessage())
    assert ", 1 zooms)" in line


def test_the_loudest_sentences_are_zoomed_by_the_rule(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = zoom_rig(project, monkeypatch, audio_file=True)
    # clip-relative 4 s, 12 s and 16 s are the three loudest of c01's ten sentences (30%)
    calls = fake_loudness(monkeypatch, [(14.0, 14.4), (22.0, 22.4), (26.0, 26.4)])

    render.run(ctx)

    assert calls == [(10.0, 30.0), (40.0, 62.5)]  # one measurement per clip
    # 0 s: first line, held until the pick 4 s later. 4 s is too close to that zoom-out. 12 s is
    # after the speaker switch at 10 s and holds until the pick at 16 s. 16 s is too close.
    spans = [ZoomSpan(0.0, 4.0, 0.5), ZoomSpan(12.0, 16.0, 0.5)]
    clips = ctx.run_dir / "clips"
    text = (clips / "c01.crop.cmd").read_text(encoding="utf-8")
    assert text == expected_zoom_commands(ctx, "c01", 20.0, spans)
    data = read_json(clips / "c01.json")
    assert data["zoom_count"] == 2 and data["zoom_spans"] == [[0.0, 4.0], [12.0, 16.0]]
    assert data["notes"] == []  # there is audio


def test_a_sentence_in_progress_at_the_clip_start_counts_from_zero_and_the_clip_may_end_zoomed(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = zoom_rig(project, monkeypatch, audio_file=True)
    # sentences end after the words at 4, 12, 22 and 32 s: the clip (10-30 s) starts inside the
    # one that began at 6 s, then has the sentences from 14 s and from 24 s (clip time 0, 4, 14)
    punctuate(ctx.run_dir, {4, 12, 22, 32})
    fake_loudness(monkeypatch, [(24.0, 29.0)])

    render.run(ctx)

    spans = [ZoomSpan(0.0, 6.0, 0.5), ZoomSpan(14.0, 20.0, 0.5)]
    text = (ctx.run_dir / "clips" / "c01.crop.cmd").read_text(encoding="utf-8")
    assert text == expected_zoom_commands(ctx, "c01", 20.0, spans)
    assert not any(line.startswith("20.000000 ") for line in text.splitlines())  # ends zoomed in
    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["zoom_spans"] == [[0.0, 6.0], [14.0, 20.0]]


def test_a_source_too_small_for_the_zoom_skips_it_and_renders_as_without(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = zoom_rig(project, monkeypatch, big=False)  # 640x360

    render.run(ctx)

    clips = ctx.run_dir / "clips"
    assert vf_of(fake.calls[0][1]).startswith("sendcmd=f=clips/c01.crop.cmd,crop=202:360:60:0,")
    assert (clips / "c01.crop.cmd").read_text(encoding="utf-8") == expected_commands(
        ctx, "c01", 20.0
    )
    data = read_json(clips / "c01.json")
    assert data["notes"] == ["zoom_skipped_upscale"]
    assert data["zoom_count"] == 0 and data["zoom_spans"] == []
    assert data["settings"]["zoom_enabled"] is True  # type: ignore[index]


def test_a_higher_max_upscale_lets_a_small_source_zoom(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = zoom_rig(
        project, monkeypatch, big=False, zoom=PUNCH.model_copy(update={"max_upscale": 10.0})
    )

    render.run(ctx)

    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["zoom_count"] == 1 and data["notes"] == ["zoom_no_audio"]


def test_a_track_without_a_face_gets_no_zoom_and_says_so(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = zoom_rig(project, monkeypatch, track_fakes.nobody)

    render.run(ctx)

    assert vf_of(fake.calls[0][1]).startswith("crop=810:1440:874:0,scale=1080:1920,")
    assert not [n for n in clip_files(ctx.run_dir) if n.endswith(".crop.cmd")]
    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["track_fallback"] == "no_face"
    assert data["notes"] == ["zoom_no_track"]
    assert data["zoom_count"] == 0 and data["crop_changes"] == 0


def test_centre_crop_mode_with_zoom_on_notes_that_there_is_no_track(
    project: Path, fake: FakeFfmpeg
) -> None:
    ctx = make_ctx(project)
    write_inputs(ctx.run_dir, **BIG)
    ctx.config.app.video.zoom = ZoomConfig(enabled=True)

    render.run(ctx)

    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["crop_mode"] == "center"
    assert data["notes"] == ["zoom_no_track"] and data["zoom_count"] == 0
    assert not [n for n in clip_files(ctx.run_dir) if n.endswith(".crop.cmd")]


def test_with_zoom_off_the_command_file_and_log_are_what_they_were_before_brief_r2(
    project: Path,
    fake: FakeFfmpeg,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx, _ = speaker_ctx(project, monkeypatch)  # zoom is off in the project fixture
    write_inputs(ctx.run_dir, **BIG)
    calls: list[object] = []
    monkeypatch.setattr(render.ffmpeg, "crop_box_commands", lambda *a, **k: calls.append(a) or "")

    with caplog.at_level("INFO", logger="clipmachine"):
        render.run(ctx)

    clips = ctx.run_dir / "clips"
    assert calls == []  # the old writer is the only one used
    assert (clips / "c01.crop.cmd").read_text(encoding="utf-8") == expected_commands(
        ctx, "c01", 20.0
    )
    assert vf_of(fake.calls[0][1]).startswith("sendcmd=f=clips/c01.crop.cmd,crop=810:1440:")
    data = read_json(clips / "c01.json")
    assert data["zoom_count"] == 0 and data["zoom_spans"] == [] and data["notes"] == []
    assert data["settings"]["zoom_enabled"] is False  # type: ignore[index]
    assert data["settings"]["zoom_scale"] == 0.0  # type: ignore[index]
    line = next(r.getMessage() for r in caplog.records if "render: c01" in r.getMessage())
    assert "zooms" not in line and "1 crop changes" in line


def test_resume_keeps_a_zoomed_clip_and_rerenders_when_zoom_is_toggled_or_tuned(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = zoom_rig(project, monkeypatch)
    render.run(ctx)
    assert fake.ids == ["c01", "c02"]

    render.run(resume(ctx))
    assert fake.ids == ["c01", "c02"]  # same zoom: nothing rendered again

    ctx.config.app.video.zoom = PUNCH.model_copy(update={"scale": 1.2})
    render.run(resume(ctx))
    assert fake.ids == ["c01", "c02", "c01", "c02"]  # the scale is part of the settings

    ctx.config.app.video.zoom = ZoomConfig(enabled=False)
    render.run(resume(ctx))
    assert fake.ids == ["c01", "c02", "c01", "c02", "c01", "c02"]  # zoom turned off
    assert read_json(ctx.run_dir / "clips" / "c01.json")["zoom_count"] == 0


def test_resume_keeps_output_rendered_before_brief_r2_while_zoom_is_off(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = speaker_ctx(project, monkeypatch)
    render.run(ctx)
    for clip_id in ("c01", "c02"):  # a JSON file as the earlier version wrote it
        path = ctx.run_dir / "clips" / f"{clip_id}.json"
        data = read_json(path)
        settings = data["settings"]
        assert isinstance(settings, dict)
        for key in [k for k in settings if k.startswith("zoom_")]:
            del settings[key]
        del data["zoom_count"], data["zoom_spans"]
        write_json_atomic(path, data)

    render.run(resume(ctx))

    assert fake.ids == ["c01", "c02"]  # still valid

    ctx.config.app.video.zoom = ZoomConfig(enabled=True)
    render.run(resume(ctx))
    assert fake.ids == ["c01", "c02", "c01", "c02"]  # zoom on makes them stale


def test_the_hook_looks_for_faces_in_the_zoomed_windows(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = zoom_rig(project, monkeypatch, style="box_karaoke")
    ctx.config.app.hook = ctx.config.app.hook.model_copy(update={"position": "avoid_faces"})
    seen: list[object] = []
    real = render.face_rects

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("boxes"))
        return real(*args, **kwargs)

    monkeypatch.setattr(render, "face_rects", spy)

    render.run(ctx)

    first = seen[0]
    assert isinstance(first, tuple) and len(first) >= 2  # the zoom-in at 0 s and the out at 6 s
    assert first[0][0] == 0.0 and first[0][1].w == 724  # the window the hook starts under


# --- smooth zooms (Brief R2.1) -------------------------------------------------------------


def cmd_times(text: str) -> list[float]:
    return [float(line.split(" ", 1)[0]) for line in text.splitlines()]


def test_punch_style_writes_exactly_the_r2_commands_and_no_ramps(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = zoom_rig(project, monkeypatch, zoom=PUNCH)

    render.run(ctx)

    clips = ctx.run_dir / "clips"
    text = (clips / "c01.crop.cmd").read_text(encoding="utf-8")
    assert text == expected_zoom_commands(ctx, "c01", 20.0, [ZoomSpan(0.0, 6.0, 0.5)])
    data = read_json(clips / "c01.json")
    assert data["zoom_ramps"] == []
    settings = data["settings"]
    assert isinstance(settings, dict)
    assert (settings["zoom_style"], settings["zoom_ramp_s"]) == ("punch", 0.4)


def test_a_smooth_zoom_writes_one_line_per_frame_inside_each_ramp(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = zoom_rig(project, monkeypatch, zoom=SMOOTH)

    render.run(ctx)

    clips = ctx.run_dir / "clips"
    text = (clips / "c01.crop.cmd").read_text(encoding="utf-8")
    times = cmd_times(text)
    # 30 fps, ramps [0, 0.4] and [5.6, 6.0]: frames 1..12 and 169..180
    assert [t for t in times if t <= 0.4 + 1e-6] == pytest.approx(
        [n / 30 for n in range(1, 13)], abs=1e-6
    )
    assert [t for t in times if 5.6 < t <= 6.0 + 1e-6] == pytest.approx(
        [n / 30 for n in range(169, 181)], abs=1e-6
    )
    assert not [t for t in times if 0.4 + 1e-6 < t < 5.6]  # the hold is still
    assert "10.000000 crop x" in text  # the speaker switch at the boundary
    data = read_json(clips / "c01.json")
    assert data["zoom_ramps"] == [[0.0, 0.4], [5.6, 6.0]]
    assert data["zoom_count"] == 1 and data["zoom_spans"] == [[0.0, 6.0]]
    assert data["crop_changes"] == len(times)
    settings = data["settings"]
    assert isinstance(settings, dict)
    assert (settings["zoom_style"], settings["zoom_ramp_s"]) == ("smooth", 0.4)


def test_a_smooth_opening_zoom_eases_in_from_the_base_window(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = zoom_rig(project, monkeypatch, zoom=SMOOTH)

    render.run(ctx)

    track = TrackFile.model_validate_json(
        (ctx.run_dir / "clips" / "c01.track.json").read_text(encoding="utf-8")
    )
    base = f"crop=810:1440:{track.samples[0].crop_x}:0,scale=1080:1920,setsar=1,"
    assert vf_of(fake.calls[0][1]).startswith("sendcmd=f=clips/c01.crop.cmd," + base)
    text = (ctx.run_dir / "clips" / "c01.crop.cmd").read_text(encoding="utf-8")
    assert cmd_times(text)[0] == pytest.approx(1 / 30, abs=1e-6) and cmd_times(text)[0] > 0.0
    data = read_json(ctx.run_dir / "clips" / "c01.json")
    assert data["crop"]["w"] == 810 and data["crop"]["h"] == 1440  # type: ignore[index]


def test_the_hook_looks_for_faces_in_the_per_frame_boxes_of_a_smooth_zoom(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = zoom_rig(project, monkeypatch, zoom=SMOOTH, style="box_karaoke")
    ctx.config.app.hook = ctx.config.app.hook.model_copy(update={"position": "avoid_faces"})
    seen: list[object] = []
    real = render.face_rects

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("boxes"))
        return real(*args, **kwargs)

    monkeypatch.setattr(render, "face_rects", spy)

    render.run(ctx)

    first = seen[0]
    assert isinstance(first, tuple) and len(first) > 20  # a box for every ramp frame
    assert first[0][1].w == 810


def test_resume_rerenders_when_the_zoom_style_changes_from_punch_to_smooth(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = zoom_rig(project, monkeypatch, zoom=PUNCH)
    render.run(ctx)
    assert fake.ids == ["c01", "c02"]

    ctx.config.app.video.zoom = PUNCH.model_copy(update={"style": "smooth"})
    render.run(resume(ctx))
    assert fake.ids == ["c01", "c02", "c01", "c02"]
    assert read_json(ctx.run_dir / "clips" / "c01.json")["zoom_ramps"] != []

    ctx.config.app.video.zoom = PUNCH.model_copy(update={"style": "smooth", "ramp_s": 0.2})
    render.run(resume(ctx))
    assert len(fake.ids) == 6  # the ramp length is part of the settings too


def test_a_clip_ending_zoomed_has_no_ramp_out_for_its_last_zoom(
    project: Path, fake: FakeFfmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = zoom_rig(project, monkeypatch, audio_file=True, zoom=SMOOTH)
    punctuate(ctx.run_dir, {4, 12, 22, 32})
    fake_loudness(monkeypatch, [(24.0, 29.0)])

    render.run(ctx)

    data = read_json(ctx.run_dir / "clips" / "c01.json")
    # spans (0, 6) and (14, 20): the second ends with the clip, so it has no ramp-out
    assert data["zoom_ramps"] == [[0.0, 0.4], [5.6, 6.0], [14.0, 14.4]]
    assert "zoom_ramp_shortened" not in data["notes"]  # type: ignore[operator]
