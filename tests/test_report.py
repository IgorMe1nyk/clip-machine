"""The report stage. All data is synthetic; no ffmpeg, no network."""

import json
import re
from datetime import date
from pathlib import Path

import pytest

from clipmachine import pipeline
from clipmachine.context import RunContext
from clipmachine.models import (
    ClipCropBox,
    CostsFile,
    CostTotals,
    RenderedClip,
    RenderedOutput,
    RenderedSource,
    ScoredClip,
    ScoredFile,
    Transcript,
)
from clipmachine.pipeline import load_state, run_pipeline, start_new_run
from clipmachine.runs import RunError, write_json_atomic
from clipmachine.stages import render, report
from test_render import make_ctx, scored_clip, write_inputs

NASTY = "<script>alert('x')</script> \"q\" & more"
ESCAPED = "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &quot;q&quot; &amp; more"


def kept(clip_id: str, rank: int, start: float, end: float, **changes: object) -> ScoredClip:
    return scored_clip(clip_id, rank, start, end).model_copy(update=changes)


def put_scored(run_dir: Path, clips: list[ScoredClip], **summary: object) -> None:
    path = run_dir / "scored.json"
    scored = ScoredFile.model_validate_json(path.read_text(encoding="utf-8"))
    n_kept = sum(1 for c in clips if c.kept)
    fields = {"candidates": len(clips), "kept": n_kept, "rejected": len(clips) - n_kept}
    scored.summary = scored.summary.model_copy(update={**fields, **summary})
    scored.clips = clips
    write_json_atomic(path, scored.model_dump(mode="json"))


def put_render(ctx: RunContext, clip: ScoredClip, *, mp4: bool = True, record: bool = True) -> None:
    folder = ctx.run_dir / "clips"
    folder.mkdir(exist_ok=True)
    if mp4:
        (folder / f"{clip.id}.mp4").write_bytes(b"fake")
    if record:
        info = RenderedClip(
            id=clip.id,
            rank=clip.rank,
            score=clip.score,
            start=clip.start,
            end=clip.end,
            duration=clip.end - clip.start,
            source=RenderedSource(
                width=640,
                height=360,
                rotation=0,
                r_frame_rate="30/1",
                avg_frame_rate="30/1",
                variable_frame_rate=False,
            ),
            crop=ClipCropBox(w=202, h=360, x=219, y=0),
            settings=render._settings(ctx),
            caption_events=1,
            output=RenderedOutput(
                width=1080,
                height=1920,
                video_codec="h264",
                audio_codec="aac",
                duration_seconds=clip.end - clip.start,
                size_bytes=4,
            ),
            render_seconds=0.1,
        )
        write_json_atomic(folder / f"{clip.id}.json", info.model_dump(mode="json"))


def put_costs(run_dir: Path, booked: float) -> None:
    totals = CostTotals(
        calls=1,
        refusals=0,
        input_tokens=1,
        output_tokens=1,
        thinking_tokens=0,
        estimated_usd=booked,
        actual_usd=booked,
        booked_usd=booked,
    )
    costs = CostsFile(totals=totals, records=[])
    write_json_atomic(run_dir / "costs.json", costs.model_dump(mode="json"))


def put_run_json(run_dir: Path, spans: dict[str, tuple[str, str | None]]) -> None:
    stages = {
        name: {"status": "done", "started_at": a, "finished_at": b, "error": None}
        for name, (a, b) in spans.items()
    }
    state = {
        "schema_version": 1,
        "input": "x.mp4",
        "client": "example-client",
        "created_at": "2026-10-10T00:00:00+00:00",
        "stages": stages,
    }
    write_json_atomic(run_dir / "run.json", state)


@pytest.fixture
def ctx(project: Path) -> RunContext:
    context = make_ctx(project, run_name="2026-10-10_sample")
    write_inputs(context.run_dir, [])
    put_run_json(
        context.run_dir,
        {
            "fetch": ("2026-10-10T10:00:00+00:00", "2026-10-10T10:00:10+00:00"),
            "score": ("2026-10-10T10:00:10+00:00", "2026-10-10T10:01:40+00:00"),
            "report": ("2026-10-10T10:01:40+00:00", None),
        },
    )
    return context


def build(ctx: RunContext, clips: list[ScoredClip], **summary: object) -> str:
    put_scored(ctx.run_dir, clips, **summary)
    for clip in clips:
        if clip.kept:
            put_render(ctx, clip)
    report.run(ctx)
    return (ctx.run_dir / "report.html").read_text(encoding="utf-8")


def cards(page: str) -> list[str]:
    return re.findall(r'<section class="card">.*?</section>', page, flags=re.S)


def stage_run_dir(project: Path) -> tuple[Path, RunContext]:
    """A real run folder (from start_new_run) holding synthetic stage outputs and one render."""
    media = project / "sample.mp4"
    media.write_bytes(b"")
    run_dir = start_new_run(project, media, "example-client", date(2026, 10, 12))
    context = RunContext(
        run_dir=run_dir,
        config=make_ctx(project).config,
        input_path=media,
        project_root=project,
    )
    write_inputs(run_dir, [])
    clip = kept("c01", 1, 10.0, 30.0)
    put_scored(run_dir, [clip])
    put_render(context, clip)
    return run_dir, context


