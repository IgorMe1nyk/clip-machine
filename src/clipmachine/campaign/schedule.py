"""One posting plan across every campaign (Brief 12.2, Igor's addition): `campaigns/schedule.yaml`
says the posting times, how many posts a platform takes per day and which campaign comes first;
`runs/_campaigns/posting_plan.csv` is the single list Igor schedules from.

Allocation, per platform, day by day from now (a slot whose time has passed today is never
given): the day's free slots go to the campaigns in priority order; a campaign takes at most its
`max_per_day` of them, so a lower campaign still gets slots. A campaign that does not post on a
platform never takes its slots. Posted rows come from each campaign's used.json.

The combined file is the master: result columns (and any extra column) are read first and never
overwritten, keyed by (campaign, id). On the first build, every per-campaign posting_plan.csv is
copied to `posting_plan.pre-12-2.bak` (never overwritten) and its result columns are carried
over. From then on each campaign's posting_plan.csv is a read-only view of its rows, headed by a
comment line. Nothing here prints or logs a hook, a caption or a closer.
"""

import csv
import io
import shutil
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from clipmachine.campaign.assign import assign_platforms
from clipmachine.campaign.loader import SLUG_RE, load_campaign, parse_moments
from clipmachine.campaign.models import (
    PLATFORMS,
    CampaignConfig,
    Moment,
    ScheduleConfig,
    UsedLog,
)
from clipmachine.campaign.plan import (
    PLAN_COLUMNS,
    PLAN_FILE,
    RESULT_COLUMNS,
    SLOT_COLUMNS,
    Key,
    Row,
    _merge,
    _posted_row,
    clock_12h,
    read_plan,
)
from clipmachine.campaign.used import is_rejected, latest_posted, latest_source_name, load_used
from clipmachine.config import ConfigError, _read_yaml, _validate, load_app_config
from clipmachine.models import AppConfig
from clipmachine.runs import write_text_atomic

EXIT_OK = 0
SCHEDULE_FILE = "schedule.yaml"  # campaigns/schedule.yaml
CAMPAIGNS_OUT = Path("runs") / "_campaigns"
COMBINED_PLAN = CAMPAIGNS_OUT / PLAN_FILE  # runs/_campaigns/posting_plan.csv
PRE_BACKUP = "posting_plan.pre-12-2.bak"  # a per-campaign plan as it was before the switch
COMBINED_SLOT_COLUMNS = (
    "date",
    "time_et",
    "platform",
    "campaign",
    "source",
    "id",
    "file",
    "text_file",
    "posted",
)
COMBINED_COLUMNS = (*COMBINED_SLOT_COLUMNS, *RESULT_COLUMNS)
COMBINED_KEY: Key = ("campaign", "id")
VIEW_HEADER = f"# read-only - edit {COMBINED_PLAN.as_posix()}"
MOMENTS_FILE = "moments.yaml"


# --- the configuration ---------------------------------------------------------------------------


def schedule_path(root: Path) -> Path:
    return root / "campaigns" / SCHEDULE_FILE


def load_schedule(root: Path) -> ScheduleConfig | None:
    """The schedule, or None when there is no campaigns/schedule.yaml (then every campaign keeps
    its own plan, as before)."""
    path = schedule_path(root)
    if not path.is_file():
        return None
    return _validate(ScheduleConfig, _read_yaml(path), f"campaigns/{SCHEDULE_FILE}")


def list_campaigns(root: Path) -> list[str]:
    """Every campaign folder holding a campaign.yaml, sorted; the example (a leading `_`) and
    folders that are not valid names are left out."""
    base = root / "campaigns"
    if not base.is_dir():
        return []
    return sorted(
        p.name
        for p in base.iterdir()
        if p.is_dir()
        and not p.name.startswith("_")
        and SLUG_RE.fullmatch(p.name)
        and (p / "campaign.yaml").is_file()
    )


def campaign_order(schedule: ScheduleConfig, slugs: Sequence[str]) -> list[tuple[str, int | None]]:
    """(slug, max_per_day) in scheduling order: the priority list first, then the other
    campaigns alphabetically with no cap. A listed campaign with no folder is an error."""
    order: list[tuple[str, int | None]] = []
    for entry in schedule.priority:
        if entry.slug not in slugs:
            raise ConfigError(
                f"campaigns/{SCHEDULE_FILE}: priority names {entry.slug!r}, but there is no "
                f"campaigns/{entry.slug}/campaign.yaml"
            )
        order.append((entry.slug, entry.max_per_day))
    listed = {slug for slug, _ in order}
    order.extend((slug, None) for slug in sorted(slugs) if slug not in listed)
    return order


# --- what each campaign brings -------------------------------------------------------------------


@dataclass(frozen=True)
class PlanInput:
    slug: str
    cfg: CampaignConfig
    moments: list[Moment]  # every moment, platform decided
    used: UsedLog
    out_dir: Path  # runs/_campaigns/<slug>


