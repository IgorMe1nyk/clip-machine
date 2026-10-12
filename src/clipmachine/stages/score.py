"""Stage 4: candidates.json -> scored.json.

Claude Sonnet judges all candidates of a run together (in chunks of score.max_per_call): a score,
keep/reject, one fixed reject reason, a hook line, a title and, optionally, a new range of
sentences (the candidate's own, trimmed, or extended into the numbered context around it). We
validate every answer, map a sentence range to times from the word timestamps (the model
never supplies a time), drop clips that overlap a better one, then keep the best clip.keep_top.
scored.json is the only output and is written last, so its presence means the stage
finished; a rerun of a finished stage is skipped by the pipeline and makes no API call.

Sentence numbering is the snapping rules by construction: the stage rebuilds the same sentence
index the candidates stage used and finds each candidate's sentences in it, so the numbers Sonnet
sees and the times we map back agree.

The candidate text is untrusted data: it goes to the model inside <candidates> tags with a system
instruction to ignore anything in it, tag look-alikes are neutralised, and nothing the model
returns is ever used as a path, a command or code. No transcript text, hook, title or model
reason is logged.
"""

import logging
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
    dedupe,
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
MAX_OVERLAP = 0.5  # a clip is dropped when it overlaps a better one by more than this fraction
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
class CutInfo:
    """How the final clip is cut. Kinds are the sentence index's end kinds, or "start" for the
    beginning of the recording; gaps are seconds of silence, None where there is no neighbour."""

    start_boundary: str | None = None  # end kind of the sentence before the clip's first
    end_boundary: str | None = None  # end kind of the clip's last sentence
    start_gap: float | None = None  # previous word's end to the clip's first word
    end_gap: float | None = None  # clip's last word to the next word


@dataclass(frozen=True)
class Adjust:
    start: float
    end: float
    trimmed: bool  # a sentence of the candidate was removed
    note: str | None
    # why the model's keep_sentences was not used: outside the shown numbers, outside the length
    # limits, or a start or end where a clean cut is not possible
    ignored: Literal["range", "length", "boundary"] | None = None
    keep: KeepSentences | None = None  # the range that was applied, when changed
    extended_start: int = 0  # sentences added before the candidate's first
    extended_end: int = 0  # sentences added after the candidate's last


@dataclass
class Draft:
    """One judged candidate before selection."""

    position: int  # index in candidates.json: the tie-break, earlier first
    candidate: Candidate
    clip: ClipScore
    adjust: Adjust
    notes: list[str]
    sentences: int = 0  # sentences in the candidate
    cut: CutInfo = CutInfo()  # how the final clip is cut


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
    start_pad: float,
    end_pad: float,
    duration: float,
) -> list[Located]:
    """Find every candidate's sentences, or fail without naming any text."""
    found = []
    for candidate in candidates:
        span = locate(
            index,
            candidate.start,
            candidate.end,
            start_pad=start_pad,
            end_pad=end_pad,
            duration=duration,
        )
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


def _sentence_text(index: SentenceIndex, number: int) -> str:
    """One sentence as single-line data with candidate-tag look-alikes removed."""
    sentence = index.sentences[number]
    return CANDIDATE_TAG_RE.sub(
        "[tag removed]", _one_line(words_text(index, sentence.first, sentence.last))
    )


def context_sentences(
    index: SentenceIndex, located: Located, before: int, after: int
) -> tuple[list[int], list[int]]:
    """Sentence indices of the context: up to `before` sentences before the clip and `after`
    after it, clipped at the ends of the transcript. Never a sentence of the clip itself."""
    earlier = list(range(max(located.first - before, 0), located.first))
    later = list(range(located.last + 1, min(located.last + 1 + after, len(index.sentences))))
    return earlier, later


def _numbered(index: SentenceIndex, i: int, number: int) -> str:
    """`number [start-end start=ok|no end=kind] text`: the marks are ours and sit inside our own
    bracket, before the untrusted text. start=ok means a clip may start at this sentence; end is
    how the sentence ends (., ?, !, pause or edge)."""
    sentence = index.sentences[i]
    start = index.words[sentence.first].start
    end = index.words[sentence.last].end
    mark = "ok" if index.cut_before(i) else "no"
    return (
        f"{number} [{start:.2f}-{end:.2f} start={mark} end={sentence.end}] "
        f"{_sentence_text(index, i)}"
    )