# --- safety -------------------------------------------------------------------------------


def test_untrusted_text_is_escaped(ctx: RunContext) -> None:
    clip = kept("c01", 1, 10.0, 30.0, hook_line=NASTY, suggested_title=NASTY, reason=NASTY)
    rejected = scored_clip("c02", None, 40.0, 60.0).model_copy(update={"reason": NASTY})
    transcript_path = ctx.run_dir / "transcript.json"
    transcript = Transcript.model_validate_json(transcript_path.read_text(encoding="utf-8"))
    transcript.segments[0].words[5].word = f" {NASTY}"  # t=10, inside c01
    write_json_atomic(transcript_path, transcript.model_dump(mode="json"))

    page = build(ctx, [clip, rejected])

    assert "<script" not in page
    assert page.count(ESCAPED) == 5  # hook, title, reason, transcript word, rejected reason


def test_no_urls_scripts_or_external_resources(ctx: RunContext) -> None:
    url_text = "see http://example.com and https://example.org/x"
    clips = [kept("c01", 1, 10.0, 30.0, reason=url_text), scored_clip("c02", None, 40.0, 60.0)]
    page = build(ctx, clips)
    assert "http://" not in page
    assert "https://" not in page
    assert "<script" not in page.lower()
    assert 'name="viewport"' in page


def test_video_src_is_relative(ctx: RunContext) -> None:
    page = build(ctx, [kept("c01", 1, 10.0, 30.0), kept("c02", 2, 40.0, 62.5)])
    sources = re.findall(r'src="([^"]*)"', page)
    assert sources == ["clips/c01.mp4", "clips/c02.mp4"]
    for src in sources:
        assert "\\" not in src
        assert not re.match(r"^[A-Za-z]:", src)
        assert not src.startswith("/")
    assert 'preload="metadata"' in page
    assert 'width="360"' in page


# --- content ------------------------------------------------------------------------------


def test_cards_are_in_rank_order(ctx: RunContext) -> None:
    clips = [
        kept("c_third", 3, 70.0, 90.0),
        kept("c_first", 1, 10.0, 30.0),
        kept("c_second", 2, 40.0, 62.5),
    ]
    found = cards(build(ctx, clips))
    assert len(found) == 3
    for card, name in zip(found, ("c_first", "c_second", "c_third"), strict=True):
        assert name in card
    assert "#1" in found[0]
    assert "0:10–0:30" in found[0]
    assert "(20 s)" in found[0]


def test_badges_title_hook_reason_and_transcript(ctx: RunContext) -> None:
    clip = kept(
        "c01",
        1,
        10.0,
        16.0,
        trimmed=True,
        start_at_segment_edge=True,
        end_at_segment_edge=True,
        hook_line="HOOK-TEXT",
        suggested_title="TITLE-TEXT",
        reason="REASON-TEXT",
    )
    card = cards(build(ctx, [clip]))[0]
    for needle in ("trimmed", "starts at segment edge", "ends at segment edge"):
        assert needle in card
    for needle in ("HOOK-TEXT", "TITLE-TEXT", "REASON-TEXT", "score 8", "C1", "C9"):
        assert needle in card
    # words at t=10, 12, 14 are inside [10, 16]; t=16 starts at the end, so it is out
    assert "w10 w12 w14<" in card
    assert "<details><summary>Transcript</summary>" in card


def test_summary_counts_cost_and_time(ctx: RunContext) -> None:
    put_costs(ctx.run_dir, 0.0234)
    clips = [
        kept("c01", 1, 10.0, 30.0),
        scored_clip("c02", None, 40.0, 60.0),
        scored_clip("c03", None, 61.0, 80.0),
    ]
    reasons = {"weak_hook": 2, "off_brand": 0}
    page = build(ctx, clips, reject_reasons=reasons, dropped_top_n=1)
    assert "2026-10-10_sample" in page
    assert "1 kept of 3 candidates" in page
    assert "$0.02" in page
    assert "1:40 processing" in page  # 10 s + 90 s; the running report stage is left out
    assert "Source length 2:00" in page
    assert "weak_hook 2" in page
    assert "off_brand" not in page
    assert "dropped top_n 1" in page


def test_extended_badge_and_the_adjust_and_overlap_drops(ctx: RunContext) -> None:
    clip = kept("c01", 1, 10.0, 30.0, extended_start=1)
    dropped = scored_clip("c02", None, 40.0, 60.0).model_copy(
        update={"verdict": "keep", "reject_reason": None, "dropped_by": "overlap"}
    )
    page = build(ctx, [clip, dropped], dropped_overlap=1, adjust_ignored=2, dropped_trim_ignored=1)
    assert "extended" in cards(page)[0]
    assert "dropped overlap 1" in page
    assert "dropped adjust_ignored 3" in page  # new and old files are counted together
    assert "dropped: overlap" in page


