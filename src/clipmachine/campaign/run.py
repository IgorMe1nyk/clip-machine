"""The `campaign` command (Brief 11): find or make the run folder of each source, then render the
chosen moments into the campaign's folder.

Campaign mode never calls the Claude API: the whole command runs with the kill switch on, the
pipeline stops after transcribe, and the cost files are checked at the end. Console lines carry
ids, platforms, seconds and counts only, never a hook, a caption, a closer, a name or a video name.

Brief 12: a campaign may span several source videos. An entry of moments.yaml names its own
`source:`; the others use the file given on the command line. Each source keeps its own run folder
(fetch and transcribe reused as before); the clips, the report and the posting plan of the whole
campaign live together in `runs/_campaigns/<slug>/` (Igor, 2026-10-13).
"""

import dataclasses
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from clipmachine import pipeline
from clipmachine.campaign.assign import ASSIGNMENTS_FILE, assign_platforms, count_by_platform
from clipmachine.campaign.autoposition import choose_position, describe
from clipmachine.campaign.loader import Campaign, load_campaign, load_moments, select_moments
from clipmachine.campaign.models import Moment, SpellingRule
from clipmachine.campaign.names import (
    AUTO_SPELLING_FILE,
    Variant,
    auto_rules,
    find_variants,
    record,
)
from clipmachine.campaign.plan import write_posting_plan
from clipmachine.campaign.post import keep_words
from clipmachine.campaign.render import META_DIR, MomentContext, MomentResult, render_moment
from clipmachine.campaign.report import write_report
from clipmachine.campaign.scoutlog import latest_records, load_scout_file
from clipmachine.campaign.used import load_used, save_used
from clipmachine.config import ConfigError, load_app_config
from clipmachine.models import AppConfig, FetchMeta, RunState
from clipmachine.runs import RunError, validate_input, write_json_atomic
from clipmachine.stages import render as stage
from clipmachine.stages.candidates import _load_transcript
from clipmachine.stages.fetch import META_FILE
from clipmachine.utils import claude
from clipmachine.utils.snapping import build_index

EXIT_OK = 0
EXIT_MOMENT_FAILED = 1
CAMPAIGNS_DIR = "_campaigns"  # runs/_campaigns/<slug>/: the campaign's clips, report and plan
NO_TIME_WARNING = (
    "warning: that run has no recorded modified time for its source; "
    "it was matched on path and size only"
)


def campaign_out_dir(root: Path, slug: str) -> Path:
    return root / "runs" / CAMPAIGNS_DIR / slug


def _size(path: Path) -> int:
    return path.stat().st_size if path.is_file() else -1


def _matches(run_dir: Path, source: Path) -> tuple[RunState, bool] | None:
    """(state, matched_without_time) when the run is of this very source file, else None."""
    try:
        state = pipeline.load_state(run_dir)
        meta = FetchMeta.model_validate_json((run_dir / META_FILE).read_text(encoding="utf-8"))
    except (RunError, OSError, ValidationError, ValueError):
        return None
    # normcase: on Windows the same file may be typed with another letter case or slashes
    if os.path.normcase(state.input) != os.path.normcase(str(source.resolve())):
        return None
    if any(state.stages[name].status != "done" for name in ("fetch", "transcribe")):
        return None
    info = source.stat()
    if meta.size_bytes != info.st_size:
        return None
    if meta.source_mtime_ns is None:
        return state, True
    return (state, False) if meta.source_mtime_ns == info.st_mtime_ns else None


def find_run(root: Path, source: Path) -> tuple[Path, bool] | None:
    """The run folder of this source file: (folder, matched_without_time), or None.

    A run matches on the resolved input path, the file size and the modified time recorded by
    fetch; a run from before the time was recorded matches on path and size alone. Runs matched
    with the time win over those matched without it; the newest wins within each kind."""
    found: list[tuple[bool, str, Path]] = []
    runs = root / "runs"
    if not runs.is_dir():
        return None
    for folder in runs.iterdir():
        if not pipeline.is_run_folder(folder):
            continue
        match = _matches(folder, source)
        if match is not None:
            state, without_time = match
            found.append((not without_time, state.created_at, folder))
    if not found:
        return None
    with_time, _, best = max(found, key=lambda item: (item[0], item[1]))
    return best, not with_time


