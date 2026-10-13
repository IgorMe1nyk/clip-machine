"""The used-moments log, closer rotation and the campaign-posted command (Brief 11)."""

import json
import random
from pathlib import Path
from typing import Any

import pytest

from campaign_helpers import moment, write_campaign, write_moments
from clipmachine import cli
from clipmachine.campaign.assign import ASSIGNMENTS_FILE
from clipmachine.campaign.models import Assignments, CloserState, UsedEntry, UsedLog
from clipmachine.campaign.used import (
    REUSE_OVERLAP,
    USED_FILE,
    latest_posted,
    load_used,
    mark_posted,
    overlapping_posted,
    posted_entry,
    record_render,
    relabel_posted,
    save_used,
    take_closer,
)
from clipmachine.config import ConfigError

SHA = "a" * 64
SECRET_HOOK = "AN INVENTED HOOK LINE"


def entry(
    moment_id: str = "A01",
    platform: str = "tiktok",
    start: float = 100.0,
    end: float = 140.0,
    *,
    posted: str | None = None,
    sha: str = SHA,
    closer: str = "closer one",
) -> UsedEntry:
    return UsedEntry.model_validate(
        {
            "id": moment_id,
            "platform": platform,
            "start": start,
            "end": end,
            "hook": SECRET_HOOK,
            "closer": closer,
            "rendered_at": "2026-10-12T10:00:00+00:00",
            "output": "runs/run/campaign/demo/A01_tiktok.mp4",
            "source_sha256": sha,
            "posted_at": posted,
        }
    )


# --- closer rotation -----------------------------------------------------------------------------


def state(next_index: int, last: str | None) -> dict[str, CloserState]:
    return {"tiktok": CloserState(next=next_index, last=last)}


def draw(
    log: UsedLog, closers: list[str], times: int, platform: str = "tiktok", first: int = 0
) -> tuple[list[str], UsedLog]:
    out = []
    for _ in range(times):
        closer, log = take_closer(log, closers, platform, first=first)
        out.append(closer)
    return out, log


def test_closers_go_round_robin() -> None:
    drawn, log = draw(UsedLog(), ["a", "b", "c"], 7)
    assert drawn == ["a", "b", "c", "a", "b", "c", "a"]
    assert log.closers["tiktok"] == CloserState(next=1, last="a")
    assert (log.next_closer, log.last_closer) == (0, None)  # the Brief 11 fields are not touched


def test_the_same_closer_is_never_used_twice_in_a_row() -> None:
    # the position points at the closer that was just used (as after a reorder)
    log = UsedLog(closers=state(0, "a"))
    closer, after = take_closer(log, ["a", "b", "c"], "tiktok")
    assert closer == "b" and after.closers["tiktok"] == CloserState(next=2, last="b")
    wrapped = UsedLog(closers=state(5, "c"))  # 5 % 3 = 2 -> "c" again
    closer, after = take_closer(wrapped, ["a", "b", "c"], "tiktok")
    assert closer == "a" and after.closers["tiktok"].next == 1


def test_each_platform_has_its_own_rotation_and_may_start_elsewhere() -> None:
    log = UsedLog()
    tiktok, log = draw(log, ["a", "b", "c"], 2)
    youtube, log = draw(log, ["a", "b", "c"], 4, platform="youtube", first=2)
    assert tiktok == ["a", "b"] and youtube == ["c", "a", "b", "c"]
    assert set(log.closers) == {"tiktok", "youtube"}
    assert log.closers["tiktok"].last == "b" and log.closers["youtube"].last == "c"


def test_an_old_file_with_the_global_rotation_loads_and_starts_fresh_per_platform(
    tmp_path: Path,
) -> None:
    (tmp_path / USED_FILE).write_text(
        '{"schema_version": 1, "next_closer": 2, "last_closer": "b", "entries": []}',
        encoding="utf-8",
    )
    log = load_used(tmp_path)
    assert log.closers == {} and log.next_closer == 2
    drawn, _ = draw(log, ["a", "b", "c"], 1)
    assert drawn == ["a"]


def test_the_rotation_continues_after_a_restart(tmp_path: Path) -> None:
    closers = ["a", "b", "c"]
    first, log = draw(UsedLog(), closers, 2)
    save_used(tmp_path, log)
    second, _ = draw(load_used(tmp_path), closers, 3)
    assert first + second == ["a", "b", "c", "a", "b"]


def test_one_closer_repeats() -> None:
    drawn, log = draw(UsedLog(), ["only"], 3)
    assert drawn == ["only"] * 3 and log.closers["tiktok"].last == "only"