def test_missing_costs_file_is_zero(ctx: RunContext) -> None:
    assert "$0.00" in build(ctx, [kept("c01", 1, 10.0, 30.0)])


def test_unreadable_costs_file_is_an_error(ctx: RunContext) -> None:
    (ctx.run_dir / "costs.json").write_text("{not json", encoding="utf-8")
    put_scored(ctx.run_dir, [kept("c01", 1, 10.0, 30.0)])
    with pytest.raises(RunError, match="costs.json"):
        report.run(ctx)


def test_rejected_list_shows_reasons_and_dropped(ctx: RunContext) -> None:
    dropped = kept("c02", 2, 40.0, 60.0).model_copy(
        update={"kept": False, "rank": None, "dropped_by": "min_score", "reason": "DROP-WHY"}
    )
    rejected = scored_clip("c03", None, 61.0, 80.0).model_copy(update={"reason": "REJ-WHY"})
    page = build(ctx, [kept("c01", 1, 10.0, 30.0), dropped, rejected])
    section = page[page.index('<details class="rejected"') :]
    assert section.startswith('<details class="rejected">')  # closed when something is kept
    assert "weak_hook" in section and "REJ-WHY" in section
    assert "dropped: min_score" in section and "DROP-WHY" in section
    assert "c03" in section and "<video" not in section
    assert page.count("<video") == 1


def test_zero_kept_state(ctx: RunContext) -> None:
    page = build(ctx, [scored_clip("c01", None, 10.0, 30.0)])
    assert "No clips passed the judge." in page
    assert '<details class="rejected" open>' in page
    assert "<video" not in page
    assert cards(page) == []


# --- render problems ----------------------------------------------------------------------


def test_render_missing_for_one_clip_only(ctx: RunContext) -> None:
    good = kept("c01", 1, 10.0, 30.0)
    bad = kept("c02", 2, 40.0, 62.5)
    put_scored(ctx.run_dir, [good, bad])
    put_render(ctx, good)
    put_render(ctx, bad, mp4=False)
    report.run(ctx)
    page = (ctx.run_dir / "report.html").read_text(encoding="utf-8")
    first, second = cards(page)
    assert "<video" in first and "Render missing" not in first
    assert "<video" not in second
    assert "Render missing: " in second
    assert str(ctx.run_dir) not in page


@pytest.mark.parametrize("what", ["no_json", "bad_json", "other_times", "other_id"])
def test_render_record_problems(ctx: RunContext, what: str) -> None:
    clip = kept("c01", 1, 10.0, 30.0)
    put_scored(ctx.run_dir, [clip])
    put_render(ctx, clip, record=what != "no_json")
    info = ctx.run_dir / "clips" / "c01.json"
    if what == "bad_json":
        info.write_text("{nope", encoding="utf-8")
    elif what in ("other_times", "other_id"):
        data = json.loads(info.read_text(encoding="utf-8"))
        if what == "other_times":
            data["end"] = 31.0
        else:
            data["id"] = "c99"
        info.write_text(json.dumps(data), encoding="utf-8")
    report.run(ctx)
    page = (ctx.run_dir / "report.html").read_text(encoding="utf-8")
    assert "Render missing: " in page
    assert "<video" not in page


def test_unsafe_clip_id_has_no_player(ctx: RunContext) -> None:
    clip = kept("..\\evil<b>", 1, 10.0, 30.0)
    put_scored(ctx.run_dir, [clip])
    report.run(ctx)
    page = (ctx.run_dir / "report.html").read_text(encoding="utf-8")
    assert "Render missing: " in page
    assert "<video" not in page
    assert "evil<b>" not in page


# --- the stage ----------------------------------------------------------------------------


def test_missing_scored_json_names_the_score_stage(project: Path) -> None:
    context = make_ctx(project, run_name="empty")
    with pytest.raises(RunError, match="score stage"):
        report.run(context)
    assert not (context.run_dir / "report.html").exists()


def test_run_writes_report_and_logs_counts_only(
    ctx: RunContext, caplog: pytest.LogCaptureFixture
) -> None:
    clip = kept("c01", 1, 10.0, 30.0, hook_line="SECRET-HOOK", suggested_title="SECRET-TITLE")
    with caplog.at_level("INFO", logger="clipmachine"):
        build(ctx, [clip])
    assert "report: 1 kept of 1 candidates" in caplog.text
    assert "SECRET" not in caplog.text
    assert "w10" not in caplog.text
    assert [p.name for p in ctx.run_dir.iterdir() if p.name.endswith(".tmp")] == []


def test_pipeline_from_stage_report(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir, _ = stage_run_dir(project)
    monkeypatch.setitem(pipeline.STAGES, "report", report.run)

    first = run_pipeline(run_dir, project_root=project)
    assert first.ok and "report" in first.ran
    assert (run_dir / "report.html").is_file()
    assert load_state(run_dir).stages["report"].status == "done"

    (run_dir / "report.html").unlink()
    again = run_pipeline(run_dir, from_stage="report", project_root=project)
    assert again.ok and again.ran == ["report"]
    assert "c01" in (run_dir / "report.html").read_text(encoding="utf-8")