def plan_input(root: Path, app: AppConfig, slug: str) -> PlanInput:
    """A campaign as the planner needs it, with nothing written: platforms are decided as
    `campaign` would, but a new lock is not saved (a moment with no clip gets no slot anyway)."""
    campaign = load_campaign(root, slug, app)
    used = load_used(campaign.folder)
    path = campaign.folder / MOMENTS_FILE
    specs = (
        parse_moments(path.read_text(encoding="utf-8"), campaign.config.hook.max_chars)
        if path.is_file() and path.read_text(encoding="utf-8").strip()
        else []
    )
    moments, _, _ = assign_platforms(campaign.folder, campaign.config, specs, used)
    return PlanInput(slug, campaign.config, list(moments), used, root / CAMPAIGNS_OUT / slug)


def pending_by_platform(inp: PlanInput) -> dict[str, deque[Moment]]:
    """The campaign's unposted, unrejected moments with a clip, per platform, in moments order."""
    pending: dict[str, deque[Moment]] = {p: deque() for p in PLATFORMS}
    for moment in inp.moments:
        if latest_posted(inp.used, moment.id) is not None or is_rejected(inp.used, moment.id):
            continue
        if (inp.out_dir / moment.platform / f"{moment.id}.mp4").is_file():
            pending[moment.platform].append(moment)
    return pending


# --- allocation ----------------------------------------------------------------------------------


def _posted_rows(inp: PlanInput, root: Path) -> list[Row]:
    rows: list[Row] = []
    seen: set[str] = set()
    for moment in inp.moments:
        posted = latest_posted(inp.used, moment.id)
        if posted is not None:
            seen.add(moment.id)
            rows.append(_posted_row(posted, root, inp.out_dir, file_prefix=f"{inp.slug}/"))
    for entry in inp.used.entries:  # posted moments that left moments.yaml still count
        if entry.posted_at and entry.id not in seen:
            latest = latest_posted(inp.used, entry.id)
            if latest is not None:
                seen.add(entry.id)
                rows.append(_posted_row(latest, root, inp.out_dir, file_prefix=f"{inp.slug}/"))
    for row in rows:
        row["campaign"] = inp.slug
    return rows


def allocate(
    schedule: ScheduleConfig,
    inputs: Sequence[PlanInput],
    order: Sequence[tuple[str, int | None]],
    *,
    root: Path,
    now: datetime,
) -> list[Row]:
    """Every row of the combined plan, slots not yet merged with the old file: posted rows by
    date, then the scheduled ones by date, time, platform and campaign order."""
    by_slug = {inp.slug: inp for inp in inputs}
    pending = {slug: pending_by_platform(by_slug[slug]) for slug, _ in order}
    times = schedule.posting_times
    usable = list(range(min(schedule.per_day, len(times))))
    now_hm = now.strftime("%H:%M")
    rank = {slug: i for i, (slug, _) in enumerate(order)}
    scheduled: list[tuple[str, int, int, int, Row]] = []
    for platform_order, platform in enumerate(PLATFORMS):
        day = now.date()
        while any(pending[slug][platform] for slug, _ in order):
            free = [i for i in usable if day != now.date() or times[i] > now_hm]
            taken: dict[str, int] = {slug: 0 for slug, _ in order}
            for slug, cap in order:
                queue = pending[slug][platform]
                while free and queue and (cap is None or taken[slug] < cap):
                    index = free.pop(0)
                    moment = queue.popleft()
                    taken[slug] += 1
                    inp = by_slug[slug]
                    row: Row = {
                        "date": day.isoformat(),
                        "time_et": clock_12h(times[index]),
                        "platform": platform,
                        "campaign": slug,
                        "source": latest_source_name(inp.used, moment.id, platform),
                        "id": moment.id,
                        "file": f"{slug}/{platform}/{moment.id}.mp4",
                        "text_file": f"{slug}/{platform}/{moment.id}.txt",
                        "posted": "no",
                    }
                    scheduled.append((day.isoformat(), index, platform_order, rank[slug], row))
            day += timedelta(days=1)
    posted = [row for inp in inputs for row in _posted_rows(inp, root)]
    posted.sort(key=lambda r: (r["date"], r["campaign"], r["id"]))
    scheduled.sort(key=lambda item: item[:4])
    return [*posted, *(row for *_, row in scheduled)]


# --- files ---------------------------------------------------------------------------------------


