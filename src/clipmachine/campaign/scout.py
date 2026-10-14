"""The `scout` command (Brief 12): the AI proposes moments for a campaign whose rules allow it.

Hard rule: it runs only when campaign.yaml says `selection: ai`. The human-picked path of campaign
mode (Brief 11, D-026) is untouched, and a campaign without that line is refused with a clear
message.

What it does, for one source video:
1. fetch and transcribe (the run folder is reused when the file was seen before);
2. the candidates (Haiku) and score (Sonnet) stages, exactly as a normal run, with the campaign's
   `scout.rules` added to the client rules, its `clip_length`, and a prompt block that asks for a
   `topic` (one of the campaign's keys), a short `hook_line` and a `caption`; the stage outputs go
   to `runs/<run>/scout/<slug>/`, the cost to the run's costs.json and the ledger as always;
3. the top `per_episode` kept clips that do not overlap (by more than half of their length) a
   moment already listed, rendered, posted or rejected for the same source;
4. the host-side heuristic, when `scout.host_side` is set: faces in the first seconds of the
   moment, on the host's side of the source frame, or not (a hint; nothing is dropped for it);
5. new entries `S<NN>` appended to moments.yaml as text, so Igor's own entries and comments stay
   byte for byte (a copy goes to moments.yaml.bak first); their details to scout.json.

Console lines carry ids, counts, seconds and the cost: never a hook, a caption, a reason or a
transcript word.
"""

import dataclasses
import os
import re
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

from clipmachine import pipeline, tracking
from clipmachine.campaign.loader import Campaign, campaign_dir, load_campaign, parse_moments
from clipmachine.campaign.models import (
    MomentSpec,
    Platform,
    RejectedMoment,
    ScoutConfig,
    ScoutRecord,
)
from clipmachine.campaign.run import prepare_run
from clipmachine.campaign.scoutlog import load_scout_file, save_scout_file
from clipmachine.campaign.used import load_used, reject, save_used
from clipmachine.config import ConfigError, ResolvedConfig, load_app_config, merge_client
from clipmachine.context import RunContext
from clipmachine.models import AppConfig, CandidatesFile, ClientRules, ScoreBatch, ScoredFile
from clipmachine.runs import RunError, write_text_atomic
from clipmachine.stages import candidates, score
from clipmachine.stages import render as stage
from clipmachine.utils import faces

EXIT_OK = 0
SCOUT_DIR = "scout"  # runs/<run>/scout/<slug>/: candidates.json and scored.json of a scout run
LAST_GOOD_JSON = "scored.last-good.json"  # a copy of the last scored.json the stage finished
HOOK_NOTE = "no hook: write one"  # the moments.yaml comment and the report line
MOMENTS_FILE = "moments.yaml"
BACKUP_SUFFIX = ".bak"
ID_PREFIX = "S"
ID_RE = re.compile(r"S(\d+)")
MAX_HOOK_WORDS = 7  # the brief: a hook the clip pays off, at most 7 words
OVERLAP = 0.5  # a proposal is skipped when it overlaps a taken stretch by more than this share
MAX_REASON_CHARS = 160  # in the moments.yaml comment
HostCheck = Literal["likely", "unsure", "not_checked"]


@dataclass(frozen=True)
class Proposal:
    id: str
    start: float
    end: float
    hook: str
    caption: str
    topic: str | None
    platform: Platform | Literal["auto"]
    score: int
    reason: str
    host_check: HostCheck
    notes: tuple[str, ...] = ()  # hook_too_long | hook_missing: the hook is left empty


# --- the prompt pieces ---------------------------------------------------------------------------


def scout_rules(campaign: Campaign) -> ClientRules:
    """The campaign as a client: its rules reach both prompts through rules_summary."""
    cfg = campaign.config
    assert cfg.scout is not None
    return ClientRules(
        name=f"campaign {campaign.slug}",
        niche=cfg.scout.niche,
        audience=cfg.scout.audience,
        tone=cfg.scout.tone,
        clip_length=cfg.clip_length,
        extra=list(cfg.scout.rules),
    )