def prepare_run(root: Path, target: Path) -> Path:
    """The run folder to use, with fetch and transcribe done. A run folder is used as given; a
    media file reuses the run of that very file, else a new run (client `general`) is made."""
    if pipeline.is_run_folder(target):
        run_dir = target.resolve()
        print(f"run folder: {run_dir}")
    else:
        validate_input(target)
        match = find_run(root, target)
        if match is not None:
            run_dir, without_time = match
            print(f"reusing run folder: {run_dir}")
            if without_time:
                print(NO_TIME_WARNING)
        else:
            run_dir = pipeline.start_new_run(root, target, "general")
            print(f"run folder: {run_dir}")
    outcome = pipeline.run_pipeline(run_dir, project_root=root, until="transcribe")
    if not outcome.ok:
        raise RunError(f"stage {outcome.failed_stage} failed: {outcome.error}")
    return run_dir


def source_of(root: Path, moment: Moment, default: Path) -> Path:
    """The source video (or run folder) a moment is cut from: its own `source:` (relative paths
    are from the repo root), else the one given on the command line. A missing or unsupported
    file is a ConfigError that names the moment."""
    if moment.source is None:
        return default
    path = Path(moment.source)
    if not path.is_absolute():
        path = root / path
    try:
        validate_input(path)
    except RunError as exc:
        raise ConfigError(f"moment {moment.id}: source: {exc}") from exc
    return path


def group_by_source(
    root: Path, moments: list[Moment], default: Path
) -> list[tuple[Path, list[Moment]]]:
    """The moments grouped by source, in first-appearance order with the default source first;
    each group keeps moments.yaml order."""
    groups: dict[str, tuple[Path, list[Moment]]] = {}
    key_default = os.path.normcase(str(default.resolve()))
    groups[key_default] = (default, [])
    for moment in moments:
        source = source_of(root, moment, default)
        key = os.path.normcase(str(source.resolve()))
        groups.setdefault(key, (source, []))[1].append(moment)
    return [(source, group) for source, group in groups.values() if group]


def _line(moment_name: str, result: MomentResult, closers: int) -> str:
    label = moment_name.replace("_", " ", 1)
    if result.outcome == "refused":
        return f"{label}: refused: already posted (use --force-reuse to render it again)"
    if result.outcome == "rejected":
        return f"{label}: skipped: rejected with campaign-reject"
    if result.outcome == "failed":
        return f"{label}: FAILED: {result.reason}"
    clip = result.clip
    assert clip is not None and clip.start is not None and clip.end is not None
    assert clip.duration is not None and clip.render_seconds is not None
    start = "start kept" if result.start_moved is None else f"start {result.start_moved:+.2f} s"
    if clip.end_mode == "auto":
        end = f"end auto (target {clip.auto_end_target:g} s, actual {clip.duration:.2f} s)"
    else:
        end = "end kept" if result.end_moved is None else f"end {result.end_moved:+.2f} s"
    zooms = ""
    if clip.settings is not None and clip.settings.zoom_enabled:
        zooms = f", {clip.zoom_count} zooms"
    closer = "" if result.closer_number is None else f"closer {result.closer_number} of {closers}, "
    return (
        f"{label}: rendered {clip.start:.2f}-{clip.end:.2f} s ({clip.duration:.2f} s), {start}, "
        f"{end}, {clip.spelling_replacements} spelling fixes{zooms}, "
        f"{closer}{clip.render_seconds:.1f} s"
    )


@dataclass
class _Source:
    """One source's run folder as the renderer needs it: the context and the names scan."""

    run_dir: Path
    ctx: MomentContext
    variants: list[Variant]
    auto: list[SpellingRule]
    covered: list[Variant]
    heard: dict[str, list[tuple[str, float]]] = field(default_factory=dict)


