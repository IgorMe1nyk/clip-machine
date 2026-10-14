"""posting_plan.csv (Brief 11.2): slots, posted rows, and Igor's columns that are never
overwritten. Invented words only."""

import csv
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from campaign_helpers import base_campaign, moment
from clipmachine.campaign.models import CampaignConfig, Moment, RejectedMoment, UsedEntry, UsedLog
from clipmachine.campaign.plan import (
    PLAN_COLUMNS,
    PLAN_FILE,
    RESULT_COLUMNS,
    clock_12h,
    plan_rows,
    read_plan,
    read_posting_plan,
    write_posting_plan,
)
from clipmachine.config import ConfigError

TODAY = date(2026, 10, 13)


def cfg(**changes: Any) -> CampaignConfig:
    data = base_campaign()
    data.update(changes)
    return CampaignConfig.model_validate(data)


def mom(moment_id: str, platform: str) -> Moment:
    return Moment.model_validate(moment(id=moment_id, platform=platform))


def entry(
    moment_id: str, platform: str, *, posted: str | None = None, output: str = ""
) -> UsedEntry:
    return UsedEntry.model_validate(
        {
            "id": moment_id,
            "platform": platform,
            "start": 1.0,
            "end": 30.0,
            "hook": "X",
            "closer": "c",
            "rendered_at": "2026-10-12T10:00:00+00:00",
            "output": output or f"runs/run/campaign/demo/{platform}/{moment_id}.mp4",
            "source_sha256": "a" * 64,
            "posted_at": posted,
        }
    )


