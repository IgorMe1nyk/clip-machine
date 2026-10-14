"""The used-moments log of a campaign (Brief 11): campaigns/<slug>/used.json.

The file is local and git-ignored. It remembers which moments were rendered, which were posted
and which closer each got, so a moment is not reused by accident. A render is not a post: only
`campaign-posted` marks a moment as posted. The functions here return a new log and never change
the one they are given.
"""

from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from clipmachine.campaign.models import (
    CloserState,
    Platform,
    RejectedMoment,
    UsedEntry,
    UsedLog,
)
from clipmachine.config import ConfigError
from clipmachine.runs import write_json_atomic

USED_FILE = "used.json"
# a posted clip counts as reused when it overlaps a new moment by more than this share of the new
# moment's own length
REUSE_OVERLAP = 0.5


def load_used(folder: Path) -> UsedLog:
    """The log, or an empty one when there is no file yet. A file that cannot be read or is not
    valid is an error, never silently reset: it holds what was posted."""
    path = folder / USED_FILE
    if not path.is_file():
        return UsedLog()
    try:
        return UsedLog.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as exc:
        raise ConfigError(
            f"{USED_FILE} in {folder.name} cannot be used ({type(exc).__name__}); "
            "fix it by hand or remove it"
        ) from exc


def save_used(folder: Path, log: UsedLog) -> None:
    write_json_atomic(folder / USED_FILE, log.model_dump(mode="json"))


def take_closer(
    log: UsedLog, closers: Sequence[str], platform: str, *, first: int = 0
) -> tuple[str, UsedLog]:
    """The platform's next closer, round-robin, never the one that platform used last when there
    are two or more (Brief 11.2: one rotation per platform). A platform with no rotation yet
    starts at `first`, so the platforms need not all open with the same closer. Works when the
    closers were edited since the last run: the position is taken modulo their count."""
    count = len(closers)
    state = log.closers.get(platform, CloserState(next=first))
    index = state.next % count
    if count >= 2 and closers[index] == state.last:
        index = (index + 1) % count
    chosen = closers[index]
    states = {**log.closers, platform: CloserState(next=index + 1, last=chosen)}
    return chosen, log.model_copy(update={"closers": states})


def posted_entry(log: UsedLog, moment_id: str, platform: str) -> UsedEntry | None:
    """The latest posted entry for this id and platform."""
    for entry in reversed(log.entries):
        if entry.id == moment_id and entry.platform == platform and entry.posted_at:
            return entry
    return None


def latest_posted(log: UsedLog, moment_id: str) -> UsedEntry | None:
    """The latest posted entry for this id on any platform."""
    for entry in reversed(log.entries):
        if entry.id == moment_id and entry.posted_at:
            return entry
    return None


def latest_source_name(log: UsedLog, moment_id: str, platform: str) -> str:
    """The source file name of the latest render of this id on this platform, or ""."""
    for entry in reversed(log.entries):
        if entry.id == moment_id and entry.platform == platform:
            return entry.source_name or ""
    return ""


def rendered_platforms(log: UsedLog, moment_id: str) -> list[str]:
    """The platforms this id has an unposted render on, in the order they were rendered."""
    return list(
        dict.fromkeys(e.platform for e in log.entries if e.id == moment_id and not e.posted_at)
    )


def relabel_posted(
    log: UsedLog, moment_id: str, new_platform: Platform, current: str | None = None
) -> tuple[UsedLog, UsedEntry, UsedEntry]:
    """Give the latest posted entry of this id another platform (Brief 11.2, to correct an early
    record): (log, before, after). `current` names the entry's platform when the id was posted on
    more than one. No posted entry, or an ambiguous choice, is an error that says so."""
    posted = [e for e in log.entries if e.id == moment_id and e.posted_at]
    if not posted:
        raise ConfigError(f"moment {moment_id} has no posted entry to relabel")
    if current is not None:
        posted = [e for e in posted if e.platform == current]
        if not posted:
            raise ConfigError(f"moment {moment_id} has no posted entry under {current}")
    platforms = {e.platform for e in posted}
    if len(platforms) > 1:
        raise ConfigError(
            f"moment {moment_id} is posted under {', '.join(sorted(platforms))}; "
            "say which with --platform"
        )
    before = posted[-1]
    after = before.model_copy(update={"platform": new_platform})
    entries = [after if e is before else e for e in log.entries]
    return log.model_copy(update={"entries": entries}), before, after


def overlapping_posted(
    log: UsedLog, start: float, end: float, source_sha256: str
) -> list[UsedEntry]:
    """Posted entries of the same source (any id, any platform) that overlap [start, end] by more
    than REUSE_OVERLAP of this moment's length."""
    length = end - start
    if length <= 0:
        return []
    return [
        e
        for e in log.entries
        if e.posted_at
        and e.source_sha256 == source_sha256
        and max(0.0, min(end, e.end) - max(start, e.start)) > REUSE_OVERLAP * length
    ]


def record_render(log: UsedLog, entry: UsedEntry) -> UsedLog:
    """Add a rendered moment. An unposted entry for the same id and platform is replaced; a
    posted one is kept."""
    kept = [
        e
        for e in log.entries
        if not (e.id == entry.id and e.platform == entry.platform and not e.posted_at)
    ]
    return log.model_copy(update={"entries": [*kept, entry]})


def is_rejected(log: UsedLog, moment_id: str) -> bool:
    return any(r.id == moment_id for r in log.rejected)


def reject(
    log: UsedLog, moment_id: str, source: str | None, start: float, end: float, when: str
) -> UsedLog:
    """Add a rejected moment (Brief 12). A posted moment cannot be rejected; a second rejection
    of the same id is an error, so Igor sees that it was already done."""
    if latest_posted(log, moment_id) is not None:
        raise ConfigError(f"moment {moment_id} is posted; a posted moment cannot be rejected")
    if is_rejected(log, moment_id):
        raise ConfigError(f"moment {moment_id} is already rejected")
    entry = RejectedMoment(id=moment_id, source=source, start=start, end=end, rejected_at=when)
    return log.model_copy(update={"rejected": [*log.rejected, entry]})


def mark_posted(
    log: UsedLog, moment_id: str, platform: str, when: str
) -> tuple[UsedLog, UsedEntry, bool]:
    """Mark the latest unposted entry for this id and platform as posted: (log, entry, True).
    Already posted and nothing newer: (the same log, that entry, False). No entry at all is an
    error."""
    entries = list(log.entries)
    for i in range(len(entries) - 1, -1, -1):
        e = entries[i]
        if e.id == moment_id and e.platform == platform and not e.posted_at:
            marked = e.model_copy(update={"posted_at": when})
            entries[i] = marked
            return log.model_copy(update={"entries": entries}), marked, True
    already = posted_entry(log, moment_id, platform)
    if already is not None:
        return log, already, False
    raise ConfigError(
        f"moment {moment_id} has not been rendered for {platform} yet; nothing to mark as posted"
    )