def _prepare_source(
    root: Path, run_dir: Path, out_dir: Path, app: AppConfig, campaign: Campaign
) -> _Source:
    cfg = campaign.config
    meta_dir = out_dir / META_DIR
    inputs = stage.load_source_inputs(run_dir, root, app)
    if inputs.track_plan is not None:  # track files live with the campaign's meta files
        inputs = dataclasses.replace(
            inputs, track_plan=dataclasses.replace(inputs.track_plan, clips_dir=meta_dir)
        )
    plan = stage._make_plan(run_dir, meta_dir, app, inputs)
    if plan.wordlists is not None:  # the campaign's own allowed and extra words
        fine = [*cfg.allowed, *cfg.names_to_check, *cfg.names]
        fine.extend(rule.to for rule in cfg.spelling)
        lists = plan.wordlists.with_masked(cfg.extra_masked).without(fine)
        plan = dataclasses.replace(plan, wordlists=lists)
    transcript = _load_transcript(run_dir)
    index = build_index(
        transcript.segments,
        app.candidates.fallback_max_words,
        min_pause=app.snap.min_pause_seconds,
        min_cut_gap=app.snap.min_cut_gap,
    )
    variants = find_variants(
        index,
        cfg.names,
        apply_threshold=app.campaign.name_match_threshold,
        suggest_threshold=app.campaign.name_suggest_threshold,
    )
    auto, covered = auto_rules(variants, cfg.spelling)
    listed = [*cfg.names_to_check, *(n for n in cfg.names if n not in cfg.names_to_check)]
    meta = stage._load_meta(run_dir)
    ctx = MomentContext(
        root=root,
        run_dir=run_dir,
        out_dir=out_dir,
        plan=plan,
        app=app,
        campaign=campaign,
        units=index.units(),
        duration=transcript.audio_duration_seconds,
        source_sha256=meta.sha256,
        source_name=meta.original_filename,
        spelling_rules=(*cfg.spelling, *auto),
        variant_texts=tuple(v.text for v in variants),
        names_to_check=tuple(listed),
        title_keep=tuple(keep_words(cfg, app.campaign.title_keep_case)),
    )
    return _Source(run_dir, ctx, variants, auto, covered, {v.text: [] for v in variants})