def rules_block(scout: ScoutConfig) -> str:
    """For the candidates prompt: the campaign's rules as hard constraints."""
    if not scout.rules:
        return ""
    lines = ["Campaign rules (hard constraints; a moment that breaks one is not a candidate):"]
    lines.extend(f"- {rule}" for rule in scout.rules)
    return "\n".join(lines) + "\n"


def score_block(scout: ScoutConfig, max_chars: int) -> str:
    """For the scoring prompt: the rules, the topic keys, and the hook and caption asked for."""
    lines = [rules_block(scout).rstrip("\n")] if scout.rules else []
    if scout.topics:
        keys = ", ".join(scout.topics)
        lines.append(f"For every clip you keep, set topic to exactly one of these keys: {keys}.")
    else:
        lines.append("Leave topic null.")
    lines.append(
        f"For every clip you keep, hook_line is at most {MAX_HOOK_WORDS} words and {max_chars} "
        "characters, says what the clip actually delivers (no clickbait the clip does not pay "
        "off), and caption is one sentence that leads with the idea in the clip. A clip you "
        "reject still gets a non-empty hook_line (same limits) and suggested_title; never leave "
        "them empty."
    )
    return "\n".join(lines) + "\n"


def scout_check(topics: Sequence[str], max_chars: int) -> Callable[[ScoreBatch], str | None]:
    """A check for call_structured, run with the id check: every kept clip has a valid topic (when
    the campaign has topics), a hook of at most MAX_HOOK_WORDS words and `max_chars` characters,
    and a caption. The message names ids and rules only, never the model's text.

    The hook rule asks for one repair and no more: the second answer for the same clips is taken
    even with a long hook (the scout then leaves that hook empty with a note, see run_scout), so
    one stubborn hook never fails the run. Topic and caption stay strict."""
    hooks_asked: set[frozenset[str]] = set()

    def check(batch: ScoreBatch) -> str | None:
        ids = frozenset(clip.id for clip in batch.clips)
        hooks_strict = ids not in hooks_asked
        hooks_asked.add(ids)
        problems: list[str] = []
        for clip in batch.clips:
            if clip.verdict != "keep":
                continue
            faults: list[str] = []
            if topics and clip.topic not in topics:
                faults.append("topic must be one of " + ", ".join(topics))
            hook = " ".join(clip.hook_line.split())
            if hooks_strict and (len(hook.split()) > MAX_HOOK_WORDS or len(hook) > max_chars):
                faults.append(
                    f"hook_line must be at most {MAX_HOOK_WORDS} words and {max_chars} characters"
                )
            if not (clip.caption or "").strip():
                faults.append("caption is missing")
            if faults:
                problems.append(f"clip {clip.id}: " + "; ".join(faults))
        return "; ".join(problems) if problems else None

    return check


# --- what is already taken -----------------------------------------------------------------------


def overlaps(start: float, end: float, taken: Iterable[tuple[float, float]]) -> bool:
    """Whether [start, end] overlaps any taken stretch by more than OVERLAP of its own length."""
    length = end - start
    if length <= 0:
        return True
    return any(min(end, b) - max(start, a) > OVERLAP * length for a, b in taken)


