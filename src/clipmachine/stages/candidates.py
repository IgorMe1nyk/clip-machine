"""Stage 3: transcript.json -> candidates.json.

Per block of the transcript, Claude Haiku proposes moments (API-enforced JSON); each moment is
validated, snapped onto sentence boundaries with the word timestamps, and near-duplicates are
removed. candidates.json is the only output and is written last, so its presence means the stage
finished; a rerun of a finished stage is skipped by the pipeline and makes no API call.

The transcript is untrusted data: it goes to the model inside <transcript> tags with a system
instruction to ignore anything in it, tag look-alikes inside it are neutralised, and nothing the
model returns is ever used as a path, a command or code. No transcript text or prompt is logged.
"""

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from clipmachine.context import RunContext
from clipmachine.models import (
    Candidate,
    CandidatesFile,
    CandidatesSummary,
    ClientRules,
    FoundMoment,
    FoundMoments,
    Transcript,
    TranscriptSegment,
)
from clipmachine.runs import RunError, write_json_atomic
from clipmachine.stages.transcribe import CONTROL_CHARS_RE, REPO_ROOT, TRANSCRIPT_JSON
from clipmachine.utils.claude import ClaudeClient
from clipmachine.utils.snapping import (
    Dropped,
    Snapped,
    build_index,
    dedupe,
    snap,
    words_text,
)

log = logging.getLogger("clipmachine")

CANDIDATES_JSON = "candidates.json"
TEMP_PATTERN = f".{CANDIDATES_JSON}.*.tmp"
PROMPT_FILE = Path("prompts") / "find_moments.md"
PURPOSE = "find_moments"
BLOCK_SLACK_SECONDS = 5.0  # a moment may reach this far outside its block
MAX_TEXT_CHARS = 300  # hook and reason are shortened to this
SECTION_RE = re.compile(r"^(SYSTEM|USER):[ \t]*\r?\n", re.MULTILINE)
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
TRANSCRIPT_TAG_RE = re.compile(r"<\s*/?\s*transcript[^>]*>?", re.IGNORECASE)


@dataclass(frozen=True)
class Block:
    number: int  # 1-based
    start: float
    end: float
    segments: tuple[TranscriptSegment, ...]


@dataclass(frozen=True)
class Pending:
    """A moment that survived validation and snapping, before de-duplication."""

    block: int
    moment: FoundMoment
    snapped: Snapped


def _remove_outputs(run_dir: Path, *, strict: bool) -> None:
    doomed = [run_dir / CANDIDATES_JSON, *run_dir.glob(TEMP_PATTERN)]
    for path in doomed:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            if strict:
                raise


# --- blocks and quota ----------------------------------------------------------------------------


def split_blocks(segments: list[TranscriptSegment], block_seconds: float) -> list[Block]:
    """Greedy blocks of at most block_seconds, cut between segments (a segment is never split).

    A block is measured from its first segment's start to the end of its last one, and always
    holds at least one segment. Segments with no text are skipped.
    """
    groups: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    for segment in segments:
        if not segment.text.strip():
            continue
        if current and segment.end - current[0].start > block_seconds:
            groups.append(current)
            current = []
        current.append(segment)
    if current:
        groups.append(current)
    return [
        Block(number, group[0].start, group[-1].end, tuple(group))
        for number, group in enumerate(groups, start=1)
    ]


def moments_per_block(candidates_target: int, blocks: int, per_block_max: int) -> int:
    """k = min(per_block_max, ceil(candidates_target / number of blocks))."""
    if blocks < 1:
        return 0
    return min(per_block_max, math.ceil(candidates_target / blocks))


# --- prompt --------------------------------------------------------------------------------------


def _one_line(text: str) -> str:
    return " ".join(CONTROL_CHARS_RE.sub(" ", text).split())


