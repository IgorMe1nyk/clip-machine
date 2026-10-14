"""The `produce` command (Brief 12.2): scout once when allowed, render this episode's unposted
moments, keep unchanged clips, rebuild the plan. Claude and ffmpeg faked; invented words only."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from campaign_helpers import moment, write_campaign, write_moments, write_run
from claude_fakes import FakeMessages, answer
from clipmachine import cli
from clipmachine.campaign.models import UsedEntry, UsedLog
from clipmachine.campaign.used import USED_FILE, load_used, reject, save_used
from clipmachine.utils import claude, ffmpeg
from test_campaign_scout import THREE, ai_campaign, clip, judged
from test_candidates import FAKE_KEY
from test_render import FakeFfmpeg

SECRET_TEXTS = ("AN INVENTED HOOK", "Zorb1e", "closer one", "A fresh hook", "because reasons")


class Rig:
    def __init__(self, project: Path, monkeypatch: pytest.MonkeyPatch, *, ai: bool) -> None:
        self.project = project
        source = project / "input.mp4"  # the scout writes `source: input.mp4` for proposals
        source.write_bytes(b"vide")
        self.run_dir = write_run(project, source=source).resolve()
        self.folder = write_campaign(project, mutate=ai_campaign if ai else None)
        (project / ".env").write_text(f"CLIPMACHINE_ANTHROPIC_API_KEY={FAKE_KEY}\n")
        self.messages = FakeMessages([])
        monkeypatch.delenv(claude.KILL_SWITCH_ENV, raising=False)
        monkeypatch.setattr(
            claude, "new_client", lambda key: SimpleNamespace(messages=self.messages)
        )
        self.ffmpeg = FakeFfmpeg()
        monkeypatch.setattr(ffmpeg, "render_clip", self.ffmpeg.render_clip)
        monkeypatch.setattr(ffmpeg, "probe", self.ffmpeg.probe)

    @property
    def out(self) -> Path:
        return self.project / "runs" / "_campaigns" / "demo"

    def script(self, *outcomes: Any) -> None:
        self.messages = FakeMessages(list(outcomes), 1500)

    def produce(self, *extra: str, target: Path | None = None) -> int:
        argv = ["produce", str(target or self.run_dir), "--campaign", "demo", *extra]
        return cli.main(argv, root=self.project)

    def calls(self) -> int:
        return len(self.messages.create_calls)

    def renders(self) -> int:
        return len(self.ffmpeg.calls)

    def mp4(self, moment_id: str, platform: str) -> Path:
        return self.out / platform / f"{moment_id}.mp4"


@pytest.fixture
def human(project: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    rig = Rig(project, monkeypatch, ai=False)
    write_moments(
        rig.folder,
        [
            moment(id="A01", start="0:24", end="0:58", platform="tiktok"),
            moment(id="A02", start="0:10", end="0:40", platform="instagram", caption="two."),
            moment(id="A03", start="0:34", end="1:04", platform="youtube", caption="three."),
        ],
    )
    return rig


@pytest.fixture
def ai(project: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(project, monkeypatch, ai=True)


def out_of(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


# --- the human path ------------------------------------------------------------------------------


def test_a_human_campaign_renders_this_episodes_moments_and_makes_no_call(
    human: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert human.produce() == 0
    text = out_of(capsys)
    assert human.calls() == 0 and human.renders() == 3
    assert "produce: tiktok 1 new, instagram 1 new, youtube 1 new; 0 unchanged" in text
    assert "API cost: $0 (no scout call)" in text
    assert "next slots: " in text and " tiktok A01" in text and " youtube A03" in text
    assert (human.out / "posting_plan.csv").is_file()
    assert (human.out / "campaign_report.html").is_file()
    for secret in SECRET_TEXTS:
        assert secret not in text


def test_a_second_produce_keeps_unchanged_clips_and_rerender_redoes_them(
    human: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert human.produce() == 0
    assert human.produce() == 0
    text = out_of(capsys)
    assert human.renders() == 3  # nothing rendered twice
    assert "A01 tiktok: unchanged since the last render, kept" in text
    assert "produce: tiktok 0 new, instagram 0 new, youtube 0 new; 3 unchanged" in text
    assert human.produce("--rerender") == 0
    assert human.renders() == 6 and human.calls() == 0


def test_moments_of_another_source_posted_and_rejected_ones_are_left_alone(human: Rig) -> None:
    write_moments(
        human.folder,
        [
            moment(id="A01", start="0:24", end="0:58", platform="tiktok"),
            moment(id="A02", start="0:10", end="0:40", platform="instagram", caption="two."),
            moment(id="A03", start="0:34", end="1:04", platform="youtube", caption="three."),
            moment(id="B01", start="0:05", end="0:35", source="episodes/other.mp4"),
        ],
    )
    assert human.produce() == 0
    assert not human.mp4("B01", "tiktok").exists() and human.renders() == 3
    assert (
        cli.main(["campaign-posted", "--campaign", "demo", "--id", "A02"], root=human.project) == 0
    )
    log = reject(load_used(human.folder), "A03", None, 34.0, 64.0, "2026-10-13T00:00:00+00:00")
    save_used(human.folder, log)
    before = (human.folder / USED_FILE).read_bytes()

    assert human.produce("--rerender") == 0
    assert human.renders() == 4  # only A01 again
    after = load_used(human.folder)
    posted = [e for e in after.entries if e.id == "A02"]
    assert len(posted) == 1 and posted[0].posted_at and after.rejected[0].id == "A03"
    assert (human.folder / USED_FILE).read_bytes() != before  # A01's new render only


def test_a_sourceless_moment_rendered_from_another_episode_is_refused(
    human: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = UsedEntry(
        id="A01",
        platform="tiktok",
        start=24.0,
        end=58.0,
        hook="X",
        rendered_at="2026-10-12T10:00:00+00:00",
        output="runs/_campaigns/demo/tiktok/A01.mp4",
        source_sha256="c" * 64,
    )
    save_used(human.folder, UsedLog(entries=[entry]))
    assert human.produce() == 2
    text = out_of(capsys)
    assert "moment A01 has no source: and was rendered from another episode" in text
    assert human.renders() == 0


def test_a_missing_episode_is_a_clear_error(human: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    assert human.produce(target=human.project / "nope.mp4") == 2
    assert "not found" in out_of(capsys) and human.renders() == 0


# --- the ai path ---------------------------------------------------------------------------------


def test_an_ai_campaign_scouts_once_then_renders_the_proposals(
    ai: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    ai.script(
        answer(THREE),
        answer(
            judged(
                clip("c01", score=9, topic="dating_relationships"),
                clip("c02", score=7, topic="values_purpose_growth"),
                clip("c03", score=3, verdict="reject"),
            )
        ),
    )
    assert ai.produce() == 0
    text = out_of(capsys)
    assert ai.calls() == 2 and ai.renders() == 2
    assert ai.mp4("S01", "instagram").is_file() and ai.mp4("S02", "youtube").is_file()
    assert "API cost: $" in text and "in 2 calls (scout)" in text
    assert "produce: tiktok 0 new, instagram 1 new, youtube 1 new" in text
    for secret in SECRET_TEXTS:
        assert secret not in text

    assert ai.produce() == 0  # the same episode: no second scout, nothing rendered twice
    text = out_of(capsys)
    assert ai.calls() == 2 and ai.renders() == 2
    assert "scout: this episode was scouted before (2 proposals in scout.json)" in text
    assert "2 unchanged" in text and "API cost: $0 (no scout call)" in text

    assert ai.produce("--rerender") == 0
    assert ai.calls() == 2 and ai.renders() == 4


def test_the_scout_json_of_the_ai_path_carries_the_episode(ai: Rig) -> None:
    ai.script(
        answer(THREE),
        answer(
            judged(
                clip("c01", score=9, topic="dating_relationships"),
                clip("c02", score=2, verdict="reject"),
                clip("c03", score=2, verdict="reject"),
            )
        ),
    )
    assert ai.produce() == 0
    records = json.loads((ai.folder / "scout.json").read_text(encoding="utf-8"))["records"]
    assert [r["id"] for r in records] == ["S01"] and records[0]["source_sha256"]