def test_edited_closer_lists_never_repeat_the_last_one() -> None:
    rng = random.Random(7)
    pool = ["a", "b", "c", "d", "e"]
    log = UsedLog()
    last = None
    for _ in range(300):
        closers = rng.sample(pool, rng.randint(2, 5))  # shorter, longer, reordered every time
        closer, log = take_closer(log, closers, "instagram")
        assert closer in closers and closer != last
        last = closer


def test_the_rotation_does_not_change_the_log_it_was_given() -> None:
    log = UsedLog(closers=state(1, "a"))
    take_closer(log, ["a", "b"], "tiktok")
    assert log == UsedLog(closers=state(1, "a"))


# --- record_render, posted_entry, overlapping_posted ---------------------------------------------


def test_a_new_render_replaces_an_unposted_entry_and_keeps_a_posted_one() -> None:
    old = entry(closer="old")
    log = record_render(UsedLog(), old)
    log = record_render(log, entry(closer="new"))
    assert [e.closer for e in log.entries] == ["new"]

    posted = entry(posted="2026-10-12T11:00:00+00:00", closer="posted")
    log = record_render(UsedLog(entries=[posted]), entry(closer="fresh"))
    assert [e.closer for e in log.entries] == ["posted", "fresh"]
    # another id or platform is left alone
    other = record_render(log, entry("A02"))
    other = record_render(other, entry(platform="youtube"))
    assert len(other.entries) == 4


def test_record_render_does_not_change_its_input() -> None:
    log = UsedLog(entries=[entry()])
    before = log.model_copy(deep=True)
    record_render(log, entry(closer="new"))
    assert log == before


def test_posted_entry_is_the_latest_posted_one_for_the_id_and_platform() -> None:
    first = entry(posted="2026-10-12T10:00:00+00:00", closer="first")
    second = entry(posted="2026-10-12T12:00:00+00:00", closer="second")
    log = UsedLog(
        entries=[first, entry(platform="youtube", posted="x"), second, entry(closer="unposted")]
    )
    found = posted_entry(log, "A01", "tiktok")
    assert found is not None and found.closer == "second"
    assert posted_entry(log, "A01", "instagram") is None
    assert posted_entry(log, "A02", "tiktok") is None
    assert posted_entry(UsedLog(entries=[entry()]), "A01", "tiktok") is None  # not posted


def test_the_overlap_threshold_is_half_of_the_new_moments_length() -> None:
    assert REUSE_OVERLAP == 0.5

    def hits(posted_start: float, posted_end: float) -> int:
        log = UsedLog(entries=[entry(start=posted_start, end=posted_end, posted="x")])
        return len(overlapping_posted(log, 100.0, 140.0, SHA))

    assert hits(100.0, 120.5) == 1  # 20.5 s of 40: just over half
    assert hits(100.0, 120.0) == 0  # exactly half
    assert hits(100.0, 110.0) == 0
    assert hits(130.0, 400.0) == 0  # 10 s
    assert hits(500.0, 600.0) == 0  # no overlap
    assert hits(90.0, 150.0) == 1  # covers the whole moment


def test_overlap_ignores_unposted_entries_and_other_sources() -> None:
    log = UsedLog(
        entries=[entry(start=100, end=140), entry(start=100, end=140, posted="x", sha="b" * 64)]
    )
    assert overlapping_posted(log, 100.0, 140.0, SHA) == []


def test_overlap_counts_any_id_and_platform_of_the_same_source() -> None:
    log = UsedLog(entries=[entry("Z9", "youtube", 100, 140, posted="x")])
    assert len(overlapping_posted(log, 100.0, 140.0, SHA)) == 1


def test_the_overlap_is_measured_against_this_moments_length() -> None:
    long_posted = UsedLog(entries=[entry(start=0, end=1000, posted="x")])
    assert len(overlapping_posted(long_posted, 100.0, 110.0, SHA)) == 1  # covers a short moment
    short_posted = UsedLog(entries=[entry(start=100, end=120, posted="x")])
    assert overlapping_posted(short_posted, 100.0, 200.0, SHA) == []  # 20 of 100
    assert overlapping_posted(short_posted, 100.0, 100.0, SHA) == []  # no length: nothing


# --- mark_posted ---------------------------------------------------------------------------------


def test_mark_posted_marks_the_latest_unposted_entry() -> None:
    older = entry(posted="2026-10-11T00:00:00+00:00", closer="older")
    newer = entry(closer="newer")
    log = UsedLog(entries=[older, newer, entry("A02")])
    before = log.model_copy(deep=True)

    after, marked, changed = mark_posted(log, "A01", "tiktok", "2026-10-12T12:00:00+00:00")

    assert changed and marked.closer == "newer"
    assert [e.posted_at for e in after.entries] == [
        "2026-10-11T00:00:00+00:00",
        "2026-10-12T12:00:00+00:00",
        None,
    ]
    assert log == before  # the argument was not changed


