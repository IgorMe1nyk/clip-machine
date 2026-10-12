"""Stage 6: build report.html, the one-page review of a run.

Reads scored.json, clips/<id>.json, costs.json, run.json, meta.json and transcript.json and writes
<run folder>/report.html. One offline file: inline CSS, no JavaScript, no URLs, videos linked by
relative path. Everything that comes from the transcript, the model or a file name is escaped by
_esc(). The page holds transcript text, so it stays in the run folder and is never committed; the
log line holds counts only.
"""

import html
import logging
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from clipmachine.context import RunContext
from clipmachine.models import (
    CostsFile,
    FetchMeta,
    RenderedClip,
    RunState,
    ScoredClip,
    ScoredFile,
    Transcript,
)
from clipmachine.runs import RunError, write_text_atomic
from clipmachine.stages.candidates import _load_transcript
from clipmachine.stages.render import CLIP_ID_RE, CLIPS_DIR, _load_meta, _load_scored
from clipmachine.utils.captions import clip_words
from clipmachine.utils.claude import COSTS_FILE

log = logging.getLogger("clipmachine")

REPORT_HTML = "report.html"
RUN_JSON = "run.json"  # the same name as pipeline.RUN_JSON; importing pipeline here is circular

RUBRIC = (
    ("C1", "complete sentences"),
    ("C2", "makes sense alone"),
    ("C3", "hook in 3 s"),
    ("C4", "payoff"),
    ("C5", "captions right"),
    ("C6", "face in frame"),
    ("C7", "audio fine"),
    ("C8", "length ok"),
    ("C9", "no sponsor or off-limits"),
)

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: #f6f7f9; color: #1d2330;
  font: 16px/1.5 system-ui, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 960px; margin: 0 auto; }
h1 { font-size: 1.3rem; margin: 0 0 4px; overflow-wrap: anywhere; }
.summary, .card, details.rejected { background: #fff; border: 1px solid #d9dde5;
  border-radius: 8px; padding: 12px 16px; margin: 12px 0; }
