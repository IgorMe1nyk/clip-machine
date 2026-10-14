"""The combined posting plan (Brief 12.2): campaigns/schedule.yaml, strict priority with caps,
one file across campaigns, migration of result columns, read-only views. Invented words only."""

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from campaign_helpers import base_campaign, moment, write_campaign, write_moments
from clipmachine import cli
from clipmachine.campaign.models import (
    CampaignConfig,
    Moment,
    RejectedMoment,
    ScheduleConfig,
    UsedEntry,
    UsedLog,
)
from clipmachine.campaign.plan import PLAN_FILE, read_posting_plan
from clipmachine.campaign.schedule import (
    COMBINED_PLAN,
    PRE_BACKUP,
    VIEW_HEADER,
    PlanInput,
    allocate,
    campaign_order,
    list_campaigns,
    load_schedule,
    write_combined_plan,
)
from clipmachine.campaign.used import record_render, save_used
from clipmachine.config import ConfigError

TIMES = ["12:30", "15:00", "17:30", "20:00"]
MORNING = datetime(2026, 10, 13, 9, 0)  # every slot of the day is still ahead
AFTERNOON = datetime(2026, 10, 13, 15, 0)  # 12:30 and 15:00 have passed


def schedule(**changes: Any) -> ScheduleConfig:
    data: dict[str, Any] = {
        "posting_times": TIMES,
        "priority": [{"slug": "alpha"}],  # beta, unlisted, comes after
    }
    data.update(changes)
    return ScheduleConfig.model_validate(data)


def cfg(**changes: Any) -> CampaignConfig:
    data = base_campaign()
    data.update(changes)
    return CampaignConfig.model_validate(data)


def mom(moment_id: str, platform: str) -> Moment:
    return Moment.model_validate(moment(id=moment_id, platform=platform))


def entry(slug: str, moment_id: str, platform: str, *, posted: str | None = None) -> UsedEntry:
    return UsedEntry.model_validate(
        {
            "id": moment_id,
            "platform": platform,
            "start": 1.0,
            "end": 30.0,
            "hook": "X",
            "closer": "c",
            "rendered_at": "2026-10-12T10:00:00+00:00",
            "output": f"runs/_campaigns/{slug}/{platform}/{moment_id}.mp4",
            "source_sha256": "a" * 64,
            "source_name": f"{slug}-ep.mp4",
            "posted_at": posted,
        }
    )


