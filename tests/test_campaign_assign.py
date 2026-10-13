"""Platform assignment and its lock file (Brief 11.2). Invented words only."""

import json
from pathlib import Path
from typing import Any

import pytest

from campaign_helpers import base_campaign, moment
from clipmachine.campaign.assign import (
    ASSIGNMENTS_FILE,
    assign_platforms,
    assigned_platform,
    count_by_platform,
    load_assignments,
)
from clipmachine.campaign.models import CampaignConfig, MomentSpec, UsedEntry, UsedLog
from clipmachine.config import ConfigError


def cfg(**changes: Any) -> CampaignConfig:
    data = base_campaign()
    data.update(changes)
    return CampaignConfig.model_validate(data)


def spec(**fields: Any) -> MomentSpec:
    entry = moment(**fields)
    if "platform" not in fields:
        del entry["platform"]  # the helper's default is an explicit tiktok
    return MomentSpec.model_validate(entry)


def posted(moment_id: str, platform: str) -> UsedEntry:
    return UsedEntry.model_validate(
        {
            "id": moment_id,
            "platform": platform,
            "start": 1.0,
            "end": 30.0,
            "hook": "X",
            "closer": "c",
            "rendered_at": "2026-10-12T10:00:00+00:00",
            "output": f"runs/run/campaign/demo/{platform}/{moment_id}.mp4",
            "source_sha256": "a" * 64,
            "posted_at": "2026-10-13T10:00:00+00:00",
        }
    )


def ids(specs: list[str]) -> list[MomentSpec]:
    return [spec(id=i) for i in specs]


def test_auto_goes_round_robin_in_file_order(tmp_path: Path) -> None:
    moments, lock, changed = assign_platforms(
        tmp_path, cfg(), ids(["A01", "A02", "A03", "A04", "B05"]), UsedLog(), now="t"
    )
    assert [m.platform for m in moments] == [
        "tiktok",
        "instagram",
        "youtube",
        "tiktok",
        "instagram",
    ]
    assert [m.clip_name for m in moments][:2] == ["A01_tiktok", "A02_instagram"]
    assert changed and lock.assigned == {
        "A01": "tiktok",
        "A02": "instagram",
        "A03": "youtube",
        "A04": "tiktok",
        "B05": "instagram",
    }
    assert lock.platforms == ["tiktok", "instagram", "youtube"] and lock.written_at == "t"


def test_the_campaigns_platform_list_and_order_are_followed(tmp_path: Path) -> None:
    moments, _, _ = assign_platforms(
        tmp_path, cfg(platforms=["youtube", "tiktok"]), ids(["A01", "A02", "A03"]), UsedLog()
    )
    assert [m.platform for m in moments] == ["youtube", "tiktok", "youtube"]


def test_an_explicit_platform_overrides_auto_and_counts_for_the_balance(tmp_path: Path) -> None:
    specs = [spec(id="A01", platform="youtube"), spec(id="A02"), spec(id="A03"), spec(id="A04")]
    moments, _, _ = assign_platforms(tmp_path, cfg(), specs, UsedLog())
    # youtube already has one, so the auto moments fill tiktok and instagram first
    assert [m.platform for m in moments] == ["youtube", "tiktok", "instagram", "tiktok"]


def test_a_posted_moment_keeps_the_platform_it_was_posted_on(tmp_path: Path) -> None:
    used = UsedLog(entries=[posted("A02", "youtube")])
    specs = [spec(id="A01"), spec(id="A02", platform="tiktok"), spec(id="A03")]
    moments, lock, _ = assign_platforms(tmp_path, cfg(), specs, used)
    assert [m.platform for m in moments] == ["tiktok", "youtube", "instagram"]
    assert lock.assigned["A02"] == "youtube"


def test_the_assignment_is_stable_across_runs_and_written_once(tmp_path: Path) -> None:
    moments, lock, changed = assign_platforms(
        tmp_path, cfg(), ids(["A01", "A02", "A03"]), UsedLog()
    )
    assert changed
    (tmp_path / ASSIGNMENTS_FILE).write_text(lock.model_dump_json(), encoding="utf-8")

    again, lock2, changed2 = assign_platforms(
        tmp_path, cfg(), ids(["A03", "A01", "A02"]), UsedLog(), now="later"
    )
    assert not changed2 and lock2 == lock  # reordering moments.yaml moves nothing
    assert {m.id: m.platform for m in again} == {m.id: m.platform for m in moments}


def test_a_new_moment_goes_to_the_least_loaded_platform(tmp_path: Path) -> None:
    _, lock, _ = assign_platforms(tmp_path, cfg(), ids(["A01", "A02", "A03", "A04"]), UsedLog())
    (tmp_path / ASSIGNMENTS_FILE).write_text(lock.model_dump_json(), encoding="utf-8")

    moments, lock2, changed = assign_platforms(
        tmp_path, cfg(), ids(["A01", "A02", "A03", "A04", "A05", "A06"]), UsedLog(), now="t2"
    )
    assert changed and [m.platform for m in moments][4:] == ["instagram", "youtube"]
    assert lock2.written_at == "t2" and lock2.assigned["A01"] == "tiktok"