def _is_view(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8-sig") as handle:
            return handle.readline().startswith("#")
    except OSError:
        return False


def migrate_results(
    root: Path, inputs: Sequence[PlanInput], existing: dict[Key, Row]
) -> list[Path]:
    """Carry the result columns of every per-campaign plan that is still a real plan (not yet
    a view) into `existing` for the rows the combined file does not hold yet, after copying
    that plan to PRE_BACKUP once. Returns the backups made now."""
    backups: list[Path] = []
    keep = [c for c in PLAN_COLUMNS if c not in SLOT_COLUMNS or c in ("date", "time_et")]
    for inp in inputs:
        path = inp.out_dir / PLAN_FILE
        if not path.is_file() or _is_view(path):
            continue
        backup = path.with_name(PRE_BACKUP)
        if not backup.exists():
            shutil.copyfile(path, backup)
            backups.append(backup)
        for moment_id, row in read_plan(path, key=("id",)).items():
            key = (inp.slug, moment_id[0])
            if key in existing:
                continue
            carried = {c: v for c, v in row.items() if c in keep or c not in PLAN_COLUMNS}
            existing[key] = {"campaign": inp.slug, "id": moment_id[0], **carried}
    return backups


def _csv(rows: Sequence[Mapping[str, str]], columns: Sequence[str], *, header: str = "") -> str:
    buffer = io.StringIO()
    if header:
        buffer.write(header + "\n")
    writer = csv.DictWriter(buffer, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return buffer.getvalue()


def write_combined_plan(
    root: Path,
    schedule: ScheduleConfig,
    inputs: Sequence[PlanInput],
    *,
    now: datetime,
) -> tuple[Path, list[Path]]:
    """Rebuild runs/_campaigns/posting_plan.csv and the per-campaign views. Returns the path and
    the pre-12.2 backups made on this call."""
    path = root / COMBINED_PLAN
    existing = read_plan(path, key=COMBINED_KEY)
    backups = migrate_results(root, inputs, existing)
    order = campaign_order(schedule, [inp.slug for inp in inputs])
    rows = [
        _merge(
            row,
            existing.get((row["campaign"], row["id"])),
            row["posted"] == "yes",
            slot_columns=COMBINED_SLOT_COLUMNS,
            columns=COMBINED_COLUMNS,
        )
        for row in allocate(schedule, inputs, order, root=root, now=now)
    ]
    extra = [c for r in rows for c in r if c not in COMBINED_COLUMNS]
    columns = [*COMBINED_COLUMNS, *dict.fromkeys(extra)]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, _csv(rows, columns))
    write_plan_views(rows, inputs, extra=list(dict.fromkeys(extra)))
    return path, backups


def write_plan_views(
    rows: Sequence[Row], inputs: Sequence[PlanInput], *, extra: Sequence[str] = ()
) -> list[Path]:
    """Each campaign's posting_plan.csv as a read-only view of its rows in the combined plan:
    the comment line first, the campaign's own columns, paths without the `<slug>/` prefix."""
    written: list[Path] = []
    for inp in inputs:
        prefix = f"{inp.slug}/"
        own = []
        for row in rows:
            if row.get("campaign") != inp.slug:
                continue
            view = dict(row)
            for column in ("file", "text_file"):
                if view.get(column, "").startswith(prefix):
                    view[column] = view[column][len(prefix) :]
            own.append(view)
        inp.out_dir.mkdir(parents=True, exist_ok=True)
        path = inp.out_dir / PLAN_FILE
        write_text_atomic(path, _csv(own, [*PLAN_COLUMNS, *extra], header=VIEW_HEADER))
        written.append(path)
    return written


def rebuild_plans(
    root: Path,
    app: AppConfig,
    *,
    now: datetime,
    current: PlanInput | None = None,
) -> tuple[Path, list[Path]] | None:
    """The combined plan from every campaign on disk (`current` replaces the campaign the
    running command already holds in memory). None when there is no schedule.yaml."""
    schedule = load_schedule(root)
    if schedule is None:
        return None
    inputs = [
        current if current is not None and current.slug == slug else plan_input(root, app, slug)
        for slug in list_campaigns(root)
    ]
    if current is not None and current.slug not in [inp.slug for inp in inputs]:
        inputs.append(current)
    return write_combined_plan(root, schedule, inputs, now=now)


def report_backups(backups: Sequence[Path]) -> None:
    for backup in backups:
        print(f"backup of the old per-campaign plan: {backup}")


def run_plan(root: Path) -> int:
    """`clipmachine plan`: rebuild the combined plan and the views from what is on disk."""
    root = root.resolve()
    if load_schedule(root) is None:
        raise ConfigError(
            f"no campaigns/{SCHEDULE_FILE}: copy campaigns/_example/{SCHEDULE_FILE} there and "
            "set the times, the daily cap and the campaign order"
        )
    result = rebuild_plans(root, load_app_config(root), now=datetime.now())
    assert result is not None
    path, backups = result
    report_backups(backups)
    rows = read_plan(path, key=COMBINED_KEY).values()
    counts = {
        p: sum(1 for r in rows if r["platform"] == p and r["posted"] == "no") for p in PLATFORMS
    }
    scheduled = ", ".join(f"{p} {n}" for p, n in counts.items())
    print(f"posting plan: {path} ({scheduled} scheduled)")
    return EXIT_OK