def _same_file(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def source_matches(root: Path, source: str | None, episode: Path) -> bool:
    """A moments.yaml `source:` (or None, the default source) counts as this episode when it is
    this file; the default always counts, so a list without sources is never duplicated."""
    if source is None:
        return True
    path = Path(source)
    return _same_file(path if path.is_absolute() else root / path, episode)


def taken_stretches(
    root: Path,
    specs: Sequence[MomentSpec],
    rejected: Sequence[RejectedMoment],
    rendered: Iterable[tuple[str, float, float]],
    episode: Path,
    source_sha256: str,
    auto_end: float,
) -> list[tuple[float, float]]:
    """Stretches of this episode a proposal must not overlap: moments.yaml entries of this
    source (an `end: auto` counts as start + the auto target), rejected moments of this source,
    and used.json renders of the same file (by hash)."""
    taken = [
        (s.start, s.end if s.end is not None else s.start + auto_end)
        for s in specs
        if source_matches(root, s.source, episode)
    ]
    taken.extend((r.start, r.end) for r in rejected if source_matches(root, r.source, episode))
    taken.extend((a, b) for sha, a, b in rendered if sha == source_sha256)
    return taken


def next_ids(existing: Iterable[str], count: int) -> list[str]:
    """S01, S02, ... continuing after the highest S<number> in `existing`."""
    highest = 0
    for moment_id in existing:
        match = ID_RE.fullmatch(moment_id)
        if match:
            highest = max(highest, int(match.group(1)))
    return [f"{ID_PREFIX}{highest + n:02d}" for n in range(1, count + 1)]


# --- the host-side hint --------------------------------------------------------------------------


def host_check(
    root: Path, run_dir: Path, app: AppConfig, scratch: Path, start: float, side: str
) -> HostCheck:
    """`likely` when a face is on the host's side of the source frame in at least
    `campaign.scout_host_share` of the frames of the moment's first `scout_host_seconds`;
    `unsure` otherwise; `not_checked` when the face models or the source are not available.
    Detection only: no identity, no biometrics (D-032)."""
    try:
        meta, video, source = stage._load_source(run_dir)
        plan = tracking.build_plan(
            run_dir, root, app, video=video, source=source, source_sha256=meta.sha256, segments=[]
        )
    except (faces.FacesError, RunError):
        return "not_checked"
    plan = dataclasses.replace(plan, clips_dir=scratch)
    detected = tracking.detect_faces(plan, start, app.campaign.scout_host_seconds)
    frames = detected.frames
    if not frames:
        return "unsure"
    on_side = sum(
        1 for found in frames if any((face.cx < 0.5) == (side == "left") for face in found)
    )
    return "likely" if on_side >= app.campaign.scout_host_share * len(frames) else "unsure"


# --- moments.yaml --------------------------------------------------------------------------------


def _one_line(text: str, limit: int) -> str:
    text = " ".join(text.replace("#", " ").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def format_entries(proposals: Sequence[Proposal], source: str, when: str) -> str:
    """The lines appended to moments.yaml: a header comment, then per moment a comment with the
    scorer's facts and one flow-style entry (quoted by the YAML library)."""
    lines = [f"# scout {when[:10]}: {len(proposals)} proposed from {Path(source).name}"]
    for p in proposals:
        topic = p.topic or "none"
        hook_note = f" {HOOK_NOTE};" if p.notes else ""
        lines.append(
            f"# {p.id}: score {p.score}, topic {topic} -> {p.platform}, host {p.host_check};"
            f"{hook_note} {_one_line(p.reason, MAX_REASON_CHARS)}"
        )
        entry = {
            "id": p.id,
            "source": source,
            "start": round(p.start, 3),
            "end": round(p.end, 3),
            "hook": p.hook,
            "caption": p.caption,
            "platform": p.platform,
        }
        flow = yaml.safe_dump(
            entry, default_flow_style=True, width=10**6, allow_unicode=True, sort_keys=False
        ).strip()
        lines.append(f"- {flow}")
    return "\n".join(lines) + "\n"


def append_moments(path: Path, addition: str, limit: int) -> Path:
    """Copy moments.yaml to moments.yaml.bak, then write the old text plus `addition`, after
    checking that the whole file still parses. Returns the backup path."""
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    joined = old if not old or old.endswith("\n") else old + "\n"
    new_text = joined + ("\n" if joined.strip() else "") + addition
    parse_moments(new_text, limit)  # a ConfigError here leaves both files as they are
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if path.is_file():
        shutil.copyfile(path, backup)
    else:
        backup.write_text("", encoding="utf-8")
    write_text_atomic(path, new_text)
    return backup


def _existing_specs(folder: Path, limit: int) -> list[MomentSpec]:
    path = folder / MOMENTS_FILE
    if not path.is_file() or not path.read_text(encoding="utf-8").strip():
        return []
    return parse_moments(path.read_text(encoding="utf-8"), limit)


def _source_label(root: Path, episode: Path, run_dir: Path) -> str:
    """How `source:` is written: the original file, repo-relative with forward slashes when it is
    inside the repo, else absolute."""
    original = (
        Path(pipeline.load_state(run_dir).input) if pipeline.is_run_folder(episode) else episode
    )
    resolved = original.resolve()
    if resolved.is_relative_to(root):
        return resolved.relative_to(root).as_posix()
    return resolved.as_posix()


# --- the command ---------------------------------------------------------------------------------


def run_scout(
    root: Path, episode: Path, slug: str, *, dry_run: bool = False, reuse_scored: bool = False
) -> int:
    """`reuse_scored`: no API call; the kept list comes from the episode's scored.json of this
    campaign (or the copy kept from the last run that finished), so a run that died after the
    scoring, or a moments.yaml that lost entries, is rebuilt for free."""
    root = root.resolve()
    app = load_app_config(root)
    campaign = load_campaign(root, slug, app)
    cfg = campaign.config
    if cfg.selection != "ai" or cfg.scout is None:
        raise ConfigError(
            f"campaign {slug!r} picks its moments by hand (selection: human); scout runs only "
            "for a campaign whose rules allow AI-picked moments: set selection: ai and a scout: "
            "section in campaign.yaml"
        )
    scout = cfg.scout
    limit = cfg.hook.max_chars
    used = load_used(campaign.folder)
    specs = _existing_specs(campaign.folder, limit)
    pipeline.check_ffmpeg()

    run_dir = prepare_run(root, episode)
    meta = stage._load_meta(run_dir)
    source_label = _source_label(root, episode, run_dir)
    rules = scout_rules(campaign)
    resolved = ResolvedConfig(
        app=merge_client(app, rules), client_name=f"campaign:{slug}", rules=rules
    )
    out_dir = run_dir / SCOUT_DIR / slug
    ctx = RunContext(
        run_dir=run_dir,
        config=resolved,
        input_path=episode,
        project_root=root,
        out_dir=out_dir,
        scout_block=rules_block(scout),
    )
    if reuse_scored:
        scored = _last_scored(out_dir)
        cost, calls = 0.0, 0
        print(f"reusing {scored.summary.candidates} scored candidates; no API call")
    else:
        candidates.run(ctx)
        score.run(
            dataclasses.replace(
                ctx,
                scout_block=score_block(scout, limit),
                extra_check=scout_check(list(scout.topics), limit),
            )
        )
        found = CandidatesFile.model_validate_json(
            (out_dir / candidates.CANDIDATES_JSON).read_text(encoding="utf-8")
        )
        scored = ScoredFile.model_validate_json(
            (out_dir / score.SCORED_JSON).read_text(encoding="utf-8")
        )
        shutil.copyfile(out_dir / score.SCORED_JSON, out_dir / LAST_GOOD_JSON)
        cost = found.summary.actual_usd + scored.summary.actual_usd
        calls = found.summary.calls + scored.summary.calls

    episode_file = Path(pipeline.load_state(run_dir).input)
    taken = taken_stretches(
        root,
        specs,
        used.rejected,
        ((e.source_sha256, e.start, e.end) for e in used.entries),
        episode_file,
        meta.sha256,
        app.campaign.auto_end_target_s,
    )
    kept = sorted((c for c in scored.clips if c.kept), key=lambda c: c.rank or 0)
    chosen: list[Proposal] = []
    skipped = dropped = without_hook = 0
    for clip in kept:
        if len(chosen) >= scout.per_episode:
            break
        if overlaps(clip.start, clip.end, [*taken, *((p.start, p.end) for p in chosen)]):
            skipped += 1
            continue
        hook = " ".join(clip.hook_line.split())
        caption = " ".join((clip.caption or "").split())
        if not caption:
            dropped += 1  # the check asked for a repair; the answer still broke the rule
            continue
        notes: tuple[str, ...] = ()
        if not hook:
            notes, without_hook = ("hook_missing",), without_hook + 1
        elif len(hook) > limit or len(hook.split()) > MAX_HOOK_WORDS:
            # the check asked for one repair; the clip is kept, the hook is Igor's to write
            notes, hook, without_hook = ("hook_too_long",), "", without_hook + 1
        topic = clip.topic if clip.topic in scout.topics else None
        platform = scout.topics.get(topic, "auto") if topic is not None else "auto"
        chosen.append(
            Proposal(
                id="",
                start=clip.start,
                end=clip.end,
                hook=hook,
                caption=caption,
                topic=topic,
                platform=platform,
                score=clip.score,
                reason=" ".join(clip.reason.split()),
                host_check="not_checked",
                notes=notes,
            )
        )
    ids = next_ids((s.id for s in specs), len(chosen))
    proposals: list[Proposal] = []
    for moment_id, p in zip(ids, chosen, strict=True):
        check: HostCheck = "not_checked"
        if scout.host_side is not None:
            check = host_check(root, run_dir, app, out_dir, p.start, scout.host_side)
        proposals.append(dataclasses.replace(p, id=moment_id, host_check=check))

    when = datetime.now(UTC).isoformat()
    if proposals and not dry_run:
        backup = append_moments(
            campaign.folder / MOMENTS_FILE, format_entries(proposals, source_label, when), limit
        )
        print(f"moments.yaml: {len(proposals)} entries appended; the old file is {backup.name}")
        records = load_scout_file(campaign.folder)
        records.records.extend(
            ScoutRecord(
                id=p.id,
                source_sha256=meta.sha256,
                source_name=meta.original_filename,
                start=p.start,
                end=p.end,
                score=p.score,
                topic=p.topic,
                platform=p.platform,
                host_check=p.host_check,
                reason=p.reason,
                proposed_at=when,
                model=scored.summary.model,
                notes=list(p.notes),
            )
            for p in proposals
        )
        save_scout_file(campaign.folder, records)
    elif dry_run:
        print("dry run: nothing written")
    by_platform = ", ".join(
        f"{platform} {sum(1 for p in proposals if p.platform == platform)}"
        for platform in dict.fromkeys(p.platform for p in proposals)
    )
    hosts = ", ".join(f"{p.id} {p.host_check}" for p in proposals if p.host_check != "not_checked")
    print(
        f"scout: {len(proposals)} proposed ({by_platform or 'none'}), {skipped} skipped as "
        f"overlapping, {dropped} dropped, of {len(kept)} kept by the scorer; "
        f"cost ${cost:.4f} in {calls} API calls"
    )
    if without_hook:
        print(f"{without_hook} proposed without a hook (too long or missing): write one")
    if hosts:
        print(f"host check: {hosts}")
    if proposals:
        print(
            "next: uv run python -m clipmachine campaign "
            f"{episode} --campaign {slug} --only {','.join(p.id for p in proposals)}"
        )
    return EXIT_OK


def _last_scored(out_dir: Path) -> ScoredFile:
    """scored.json of the last scout run of this episode and campaign, else the copy of the
    last one that finished; a ConfigError when there is neither."""
    for name in (score.SCORED_JSON, LAST_GOOD_JSON):
        path = out_dir / name
        if path.is_file():
            try:
                return ScoredFile.model_validate_json(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise ConfigError(f"{path} is not a valid scored file: {exc}") from exc
    raise ConfigError(
        f"no scored.json for this episode and campaign in {out_dir}; run scout without "
        "--reuse-scored first"
    )


def reject_moment(root: Path, slug: str, moment_id: str, when: str) -> RejectedMoment:
    """`campaign-reject`: record the moment's stretch of its source in used.json."""
    root = root.resolve()
    folder = campaign_dir(root, slug)
    campaign = load_campaign(root, slug, load_app_config(root))
    specs = _existing_specs(folder, campaign.config.hook.max_chars)
    spec = next((s for s in specs if s.id == moment_id), None)
    if spec is None:
        raise ConfigError(f"moment {moment_id} is not in moments.yaml; nothing to reject")
    end = (
        spec.end
        if spec.end is not None
        else spec.start + load_app_config(root).campaign.auto_end_target_s
    )
    log = reject(load_used(folder), moment_id, spec.source, spec.start, end, when)
    save_used(folder, log)
    return log.rejected[-1]
