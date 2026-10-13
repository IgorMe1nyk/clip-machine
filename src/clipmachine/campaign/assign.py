"""Which platform each moment belongs to (Brief 11.2): campaigns/<slug>/assignments.json.

A moment is posted once, on one platform. moments.yaml may say which (`platform: tiktok`) or
leave it to the tool (`platform: auto`, or no `platform` at all). The choice is made once and
locked in assignments.json, so a re-render never moves a moment; delete the file to choose again.

For one moment, in this order:
1. the platform it was **posted** on (used.json), whatever the file or moments.yaml say;
2. an explicit `platform:` in moments.yaml;
3. the platform locked in assignments.json;
4. the platform with the fewest moments so far among `campaign.platforms`, ties going to the
   list's order. Walking moments.yaml in order (the list is ranked), this is a round-robin on a
   fresh campaign and stays balanced when moments are added later.

Pure apart from reading the lock; run.py writes it. Nothing here prints or logs a hook or a name.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from clipmachine.campaign.models import (
    Assignments,
    CampaignConfig,
    Moment,
    MomentSpec,
    Platform,
    UsedLog,
)
from clipmachine.campaign.used import latest_posted
from clipmachine.config import ConfigError

ASSIGNMENTS_FILE = "assignments.json"


def load_assignments(folder: Path) -> Assignments | None:
    """The lock, or None when there is no file. A file that cannot be read is an error, never
    silently reset: it decides where posted and rendered moments belong."""
    path = folder / ASSIGNMENTS_FILE
    if not path.is_file():
        return None
    try:
        return Assignments.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as exc:
        raise ConfigError(
            f"{ASSIGNMENTS_FILE} in {folder.name} cannot be read ({type(exc).__name__}); "
            "fix it by hand or delete it to assign the platforms again"
        ) from exc


def assign_platforms(
    folder: Path,
    cfg: CampaignConfig,
    specs: Sequence[MomentSpec],
    used: UsedLog,
    *,
    now: str | None = None,
) -> tuple[list[Moment], Assignments, bool]:
    """The moments with their platforms, in moments.yaml order, the lock to keep, and whether
    the lock differs from the file (then the caller writes it)."""
    lock = load_assignments(folder)
    locked = dict(lock.assigned) if lock is not None else {}
    counts: dict[str, int] = dict.fromkeys(cfg.platforms, 0)
    assigned: dict[str, Platform] = {}
    moments: list[Moment] = []
    for spec in specs:
        posted = latest_posted(used, spec.id)
        platform: Platform
        if posted is not None:
            platform = posted.platform
        elif spec.platform != "auto":
            platform = spec.platform
        elif spec.id in locked:
            platform = locked[spec.id]
        else:
            platform = min(cfg.platforms, key=lambda p: (counts[p], cfg.platforms.index(p)))
        if platform in counts:
            counts[platform] += 1
        assigned[spec.id] = platform
        moments.append(Moment.from_spec(spec, platform))
    # ids no longer in moments.yaml keep their place in the file: they may come back
    merged = {**locked, **assigned}
    changed = lock is None or lock.assigned != merged or lock.platforms != cfg.platforms
    written_at = (
        (now or datetime.now(UTC).isoformat()) if changed or lock is None else lock.written_at
    )
    new_lock = Assignments(platforms=list(cfg.platforms), assigned=merged, written_at=written_at)
    return moments, new_lock, changed


def assigned_platform(folder: Path, moment_id: str) -> Platform | None:
    """The platform the lock gives a moment, or None (no file, or the id is not in it)."""
    lock = load_assignments(folder)
    if lock is None:
        return None
    return lock.assigned.get(moment_id)


def count_by_platform(moments: Sequence[Moment], platforms: Sequence[Platform]) -> dict[str, int]:
    """How many moments each listed platform has, in the list's order (for the console and the
    report)."""
    counts: dict[str, int] = dict.fromkeys(platforms, 0)
    for moment in moments:
        counts[moment.platform] = counts.get(moment.platform, 0) + 1
    return counts