def test_mark_posted_twice_reports_already_and_changes_nothing() -> None:
    log = UsedLog(entries=[entry(posted="2026-10-12T12:00:00+00:00")])
    after, found, changed = mark_posted(log, "A01", "tiktok", "later")
    assert not changed and after is log and found.posted_at == "2026-10-12T12:00:00+00:00"


def test_mark_posted_without_a_render_is_an_error() -> None:
    with pytest.raises(ConfigError, match="has not been rendered for youtube"):
        mark_posted(UsedLog(entries=[entry()]), "A01", "youtube", "now")
    with pytest.raises(ConfigError, match="has not been rendered"):
        mark_posted(UsedLog(), "A01", "tiktok", "now")


# --- the file ------------------------------------------------------------------------------------


def test_a_missing_file_is_an_empty_log(tmp_path: Path) -> None:
    assert load_used(tmp_path) == UsedLog()


def test_the_log_survives_a_save_and_load(tmp_path: Path) -> None:
    log = UsedLog(next_closer=2, last_closer="b", entries=[entry(), entry("A02", posted="x")])
    save_used(tmp_path, log)
    assert load_used(tmp_path) == log
    assert [p.name for p in tmp_path.iterdir()] == [USED_FILE]  # no temp file left


@pytest.mark.parametrize(
    "content", ["not json", "[]", '{"entries": [{"id": "A01"}]}', '{"surprise": 1}', ""]
)
def test_an_invalid_file_is_an_error_and_is_left_alone(tmp_path: Path, content: str) -> None:
    (tmp_path / USED_FILE).write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match="fix it by hand or remove it"):
        load_used(tmp_path)
    assert (tmp_path / USED_FILE).read_text(encoding="utf-8") == content


# --- campaign-posted -----------------------------------------------------------------------------


def run_cli(project: Path, *args: str) -> int:
    return cli.main(["campaign-posted", *args], root=project)


def rendered_campaign(project: Path) -> Path:
    folder = project / "campaigns" / "demo"
    folder.mkdir(parents=True)  # no campaign.yaml and no watermark: the command must not need them
    save_used(folder, record_render(UsedLog(), entry()))
    return folder


