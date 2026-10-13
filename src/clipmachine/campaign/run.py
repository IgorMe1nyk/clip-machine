"""The `campaign` command (Brief 11): find or make the run folder, then render the chosen moments.

Campaign mode never calls the Claude API: the whole command runs with the kill switch on, the
pipeline stops after transcribe, and the cost files are checked at the end. Console lines carry
ids, platforms, seconds and counts only, never a hook, a caption, a closer, a name or a video name.
"""

import dataclasses
import os
import time
from pathlib import Path

from pydantic import ValidationError

from clipmachine import pipeline
from clipmachine.campaign.loader import load_campaign, load_moments, select_moments
from clipmachine.campaign.names import AUTO_SPELLING_FILE, auto_rules, find_variants, record
from clipmachine.campaign.render import MomentContext, MomentResult, render_moment
from clipmachine.campaign.report import write_report
from clipmachine.campaign.used import load_used, save_used
from clipmachine.config import ConfigError, load_app_config
from clipmachine.models import FetchMeta, RunState
from clipmachine.runs import RunError, validate_input, write_json_atomic
from clipmachine.stages import render as stage
from clipmachine.stages.candidates import _load_transcript
from clipmachine.stages.fetch import META_FILE
from clipmachine.utils import claude
from clipmachine.utils.snapping import build_index

EXIT_OK = 0
EXIT_MOMENT_FAILED = 1
CAMPAIGN_DIR = "campaign"
NO_TIME_WARNING = (
    "warning: that run has no recorded modified time for its source; "
    "it was matched on path and size only"
)


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


def _line(moment_name: str, result: MomentResult, closers: int) -> str:
    label = moment_name.replace("_", " ", 1)
    if result.outcome == "refused":
        return f"{label}: refused: already posted (use --force-reuse to render it again)"
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
    return (
        f"{label}: rendered {clip.start:.2f}-{clip.end:.2f} s ({clip.duration:.2f} s), {start}, "
        f"{end}, {clip.spelling_replacements} spelling fixes, "
        f"closer {result.closer_number} of {closers}, {clip.render_seconds:.1f} s"
    )


def run_campaign(
    root: Path, target: Path, slug: str, *, only: str | None, force_reuse: bool
) -> int:
    """The exit code: 1 when a moment failed or one named in `only` was refused, else 0."""
    root = root.resolve()
    app = load_app_config(root)  # the current config, not the run's resolved copy
    if app.captions.style != "box_karaoke":
        raise ConfigError("campaign mode needs captions.style: box_karaoke in config/default.yaml")
    campaign = load_campaign(root, slug, app)
    all_moments = load_moments(campaign)
    moments = select_moments(all_moments, only)
    for warning in campaign.warnings:
        print(f"warning: {warning}")
    pipeline.check_ffmpeg()
    started = time.perf_counter()

    with claude.no_api():
        ledger = root / claude.LEDGER_PATH
        ledger_before = _size(ledger)
        run_dir = prepare_run(root, target)
        costs = run_dir / claude.COSTS_FILE
        costs_before = _size(costs)

        inputs = stage.load_source_inputs(run_dir, root, app)
        out_dir = run_dir / CAMPAIGN_DIR / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        if inputs.track_plan is not None:  # track files live with the campaign's clips
            inputs = dataclasses.replace(
                inputs, track_plan=dataclasses.replace(inputs.track_plan, clips_dir=out_dir)
            )
        plan = stage._make_plan(run_dir, out_dir, app, inputs)
        if plan.wordlists is not None:  # the campaign's own allowed and extra words
            cfg = campaign.config
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
        cfg = campaign.config
        variants = find_variants(
            index,
            cfg.names,
            apply_threshold=app.campaign.name_match_threshold,
            suggest_threshold=app.campaign.name_suggest_threshold,
        )
        auto, covered = auto_rules(variants, cfg.spelling)
        listed = [*cfg.names_to_check, *(n for n in cfg.names if n not in cfg.names_to_check)]
        source_sha256 = stage._load_meta(run_dir).sha256
        ctx = MomentContext(
            root=root,
            run_dir=run_dir,
            out_dir=out_dir,
            plan=plan,
            app=app,
            campaign=campaign,
            units=index.units(),
            duration=transcript.audio_duration_seconds,
            source_sha256=source_sha256,
            spelling_rules=(*cfg.spelling, *auto),
            variant_texts=tuple(v.text for v in variants),
            names_to_check=tuple(listed),
        )
        heard: dict[str, list[tuple[str, float]]] = {v.text: [] for v in variants}
        used = load_used(campaign.folder)
        closers = len(campaign.config.post.closers)
        wanted = {part.strip() for part in only.split(",")} if only else set()
        counts = {"rendered": 0, "failed": 0, "refused": 0}
        refused_named = False
        for moment in moments:
            result = render_moment(ctx, moment, used, force_reuse=force_reuse)
            used = result.used
            counts[result.outcome] += 1
            if result.outcome == "rendered":
                save_used(campaign.folder, used)
                for text, times in (result.variant_times or {}).items():
                    heard[text].extend((moment.clip_name, t) for t in times)
            if result.outcome == "refused" and moment.id in wanted:
                refused_named = True
            print(_line(moment.clip_name, result, closers))

        if _size(ledger) != ledger_before or _size(costs) != costs_before:
            raise RunError("campaign mode made an API call; this must never happen")

    seconds = time.perf_counter() - started
    auto_spelling = None
    if cfg.names:
        auto_spelling = record(
            variants,
            covered,
            heard,
            names=cfg.names,
            apply_threshold=app.campaign.name_match_threshold,
            suggest_threshold=app.campaign.name_suggest_threshold,
            source_sha256=source_sha256,
        )
        write_json_atomic(
            campaign.folder / AUTO_SPELLING_FILE, auto_spelling.model_dump(mode="json")
        )
        suggestions = sum(1 for v in variants if v.status == "suggest")
        print(
            f"names: {len(auto)} automatic spelling rules, {suggestions} suggestions, "
            f"{len(covered)} already in spelling; see {AUTO_SPELLING_FILE} and the report"
        )
    print(
        f"campaign: {counts['rendered']} rendered, {counts['failed']} failed, "
        f"{counts['refused']} refused; API calls: 0; {seconds:.1f} s"
    )
    print(f"output: {out_dir}")
    report = write_report(out_dir, campaign, all_moments, run_dir.name, auto=auto_spelling)
    print(f"report: {report}")
    return EXIT_MOMENT_FAILED if counts["failed"] or refused_named else EXIT_OK