def load_prompt(root: Path) -> tuple[str, str]:
    """(system, user) templates from prompts/find_moments.md."""
    path = root / PROMPT_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RunError(f"cannot read {PROMPT_FILE}: {type(exc).__name__}") from exc
    parts = SECTION_RE.split(text)
    # ['', 'SYSTEM', body, 'USER', body]
    if len(parts) != 5 or parts[0].strip() or [parts[1], parts[3]] != ["SYSTEM", "USER"]:
        raise RunError(f"{PROMPT_FILE} must contain one SYSTEM: section, then one USER: section")
    return parts[2].strip(), parts[4].strip()


def fill(template: str, values: dict[str, str]) -> str:
    """Replace {name} placeholders in one pass: inserted values are never scanned again."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise RunError(f"{PROMPT_FILE} uses an unknown placeholder {{{name}}}")
        return values[name]

    return PLACEHOLDER_RE.sub(replace, template)


def rules_summary(rules: ClientRules) -> str:
    parts = [
        f"niche: {rules.niche}",
        f"audience: {rules.audience}",
        f"tone: {rules.tone}",
    ]
    if rules.prefer:
        parts.append("prefer: " + "; ".join(rules.prefer))
    if rules.avoid:
        parts.append("avoid: " + "; ".join(rules.avoid))
    return _one_line(". ".join(parts))


def transcript_lines(block: Block) -> str:
    """One line per segment: [start-end] text. The text is data, so it is made single-line and
    anything that looks like a transcript tag is removed."""
    lines = []
    for segment in block.segments:
        text = TRANSCRIPT_TAG_RE.sub("[tag removed]", _one_line(segment.text))
        if text:
            lines.append(f"[{segment.start:.2f}-{segment.end:.2f}] {text}")
    return "\n".join(lines)


# --- validation ----------------------------------------------------------------------------------


def invalid_reason(moment: FoundMoment, block: Block) -> str | None:
    """Why a returned moment cannot be used, or None. The reason never contains text."""
    if not (math.isfinite(moment.start) and math.isfinite(moment.end)):
        return "a time is not a finite number"
    if moment.start >= moment.end:
        return "start is not before end"
    if moment.start < block.start - BLOCK_SLACK_SECONDS:
        return "starts before its block"
    if moment.end > block.end + BLOCK_SLACK_SECONDS:
        return "ends after its block"
    if not _one_line(moment.hook):
        return "empty hook"
    return None


# --- the stage -----------------------------------------------------------------------------------


def run(ctx: RunContext) -> None:
    run_dir = ctx.run_dir.resolve()
    _remove_outputs(run_dir, strict=True)  # a rerun replaces the output cleanly
    try:
        _find(ctx, run_dir)
    except BaseException:
        _remove_outputs(run_dir, strict=False)
        raise


def _load_transcript(run_dir: Path) -> Transcript:
    path = run_dir / TRANSCRIPT_JSON
    if not path.is_file():
        raise RunError(
            f"{TRANSCRIPT_JSON} not found in the run folder; run the transcribe stage first"
        )
    try:
        return Transcript.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunError(
            f"{TRANSCRIPT_JSON} is not a valid transcript ({type(exc).__name__})"
        ) from exc


def _find(ctx: RunContext, run_dir: Path) -> None:
    app = ctx.config.app
    cfg = app.candidates
    root = ctx.project_root if ctx.project_root is not None else REPO_ROOT
    transcript = _load_transcript(run_dir)
    min_length, max_length = app.clip.min_seconds, app.clip.max_seconds

    blocks = split_blocks(transcript.segments, cfg.block_minutes * 60)
    k = moments_per_block(app.clip.candidates_target, len(blocks), cfg.per_block_max)
    warnings: list[str] = []
    if not blocks:
        warnings.append("the transcript has no speech, so no moment was requested")

    returned = invalid = dropped = 0
    pending: list[Pending] = []
    system_template, user_template = load_prompt(root)
    client = ClaudeClient(run_dir, root, app)
    summary_rules = rules_summary(ctx.config.rules)
    index = build_index(transcript.segments)

    for block in blocks:
        values = {
            "rules_summary": summary_rules,
            "min_seconds": f"{min_length:g}",
            "max_seconds": f"{max_length:g}",
            "block_start": f"{block.start:.1f}",
            "block_end": f"{block.end:.1f}",
            "k": str(k),
            "lines": transcript_lines(block),
        }
        found, _ = client.call_structured(
            f"{PURPOSE}:block-{block.number}",
            app.models.find,
            fill(system_template, values),
            fill(user_template, values),
            FoundMoments,
            cfg.max_output_tokens,
            thinking=cfg.thinking,
            effort=cfg.effort,
        )
        returned += len(found.moments)
        for number, moment in enumerate(found.moments, start=1):
            where = f"block {block.number} moment {number}"
            if number > k:
                invalid += 1
                warnings.append(f"{where}: dropped, over the quota of {k}")
                continue
            reason = invalid_reason(moment, block)
            if reason is not None:
                invalid += 1
                warnings.append(f"{where}: dropped, {reason}")
                continue
            result = snap(
                index,
                moment.start,
                moment.end,
                padding=cfg.snap_padding_seconds,
                duration=transcript.audio_duration_seconds,
                min_length=min_length,
                max_length=max_length,
            )
            if isinstance(result, Dropped):
                dropped += 1
                warnings.append(f"{where}: dropped, {result.reason}")
                continue
            pending.append(Pending(block.number, moment, result))
        log.info(f"candidates: block {block.number}/{len(blocks)}: {len(found.moments)} returned")

    kept = dedupe([(p.snapped.start, p.snapped.end) for p in pending], cfg.dedupe_overlap)
    deduped = len(pending) - len(kept)
    survivors = sorted((pending[i] for i in kept), key=lambda p: p.snapped.start)

    candidates = []
    for number, item in enumerate(survivors, start=1):
        snapped, moment = item.snapped, item.moment
        text = words_text(index, snapped.first_word, snapped.last_word)
        candidates.append(
            Candidate(
                id=f"c{number:02d}",
                start=snapped.start,
                end=snapped.end,
                model_start=moment.start,
                model_end=moment.end,
                duration=round(snapped.end - snapped.start, 3),
                block=item.block,
                hook=_one_line(moment.hook)[:MAX_TEXT_CHARS],
                reason=_one_line(moment.reason)[:MAX_TEXT_CHARS],
                type=moment.type,
                text=text,
                word_count=len(text.split()),
                snap_notes=list(snapped.notes),
            )
        )

    totals = client.session_totals()
    summary = CandidatesSummary(
        blocks=len(blocks),
        moments_per_block=k,
        requested=k * len(blocks),
        returned=returned,
        invalid=invalid,
        dropped=dropped,
        deduped=deduped,
        final=len(candidates),
        segment_edge_starts=sum(1 for s in survivors if s.snapped.edge_start),
        segment_edge_ends=sum(1 for s in survivors if s.snapped.edge_end),
        calls=totals.calls,
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        estimated_usd=totals.estimated_usd,
        actual_usd=totals.actual_usd,
    )
    output = CandidatesFile(
        model=app.models.find,
        clip_min_seconds=min_length,
        clip_max_seconds=max_length,
        settings=cfg,
        summary=summary,
        candidates=candidates,
        warnings=warnings,
    )
    write_json_atomic(run_dir / CANDIDATES_JSON, output.model_dump(mode="json"))
    for candidate in candidates:
        log.info(
            f"candidates: {candidate.id} {candidate.start:.2f}-{candidate.end:.2f} s "
            f"({candidate.duration:.1f} s) {candidate.type}"
        )
    log.info(
        f"candidates: {summary.final} kept of {summary.returned} returned "
        f"({summary.invalid} invalid, {summary.dropped} dropped, {summary.deduped} duplicates) "
        f"in {summary.blocks} block(s); cost ${summary.actual_usd:.4f}"
    )