def candidate_block(located: Located, index: SentenceIndex, before: int, after: int) -> str:
    """One <candidate> element: context before, the clip's sentences, context after, all numbered
    continuously with [start-end start=ok|no end=kind]: the context before is ... -1, 0, the clip
    is 1..n, the context after is n+1... keep_sentences may use any of these numbers.

    All text is data: made single-line, and anything that looks like a candidate tag is removed.
    """
    candidate = located.candidate
    earlier, later = context_sentences(index, located, before, after)
    n = located.count
    lines = [
        f'<candidate id="{candidate.id}" seconds="{candidate.duration:.1f}" sentences="{n}">',
        "<before>",
        *(_numbered(index, i, number) for number, i in enumerate(earlier, start=1 - len(earlier))),
        *([] if earlier else ["- (none: the recording starts here)"]),
        "</before>",
    ]
    for number, i in enumerate(range(located.first, located.last + 1), start=1):
        lines.append(_numbered(index, i, number))
    lines += [
        "<after>",
        *(_numbered(index, i, number) for number, i in enumerate(later, start=n + 1)),
        *([] if later else ["- (none: the recording ends here)"]),
        "</after>",
        "</candidate>",
    ]
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


def resolve_adjust(
    index: SentenceIndex,
    located: Located,
    keep: KeepSentences | None,
    *,
    before: int,
    after: int,
    start_pad: float,
    end_pad: float,
    duration: float,
    min_length: float,
    max_length: float,
) -> Adjust:
    """Times for the model's keep_sentences, or the candidate's own times with a note.

    first and last use the numbers the model was shown: the clip is 1..n, the context before it
    is ... -1, 0 and the context after it is n+1.... The new times come from those sentences'
    word times plus the same mid-gap cut times snapping uses. A range outside the shown numbers,
    one that contains none of the clip's own sentences, one that would start where a clip may not
    start (a weak end before it) or end on a weak end, or one that lands outside the clip length
    is ignored and noted: the model never supplies a time. The boundary check comes before the
    length check.
    """
    candidate = located.candidate
    whole = Adjust(candidate.start, candidate.end, False, None)
    if keep is None:
        return whole
    n = located.count
    earlier, later = context_sentences(index, located, before, after)
    low, high = 1 - len(earlier), n + len(later)
    note = ""
    if not low <= keep.first <= keep.last <= high:
        note = f"adjustment ignored: sentences {keep.first}-{keep.last} are outside {low}-{high}"
    elif keep.first > n or keep.last < 1:
        note = (
            f"adjustment ignored: sentences {keep.first}-{keep.last} contain none of the "
            f"clip's own sentences 1-{n}"
        )
    if note:
        return Adjust(candidate.start, candidate.end, False, note, ignored="range")
    if keep.first == 1 and keep.last == n:
        return whole
    a, b = located.first + keep.first - 1, located.first + keep.last - 1
    bad_start, bad_end = not index.cut_before(a), not index.cut_after(b)
    if bad_start or bad_end:
        where = "start and end" if bad_start and bad_end else "start" if bad_start else "end"
        return Adjust(
            candidate.start,
            candidate.end,
            False,
            f"adjustment to sentences {keep.first}-{keep.last} ignored: "
            f"it would {where} where there is no pause",
            ignored="boundary",
        )
    start, end = span_times(
        index,
        a,
        b,
        start_pad=start_pad,
        end_pad=end_pad,
        duration=duration,
    )
    length = end - start
    if length < min_length - EPSILON:
        reason = f"{length:.1f} s is under the {min_length:g} s minimum"
    elif length > max_length + EPSILON:
        reason = f"{length:.1f} s is over the {max_length:g} s maximum"
    else:
        trimmed = keep.first > 1 or keep.last < n
        extended_start = max(0, 1 - keep.first)
        extended_end = max(0, keep.last - n)
        if trimmed and not extended_start and not extended_end:
            text = f"trimmed to sentences {keep.first}-{keep.last} of {n}"
        else:
            parts = []
            if extended_start:
                parts.append(f"start extended by {extended_start}")
            if extended_end:
                parts.append(f"end extended by {extended_end}")
            if trimmed:
                parts.append("trimmed")
            text = f"adjusted to sentences {keep.first}-{keep.last} of {n}: " + ", ".join(parts)
        return Adjust(
            start,
            end,
            trimmed,
            text,
            keep=keep,
            extended_start=extended_start,
            extended_end=extended_end,
        )
    return Adjust(
        candidate.start,
        candidate.end,
        False,
        f"adjustment to sentences {keep.first}-{keep.last} ignored: {reason}",
        ignored="length",
    )


