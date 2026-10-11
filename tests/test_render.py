"""The render stage with ffmpeg and ffprobe faked. All data is synthetic."""

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest

from clipmachine import pipeline
from clipmachine.config import ResolvedConfig, load_resolved
from clipmachine.context import RunContext
from clipmachine.models import (
    AudioStreamInfo,
    FetchMeta,
    MediaInfo,
    ScoreConfig,
    ScoredClip,
    ScoredFile,
    ScoredSummary,
    TranscribeConfig,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
    VideoStreamInfo,
)
from clipmachine.pipeline import load_state, run_pipeline, start_new_run
from clipmachine.runs import RunError, write_json_atomic
from clipmachine.stages import render
from clipmachine.utils.ffmpeg import MediaError

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


def make_ctx(project: Path, *, resuming: bool = False, run_name: str = "run") -> RunContext:
    run_dir = project / "runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    resolved = load_resolved(project, "example-client")
    # the shipped default offset is 0.15; these tests assume captions at their spoken time
    captions = resolved.app.captions.model_copy(update={"offset_seconds": 0.0})
    app = resolved.app.model_copy(update={"captions": captions})
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


def test_speaker_crop_is_refused_until_the_render_stage_supports_it(
    project: Path, fake: FakeFfmpeg
) -> None:
    ctx = make_ctx(project)
    write_inputs(ctx.run_dir)
    ctx.config.app.video.crop = "speaker"

    with pytest.raises(RunError, match="not implemented in the render stage yet"):
        render.run(ctx)

    assert fake.calls == []
