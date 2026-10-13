"""posting_plan.csv (Brief 11.2): what to post when, and what happened to what was posted.

One row per clip, keyed by the moment id, opened in Excel:

- a **posted** clip (used.json) keeps its row: `posted` yes, the date it was marked posted (the
  computer's local date), the platform it really went to, no new slot;
- an **unposted** rendered clip gets a slot: per platform, the clips in assignment order
  (strongest first) take the campaign's `posting_times` in turn, one set of times per day,
  starting the day after `today`.

The result columns (views at 48 h, likes, comments, shares, the share of US/CA/UK viewers, the
average watch time, the share who finished, notes) are Igor's. The old file is read first and a
value already in it is never overwritten; only the slot of an unposted clip is recomputed. Extra
columns Igor adds are kept. Nothing here prints or logs a hook, a caption or a closer.
"""

import csv
import io
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from clipmachine.campaign.models import CampaignConfig, Moment, UsedEntry, UsedLog
from clipmachine.campaign.used import latest_posted
from clipmachine.config import ConfigError
from clipmachine.runs import write_text_atomic

PLAN_FILE = "posting_plan.csv"
SLOT_COLUMNS = ("date", "time_et", "platform", "id", "file", "text_file", "posted")
RESULT_COLUMNS = (
    "views_48h",
    "likes",
    "comments",
    "shares",
    "pct_us_ca_uk",
    "avg_watch_s",
    "pct_completed",
    "notes",
)
PLAN_COLUMNS = (*SLOT_COLUMNS, *RESULT_COLUMNS)
Row = dict[str, str]


def clock_12h(time_24h: str) -> str:
    """'15:30' -> '3:30 PM', '00:05' -> '12:05 AM'."""
    hours, minutes = (int(part) for part in time_24h.split(":"))
    suffix = "AM" if hours < 12 else "PM"
    shown = hours % 12 or 12
    return f"{shown}:{minutes:02d} {suffix}"


def local_date(stamp: str) -> str:
    """The local calendar date of an ISO 8601 time stamp (used.json keeps UTC)."""
    return datetime.fromisoformat(stamp).astimezone().date().isoformat()


def read_posting_plan(path: Path) -> dict[str, Row]:
    """The rows of an existing plan by id (Excel's BOM and CRLF are fine); {} when there is no
    file. A file with no `id` column is an error: it is not a plan."""
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"{path.name} cannot be read ({type(exc).__name__})") from exc
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or "id" not in reader.fieldnames:
        raise ConfigError(f"{path.name} has no 'id' column; rename or delete it")
    rows: dict[str, Row] = {}
    for raw in reader:
        row = {k: (v or "").strip() for k, v in raw.items() if k is not None}
        if row.get("id"):
            rows[row["id"]] = row
    return rows


def _posted_row(entry: UsedEntry, root: Path, out_dir: Path) -> Row:
    mp4 = root / entry.output
    try:
        file = mp4.relative_to(out_dir).as_posix()
    except ValueError:
        file = entry.output
    text_file = ""
    for candidate in (mp4.with_suffix(".txt"), mp4.with_name(mp4.stem + ".post.txt")):
        if candidate.is_file():
            text_file = (
                candidate.relative_to(out_dir).as_posix()
                if candidate.is_relative_to(out_dir)
                else candidate.relative_to(root).as_posix()
            )
            break
    return {
        "date": local_date(entry.posted_at) if entry.posted_at else "",
        "time_et": "",
        "platform": entry.platform,
        "id": entry.id,
        "file": file,
        "text_file": text_file,
        "posted": "yes",
    }


def plan_rows(
    cfg: CampaignConfig,
    moments: Sequence[Moment],
    used: UsedLog,
    *,
    root: Path,
    out_dir: Path,
    start: date,
    existing: Mapping[str, Row] | None = None,
) -> list[Row]:
    """The rows to write: posted clips by date, then the scheduled ones by date, time and the
    campaign's platform order. Values from `existing` win over computed ones, except the slot of
    an unposted clip."""
    old = existing or {}
    posted_rows: list[Row] = []
    scheduled: list[tuple[date, int, int, Row]] = []
    posted_ids: set[str] = set()
    for moment in moments:
        posted = latest_posted(used, moment.id)
        if posted is not None:
            posted_ids.add(moment.id)
            posted_rows.append(_posted_row(posted, root, out_dir))
    for entry in used.entries:  # posted moments that left moments.yaml still count
        if entry.posted_at and entry.id not in posted_ids:
            latest = latest_posted(used, entry.id)
            if latest is not None:
                posted_ids.add(entry.id)
                posted_rows.append(_posted_row(latest, root, out_dir))
    times = cfg.posting_times
    for order, platform in enumerate(cfg.platforms):
        slot = 0
        for moment in moments:
            if moment.platform != platform or moment.id in posted_ids:
                continue
            if not (out_dir / platform / f"{moment.id}.mp4").is_file():
                continue
            day = start + timedelta(days=slot // len(times))
            when = times[slot % len(times)]
            row: Row = {
                "date": day.isoformat(),
                "time_et": clock_12h(when),
                "platform": platform,
                "id": moment.id,
                "file": f"{platform}/{moment.id}.mp4",
                "text_file": f"{platform}/{moment.id}.txt",
                "posted": "no",
            }
            scheduled.append((day, slot % len(times), order, row))
            slot += 1
    posted_rows.sort(key=lambda r: (r["date"], r["id"]))
    scheduled.sort(key=lambda item: item[:3])
    rows = [*posted_rows, *(row for _, _, _, row in scheduled)]
    return [_merge(row, old.get(row["id"]), row["posted"] == "yes") for row in rows]


def _merge(row: Row, old: Row | None, posted: bool) -> Row:
    """Igor's values (and any extra column) first; the slot of an unposted clip is always the
    new one. A posted clip's platform and status come from used.json (which --fix-platform
    corrects) and its date from used.json the first time it shows as posted; after that its date,
    time and files stay as Igor left them. The time of a clip that was scheduled and then posted
    stays its planned slot until Igor corrects it."""
    merged: Row = dict.fromkeys(PLAN_COLUMNS, "")
    if old is not None:
        merged.update(old)
    was_posted = old is not None and old.get("posted") == "yes"
    for column in SLOT_COLUMNS:
        keep = posted and column not in ("platform", "posted") and merged.get(column)
        if keep and (column != "date" or was_posted):
            continue
        merged[column] = row[column]
    return merged


def write_posting_plan(
    out_dir: Path,
    cfg: CampaignConfig,
    moments: Sequence[Moment],
    used: UsedLog,
    *,
    root: Path,
    today: date,
) -> Path:
    """Read the old plan, rebuild the rows with slots from tomorrow on, write it (UTF-8, LF)."""
    path = out_dir / PLAN_FILE
    existing = read_posting_plan(path)
    rows = plan_rows(
        cfg,
        moments,
        used,
        root=root,
        out_dir=out_dir,
        start=today + timedelta(days=1),
        existing=existing,
    )
    extra = [c for r in rows for c in r if c not in PLAN_COLUMNS]
    columns = [*PLAN_COLUMNS, *dict.fromkeys(extra)]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    write_text_atomic(path, buffer.getvalue())
    return path