.summary p { margin: 4px 0; }
.muted { color: #5b6475; }
.card { display: flex; flex-wrap: wrap; gap: 16px; }
.player { flex: 0 0 auto; max-width: 100%; }
video { display: block; width: 360px; max-width: 100%; background: #000; border-radius: 6px; }
.missing { width: 360px; max-width: 100%; padding: 16px; border: 1px dashed #b3261e;
  color: #b3261e; border-radius: 6px; }
.info { flex: 1 1 280px; min-width: 0; overflow-wrap: anywhere; }
.info h2 { font-size: 1.05rem; margin: 0 0 6px; }
.info p { margin: 6px 0; }
.badge { display: inline-block; padding: 0 8px; border-radius: 10px; font-size: .8rem;
  background: #e8edf7; color: #2a3b66; margin-left: 4px; }
.rubric { font-size: .85rem; color: #5b6475; }
summary { cursor: pointer; font-weight: 600; }
details.rejected ul { padding-left: 18px; margin: 8px 0 0; }
details.rejected li { margin: 8px 0; overflow-wrap: anywhere; }
.transcript { white-space: pre-wrap; margin: 6px 0 0; }
@media (max-width: 480px) { body { padding: 8px; } }
"""


def _esc(value: object) -> str:
    """The one escaping helper. Also breaks up "://" so no URL ever appears in the page."""
    return html.escape(str(value), quote=True).replace("://", ":&#47;&#47;")


def _mmss(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 60}:{total % 60:02d}"


def _hms(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _times(clip: ScoredClip) -> str:
    return f"{_mmss(clip.start)}–{_mmss(clip.end)}"


# --- inputs ---------------------------------------------------------------------------------


def _load_run_state(run_dir: Path) -> RunState:
    path = run_dir / RUN_JSON
    try:
        return RunState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise RunError(f"{RUN_JSON} is missing or not valid ({type(exc).__name__})") from exc


def _load_cost(run_dir: Path) -> float:
    """booked_usd from costs.json; no file means no API calls were made."""
    path = run_dir / COSTS_FILE
    if not path.is_file():
        return 0.0
    try:
        costs = CostsFile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise RunError(f"{COSTS_FILE} is not valid ({type(exc).__name__})") from exc
    return costs.totals.booked_usd


def _processing_seconds(state: RunState) -> float:
    """Sum of finished - started over stages that have both. The report stage is still running
    when this is called, so it has no finished_at and is left out."""
    total = 0.0
    for stage in state.stages.values():
        if stage.started_at is None or stage.finished_at is None:
            continue
        try:
            span = datetime.fromisoformat(stage.finished_at) - datetime.fromisoformat(
                stage.started_at
            )
        except ValueError:
            continue
        total += max(0.0, span.total_seconds())
    return total


def render_problem(run_dir: Path, clip: ScoredClip) -> str | None:
    """A short reason the kept clip has no usable render, or None when it looks fine."""
    if not CLIP_ID_RE.fullmatch(clip.id):
        return "unsafe clip id"
    clips_dir = run_dir / CLIPS_DIR
    if not (clips_dir / f"{clip.id}.mp4").is_file():
        return "video file not found"
    info = clips_dir / f"{clip.id}.json"
    if not info.is_file():
        return "clip record not found"
    try:
        rendered = RenderedClip.model_validate_json(info.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "clip record is not valid"
    if (rendered.id, rendered.start, rendered.end) != (clip.id, clip.start, clip.end):
        return "clip record does not match scored.json"
    return None


# --- html -----------------------------------------------------------------------------------


def _transcript_text(transcript: Transcript, clip: ScoredClip) -> str:
    words = clip_words(transcript.segments, clip.start, clip.end)
    return " ".join(w.text for w in words)


def _cut_line(label: str, kind: str, gap: float | None) -> str:
    gap_text = f" (gap {gap:.2f} s)" if gap is not None else ""
    return f'<span class="badge">{_esc(label)}: {_esc(kind)}{_esc(gap_text)}</span>'


def _cut_badges(clip: ScoredClip) -> str:
    """How the clip is cut (Brief 10); nothing for clips from older files."""
    badges = ""
    if clip.start_boundary == "start":
        badges += '<span class="badge">starts at the start of the recording</span>'
    elif clip.start_boundary is not None:
        badges += _cut_line("starts after", clip.start_boundary, clip.start_gap)
    if clip.end_boundary is not None:
        badges += _cut_line("ends on", clip.end_boundary, clip.end_gap)
    return badges


def _card(clip: ScoredClip, problem: str | None, text: str) -> str:
    badges = ""
    if clip.trimmed:
        badges += '<span class="badge">trimmed</span>'
    if clip.extended_start or clip.extended_end:
        badges += '<span class="badge">extended</span>'
    if clip.start_at_segment_edge:
        badges += '<span class="badge">starts at segment edge</span>'
    if clip.end_at_segment_edge:
        badges += '<span class="badge">ends at segment edge</span>'
    badges += _cut_badges(clip)
    if problem is None:
        player = (
            f'<video controls preload="metadata" width="360" '
            f'src="{CLIPS_DIR}/{_esc(clip.id)}.mp4"></video>'
        )
    else:
        player = f'<div class="missing">Render missing: {_esc(problem)}</div>'
    rubric = " &middot; ".join(f"<b>{code}</b> {_esc(label)}" for code, label in RUBRIC)
    return (
        '<section class="card">\n'
        f'<div class="player">{player}</div>\n'
        '<div class="info">\n'
        f"<h2>#{_esc(clip.rank)} &middot; {_esc(clip.id)} &middot; score {_esc(clip.score)}</h2>\n"
        f'<p class="muted">{_times(clip)} ({_esc(round(clip.end - clip.start))} s){badges}</p>\n'
        f"<p><b>Title:</b> {_esc(clip.suggested_title)}</p>\n"
        f"<p><b>Hook:</b> {_esc(clip.hook_line)}</p>\n"
        f"<p><b>Why kept:</b> {_esc(clip.reason)}</p>\n"
        f'<details><summary>Transcript</summary><p class="transcript">{_esc(text)}</p></details>\n'
        f'<p class="rubric">Rubric: {rubric}</p>\n'
        "</div>\n"
        "</section>"
    )


def _rejected_item(clip: ScoredClip) -> str:
    if clip.reject_reason is not None:
        why = str(clip.reject_reason)
    elif clip.dropped_by is not None:
        why = f"dropped: {clip.dropped_by}"
    else:
        why = "not kept"
    return (
        f"<li><b>{_esc(clip.id)}</b> {_times(clip)} &middot; score {_esc(clip.score)} "
        f"&middot; {_esc(why)}<br>{_esc(clip.reason)}</li>"
    )


def _summary(
    run_name: str,
    scored: ScoredFile,
    source_seconds: float | None,
    cost_usd: float,
    processing_seconds: float,
) -> str:
    s = scored.summary
    length = _hms(source_seconds) if source_seconds is not None else "unknown"
    reasons = [f"{name} {n}" for name, n in sorted(s.reject_reasons.items()) if n]
    dropped = [
        f"{name} {n}"
        for name, n in (
            ("dropped min_score", s.dropped_min_score),
            ("dropped top_n", s.dropped_top_n),
            ("dropped adjust_ignored", s.adjust_ignored + s.dropped_trim_ignored),
            ("dropped overlap", s.dropped_overlap),
        )
        if n
    ]
    lines = [
        f"<h1>{_esc(run_name)}</h1>",
        f'<p class="muted">Source length {_esc(length)}</p>',
        f"<p><b>{_esc(s.kept)} kept of {_esc(s.candidates)} candidates</b> &middot; "
        f"${cost_usd:.2f} &middot; {_esc(_hms(processing_seconds))} processing</p>",
    ]
    if reasons or dropped:
        joined = " · ".join(reasons + dropped)
        lines.append(f"<p>Rejected: {_esc(joined)}</p>")
    return '<header class="summary">\n' + "\n".join(lines) + "\n</header>"


def build_html(
    *,
    run_name: str,
    scored: ScoredFile,
    transcript: Transcript,
    source_seconds: float | None,
    cost_usd: float,
    processing_seconds: float,
    problems: dict[str, str | None],
) -> str:
    """The whole page. `problems` maps a kept clip id to its render problem (None = fine)."""
    kept = sorted((c for c in scored.clips if c.kept), key=lambda c: c.rank or 0)
    others = [c for c in scored.clips if not c.kept]
    parts = [_summary(run_name, scored, source_seconds, cost_usd, processing_seconds)]
    if not kept:
        parts.append('<p class="summary"><b>No clips passed the judge.</b></p>')
    for clip in kept:
        parts.append(_card(clip, problems.get(clip.id), _transcript_text(transcript, clip)))
    open_attr = " open" if not kept else ""
    items = "\n".join(_rejected_item(c) for c in others) or "<li>None.</li>"
    parts.append(
        f'<details class="rejected"{open_attr}>'
        f"<summary>Rejected candidates ({len(others)})</summary>\n<ul>\n{items}\n</ul></details>"
    )
    body = "\n".join(parts)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>Report: {_esc(run_name)}</title>\n"
        f"<style>{CSS}</style>\n</head>\n<body>\n<main>\n{body}\n</main>\n</body>\n</html>\n"
    )


def _source_seconds(meta: FetchMeta, transcript: Transcript) -> float | None:
    if meta.media.duration_seconds is not None:
        return meta.media.duration_seconds
    return transcript.audio_duration_seconds


def run(ctx: RunContext) -> None:
    run_dir = ctx.run_dir.resolve()
    scored = _load_scored(run_dir)
    meta = _load_meta(run_dir)
    transcript = _load_transcript(run_dir)
    state = _load_run_state(run_dir)
    cost = _load_cost(run_dir)

    problems = {c.id: render_problem(run_dir, c) for c in scored.clips if c.kept}
    page = build_html(
        run_name=run_dir.name,
        scored=scored,
        transcript=transcript,
        source_seconds=_source_seconds(meta, transcript),
        cost_usd=cost,
        processing_seconds=_processing_seconds(state),
        problems=problems,
    )
    write_text_atomic(run_dir / REPORT_HTML, page)
    missing = sum(1 for p in problems.values() if p is not None)
    log.info(
        f"report: {scored.summary.kept} kept of {scored.summary.candidates} candidates, "
        f"{len(scored.clips) - len(problems)} rejected listed, {missing} render missing, "
        f"{REPORT_HTML} written"
    )
