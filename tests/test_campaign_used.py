"""The used-moments log, closer rotation and the campaign-posted command (Brief 11)."""

import json
import random
from pathlib import Path
from typing import Any

import pytest

from campaign_helpers import moment, write_campaign, write_moments
from clipmachine import cli
from clipmachine.campaign.models import UsedEntry, UsedLog
from clipmachine.campaign.used import (
    REUSE_OVERLAP,
    USED_FILE,
    load_used,
    mark_posted,
    overlapping_posted,
    posted_entry,
    record_render,
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


def draw(log: UsedLog, closers: list[str], times: int) -> tuple[list[str], UsedLog]:
    out = []
    for _ in range(times):
        closer, log = take_closer(log, closers)
        out.append(closer)
    return out, log


def test_closers_go_round_robin() -> None:
    drawn, log = draw(UsedLog(), ["a", "b", "c"], 7)
    assert drawn == ["a", "b", "c", "a", "b", "c", "a"]
    assert log.last_closer == "a" and log.next_closer == 1


def test_the_same_closer_is_never_used_twice_in_a_row() -> None:
    # the position points at the closer that was just used (as after a reorder)
    log = UsedLog(next_closer=0, last_closer="a")
    closer, after = take_closer(log, ["a", "b", "c"])
    assert closer == "b" and after.next_closer == 2 and after.last_closer == "b"
    wrapped = UsedLog(next_closer=5, last_closer="c")  # 5 % 3 = 2 -> "c" again
    closer, after = take_closer(wrapped, ["a", "b", "c"])
    assert closer == "a" and after.next_closer == 1


def test_the_rotation_continues_after_a_restart(tmp_path: Path) -> None:
    closers = ["a", "b", "c"]
    first, log = draw(UsedLog(), closers, 2)
    save_used(tmp_path, log)
    second, _ = draw(load_used(tmp_path), closers, 3)
    assert first + second == ["a", "b", "c", "a", "b"]


def test_one_closer_repeats() -> None:
    drawn, log = draw(UsedLog(), ["only"], 3)
    assert drawn == ["only"] * 3 and log.last_closer == "only"


def test_edited_closer_lists_never_repeat_the_last_one() -> None:
    rng = random.Random(7)
    pool = ["a", "b", "c", "d", "e"]
    log = UsedLog()
    last = None
    for _ in range(300):
        closers = rng.sample(pool, rng.randint(2, 5))  # shorter, longer, reordered every time
        closer, log = take_closer(log, closers)
        assert closer in closers and closer != last
        last = closer


def test_the_rotation_does_not_change_the_log_it_was_given() -> None:
    log = UsedLog(next_closer=1, last_closer="a")
    take_closer(log, ["a", "b"])
    assert log == UsedLog(next_closer=1, last_closer="a")


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


@pytest.mark.parametrize("missing", ["--campaign", "--id", "--platform"])
def test_every_option_is_required(missing: str) -> None:
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
