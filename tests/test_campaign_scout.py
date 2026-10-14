"""The `scout` and `campaign-reject` commands (Brief 12), with the Claude client faked and the
face finder faked. Invented words only; synthetic transcripts from campaign_helpers.write_run."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import track_fakes
from campaign_helpers import moment, write_campaign, write_moments, write_run
from claude_fakes import FakeMessages, answer
from clipmachine import cli
from clipmachine.campaign.models import ScoutFile, UsedEntry, UsedLog
from clipmachine.campaign.scout import (
    format_entries,
    next_ids,
    overlaps,
    score_block,
    scout_check,
)
from clipmachine.campaign.used import load_used, reject, save_used
from clipmachine.config import load_app_config
from clipmachine.models import ScoreBatch
from clipmachine.utils import claude
from helpers import edit_yaml
from test_candidates import FAKE_KEY
from test_render import FakeFfmpeg

TOPICS = {"values_purpose_growth": "youtube", "dating_relationships": "instagram"}
RULES = ["skip ad reads and sponsor segments", "never cut mid-thought"]
SECRET_TEXTS = ("AN INVENTED HOOK", "Zorb1e", "closer one", "A fresh hook", "because reasons")


def ai_campaign(d: dict[str, Any]) -> None:
    d["selection"] = "ai"
    d["scout"] = {"per_episode": 8, "rules": RULES, "topics": dict(TOPICS)}
    del d["watermark"]  # the second campaign wants no watermark and no closers
    d["post"]["closers"] = []


# --- the fake model's answers --------------------------------------------------------------------
#
# The transcript of write_run: sentence k runs 4.9*k .. 4.9*k+3.9 s, 20 sentences, a 100 s source.


def found(*spans: tuple[float, float]) -> str:
    return json.dumps(
        {
            "moments": [
                {"start": a, "end": b, "hook": "A hook.", "reason": "why", "type": "story"}
                for a, b in spans
            ]
        }
    )


def judged(
    *clips: dict[str, Any],
) -> str:
    return json.dumps({"clips": list(clips)})


def clip(
    clip_id: str,
    score: int = 8,
    topic: str | None = "values_purpose_growth",
    hook: str = "A fresh hook",
    caption: str | None = "Leads with the idea.",
    verdict: str = "keep",
) -> dict[str, Any]:
    return {
        "id": clip_id,
        "score": score,
        "verdict": verdict,
        "reject_reason": None if verdict == "keep" else "weak_hook",
        "hook_line": hook,
        "suggested_title": "A title",
        "reason": "because reasons",
        "keep_sentences": None,
        "topic": topic,
        "caption": caption,
    }


THREE = found((0.0, 28.4), (34.3, 62.7), (68.6, 97.1))  # sentences 0-5, 7-12, 14-19


class Rig:
    def __init__(self, project: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeFfmpeg) -> None:
        self.project = project
        self.run_dir = write_run(project).resolve()
        self.folder = write_campaign(project, mutate=ai_campaign)
        (project / ".env").write_text(f"CLIPMACHINE_ANTHROPIC_API_KEY={FAKE_KEY}\n")
        self.messages = FakeMessages([])
        monkeypatch.delenv(claude.KILL_SWITCH_ENV, raising=False)
        monkeypatch.setattr(
            claude, "new_client", lambda key: SimpleNamespace(messages=self.messages)
        )
        self.ffmpeg = fake

    def script(self, *outcomes: Any) -> None:
        self.messages = FakeMessages(list(outcomes), 1500)

    def scout(self, *extra: str) -> int:
        argv = ["scout", str(self.run_dir), "--campaign", "demo", *extra]
        return cli.main(argv, root=self.project)

    def moments_text(self) -> str:
        path = self.folder / "moments.yaml"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def ids(self) -> list[str]:
        return [
            line.split("id: ")[1].split(",")[0]
            for line in self.moments_text().splitlines()
            if line.startswith("- ")
        ]

    def records(self) -> ScoutFile:
        return ScoutFile.model_validate_json((self.folder / "scout.json").read_text("utf-8"))

    def user_text(self, call: int) -> str:
        return str(self.messages.create_calls[call]["messages"][0]["content"])


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeFfmpeg:
    from clipmachine.utils import ffmpeg

    fake = FakeFfmpeg()
    monkeypatch.setattr(ffmpeg, "render_clip", fake.render_clip)
    monkeypatch.setattr(ffmpeg, "probe", fake.probe)
    return fake


@pytest.fixture
def rig(project: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeFfmpeg) -> Rig:
    return Rig(project, monkeypatch, fake)


def out_of(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


# --- the gate ------------------------------------------------------------------------------------


def test_scout_refuses_a_human_campaign_and_makes_no_call(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    edit_yaml(rig.folder / "campaign.yaml", lambda d: d.update(selection="human"))
    assert rig.scout() == 2
    text = out_of(capsys)
    assert "selection: human" in text and "set selection: ai" in text
    assert rig.messages.create_calls == [] and not (rig.folder / "scout.json").exists()


def test_selection_ai_needs_a_scout_section_and_old_files_still_load(project: Path) -> None:
    from clipmachine.campaign.loader import load_campaign
    from clipmachine.config import ConfigError

    def no_scout(d: dict[str, Any]) -> None:
        d["selection"] = "ai"

    write_campaign(project, mutate=no_scout)
    with pytest.raises(ConfigError, match="selection: ai needs a scout: section"):
        load_campaign(project, "demo", load_app_config(project))
    write_campaign(project)  # Brief 11 shape: no selection, no scout
    cfg = load_campaign(project, "demo", load_app_config(project)).config
    assert cfg.selection == "human" and cfg.scout is None


# --- the happy path ------------------------------------------------------------------------------


def test_scout_appends_proposals_with_platforms_from_the_topics(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    write_moments(rig.folder, [moment(id="A01", start="1:30", end="1:40", caption="mine.")])
    before = rig.moments_text()
    with_comment = before + "# Igor's note stays\n"
    (rig.folder / "moments.yaml").write_text(with_comment, encoding="utf-8")
    rig.script(
        answer(THREE),
        answer(
            judged(
                clip("c01", score=9, topic="dating_relationships"),
                clip("c02", score=7, topic="values_purpose_growth"),
                clip("c03", score=8, topic="values_purpose_growth"),
            )
        ),
    )

    assert rig.scout() == 0

    text = rig.moments_text()
    assert text.startswith(with_comment)  # Igor's entries and comments, byte for byte
    assert (rig.folder / "moments.yaml.bak").read_text(encoding="utf-8") == with_comment
    assert rig.ids() == ["A01", "S01", "S02", "S03"]
    lines = text.splitlines()
    s01 = next(line for line in lines if line.startswith("- ") and "id: S01" in line)
    assert "platform: instagram" in s01 and "source: input.mp4" in s01
    assert "hook: A fresh hook" in s01 and "caption: Leads with the idea." in s01
    assert "start: 0.0, end: 28.4" in s01 or "start: 0.0," in s01
    s03 = next(line for line in lines if "id: S03" in line and line.startswith("- "))
    assert "platform: youtube" in s03
    comment = next(line for line in lines if line.startswith("# S01:"))
    assert "score 9, topic dating_relationships -> instagram, host not_checked" in comment
    assert "because reasons" in comment

    records = rig.records().records
    assert [(r.id, r.score, r.platform) for r in records] == [
        ("S01", 9, "instagram"),
        ("S02", 8, "youtube"),
        ("S03", 7, "youtube"),
    ]
    assert all(r.source_sha256 == "b" * 64 and r.source_name == "original.mp4" for r in records)
    console = out_of(capsys)
    assert "scout: 3 proposed" in console and "0 skipped as overlapping" in console
    assert "--only S01,S02,S03" in console and "cost $" in console
    for secret in SECRET_TEXTS:
        assert secret not in console
    assert (rig.run_dir / "scout" / "demo" / "scored.json").is_file()
    assert not (rig.run_dir / "scored.json").exists()
    assert (rig.run_dir / "costs.json").is_file()  # the cost is booked against the run
    assert (rig.project / "runs" / "_ledger" / "costs.jsonl").is_file()


def test_the_prompts_carry_the_campaign_rules_and_the_topics(rig: Rig) -> None:
    rig.script(answer(THREE), answer(judged(clip("c01"), clip("c02"), clip("c03"))))
    assert rig.scout() == 0
    find = rig.user_text(0)
    assert "Campaign rules (hard constraints" in find and "- never cut mid-thought" in find
    assert "rules: skip ad reads and sponsor segments; never cut mid-thought" in find
    assert "Clip length: 20-60 seconds." in find  # the campaign's clip_length
    judge = rig.user_text(1)
    assert (
        "set topic to exactly one of these keys: values_purpose_growth, dating_relationships"
        in judge
    )
    assert "hook_line is at most 7 words and 30 characters" in judge
    assert "{scout_block}" not in find and "{scout_block}" not in judge


def test_proposals_are_ranked_by_score_and_capped_by_per_episode(rig: Rig) -> None:
    edit_yaml(rig.folder / "campaign.yaml", lambda d: d["scout"].update(per_episode=2))
    rig.script(
        answer(THREE),
        answer(judged(clip("c01", score=6), clip("c02", score=9), clip("c03", score=8))),
    )
    assert rig.scout() == 0
    records = rig.records().records
    assert [(r.id, r.score) for r in records] == [("S01", 9), ("S02", 8)]
    assert records[0].start == pytest.approx(34.05, abs=0.5)  # the second span, best first


def test_ids_continue_after_the_highest_existing_s_number(rig: Rig) -> None:
    write_moments(
        rig.folder,
        [moment(id="S05", start="1:30", end="1:40"), moment(id="B02", start="1:30", end="1:40")],
    )
    rig.script(
        answer(THREE), answer(judged(clip("c01"), clip("c02", verdict="reject"), clip("c03")))
    )
    assert rig.scout() == 0
    assert rig.ids() == ["S05", "B02", "S06", "S07"]
    assert next_ids(["S99", "S100"], 2) == ["S101", "S102"] and next_ids([], 1) == ["S01"]


def test_a_moments_file_that_does_not_exist_yet_is_created(rig: Rig) -> None:
    rig.script(answer(THREE), answer(judged(clip("c01"), clip("c02"), clip("c03"))))
    assert not (rig.folder / "moments.yaml").exists()
    assert rig.scout() == 0
    assert rig.ids() == ["S01", "S02", "S03"]
    assert (rig.folder / "moments.yaml.bak").read_text(encoding="utf-8") == ""


# --- overlap -------------------------------------------------------------------------------------


def test_overlaps_is_more_than_half_of_the_new_stretch() -> None:
    assert overlaps(10, 40, [(0, 26)]) and not overlaps(10, 40, [(0, 25)])
    assert overlaps(10, 40, [(20, 100)]) and not overlaps(10, 40, [(50, 60)])
    assert overlaps(10, 10, [])  # an empty stretch is never proposed


def test_stretches_already_listed_rendered_or_rejected_are_skipped(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    write_moments(rig.folder, [moment(id="A01", start="0:00", end="0:30")])  # covers span 1
    rendered = UsedEntry.model_validate(
        {
            "id": "Z1",
            "platform": "tiktok",
            "start": 34.0,
            "end": 63.0,
            "hook": "X",
            "closer": None,
            "rendered_at": "t",
            "output": "runs/_campaigns/demo/tiktok/Z1.mp4",
            "source_sha256": "b" * 64,
        }
    )
    log = reject(UsedLog(entries=[rendered]), "R1", None, 68.0, 98.0, "t")
    save_used(rig.folder, log)
    rig.script(answer(THREE), answer(judged(clip("c01"), clip("c02"), clip("c03"))))

    assert rig.scout() == 0

    assert rig.ids() == ["A01"]
    assert "scout: 0 proposed (none), 3 skipped as overlapping" in out_of(capsys)
    assert (
        not (rig.folder / "moments.yaml.bak").exists() and not (rig.folder / "scout.json").exists()
    )


def test_a_render_of_another_source_does_not_block(rig: Rig) -> None:
    other = UsedEntry.model_validate(
        {
            "id": "Z1",
            "platform": "tiktok",
            "start": 0.0,
            "end": 100.0,
            "hook": "X",
            "rendered_at": "t",
            "output": "x.mp4",
            "source_sha256": "c" * 64,
        }
    )
    save_used(rig.folder, UsedLog(entries=[other]))
    rig.script(answer(THREE), answer(judged(clip("c01"), clip("c02"), clip("c03"))))
    assert rig.scout() == 0
    assert len(rig.ids()) == 3


# --- the scorer's answer is checked ------------------------------------------------------------


def test_scout_check_names_the_rules_and_the_ids_only() -> None:
    check = scout_check(["a", "b"], 30)
    good = ScoreBatch.model_validate({"clips": [clip("c01", topic="a")]})
    assert check(good) is None
    bad = ScoreBatch.model_validate(
        {
            "clips": [
                clip("c01", topic="zzz", hook="one two three four five six seven eight"),
                clip("c02", topic="a", caption=None),
                clip("c03", topic="zzz", verdict="reject"),  # rejected clips are not checked
            ]
        }
    )
    message = check(bad) or ""
    assert "clip c01: topic must be one of a, b; hook_line must be at most 7 words" in message
    assert "clip c02: caption is missing" in message and "c03" not in message
    assert "zzz" not in message and "eight" not in message
    assert (
        scout_check([], 30)(ScoreBatch.model_validate({"clips": [clip("c01", topic=None)]})) is None
    )


def test_a_bad_topic_is_repaired_once_and_a_second_bad_answer_stops_scout(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.script(
        answer(THREE),
        answer(judged(clip("c01", topic="zzz"), clip("c02"), clip("c03"))),
        answer(judged(clip("c01"), clip("c02"), clip("c03"))),  # the repair
    )
    assert rig.scout() == 0
    assert len(rig.messages.create_calls) == 3 and len(rig.ids()) == 3
    repair = rig.user_text(2)
    assert "clip c01: topic must be one of" in repair

    rig.script(
        answer(THREE),
        answer(judged(clip("c01", caption=None), clip("c02"), clip("c03"))),
        answer(judged(clip("c01", caption=None), clip("c02"), clip("c03"))),
    )
    capsys.readouterr()
    assert rig.scout() == 2
    assert "error:" in out_of(capsys)
    assert len(rig.ids()) == 3  # nothing more was written


def test_dry_run_writes_nothing(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    rig.script(answer(THREE), answer(judged(clip("c01"), clip("c02"), clip("c03"))))
    assert rig.scout("--dry-run") == 0
    assert not (rig.folder / "moments.yaml").exists() and not (rig.folder / "scout.json").exists()
    text = out_of(capsys)
    assert "dry run: nothing written" in text and "scout: 3 proposed" in text


# --- the host-side hint --------------------------------------------------------------------------


def host_rig(rig: Rig, monkeypatch: pytest.MonkeyPatch, scenario: Any, side: str) -> None:
    edit_yaml(rig.folder / "campaign.yaml", lambda d: d["scout"].update(host_side=side))
    track_fakes.write_model(rig.project)
    track_fakes.install(monkeypatch, scenario)


@pytest.mark.parametrize(("side", "expected"), [("left", "likely"), ("right", "likely")])
def test_a_face_on_the_hosts_side_is_likely(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, side: str, expected: str
) -> None:
    host_rig(rig, monkeypatch, track_fakes.two_people(), side)  # faces at 0.25 and 0.75
    rig.script(answer(THREE), answer(judged(clip("c01"), clip("c02"), clip("c03"))))
    assert rig.scout() == 0
    assert {r.host_check for r in rig.records().records} == {expected}


def test_a_face_only_on_the_other_side_or_no_face_is_unsure(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def right_only(t: float) -> list[track_fakes.RawFace]:
        return [track_fakes.RawFace(0.75, 0.5, 0.15, 0.3, 0.1)]

    host_rig(rig, monkeypatch, right_only, "left")
    rig.script(answer(THREE), answer(judged(clip("c01"), clip("c02"), clip("c03"))))
    assert rig.scout() == 0
    assert {r.host_check for r in rig.records().records} == {"unsure"}
    assert "host check: S01 unsure, S02 unsure, S03 unsure" in out_of(capsys)
    assert len(rig.ids()) == 3  # never dropped for it


def test_without_the_face_models_the_check_is_not_made(rig: Rig) -> None:
    edit_yaml(rig.folder / "campaign.yaml", lambda d: d["scout"].update(host_side="left"))
    rig.script(answer(THREE), answer(judged(clip("c01"), clip("c02"), clip("c03"))))
    assert rig.scout() == 0
    assert {r.host_check for r in rig.records().records} == {"not_checked"}


# --- campaign-reject -----------------------------------------------------------------------------


def test_reject_marks_the_moment_and_the_render_skips_it(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    write_moments(
        rig.folder,
        [
            moment(id="S01", start="0:24", end="0:58", platform="tiktok"),
            moment(id="S02", start="0:34", end="1:04", platform="youtube"),
        ],
    )
    assert cli.main(["campaign-reject", "--campaign", "demo", "--id", "S01"], root=rig.project) == 0
    text = out_of(capsys)
    assert "S01: rejected (24.00-58.00 s of its source)" in text
    (rejected,) = load_used(rig.folder).rejected
    assert (rejected.id, rejected.start, rejected.end, rejected.source) == ("S01", 24.0, 58.0, None)

    code = cli.main(["campaign", str(rig.run_dir), "--campaign", "demo"], root=rig.project)
    assert code == 0
    text = out_of(capsys)
    assert "S01 tiktok: skipped: rejected with campaign-reject" in text
    assert "1 rendered, 0 failed, 0 refused, 1 rejected" in text
    assert rig.ffmpeg.ids == ["S02_youtube"]


def test_reject_refuses_unknown_posted_and_repeated_ids(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    write_moments(rig.folder, [moment(id="S01", start="0:24", end="0:58")])
    assert cli.main(["campaign-reject", "--campaign", "demo", "--id", "Z9"], root=rig.project) == 2
    assert "moment Z9 is not in moments.yaml" in out_of(capsys)
    assert cli.main(["campaign-reject", "--campaign", "demo", "--id", "S01"], root=rig.project) == 0
    assert cli.main(["campaign-reject", "--campaign", "demo", "--id", "S01"], root=rig.project) == 2
    assert "already rejected" in out_of(capsys)
    posted = UsedEntry.model_validate(
        {
            "id": "S02",
            "platform": "tiktok",
            "start": 1.0,
            "end": 30.0,
            "hook": "X",
            "rendered_at": "t",
            "output": "x.mp4",
            "source_sha256": "b" * 64,
            "posted_at": "t",
        }
    )
    log = load_used(rig.folder).model_copy(update={"entries": [posted]})
    save_used(rig.folder, log)
    write_moments(rig.folder, [moment(id="S02", start="0:24", end="0:58")])
    assert cli.main(["campaign-reject", "--campaign", "demo", "--id", "S02"], root=rig.project) == 2
    assert "posted moment cannot be rejected" in out_of(capsys)


def test_a_rejected_stretch_is_never_proposed_again(rig: Rig) -> None:
    write_moments(rig.folder, [moment(id="S01", start="0:00", end="0:28")])
    assert cli.main(["campaign-reject", "--campaign", "demo", "--id", "S01"], root=rig.project) == 0
    # Igor then deletes the entry from moments.yaml: the rejection still counts
    (rig.folder / "moments.yaml").write_text("", encoding="utf-8")
    rig.script(answer(THREE), answer(judged(clip("c01"), clip("c02"), clip("c03"))))
    assert rig.scout() == 0
    assert rig.ids() == ["S01", "S02"]  # the first span was skipped; ids restart (file was empty)
    assert all(r.start > 30 for r in rig.records().records)


# --- small pieces --------------------------------------------------------------------------------


def test_format_entries_quotes_text_and_keeps_one_entry_per_line() -> None:
    from clipmachine.campaign.scout import Proposal

    p = Proposal(
        id="S01",
        start=1.25,
        end=30.5,
        hook="Why: the # sign",
        caption="A caption, with commas: and colons.",
        topic=None,
        platform="auto",
        score=7,
        reason="line one\nline two # hash",
        host_check="unsure",
    )
    text = format_entries([p], "episodes/ep1.mp4", "2026-10-13T00:00:00+00:00")
    lines = text.splitlines()
    assert lines[0] == "# scout 2026-10-13: 1 proposed from ep1.mp4"
    assert lines[1] == "# S01: score 7, topic none -> auto, host unsure; line one line two hash"
    assert lines[2].startswith("- {id: S01, source: episodes/ep1.mp4, start: 1.25, end: 30.5, ")
    from clipmachine.campaign.loader import parse_moments

    (spec,) = parse_moments(text, 48)
    assert spec.hook == "Why: the # sign" and spec.caption == "A caption, with commas: and colons."
    assert spec.platform == "auto" and spec.source == "episodes/ep1.mp4"


def test_score_block_without_topics_asks_for_null() -> None:
    from clipmachine.campaign.models import ScoutConfig

    block = score_block(ScoutConfig(), 48)
    assert (
        "Leave topic null." in block and "hook_line is at most 7 words and 48 characters" in block
    )
    # A rejected clip must keep a hook and title too: the base ClipScore validator requires
    # them, and the real run showed the model emptying them for rejects in a repair round.
    assert "A clip you reject still gets a non-empty hook_line" in block


def test_without_topics_every_proposal_is_auto(rig: Rig) -> None:
    edit_yaml(rig.folder / "campaign.yaml", lambda d: d["scout"].update(topics={}))
    rig.script(
        answer(THREE),
        answer(judged(clip("c01", topic=None), clip("c02", topic=None), clip("c03", topic=None))),
    )
    assert rig.scout() == 0
    assert {r.platform for r in rig.records().records} == {"auto"}
    assert all(
        "platform: auto" in line
        for line in rig.moments_text().splitlines()
        if line.startswith("- ")
    )
