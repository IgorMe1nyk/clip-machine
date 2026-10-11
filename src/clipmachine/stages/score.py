"""Stage 4: candidates.json -> scored.json.

Claude Sonnet judges all candidates of a run together (in chunks of score.max_per_call): a score,
keep/reject, one fixed reject reason, a hook line, a title and, optionally, a tighter range of the
candidate's own sentences. We validate every answer, map a sentence range to times from the word
timestamps (the model never supplies a time), then keep the best clip.keep_top. scored.json is
the only output and is written last, so its presence means the stage finished; a rerun of a
finished stage is skipped by the pipeline and makes no API call.

Sentence numbering is the snapping rules by construction: the stage rebuilds the same sentence
index the candidates stage used and finds each candidate's sentences in it, so the numbers Sonnet
sees and the times we map back agree.

The candidate text is untrusted data: it goes to the model inside <candidates> tags with a system
instruction to ignore anything in it, tag look-alikes are neutralised, and nothing the model
returns is ever used as a path, a command or code. No transcript text, hook, title or model
reason is logged.
"""

import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from clipmachine.context import RunContext
from clipmachine.models import (
    REJECT_REASONS,
    Candidate,
    CandidatesFile,
    ClipScore,
    DroppedBy,
    KeepSentences,
    ScoreBatch,
    ScoredClip,
    ScoredFile,
    ScoredSummary,
)
from clipmachine.runs import RunError, write_json_atomic
from clipmachine.stages.candidates import (
    CANDIDATES_JSON,
    MAX_TEXT_CHARS,
    _load_transcript,
    _one_line,
    fill,
    load_prompt,
    rules_summary,
)
from clipmachine.stages.transcribe import REPO_ROOT
from clipmachine.utils.claude import ClaudeClient
from clipmachine.utils.snapping import (
    EPSILON,
    SentenceIndex,
    build_index,
    locate,
    span_times,
    words_text,
)

log = logging.getLogger("clipmachine")

SCORED_JSON = "scored.json"
TEMP_PATTERN = f".{SCORED_JSON}.*.tmp"
PROMPT_FILE = Path("prompts") / "score_clip.md"
PURPOSE = "score"
CANDIDATE_TAG_RE = re.compile(r"<\s*/?\s*candidates?[^>]*>?", re.IGNORECASE)
MAX_HOOK_WORDS = 10
MAX_REASON_WORDS = 25
MAX_PROBLEM_IDS = 5


@dataclass(frozen=True)
class Located:
    """A candidate and the sentences of the transcript it covers (indices, inclusive)."""

    candidate: Candidate
    first: int
    last: int

    @property
    def count(self) -> int:
        return self.last - self.first + 1


@dataclass(frozen=True)
class Trim:
    start: float
    end: float
    trimmed: bool
    note: str | None


@dataclass
class Draft:
    """One judged candidate before selection."""

    position: int  # index in candidates.json: the tie-break, earlier first
    candidate: Candidate
    clip: ClipScore
    trim: Trim
    notes: list[str]


def _remove_outputs(run_dir: Path, *, strict: bool) -> None:
    doomed = [run_dir / SCORED_JSON, *run_dir.glob(TEMP_PATTERN)]
    for path in doomed:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            if strict:
                raise


# --- sentences the model sees --------------------------------------------------------------------


def locate_all(
    index: SentenceIndex,
    candidates: list[Candidate],
    *,
    padding: float,
    duration: float,
) -> list[Located]:
    """Find every candidate's sentences, or fail without naming any text."""
    found = []
    for candidate in candidates:
        span = locate(index, candidate.start, candidate.end, padding=padding, duration=duration)
        if span is not None:
            a, b = span
            text = words_text(index, index.sentences[a].first, index.sentences[b].last)
            if text == candidate.text:
                found.append(Located(candidate, a, b))
                continue
        raise RunError(
            f"{CANDIDATES_JSON} does not match transcript.json (candidate {candidate.id}): "
            "rerun from the candidates stage"
        )
    return found


def candidate_block(located: Located, index: SentenceIndex) -> str:
    """One <candidate> element: numbered sentences, each with [start-end] seconds.

    The text is data, so it is made single-line and anything that looks like a candidate tag is
    removed.
    """
    candidate = located.candidate
    lines = [
        f'<candidate id="{candidate.id}" seconds="{candidate.duration:.1f}" '
        f'sentences="{located.count}">'
    ]
    for number, i in enumerate(range(located.first, located.last + 1), start=1):
        sentence = index.sentences[i]
        start = index.words[sentence.first].start
        end = index.words[sentence.last].end
        text = CANDIDATE_TAG_RE.sub(
            "[tag removed]", _one_line(words_text(index, sentence.first, sentence.last))
        )
        lines.append(f"{number} [{start:.2f}-{end:.2f}] {text}")
    lines.append("</candidate>")
    return "\n".join(lines)