def test_the_command_marks_the_entry_and_a_second_call_says_already(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = rendered_campaign(project)
    args = ("--campaign", "demo", "--id", "A01", "--platform", "tiktok")

    assert run_cli(project, *args) == 0
    out = capsys.readouterr().out
    assert out.strip() == "A01 tiktok: marked as posted"
    data = json.loads((folder / USED_FILE).read_text(encoding="utf-8"))
    stamp = data["entries"][0]["posted_at"]
    assert stamp is not None and stamp.endswith("+00:00")

    assert run_cli(project, *args) == 0
    out = capsys.readouterr().out
    assert out.strip() == "A01 tiktok: already marked as posted"
    assert (
        json.loads((folder / USED_FILE).read_text(encoding="utf-8"))["entries"][0]["posted_at"]
        == stamp
    )


def test_the_command_never_prints_the_hook_or_the_closer(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rendered_campaign(project)
    run_cli(project, "--campaign", "demo", "--id", "A01", "--platform", "tiktok")
    captured = capsys.readouterr()
    assert SECRET_HOOK not in captured.out + captured.err
    assert "closer one" not in captured.out + captured.err


def test_an_unknown_campaign_is_exit_2_with_a_message(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(project, "--campaign", "nothing", "--id", "A01", "--platform", "tiktok") == 2
    assert "unknown campaign" in capsys.readouterr().err


def test_a_moment_that_was_never_rendered_is_exit_2(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rendered_campaign(project)
    assert run_cli(project, "--campaign", "demo", "--id", "B07", "--platform", "tiktok") == 2
    assert "has not been rendered for tiktok" in capsys.readouterr().err
    assert run_cli(project, "--campaign", "demo", "--id", "A01", "--platform", "youtube") == 2


def test_a_broken_used_file_is_exit_2(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = rendered_campaign(project)
    (folder / USED_FILE).write_text("{", encoding="utf-8")
    assert run_cli(project, "--campaign", "demo", "--id", "A01", "--platform", "tiktok") == 2
    assert "fix it by hand" in capsys.readouterr().err


def test_a_bad_platform_is_an_argument_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        run_cli(Path("."), "--campaign", "demo", "--id", "A01", "--platform", "myspace")
    assert info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["--campaign", "--id"])
def test_campaign_and_id_are_required(missing: str) -> None:
    full: dict[str, Any] = {"--campaign": "demo", "--id": "A01", "--platform": "tiktok"}
    del full[missing]
    argv = [part for pair in full.items() for part in pair]
    with pytest.raises(SystemExit) as info:
        run_cli(Path("."), *argv)
    assert info.value.code == 2


def test_the_command_works_next_to_a_full_campaign_folder(project: Path) -> None:
    folder = write_campaign(project)
    write_moments(folder, [moment()])
    save_used(folder, record_render(UsedLog(), entry()))
    assert run_cli(project, "--campaign", "demo", "--id", "A01", "--platform", "tiktok") == 0
    assert load_used(folder).entries[0].posted_at is not None


# --- the platform may be left out (Brief 11.2) ---------------------------------------------------


def lock(folder: Path, **assigned: str) -> None:
    data = Assignments.model_validate(
        {"platforms": ["tiktok", "instagram", "youtube"], "assigned": assigned, "written_at": "t"}
    )
    (folder / ASSIGNMENTS_FILE).write_text(data.model_dump_json(), encoding="utf-8")


def test_without_a_platform_the_assigned_one_is_used(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = rendered_campaign(project)
    lock(folder, A01="tiktok")
    assert run_cli(project, "--campaign", "demo", "--id", "A01") == 0
    assert capsys.readouterr().out.strip() == "A01 tiktok: marked as posted"
    assert load_used(folder).entries[0].posted_at is not None


def test_without_a_platform_and_without_an_assignment_the_command_says_so(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = rendered_campaign(project)
    assert run_cli(project, "--campaign", "demo", "--id", "A01") == 2
    assert "no platform assigned yet" in capsys.readouterr().err
    lock(folder, B02="youtube")  # another id
    assert run_cli(project, "--campaign", "demo", "--id", "A01") == 2
    assert "say which with --platform" in capsys.readouterr().err
    assert load_used(folder).entries[0].posted_at is None


# --- --fix-platform (Brief 11.2) -----------------------------------------------------------------


def test_relabel_posted_moves_the_latest_posted_entry_and_nothing_else() -> None:
    log = UsedLog(entries=[entry(posted="x"), entry("A02", posted="y"), entry("A01")])
    after, before, now = relabel_posted(log, "A01", "instagram")
    assert (before.platform, now.platform) == ("tiktok", "instagram")
    assert [(e.id, e.platform, e.posted_at) for e in after.entries] == [
        ("A01", "instagram", "x"),
        ("A02", "tiktok", "y"),
        ("A01", "tiktok", None),
    ]
    assert log.entries[0].platform == "tiktok"  # the input is not changed
    assert latest_posted(after, "A01") is now


def test_relabel_posted_errors_are_clear() -> None:
    with pytest.raises(ConfigError, match="moment A01 has no posted entry to relabel"):
        relabel_posted(UsedLog(entries=[entry()]), "A01", "youtube")
    with pytest.raises(ConfigError, match="moment Z9 has no posted entry to relabel"):
        relabel_posted(UsedLog(entries=[entry(posted="x")]), "Z9", "youtube")
    two = UsedLog(entries=[entry(posted="x"), entry(platform="youtube", posted="y")])
    with pytest.raises(ConfigError, match="posted under tiktok, youtube; say which"):
        relabel_posted(two, "A01", "instagram")
    with pytest.raises(ConfigError, match="no posted entry under instagram"):
        relabel_posted(two, "A01", "youtube", "instagram")
    after, _, now = relabel_posted(two, "A01", "instagram", "youtube")
    assert [e.platform for e in after.entries] == ["tiktok", "instagram"] and now.platform == (
        "instagram"
    )


def test_the_fix_platform_option_relabels_and_a_second_call_says_already(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = rendered_campaign(project)
    run_cli(project, "--campaign", "demo", "--id", "A01", "--platform", "tiktok")
    capsys.readouterr()

    code = run_cli(project, "--campaign", "demo", "--id", "A01", "--fix-platform", "instagram")

    assert code == 0
    assert capsys.readouterr().out.strip() == "A01: posted entry relabelled tiktok -> instagram"
    (saved,) = load_used(folder).entries
    assert saved.platform == "instagram" and saved.posted_at is not None
    assert run_cli(project, "--campaign", "demo", "--id", "A01", "--fix-platform", "instagram") == 0
    assert capsys.readouterr().out.strip() == "A01: posted entry is already instagram"
    assert posted_entry(load_used(folder), "A01", "tiktok") is None


def test_fix_platform_on_an_unknown_or_unposted_id_is_exit_2(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rendered_campaign(project)  # A01 rendered, not posted
    assert run_cli(project, "--campaign", "demo", "--id", "A01", "--fix-platform", "youtube") == 2
    assert "moment A01 has no posted entry to relabel" in capsys.readouterr().err
    assert run_cli(project, "--campaign", "demo", "--id", "Z9", "--fix-platform", "youtube") == 2
    assert "moment Z9 has no posted entry to relabel" in capsys.readouterr().err
