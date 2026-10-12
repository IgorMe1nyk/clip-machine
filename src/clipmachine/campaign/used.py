"""The used-moments log of a campaign (Brief 11): campaigns/<slug>/used.json.

The file is local and git-ignored. It remembers which moments were rendered, which were posted
and which closer each got, so a moment is not reused by accident. A render is not a post: only
`campaign-posted` marks a moment as posted. The functions here return a new log and never change
the one they are given.
"""

from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from clipmachine.campaign.models import UsedEntry, UsedLog
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


def take_closer(log: UsedLog, closers: Sequence[str]) -> tuple[str, UsedLog]:
    """The next closer, round-robin, never the one used last when there are two or more. Works
    when the closers were edited since the last run: the position is taken modulo their count."""
    count = len(closers)
    index = log.next_closer % count
    if count >= 2 and closers[index] == log.last_closer:
        index = (index + 1) % count
    chosen = closers[index]
    return chosen, log.model_copy(update={"next_closer": index + 1, "last_closer": chosen})


def posted_entry(log: UsedLog, moment_id: str, platform: str) -> UsedEntry | None:
    """The latest posted entry for this id and platform."""
    for entry in reversed(log.entries):
        if entry.id == moment_id and entry.platform == platform and entry.posted_at:
            return entry
    return None


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