def run_campaign(
    root: Path, target: Path, slug: str, *, only: str | None, force_reuse: bool
) -> int:
    """The exit code: 1 when a moment failed or one named in `only` was refused, else 0."""
    root = root.resolve()
    app = load_app_config(root)  # the current config, not the run's resolved copy
    if app.captions.style != "box_karaoke":
        raise ConfigError("campaign mode needs captions.style: box_karaoke in config/default.yaml")
    campaign = load_campaign(root, slug, app)
    # the campaign's video.zoom and video.graphics keys win; everything below reads the merged
    # settings from `app`
    app = app.model_copy(
        update={
            "video": app.video.model_copy(
                update={"zoom": campaign.zoom, "graphics": campaign.graphics}
            )
        }
    )
    used = load_used(campaign.folder)
    specs = load_moments(campaign)
    all_moments, assignments, assignments_changed = assign_platforms(
        campaign.folder, campaign.config, specs, used
    )
    if assignments_changed:
        write_json_atomic(campaign.folder / ASSIGNMENTS_FILE, assignments.model_dump(mode="json"))
    counts_by = count_by_platform(all_moments, campaign.config.platforms)
    shares = ", ".join(f"{platform} {n}" for platform, n in counts_by.items())
    new = sum(1 for s in specs if s.platform == "auto")
    print(f"platforms: {shares} ({new} auto; locked in {ASSIGNMENTS_FILE})")
    moments = select_moments(all_moments, only)
    groups = group_by_source(root, moments, target)
    if len(groups) > 1:
        print(f"sources: {len(groups)} videos")
    for warning in campaign.warnings:
        print(f"warning: {warning}")
    wm_cfg = campaign.config.watermark
    if wm_cfg is None:
        print("watermark: none required by this campaign")
    elif wm_cfg.position == "auto":
        if campaign.auto is not None:
            print(f"{describe(campaign.auto)} (locked in auto_position.json)")
        else:
            print(
                "watermark position: auto; choosing from the first "
                f"{app.campaign.auto_watermark_sample} moments being rendered"
            )
    pipeline.check_ffmpeg()
    started = time.perf_counter()
    cfg = campaign.config
    out_dir = campaign_out_dir(root, slug)
    (out_dir / META_DIR).mkdir(parents=True, exist_ok=True)
    for platform in cfg.platforms:
        (out_dir / platform).mkdir(exist_ok=True)

    closers = len(cfg.post.closers)
    wanted = {part.strip() for part in only.split(",")} if only else set()
    counts = {"rendered": 0, "failed": 0, "refused": 0, "rejected": 0}
    refused_named = False
    sources: list[_Source] = []
    source_names: dict[str, str] = {}
    with claude.no_api():
        ledger = root / claude.LEDGER_PATH
        ledger_before = _size(ledger)
        costs_before: list[tuple[Path, int]] = []
        for source, group in groups:
            run_dir = prepare_run(root, source)
            costs = run_dir / claude.COSTS_FILE
            costs_before.append((costs, _size(costs)))
            current = _prepare_source(root, run_dir, out_dir, app, campaign)
            sources.append(current)
            ctx = current.ctx
            if campaign.has_watermark and campaign.watermark is None:  # auto, not chosen yet
                campaign, lock = choose_position(ctx, group, used, force_reuse=force_reuse)
                print(describe(lock))
            ctx = dataclasses.replace(ctx, campaign=campaign)
            for moment in group:
                source_names[moment.id] = ctx.source_name
                result = render_moment(ctx, moment, used, force_reuse=force_reuse)
                used = result.used
                counts[result.outcome] += 1
                if result.outcome == "rendered":
                    save_used(campaign.folder, used)
                    for text, times in (result.variant_times or {}).items():
                        current.heard[text].extend((moment.clip_name, t) for t in times)
                if result.outcome == "refused" and moment.id in wanted:
                    refused_named = True
                print(_line(moment.clip_name, result, closers))

        spent = _size(ledger) != ledger_before or any(
            _size(path) != before for path, before in costs_before
        )
        if spent:
            raise RunError("campaign mode made an API call; this must never happen")

    seconds = time.perf_counter() - started
    auto_spelling = None
    if cfg.names and sources:
        # the names scan is recorded for the first source (the one on the command line)
        first = sources[0]
        auto_spelling = record(
            first.variants,
            first.covered,
            first.heard,
            names=cfg.names,
            apply_threshold=app.campaign.name_match_threshold,
            suggest_threshold=app.campaign.name_suggest_threshold,
            source_sha256=first.ctx.source_sha256,
        )
        write_json_atomic(
            campaign.folder / AUTO_SPELLING_FILE, auto_spelling.model_dump(mode="json")
        )
        suggestions = sum(1 for v in first.variants if v.status == "suggest")
        print(
            f"names: {len(first.auto)} automatic spelling rules, {suggestions} suggestions, "
            f"{len(first.covered)} already in spelling; see {AUTO_SPELLING_FILE} and the report"
        )
    rejected = f", {counts['rejected']} rejected" if counts["rejected"] else ""
    print(
        f"campaign: {counts['rendered']} rendered, {counts['failed']} failed, "
        f"{counts['refused']} refused{rejected}; API calls: 0; {seconds:.1f} s"
    )
    print(f"output: {out_dir}")
    run_names = ", ".join(dict.fromkeys(s.run_dir.name for s in sources))
    scouted = latest_records(load_scout_file(campaign.folder))
    report = write_report(
        out_dir, campaign, all_moments, run_names, auto=auto_spelling, scout=scouted
    )
    print(f"report: {report}")
    plan_path = write_posting_plan(
        out_dir,
        cfg,
        all_moments,
        used,
        root=root,
        today=date.today(),
        sources=source_names,
    )
    print(f"posting plan: {plan_path}")
    return EXIT_MOMENT_FAILED if counts["failed"] or refused_named else EXIT_OK