def final_cut(index: SentenceIndex, first: int, last: int) -> CutInfo:
    """How a clip made of sentences first..last (indices into index.sentences) is cut."""
    end_gap = index.sentences[last].gap
    return CutInfo(
        start_boundary=index.sentences[first - 1].end if first > 0 else "start",
        end_boundary=index.sentences[last].end,
        start_gap=round(index.sentences[first - 1].gap, 3) if first > 0 else None,
        end_gap=None if math.isinf(end_gap) else round(end_gap, 3),
    )


# --- selection -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    ranks: dict[str, int]  # candidate id -> rank, kept clips only
    dropped: dict[str, DroppedBy]  # model said keep, we did not


def select(drafts: list[Draft], min_score: int, keep_top: int) -> Selection:
    """Kept = verdict keep, score >= min_score, the model's adjustment (if any) was usable, and
    no better clip overlaps it by more than half of the shorter one; best score first, ties to the
    earlier candidate; the first keep_top get rank 1..N.

    A clip whose adjustment was ignored is not kept: its score described a clip we did not cut. A
    low score is reported as min_score first, whatever else is wrong with the clip. Overlap is
    judged on the final (adjusted) times, in score order, before the keep_top cut, so a clip
    dropped as an overlap never takes a rank.
    """
    dropped: dict[str, DroppedBy] = {}
    eligible = []
    for draft in drafts:
        if draft.clip.verdict != "keep":
            continue
        if draft.clip.score < min_score:
            dropped[draft.candidate.id] = "min_score"
        elif draft.adjust.ignored is not None:
            dropped[draft.candidate.id] = "adjust_ignored"
        else:
            eligible.append(draft)
    ordered = sorted(eligible, key=lambda d: (-d.clip.score, d.position))
    chosen = set(dedupe([(d.adjust.start, d.adjust.end) for d in ordered], MAX_OVERLAP))
    best = [d for i, d in enumerate(ordered) if i in chosen]
    for i, draft in enumerate(ordered):
        if i not in chosen:
            dropped[draft.candidate.id] = "overlap"
    ranks = {d.candidate.id: rank for rank, d in enumerate(best[:keep_top], start=1)}
    for draft in best[keep_top:]:
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
    if found.snap is None:
        raise RunError(
            f"{CANDIDATES_JSON} was made before Brief 10 (it records no snap settings): "
            "rerun from the candidates stage"
        )
    candidates = found.candidates
    min_length, max_length = app.clip.min_seconds, app.clip.max_seconds
    start_pad, end_pad = found.snap.max_start_pad_seconds, found.snap.max_end_pad_seconds
    duration = transcript.audio_duration_seconds

    warnings: list[str] = []
    if not candidates:
        warnings.append("there are no candidates, so nothing was scored")

    index = build_index(
        transcript.segments,
        found.settings.fallback_max_words,
        min_pause=found.snap.min_pause_seconds,
        min_cut_gap=found.snap.min_cut_gap,
    )
    located = locate_all(index, candidates, start_pad=start_pad, end_pad=end_pad, duration=duration)

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
            "blocks": "\n".join(
                candidate_block(item, index, cfg.extend_before, cfg.extend_after) for item in group
            ),
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
        adjust = resolve_adjust(
            index,
            item,
            clip.keep_sentences,
            before=cfg.extend_before,
            after=cfg.extend_after,
            start_pad=start_pad,
            end_pad=end_pad,
            duration=duration,
            min_length=min_length,
            max_length=max_length,
        )
        notes = [adjust.note] if adjust.note else []
        if _words(clip.hook_line) > MAX_HOOK_WORDS:
            notes.append(f"hook_line is {_words(clip.hook_line)} words (over {MAX_HOOK_WORDS})")
        if _words(clip.reason) > MAX_REASON_WORDS:
            notes.append(f"reason is {_words(clip.reason)} words (over {MAX_REASON_WORDS})")
        if adjust.keep is not None:
            first, last = item.first + adjust.keep.first - 1, item.first + adjust.keep.last - 1
        else:
            first, last = item.first, item.last
        drafts.append(
            Draft(
                position,
                item.candidate,
                clip,
                adjust,
                notes,
                sentences=item.count,
                cut=final_cut(index, first, last),
            )
        )

    selection = select(drafts, cfg.min_score, app.clip.keep_top)
    scored: list[ScoredClip] = []
    for draft in drafts:
        clip, candidate_id = draft.clip, draft.candidate.id
        dropped_by = selection.dropped.get(candidate_id)
        notes = list(draft.notes)
        if dropped_by == "min_score":
            notes.append(f"score {clip.score} is under min_score {cfg.min_score}")
        elif dropped_by == "adjust_ignored":
            notes.append(
                "the model's adjustment was ignored, so its score does not describe this clip"
            )
            warnings.append(
                f"{candidate_id}: kept by the model but its adjustment was ignored: not kept"
            )
        elif dropped_by == "overlap":
            notes.append("overlaps a better clip by more than half of the shorter one")
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
                start=draft.adjust.start,
                end=draft.adjust.end,
                candidate_start=draft.candidate.start,
                candidate_end=draft.candidate.end,
                trimmed=draft.adjust.trimmed,
                notes=notes,
                start_boundary=draft.cut.start_boundary,
                end_boundary=draft.cut.end_boundary,
                start_gap=draft.cut.start_gap,
                end_gap=draft.cut.end_gap,
                sentences=draft.sentences,
                kept_sentences=draft.adjust.keep,
                extended_start=draft.adjust.extended_start,
                extended_end=draft.adjust.extended_end,
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
        dropped_overlap=sum(1 for s in scored if s.dropped_by == "overlap"),
        adjust_ignored=sum(1 for s in scored if s.dropped_by == "adjust_ignored"),
        extensions_applied=sum(
            1 for d in drafts if d.adjust.extended_start or d.adjust.extended_end
        ),
        trimmed=sum(1 for s in scored if s.trimmed),
        trims_applied=sum(1 for d in drafts if d.adjust.trimmed),
        trims_ignored=sum(1 for d in drafts if d.adjust.ignored == "length"),
        trims_out_of_range=sum(1 for d in drafts if d.adjust.ignored == "range"),
        adjusts_on_weak_boundary=sum(1 for d in drafts if d.adjust.ignored == "boundary"),
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
            f"{entry.start:.2f}-{entry.end:.2f} s trimmed={entry.trimmed} "
            f"extended={entry.extended_start}/{entry.extended_end} "
            f"cut={entry.start_boundary}/{entry.end_boundary} "
            f"gaps={entry.start_gap}/{entry.end_gap}"
        )
    ours = (
        summary.dropped_min_score
        + summary.dropped_top_n
        + summary.adjust_ignored
        + summary.dropped_overlap
    )
    ignored = summary.trims_ignored + summary.trims_out_of_range + summary.adjusts_on_weak_boundary
    log.info(
        f"score: {summary.kept} kept of {summary.candidates} ({summary.rejected} rejected, "
        f"{ours} dropped by our rules ({summary.dropped_overlap} overlaps), "
        f"{summary.trims_applied} trims applied, {summary.extensions_applied} extended, "
        f"{ignored} ignored); "
        f"cost ${summary.actual_usd:.4f}"
    )