class Rig:
    """A campaign output folder with rendered clips for the given moments."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.out = root / "runs" / "run" / "campaign" / "demo"
        self.out.mkdir(parents=True)

    def render(self, *moments: Moment) -> list[Moment]:
        for m in moments:
            folder = self.out / m.platform
            folder.mkdir(exist_ok=True)
            (folder / f"{m.id}.mp4").write_bytes(b"v")
            (folder / f"{m.id}.txt").write_text("text\n", encoding="utf-8")
        return list(moments)

    def rows(
        self,
        moments: list[Moment],
        used: UsedLog,
        existing: dict[str, dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        return plan_rows(
            cfg(),
            moments,
            used,
            root=self.root,
            out_dir=self.out,
            start=TODAY,
            existing=existing,
        )

    def write(self, moments: list[Moment], used: UsedLog) -> list[dict[str, str]]:
        path = write_posting_plan(self.out, cfg(), moments, used, root=self.root, today=TODAY)
        assert path == self.out / PLAN_FILE
        text = path.read_text(encoding="utf-8")
        assert "\r" not in text and text.endswith("\n")
        return list(csv.DictReader(text.splitlines()))


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def slots(rows: list[dict[str, str]]) -> list[tuple[str, str, str, str]]:
    return [(r["date"], r["time_et"], r["platform"], r["id"]) for r in rows]


# --- slots ---------------------------------------------------------------------------------------


def test_each_platform_fills_the_posting_times_in_assignment_order_from_the_start_date(
    rig: Rig,
) -> None:
    moments = rig.render(
        mom("A01", "tiktok"),
        mom("A02", "instagram"),
        mom("A03", "tiktok"),
        mom("A04", "tiktok"),
        mom("A05", "tiktok"),
    )
    rows = rig.rows(moments, UsedLog())
    assert slots(rows) == [
        ("2026-10-13", "12:30 PM", "tiktok", "A01"),
        ("2026-10-13", "12:30 PM", "instagram", "A02"),
        ("2026-10-13", "3:30 PM", "tiktok", "A03"),
        ("2026-10-13", "6:30 PM", "tiktok", "A04"),
        ("2026-10-14", "12:30 PM", "tiktok", "A05"),
    ]
    first = rows[0]
    assert (first["file"], first["text_file"], first["posted"]) == (
        "tiktok/A01.mp4",
        "tiktok/A01.txt",
        "no",
    )
    assert all(first[c] == "" for c in RESULT_COLUMNS)
    assert list(first) == list(PLAN_COLUMNS)


def test_the_campaigns_times_and_platform_order_are_used(rig: Rig) -> None:
    moments = rig.render(mom("A01", "tiktok"), mom("A02", "youtube"))
    rows = plan_rows(
        cfg(platforms=["youtube", "tiktok"], posting_times=["09:05", "21:00"]),
        moments,
        UsedLog(),
        root=rig.root,
        out_dir=rig.out,
        start=TODAY,
    )
    assert slots(rows) == [
        ("2026-10-13", "9:05 AM", "youtube", "A02"),
        ("2026-10-13", "9:05 AM", "tiktok", "A01"),
    ]


def test_clock_12h() -> None:
    assert [clock_12h(t) for t in ("00:05", "09:30", "12:00", "12:30", "15:30", "23:59")] == [
        "12:05 AM",
        "9:30 AM",
        "12:00 PM",
        "12:30 PM",
        "3:30 PM",
        "11:59 PM",
    ]


def test_unrendered_moments_have_no_row(rig: Rig) -> None:
    rendered = rig.render(mom("A02", "tiktok"))
    rows = rig.rows([mom("A01", "tiktok"), *rendered, mom("A03", "youtube")], UsedLog())
    assert [r["id"] for r in rows] == ["A02"]
    assert rows[0]["time_et"] == "12:30 PM"  # the first slot, not the second


# --- posted rows ---------------------------------------------------------------------------------


def test_posted_clips_keep_a_row_with_their_date_and_real_platform_and_no_slot(rig: Rig) -> None:
    moments = rig.render(mom("A01", "tiktok"), mom("A02", "instagram"), mom("A03", "youtube"))
    (rig.out / "A02_tiktok.mp4").write_bytes(b"old")  # the flat layout the first posts used
    (rig.out / "A02_tiktok.post.txt").write_text("old\n", encoding="utf-8")
    used = UsedLog(
        entries=[
            entry(
                "A02",
                "instagram",
                posted="2026-10-13T03:00:00+00:00",
                output="runs/run/campaign/demo/A02_tiktok.mp4",
            ),
            entry("A03", "youtube"),
        ]
    )
    rows = rig.rows(moments, used)
    posted, *scheduled = rows
    assert posted["id"] == "A02" and posted["posted"] == "yes"
    assert posted["platform"] == "instagram"  # from used.json, as --fix-platform left it
    assert posted["date"] and posted["time_et"] == ""  # the local date of posted_at
    assert posted["file"] == "A02_tiktok.mp4" and posted["text_file"] == "A02_tiktok.post.txt"
    assert slots(scheduled) == [
        ("2026-10-13", "12:30 PM", "tiktok", "A01"),
        ("2026-10-13", "12:30 PM", "youtube", "A03"),
    ]


def test_a_posted_moment_that_left_moments_yaml_still_has_its_row(rig: Rig) -> None:
    used = UsedLog(entries=[entry("Z9", "tiktok", posted="2026-10-13T03:00:00+00:00")])
    rows = rig.rows(rig.render(mom("A01", "tiktok")), used)
    assert [(r["id"], r["posted"]) for r in rows] == [("Z9", "yes"), ("A01", "no")]
    assert rows[0]["file"] == "tiktok/Z9.mp4" and rows[0]["text_file"] == ""


# --- Igor's columns ------------------------------------------------------------------------------


def test_filled_values_survive_a_regeneration_and_unposted_slots_are_recomputed(rig: Rig) -> None:
    moments = rig.render(mom("A01", "tiktok"), mom("A02", "tiktok"))
    first = rig.write(moments, UsedLog())
    assert [r["time_et"] for r in first] == ["12:30 PM", "3:30 PM"]

    # Igor posts A01, fills its results in Excel (BOM, CRLF), and corrects the time by hand
    path = rig.out / PLAN_FILE
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    rows[0].update(
        time_et="12:41 PM", views_48h="1200", pct_us_ca_uk="63", notes="pinned a comment"
    )
    rows[1].update(notes="hold this one")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*PLAN_COLUMNS, "mood"], lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "mood": "ok"})
    used = UsedLog(entries=[entry("A01", "tiktok", posted="2026-10-13T12:00:00+00:00")])

    second = rig.write(moments, used)

    a01, a02 = second
    assert (a01["posted"], a01["time_et"], a01["views_48h"], a01["pct_us_ca_uk"]) == (
        "yes",
        "12:41 PM",
        "1200",
        "63",
    )
    assert a01["notes"] == "pinned a comment" and a01["mood"] == "ok"
    assert a01["date"] and a01["platform"] == "tiktok"
    assert a01["date"] != "2026-10-14"  # the posted date, not the planned one
    assert (a02["date"], a02["time_et"], a02["posted"]) == ("2026-10-14", "12:30 PM", "no")
    assert a02["notes"] == "hold this one" and a02["mood"] == "ok"  # kept; the slot moved up
    assert list(second[0]) == [*PLAN_COLUMNS, "mood"]


def test_a_posted_rows_platform_follows_used_json_after_a_fix(rig: Rig) -> None:
    moments = rig.render(mom("A01", "instagram"))
    existing = {
        "A01": {c: "" for c in PLAN_COLUMNS}
        | {"id": "A01", "platform": "tiktok", "posted": "yes", "likes": "7"}
    }
    used = UsedLog(entries=[entry("A01", "instagram", posted="2026-10-13T20:00:00+00:00")])
    (row,) = rig.rows(moments, used, existing)
    assert row["platform"] == "instagram" and row["likes"] == "7"


def test_read_posting_plan_handles_a_missing_file_and_refuses_one_without_ids(
    tmp_path: Path,
) -> None:
    assert read_posting_plan(tmp_path / PLAN_FILE) == {}
    (tmp_path / PLAN_FILE).write_text("date,platform\n2026-10-13,tiktok\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="no 'id' column"):
        read_posting_plan(tmp_path / PLAN_FILE)
    (tmp_path / PLAN_FILE).write_text("﻿id,notes\r\nA01, hi \r\n,blank\r\n", encoding="utf-8")
    assert read_posting_plan(tmp_path / PLAN_FILE) == {"A01": {"id": "A01", "notes": "hi"}}


# --- Brief 12.2: comment lines, keyed reads, source and rejected rows ---------------------------


def test_leading_comment_lines_are_skipped_when_a_plan_is_read(tmp_path: Path) -> None:
    path = tmp_path / PLAN_FILE
    path.write_text("# read-only - edit elsewhere\nid,notes\nA01,kept\n", encoding="utf-8")
    assert read_posting_plan(path) == {"A01": {"id": "A01", "notes": "kept"}}


def test_read_plan_keys_rows_by_several_columns_and_names_the_missing_one(tmp_path: Path) -> None:
    path = tmp_path / PLAN_FILE
    path.write_text("campaign,id,likes\ndemo,A01,3\n,A02,4\nother,A01,5\n", encoding="utf-8")
    rows = read_plan(path, key=("campaign", "id"))
    assert set(rows) == {("demo", "A01"), ("other", "A01")}  # a row with an empty key is skipped
    assert rows[("other", "A01")]["likes"] == "5"
    (tmp_path / "x.csv").write_text("id\nA01\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="no 'campaign' column"):
        read_plan(tmp_path / "x.csv", key=("campaign", "id"))


def test_a_moment_missing_from_sources_takes_its_source_name_from_used_json(rig: Rig) -> None:
    moments = rig.render(mom("A01", "tiktok"), mom("A02", "tiktok"))
    rendered = entry("A01", "tiktok").model_copy(update={"source_name": "ep1.mp4"})
    rows = plan_rows(
        cfg(),
        moments,
        UsedLog(entries=[rendered]),
        root=rig.root,
        out_dir=rig.out,
        start=TODAY,
        sources={"A02": "ep2.mp4"},
    )
    assert [(r["id"], r["source"]) for r in rows] == [("A01", "ep1.mp4"), ("A02", "ep2.mp4")]


def test_a_rejected_moment_gets_no_slot_even_when_its_clip_exists(rig: Rig) -> None:
    moments = rig.render(mom("A01", "tiktok"), mom("A02", "tiktok"))
    rejected = RejectedMoment(
        id="A01", source=None, start=1.0, end=30.0, rejected_at="2026-10-13T00:00:00+00:00"
    )
    rows = rig.rows(moments, UsedLog(rejected=[rejected]))
    assert slots(rows) == [("2026-10-13", "12:30 PM", "tiktok", "A02")]