def chunk(items: list[Located], size: int) -> list[list[Located]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# --- validation ----------------------------------------------------------------------------------


def ids_problem(expected: list[str]) -> Callable[[ScoreBatch], str | None]:
    """A check for ClaudeClient.call_structured: every expected id exactly once.

    The message may name only ids that are ours; an id the model invented is counted, never
    quoted, because the message is logged and sent back to the model.
    """

    def check(batch: ScoreBatch) -> str | None:
        counts = Counter(clip.id for clip in batch.clips)
        known = set(expected)
        missing = [i for i in expected if i not in counts]
        duplicate = [i for i in expected if counts[i] > 1]
        unknown = sum(n for i, n in counts.items() if i not in known)
        parts = []
        if missing:
            parts.append("missing ids: " + ", ".join(missing[:MAX_PROBLEM_IDS]))
        if duplicate:
            parts.append("duplicate ids: " + ", ".join(duplicate[:MAX_PROBLEM_IDS]))
        if unknown:
            parts.append(f"{unknown} answer(s) with an id that is not in the list")
        if not parts:
            return None
        return "; ".join(parts) + " (return each listed id exactly once)"

    return check


def resolve_trim(
    index: SentenceIndex,
    located: Located,
    keep: KeepSentences | None,
    *,
    padding: float,
    duration: float,
    min_length: float,
    max_length: float,
) -> Trim:
    """Times for the model's keep_sentences, or the candidate's own times with a note.

    first and last are the candidate's own sentence numbers (1..n). The new times come from those
    sentences' word times plus the same padding snapping uses. A range that is out of bounds, or
    that lands outside the clip length, is ignored and noted: the model never supplies a time.
    """
    candidate = located.candidate
    whole = Trim(candidate.start, candidate.end, False, None)
    if keep is None:
        return whole
    n = located.count
    if not 1 <= keep.first <= keep.last <= n:
        return Trim(
            candidate.start,
            candidate.end,
            False,
            f"trim ignored: sentences {keep.first}-{keep.last} are outside 1-{n}",
        )
    if keep.first == 1 and keep.last == n:
        return whole
    start, end = span_times(
        index,
        located.first + keep.first - 1,
        located.first + keep.last - 1,
        padding=padding,
        duration=duration,
    )
    length = end - start
    if length < min_length - EPSILON:
        reason = f"{length:.1f} s is under the {min_length:g} s minimum"
    elif length > max_length + EPSILON:
        reason = f"{length:.1f} s is over the {max_length:g} s maximum"
    else:
        return Trim(start, end, True, f"trimmed to sentences {keep.first}-{keep.last} of {n}")
    return Trim(
        candidate.start,
        candidate.end,
        False,
        f"trim to sentences {keep.first}-{keep.last} ignored: {reason}",
    )


# --- selection -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    ranks: dict[str, int]  # candidate id -> rank, kept clips only
    dropped: dict[str, DroppedBy]  # model said keep, we did not


def select(drafts: list[Draft], min_score: int, keep_top: int) -> Selection:
    """Kept = verdict keep and score >= min_score; best score first, ties to the earlier
    candidate; the first keep_top get rank 1..N."""
    dropped: dict[str, DroppedBy] = {}
    eligible = []
    for draft in drafts:
        if draft.clip.verdict != "keep":
            continue
        if draft.clip.score < min_score:
            dropped[draft.candidate.id] = "min_score"
        else:
            eligible.append(draft)
    ordered = sorted(eligible, key=lambda d: (-d.clip.score, d.position))
    ranks = {d.candidate.id: rank for rank, d in enumerate(ordered[:keep_top], start=1)}
    for draft in ordered[keep_top:]:
        dropped[draft.candidate.id] = "top_n"
    return Selection(ranks, dropped)


# --- the stage -----------------------------------------------------------------------------------


def run(ctx: RunContext) -> None:
    run_dir = ctx.run_dir.resolve()
    _remove_outputs(run_dir, strict=True)  # a rerun replaces the output cleanly
    try:
        _score(ctx, run_dir)
    except BaseException:
        _remove_outputs(run_dir, strict=False)
        raise


def _load_candidates(run_dir: Path) -> CandidatesFile:
    path = run_dir / CANDIDATES_JSON
    if not path.is_file():
        raise RunError(
            f"{CANDIDATES_JSON} not found in the run folder; run the candidates stage first"
        )
    try:
        return CandidatesFile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunError(
            f"{CANDIDATES_JSON} is not a valid candidates file ({type(exc).__name__})"
        ) from exc


def _words(text: str) -> int:
    return len(text.split())


def _score(ctx: RunContext, run_dir: Path) -> None:
    app = ctx.config.app
    cfg = app.score
    root = ctx.project_root if ctx.project_root is not None else REPO_ROOT
    transcript = _load_transcript(run_dir)
    found = _load_candidates(run_dir)
    candidates = found.candidates
    min_length, max_length = app.clip.min_seconds, app.clip.max_seconds
    padding = found.settings.snap_padding_seconds
    duration = transcript.audio_duration_seconds

    warnings: list[str] = []
    if not candidates:
        warnings.append("there are no candidates, so nothing was scored")

    index = build_index(transcript.segments, found.settings.fallback_max_words)
    located = locate_all(index, candidates, padding=padding, duration=duration)

    system_template, user_template = load_prompt(root, PROMPT_FILE)
    client = ClaudeClient(run_dir, root, app)
    summary_rules = rules_summary(ctx.config.rules)

    answers: dict[str, ClipScore] = {}
    groups = chunk(located, cfg.max_per_call)
    for number, group in enumerate(groups, start=1):
        values = {
            "rules_summary": summary_rules,
            "min_seconds": f"{min_length:g}",
            "max_seconds": f"{max_length:g}",
            "count": str(len(group)),
            "blocks": "\n".join(candidate_block(item, index) for item in group),
        }
        batch, _ = client.call_structured(
            f"{PURPOSE}:chunk-{number}",
            app.models.score,
            fill(system_template, values, PROMPT_FILE),
            fill(user_template, values, PROMPT_FILE),
            ScoreBatch,
            cfg.max_output_tokens,
            thinking=cfg.thinking,
            effort=cfg.effort,
            check=ids_problem([item.candidate.id for item in group]),
            repairs=cfg.repair_attempts,
        )
        for clip in batch.clips:
            answers[clip.id] = clip
        log.info(f"score: chunk {number}/{len(groups)}: {len(batch.clips)} judged")

    drafts: list[Draft] = []
    for position, item in enumerate(located):
        clip = answers[item.candidate.id]
        trim = resolve_trim(
            index,
            item,
            clip.keep_sentences,
            padding=padding,
            duration=duration,
            min_length=min_length,
            max_length=max_length,
        )
        notes = [trim.note] if trim.note else []
        if _words(clip.hook_line) > MAX_HOOK_WORDS:
            notes.append(f"hook_line is {_words(clip.hook_line)} words (over {MAX_HOOK_WORDS})")
        if _words(clip.reason) > MAX_REASON_WORDS:
            notes.append(f"reason is {_words(clip.reason)} words (over {MAX_REASON_WORDS})")
        drafts.append(Draft(position, item.candidate, clip, trim, notes))

    selection = select(drafts, cfg.min_score, app.clip.keep_top)
    scored: list[ScoredClip] = []
    for draft in drafts:
        clip, candidate_id = draft.clip, draft.candidate.id
        dropped_by = selection.dropped.get(candidate_id)
        notes = list(draft.notes)
        if dropped_by == "min_score":
            notes.append(f"score {clip.score} is under min_score {cfg.min_score}")
        elif dropped_by == "top_n":
            notes.append(f"outside the top {app.clip.keep_top}")
        scored.append(
            ScoredClip(
                id=candidate_id,
                rank=selection.ranks.get(candidate_id),
                kept=candidate_id in selection.ranks,
                score=clip.score,
                verdict=clip.verdict,
                reject_reason=clip.reject_reason,
                dropped_by=dropped_by,
                hook_line=_one_line(clip.hook_line)[:MAX_TEXT_CHARS],
                suggested_title=_one_line(clip.suggested_title)[:MAX_TEXT_CHARS],
                reason=_one_line(clip.reason)[:MAX_TEXT_CHARS],
                start=draft.trim.start,
                end=draft.trim.end,
                candidate_start=draft.candidate.start,
                candidate_end=draft.candidate.end,
                trimmed=draft.trim.trimmed,
                notes=notes,
            )
        )

    reasons = {reason: 0 for reason in REJECT_REASONS}
    for entry in scored:
        if entry.reject_reason is not None:
            reasons[entry.reject_reason] += 1
    totals = client.session_totals()
    summary = ScoredSummary(
        candidates=len(scored),
        kept=sum(1 for s in scored if s.kept),
        rejected=sum(1 for s in scored if s.verdict == "reject"),
        dropped_min_score=sum(1 for s in scored if s.dropped_by == "min_score"),
        dropped_top_n=sum(1 for s in scored if s.dropped_by == "top_n"),
        trimmed=sum(1 for s in scored if s.trimmed),
        reject_reasons=reasons,
        model=app.models.score,
        effort=cfg.effort,
        thinking=cfg.thinking,
        min_score=cfg.min_score,
        keep_top=app.clip.keep_top,
        chunks=len(groups),
        calls=totals.calls,
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        thinking_tokens=totals.thinking_tokens,
        estimated_usd=totals.estimated_usd,
        actual_usd=totals.actual_usd,
    )
    output = ScoredFile(settings=cfg, summary=summary, clips=scored, warnings=warnings)
    write_json_atomic(run_dir / SCORED_JSON, output.model_dump(mode="json"))
    for entry in scored:
        log.info(
            f"score: {entry.id} rank={entry.rank} kept={entry.kept} score={entry.score} "
            f"{entry.verdict} {entry.reject_reason} dropped_by={entry.dropped_by} "
            f"{entry.start:.2f}-{entry.end:.2f} s trimmed={entry.trimmed}"
        )
    log.info(
        f"score: {summary.kept} kept of {summary.candidates} ({summary.rejected} rejected, "
        f"{summary.dropped_min_score + summary.dropped_top_n} dropped by our rules, "
        f"{summary.trimmed} trimmed); cost ${summary.actual_usd:.4f}"
    )