def test_an_id_that_left_moments_yaml_stays_in_the_lock(tmp_path: Path) -> None:
    _, lock, _ = assign_platforms(tmp_path, cfg(), ids(["A01", "A02"]), UsedLog())
    (tmp_path / ASSIGNMENTS_FILE).write_text(lock.model_dump_json(), encoding="utf-8")
    _, lock2, changed = assign_platforms(tmp_path, cfg(), ids(["A01"]), UsedLog())
    assert not changed and lock2.assigned == {"A01": "tiktok", "A02": "instagram"}


def test_deleting_the_lock_assigns_again(tmp_path: Path) -> None:
    specs = ids(["A01", "A02"])
    _, lock, _ = assign_platforms(tmp_path, cfg(platforms=["youtube"]), specs, UsedLog())
    (tmp_path / ASSIGNMENTS_FILE).write_text(lock.model_dump_json(), encoding="utf-8")
    moments, _, _ = assign_platforms(tmp_path, cfg(), specs, UsedLog())
    assert [m.platform for m in moments] == ["youtube", "youtube"]  # locked
    (tmp_path / ASSIGNMENTS_FILE).unlink()
    moments, _, _ = assign_platforms(tmp_path, cfg(), specs, UsedLog())
    assert [m.platform for m in moments] == ["tiktok", "instagram"]


def test_a_changed_platform_list_rewrites_the_lock_but_moves_nothing(tmp_path: Path) -> None:
    _, lock, _ = assign_platforms(tmp_path, cfg(), ids(["A01", "A02"]), UsedLog())
    (tmp_path / ASSIGNMENTS_FILE).write_text(lock.model_dump_json(), encoding="utf-8")
    moments, lock2, changed = assign_platforms(
        tmp_path, cfg(platforms=["youtube"]), ids(["A01", "A02", "A03"]), UsedLog()
    )
    assert changed and lock2.platforms == ["youtube"]
    assert [m.platform for m in moments] == ["tiktok", "instagram", "youtube"]


@pytest.mark.parametrize("content", ["{", '{"schema_version": 1}', "[]"])
def test_an_unreadable_lock_is_an_error_and_is_left_alone(tmp_path: Path, content: str) -> None:
    (tmp_path / ASSIGNMENTS_FILE).write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match="assignments.json in .* cannot be read .*delete it"):
        load_assignments(tmp_path)
    with pytest.raises(ConfigError):
        assign_platforms(tmp_path, cfg(), ids(["A01"]), UsedLog())
    assert (tmp_path / ASSIGNMENTS_FILE).read_text(encoding="utf-8") == content


def test_assigned_platform_reads_the_lock_only(tmp_path: Path) -> None:
    assert assigned_platform(tmp_path, "A01") is None
    _, lock, _ = assign_platforms(tmp_path, cfg(), ids(["A01", "A02"]), UsedLog())
    (tmp_path / ASSIGNMENTS_FILE).write_text(lock.model_dump_json(), encoding="utf-8")
    assert assigned_platform(tmp_path, "A02") == "instagram"
    assert assigned_platform(tmp_path, "Z9") is None


def test_the_lock_file_holds_ids_and_platforms_only(tmp_path: Path) -> None:
    _, lock, _ = assign_platforms(
        tmp_path, cfg(), [spec(id="A01", caption="secret words")], UsedLog()
    )
    raw = lock.model_dump_json()
    assert "secret" not in raw and "INVENTED" not in raw
    assert set(json.loads(raw)) == {"schema_version", "platforms", "assigned", "written_at"}


def test_count_by_platform_keeps_the_list_order_and_zeroes() -> None:
    moments, _, _ = assign_platforms(Path("."), cfg(), ids(["A01", "A02"]), UsedLog())
    assert count_by_platform(moments, ["youtube", "tiktok", "instagram"]) == {
        "youtube": 0,
        "tiktok": 1,
        "instagram": 1,
    }


def test_campaign_yaml_validates_the_new_lists() -> None:
    assert cfg().posting_times == ["12:30", "15:30", "18:30"]
    assert cfg(posting_times=["9:00", "23:59"]).posting_times == ["9:00", "23:59"]
    for bad in ([], ["tiktok", "tiktok"], ["myspace"]):
        with pytest.raises(ValueError, match="platform"):
            cfg(platforms=bad)
    for bad_times in ([], ["12:30", "12:30"], ["25:00"], ["noon"], ["12:3"]):
        with pytest.raises(ValueError, match="posting time"):
            cfg(posting_times=bad_times)
