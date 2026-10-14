"""The `produce` command (Brief 12.2): one command per episode.

1. When campaign.yaml says `selection: ai`, the scout proposes moments for this episode (once per
   episode: a source already in scout.json is not scouted again; the `scout` command still can).
2. This episode's unposted, unrejected moments are rendered into the campaign folder. A clip
   whose moments.yaml entry, source and files did not change is kept (`--rerender` redoes it).
3. The report and the posting plan are rebuilt, and the next slots of the new clips are printed.

Console lines carry ids, platforms, counts, dollars and paths only, never a hook, a caption, a
closer or a name.
"""

from collections.abc import Sequence
from datetime import date
from pathlib import Path

from clipmachine import pipeline
from clipmachine.campaign.loader import load_campaign
from clipmachine.campaign.models import Moment, UsedLog
from clipmachine.campaign.plan import read_plan
from clipmachine.campaign.run import (
    EXIT_MOMENT_FAILED,
    EXIT_OK,
    finalize,
    open_campaign,
    prepare_run,
    render_moments,
)
from clipmachine.campaign.scout import (
    MOMENTS_FILE,
    already_scouted,
    scout_episode,
    source_matches,
)
from clipmachine.campaign.used import is_rejected, latest_posted
from clipmachine.config import ConfigError, load_app_config
from clipmachine.stages import render as stage
from clipmachine.utils import claude


def episode_moments(
    root: Path, moments: Sequence[Moment], used: UsedLog, episode: Path
) -> list[Moment]:
    """This episode's moments (their `source:` is this file, or they have none) that are neither
    posted nor rejected, in moments.yaml order."""
    return [
        m
        for m in moments
        if source_matches(root, m.source, episode)
        and latest_posted(used, m.id) is None
        and not is_rejected(used, m.id)
    ]


def check_sourceless(moments: Sequence[Moment], used: UsedLog, source_sha256: str) -> None:
    """A moment without `source:` that was rendered from another episode before is ambiguous:
    refuse rather than cut it from this one (Igor, 2026-10-13)."""
    for moment in moments:
        if moment.source is not None:
            continue
        elsewhere = [
            e for e in used.entries if e.id == moment.id and e.source_sha256 != source_sha256
        ]
        if elsewhere:
            raise ConfigError(
                f"moment {moment.id} has no source: and was rendered from another episode before; "
                "add source: to it in moments.yaml (or produce that episode)"
            )


def _next_slots(plan_path: Path, rendered: Sequence[Moment], slug: str) -> list[str]:
    """The planned slot of each newly rendered clip, from the plan just written."""
    rows = read_plan(plan_path, key=("id",))
    lines = []
    for moment in rendered:
        row = rows.get((moment.id,))
        if row is None or row.get("campaign", slug) != slug:
            continue
        if row.get("date"):
            lines.append(f"{row['date']} {row['time_et']} {row['platform']} {moment.id}")
    return lines


def run_produce(
    root: Path, episode: Path, slug: str, *, rerender: bool, today: date | None = None
) -> int:
    """The exit code: 1 when a moment failed to render, else 0."""
    root = root.resolve()
    campaign = load_campaign(root, slug, load_app_config(root))  # moments.yaml may not exist yet
    cfg = campaign.config
    cost_usd, calls = 0.0, 0
    pipeline.check_ffmpeg()
    scouting = cfg.selection == "ai" and cfg.scout is not None
    if scouting:
        run_dir = prepare_run(root, episode)
        sha = stage._load_meta(run_dir).sha256
        seen = already_scouted(campaign.folder, sha)
        if seen:
            print(
                f"scout: this episode was scouted before ({seen} proposals in scout.json); "
                f"nothing added. To scout it again: scout {episode} --campaign {slug}"
            )
        else:
            outcome = scout_episode(root, episode, slug)
            cost_usd, calls = outcome.cost_usd, outcome.calls
    else:
        with claude.no_api():
            run_dir = prepare_run(root, episode)
        sha = stage._load_meta(run_dir).sha256
    if not (campaign.folder / MOMENTS_FILE).is_file():
        print("produce: this campaign has no moments.yaml yet; nothing to render")
        if scouting and calls:
            print(f"API cost: ${cost_usd:.4f} in {calls} calls (scout)")
        return EXIT_OK
    opened = open_campaign(root, slug)
    episode_file = Path(pipeline.load_state(run_dir).input)
    moments = episode_moments(root, opened.moments, opened.used, episode_file)
    check_sourceless(moments, opened.used, sha)
    if not moments:
        print("produce: no unposted moments for this episode")
    rendered = render_moments(
        root, opened, moments, episode, force_reuse=False, skip_unchanged=not rerender
    )
    _, plan_path = finalize(root, opened, rendered, today=today or date.today())

    by_platform = ", ".join(
        f"{platform} {sum(1 for m in rendered.rendered if m.platform == platform)} new"
        for platform in cfg.platforms
    )
    counts = rendered.counts
    print(
        f"produce: {by_platform}; {counts['unchanged']} unchanged, {counts['failed']} failed, "
        f"{counts['rejected']} rejected"
    )
    slots = _next_slots(plan_path, rendered.rendered, slug)
    print("next slots: " + ("; ".join(slots) if slots else "none (see the plan)"))
    if scouting and calls:
        print(f"API cost: ${cost_usd:.4f} in {calls} calls (scout)")
    else:
        print("API cost: $0 (no scout call)")
    return EXIT_MOMENT_FAILED if counts["failed"] else EXIT_OK