class Rig:
    """Campaign inputs with rendered clips under runs/_campaigns/<slug>/<platform>/."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def campaign(
        self,
        slug: str,
        *clips: tuple[str, str],
        used: UsedLog | None = None,
        platforms: list[str] | None = None,
    ) -> PlanInput:
        out = self.root / "runs" / "_campaigns" / slug
        moments = []
        for moment_id, platform in clips:
            (out / platform).mkdir(parents=True, exist_ok=True)
            (out / platform / f"{moment_id}.mp4").write_bytes(b"v")
            (out / platform / f"{moment_id}.txt").write_text("t\n", encoding="utf-8")
            moments.append(mom(moment_id, platform))
        config = cfg(platforms=platforms) if platforms else cfg()
        return PlanInput(slug, config, moments, used or UsedLog(), out)

    def rows(
        self,
        sched: ScheduleConfig,
        *inputs: PlanInput,
        now: datetime = MORNING,
    ) -> list[dict[str, str]]:
        order = campaign_order(sched, [i.slug for i in inputs])
        return allocate(sched, inputs, order, root=self.root, now=now)

    def write(self, sched: ScheduleConfig, *inputs: PlanInput, now: datetime = MORNING) -> Path:
        path, _ = write_combined_plan(self.root, sched, inputs, now=now)
        assert path == self.root / COMBINED_PLAN
        return path


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def slots(rows: list[dict[str, str]]) -> list[tuple[str, str, str, str, str]]:
    return [(r["date"], r["time_et"], r["platform"], r["campaign"], r["id"]) for r in rows]


def read(path: Path) -> list[dict[str, str]]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.startswith("#")]
    return list(csv.DictReader(lines))


# --- allocation ----------------------------------------------------------------------------------


def test_strict_order_the_first_campaign_takes_every_free_slot_until_it_runs_dry(rig: Rig) -> None:
    alpha = rig.campaign("alpha", *((f"A{i}", "tiktok") for i in range(1, 7)))
    beta = rig.campaign("beta", ("B1", "tiktok"), ("B2", "tiktok"), ("B3", "tiktok"))
    rows = rig.rows(schedule(), alpha, beta)
    assert slots(rows) == [
        ("2026-10-13", "12:30 PM", "tiktok", "alpha", "A1"),
        ("2026-10-13", "3:00 PM", "tiktok", "alpha", "A2"),
        ("2026-10-13", "5:30 PM", "tiktok", "alpha", "A3"),
        ("2026-10-13", "8:00 PM", "tiktok", "alpha", "A4"),
        ("2026-10-14", "12:30 PM", "tiktok", "alpha", "A5"),
        ("2026-10-14", "3:00 PM", "tiktok", "alpha", "A6"),
        ("2026-10-14", "5:30 PM", "tiktok", "beta", "B1"),
        ("2026-10-14", "8:00 PM", "tiktok", "beta", "B2"),
        ("2026-10-15", "12:30 PM", "tiktok", "beta", "B3"),
    ]
    first = rows[0]
    assert (first["file"], first["text_file"], first["posted"], first["source"]) == (
        "alpha/tiktok/A1.mp4",
        "alpha/tiktok/A1.txt",
        "no",
        "",
    )


def test_a_per_campaign_cap_leaves_the_rest_of_the_day_to_the_next_campaign(rig: Rig) -> None:
    alpha = rig.campaign("alpha", *((f"A{i}", "tiktok") for i in range(1, 6)))
    beta = rig.campaign("beta", ("B1", "tiktok"), ("B2", "tiktok"), ("B3", "tiktok"))
    sched = schedule(priority=[{"slug": "alpha", "max_per_day": 2}, {"slug": "beta"}])
    assert [(r["date"], r["campaign"], r["id"]) for r in rig.rows(sched, alpha, beta)] == [
        ("2026-10-13", "alpha", "A1"),
        ("2026-10-13", "alpha", "A2"),
        ("2026-10-13", "beta", "B1"),
        ("2026-10-13", "beta", "B2"),
        ("2026-10-14", "alpha", "A3"),
        ("2026-10-14", "alpha", "A4"),
        ("2026-10-14", "beta", "B3"),
        ("2026-10-15", "alpha", "A5"),
    ]


def test_max_per_platform_per_day_uses_only_the_first_times(rig: Rig) -> None:
    alpha = rig.campaign("alpha", ("A1", "tiktok"), ("A2", "tiktok"), ("A3", "tiktok"))
    rows = rig.rows(schedule(max_per_platform_per_day=2), alpha)
    assert [(r["date"], r["time_et"]) for r in rows] == [
        ("2026-10-13", "12:30 PM"),
        ("2026-10-13", "3:00 PM"),
        ("2026-10-14", "12:30 PM"),
    ]


def test_a_single_digit_posting_time_that_has_passed_is_never_given(rig: Rig) -> None:
    alpha = rig.campaign("alpha", ("A1", "tiktok"), ("A2", "tiktok"))
    sched = schedule(posting_times=["9:00", "12:30"])
    rows = rig.rows(sched, alpha, now=datetime(2026, 10, 13, 10, 15))
    assert [(r["date"], r["time_et"]) for r in rows] == [
        ("2026-10-13", "12:30 PM"),
        ("2026-10-14", "9:00 AM"),
    ]


def test_a_slot_whose_time_has_passed_today_is_never_given(rig: Rig) -> None:
    alpha = rig.campaign("alpha", ("A1", "tiktok"), ("A2", "tiktok"), ("A3", "tiktok"))
    rows = rig.rows(schedule(), alpha, now=AFTERNOON)
    assert [(r["date"], r["time_et"]) for r in rows] == [
        ("2026-10-13", "5:30 PM"),
        ("2026-10-13", "8:00 PM"),
        ("2026-10-14", "12:30 PM"),
    ]
    late = datetime(2026, 10, 13, 23, 59)
    rows = rig.rows(schedule(), alpha, now=late)
    assert rows[0]["date"] == "2026-10-14" and rows[0]["time_et"] == "12:30 PM"


def test_platforms_are_independent_and_a_campaign_without_one_never_takes_its_slots(
    rig: Rig,
) -> None:
    alpha = rig.campaign("alpha", ("A1", "tiktok"), ("A2", "instagram"))
    beta = rig.campaign(
        "beta", ("B1", "instagram"), ("B2", "youtube"), platforms=["youtube", "instagram"]
    )
    rows = rig.rows(schedule(), alpha, beta)
    assert slots(rows) == [
        ("2026-10-13", "12:30 PM", "tiktok", "alpha", "A1"),
        ("2026-10-13", "12:30 PM", "instagram", "alpha", "A2"),
        ("2026-10-13", "12:30 PM", "youtube", "beta", "B2"),
        ("2026-10-13", "3:00 PM", "instagram", "beta", "B1"),
    ]


def test_unlisted_campaigns_come_last_alphabetically_and_a_missing_one_is_an_error(
    rig: Rig,
) -> None:
    sched = schedule(priority=[{"slug": "beta", "max_per_day": 1}])
    assert campaign_order(sched, ["gamma", "alpha", "beta"]) == [
        ("beta", 1),
        ("alpha", None),
        ("gamma", None),
    ]
    with pytest.raises(ConfigError, match="priority names 'beta', but there is no"):
        campaign_order(sched, ["alpha"])


def test_posted_and_rejected_moments_get_no_slot_and_posted_rows_are_prefixed(rig: Rig) -> None:
    used = UsedLog(
        entries=[entry("alpha", "A1", "tiktok", posted="2026-10-12T20:00:00+00:00")],
        rejected=[
            RejectedMoment(
                id="A2", source=None, start=1.0, end=30.0, rejected_at="2026-10-12T00:00:00+00:00"
            )
        ],
    )
    alpha = rig.campaign("alpha", ("A1", "tiktok"), ("A2", "tiktok"), ("A3", "tiktok"), used=used)
    rows = rig.rows(schedule(), alpha)
    assert [(r["id"], r["posted"], r["campaign"]) for r in rows] == [
        ("A1", "yes", "alpha"),
        ("A3", "no", "alpha"),
    ]
    assert (
        rows[0]["file"] == "alpha/tiktok/A1.mp4" and rows[0]["text_file"] == "alpha/tiktok/A1.txt"
    )
    assert rows[0]["date"] and rows[0]["time_et"] == ""
    assert rows[1]["source"] == ""  # no render recorded for A3
    rendered = UsedLog(entries=[entry("alpha", "A3", "tiktok")])
    alpha = rig.campaign("alpha", ("A3", "tiktok"), used=rendered)
    assert rig.rows(schedule(), alpha)[0]["source"] == "alpha-ep.mp4"


# --- the file, migration and views ---------------------------------------------------------------


def test_the_first_build_backs_up_and_carries_over_each_campaigns_results(rig: Rig) -> None:
    alpha = rig.campaign("alpha", ("A1", "tiktok"), ("A2", "tiktok"))
    beta = rig.campaign("beta", ("B1", "tiktok"))
    old_alpha = (
        "date,time_et,platform,source,id,file,text_file,posted,views_48h,likes,comments,shares,"
        "pct_us_ca_uk,avg_watch_s,pct_completed,notes,mood\n"
        "2026-10-10,12:41 PM,tiktok,ep.mp4,A1,tiktok/A1.mp4,tiktok/A1.txt,no,"
        "1200,,,,63,,,pinned,ok\n"
    )
    (alpha.out_dir / PLAN_FILE).write_text(old_alpha, encoding="utf-8")
    old_beta = "id,notes\nB1,hold\n"
    (beta.out_dir / PLAN_FILE).write_text(old_beta, encoding="utf-8")

    path, backups = write_combined_plan(rig.root, schedule(), [alpha, beta], now=MORNING)
    assert backups == [alpha.out_dir / PRE_BACKUP, beta.out_dir / PRE_BACKUP]
    assert backups[0].read_text(encoding="utf-8") == old_alpha
    assert backups[1].read_text(encoding="utf-8") == old_beta
    rows = {(r["campaign"], r["id"]): r for r in read(path)}
    a1 = rows[("alpha", "A1")]
    assert (a1["views_48h"], a1["pct_us_ca_uk"], a1["notes"], a1["mood"]) == (
        "1200",
        "63",
        "pinned",
        "ok",
    )
    assert (a1["date"], a1["time_et"], a1["file"]) == (
        "2026-10-13",
        "12:30 PM",
        "alpha/tiktok/A1.mp4",
    )
    assert rows[("beta", "B1")]["notes"] == "hold" and rows[("beta", "B1")]["mood"] == ""
    assert list(read(path)[0])[-1] == "mood"  # the extra column is kept, last

    # the per-campaign files are views now; editing one changes nothing, and the backup stays
    view = (alpha.out_dir / PLAN_FILE).read_text(encoding="utf-8")
    assert view.startswith(VIEW_HEADER + "\n")
    (alpha.out_dir / PLAN_FILE).write_text(view.replace("1200", "9999"), encoding="utf-8")
    backups[0].write_text("keep me", encoding="utf-8")
    _, again = write_combined_plan(rig.root, schedule(), [alpha, beta], now=MORNING)
    assert again == [] and backups[0].read_text(encoding="utf-8") == "keep me"
    assert {(r["campaign"], r["id"]): r["views_48h"] for r in read(path)}[("alpha", "A1")] == "1200"


def test_results_survive_a_rebuild_and_unposted_slots_are_recomputed(rig: Rig) -> None:
    alpha = rig.campaign("alpha", ("A1", "tiktok"), ("A2", "tiktok"))
    path = rig.write(schedule(), alpha)
    rows = read(path)
    rows[0].update(views_48h="500", notes="first")
    rows[1].update(notes="second")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
    posted = UsedLog(entries=[entry("alpha", "A1", "tiktok", posted="2026-10-13T12:35:00+00:00")])
    alpha = rig.campaign("alpha", ("A1", "tiktok"), ("A2", "tiktok"), used=posted)

    rows = read(rig.write(schedule(), alpha, now=AFTERNOON))
    a1, a2 = rows
    assert (a1["posted"], a1["views_48h"], a1["notes"], a1["time_et"]) == (
        "yes",
        "500",
        "first",
        "12:30 PM",
    )
    assert (a2["posted"], a2["notes"], a2["date"], a2["time_et"]) == (
        "no",
        "second",
        "2026-10-13",
        "5:30 PM",
    )


def test_each_campaign_gets_a_read_only_view_of_its_own_rows(rig: Rig) -> None:
    alpha = rig.campaign("alpha", ("A1", "tiktok"))
    beta = rig.campaign("beta", ("B1", "youtube"))
    rig.write(schedule(), alpha, beta)
    view = alpha.out_dir / PLAN_FILE
    text = view.read_text(encoding="utf-8")
    assert text.startswith(VIEW_HEADER + "\n") and "\r" not in text
    rows = read(view)
    assert [(r["id"], r["file"], r["text_file"]) for r in rows] == [
        ("A1", "tiktok/A1.mp4", "tiktok/A1.txt")
    ]
    assert "campaign" not in rows[0] and read_posting_plan(view)["A1"]["platform"] == "tiktok"
    assert [r["id"] for r in read(beta.out_dir / PLAN_FILE)] == ["B1"]


# --- configuration -------------------------------------------------------------------------------


def test_schedule_yaml_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bad posting time"):
        schedule(posting_times=["25:00"])
    with pytest.raises(ValueError, match="is more than the 4 posting times"):
        schedule(max_per_platform_per_day=5)
    with pytest.raises(ValueError, match="lists a campaign twice"):
        schedule(priority=[{"slug": "alpha"}, {"slug": "alpha"}])
    with pytest.raises(ValueError):
        schedule(priority=[{"slug": "Bad Name"}])
    assert schedule().per_day == 4 and schedule(max_per_platform_per_day=3).per_day == 3
    # single-digit hours are padded and the list is put in clock order (review finding)
    assert schedule(posting_times=["18:00", "9:00"]).posting_times == ["09:00", "18:00"]
    assert load_schedule(tmp_path) is None
    (tmp_path / "campaigns").mkdir()
    (tmp_path / "campaigns" / "schedule.yaml").write_text("posting_times: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="campaigns/schedule.yaml"):
        load_schedule(tmp_path)


def test_the_example_schedule_loads_and_campaign_folders_are_listed(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "campaigns" / "_example" / "schedule.yaml"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    sched = ScheduleConfig.model_validate(data)
    assert sched.per_day == 4 and [p.slug for p in sched.priority] == ["example-a", "example-b"]
    base = tmp_path / "campaigns"
    for name in ("_example", "zeta", "alpha", "Bad", "empty"):
        (base / name).mkdir(parents=True)
        if name != "empty":
            (base / name / "campaign.yaml").write_text("name: x\n", encoding="utf-8")
    assert list_campaigns(tmp_path) == ["alpha", "zeta"]


# --- the commands --------------------------------------------------------------------------------


def real_campaign(project: Path, slug: str, *clips: tuple[str, str]) -> Path:
    folder = write_campaign(project, slug)
    write_moments(folder, [moment(id=i, platform=p) for i, p in clips])
    out = project / "runs" / "_campaigns" / slug
    log = UsedLog()
    for moment_id, platform in clips:
        (out / platform).mkdir(parents=True, exist_ok=True)
        (out / platform / f"{moment_id}.mp4").write_bytes(b"v")
        log = record_render(log, entry(slug, moment_id, platform))
    save_used(folder, log)
    return folder


def write_schedule(project: Path, **changes: Any) -> None:
    data: dict[str, Any] = {"posting_times": TIMES, "priority": [{"slug": "alpha"}]}
    data.update(changes)
    (project / "campaigns" / "schedule.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_plan_needs_a_schedule_then_writes_the_combined_file(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_campaign(project, "alpha", ("A1", "tiktok"), ("A2", "youtube"))
    real_campaign(project, "beta", ("B1", "tiktok"))
    assert cli.main(["plan"], root=project) == 2
    assert "copy campaigns/_example/schedule.yaml" in capsys.readouterr().err
    write_schedule(project)
    assert cli.main(["plan"], root=project) == 0
    out = capsys.readouterr().out
    path = project / COMBINED_PLAN
    assert f"posting plan: {path} (tiktok 2, instagram 0, youtube 1 scheduled)" in out
    assert [(r["campaign"], r["id"]) for r in read(path)] == [
        ("alpha", "A1"),
        ("alpha", "A2"),
        ("beta", "B1"),
    ]
    assert (
        (project / "runs" / "_campaigns" / "beta" / PLAN_FILE)
        .read_text(encoding="utf-8")
        .startswith(VIEW_HEADER)
    )


def test_campaign_posted_and_fix_platform_rebuild_the_combined_plan(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_campaign(project, "alpha", ("A1", "tiktok"), ("A2", "tiktok"))
    write_schedule(project)
    assert cli.main(["plan"], root=project) == 0
    path = project / COMBINED_PLAN
    args = ["campaign-posted", "--campaign", "alpha", "--id", "A1", "--platform", "tiktok"]
    assert cli.main(args, root=project) == 0
    out = capsys.readouterr().out
    assert "A1 tiktok: marked as posted" in out and f"posting plan: {path}" in out
    rows = {r["id"]: r for r in read(path)}
    assert rows["A1"]["posted"] == "yes" and rows["A2"]["posted"] == "no"
    assert rows["A2"]["date"] and rows["A2"]["time_et"] == "12:30 PM"  # moved into the first slot
    args = ["campaign-posted", "--campaign", "alpha", "--id", "A1", "--fix-platform", "youtube"]
    assert cli.main(args, root=project) == 0
    assert {r["id"]: r["platform"] for r in read(path)}["A1"] == "youtube"
    assert (
        read_posting_plan(project / "runs" / "_campaigns" / "alpha" / PLAN_FILE)["A1"]["platform"]
        == "youtube"
    )


def test_a_campaign_that_does_not_load_is_left_out_with_a_warning(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_campaign(project, "alpha", ("A1", "tiktok"))
    broken = real_campaign(project, "beta", ("B1", "tiktok"))
    (broken / "campaign.yaml").write_text("name: beta\nnonsense: 1\n", encoding="utf-8")
    write_schedule(project)
    assert cli.main(["plan"], root=project) == 0
    out = capsys.readouterr().out
    assert "warning: campaign 'beta' left out of the combined plan" in out
    assert [r["campaign"] for r in read(project / COMBINED_PLAN)] == ["alpha"]


def test_a_priority_slug_without_a_folder_fails_the_plan_command(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_campaign(project, "alpha", ("A1", "tiktok"))
    write_schedule(project, priority=[{"slug": "ghost"}])
    assert cli.main(["plan"], root=project) == 2
    assert "priority names 'ghost'" in capsys.readouterr().err
