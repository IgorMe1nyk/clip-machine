"""claude.no_api(): inside it every Claude call is refused by the kill switch (Brief 11)."""

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from claude_fakes import FakeMessages, answer
from clipmachine.config import load_app_config
from clipmachine.models import FoundMoments
from clipmachine.utils import claude
from clipmachine.utils.claude import ApiRefused, ClaudeClient

ENV = claude.KILL_SWITCH_ENV


def test_it_sets_the_switch_and_removes_it_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    with claude.no_api():
        assert os.environ[ENV] == "1"
    assert ENV not in os.environ


def test_it_puts_back_the_value_that_was_there(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "0")
    with claude.no_api():
        assert os.environ[ENV] == "1"
    assert os.environ[ENV] == "0"


def test_it_restores_the_variable_when_the_block_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    with pytest.raises(RuntimeError), claude.no_api():
        raise RuntimeError("inside")
    assert ENV not in os.environ

    monkeypatch.setenv(ENV, "true")
    with pytest.raises(RuntimeError), claude.no_api():
        raise RuntimeError("inside")
    assert os.environ[ENV] == "true"


def test_a_call_inside_it_is_refused_without_a_client_or_a_key(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV, raising=False)
    run_dir = project / "runs" / "2026-10-12_test"
    run_dir.mkdir()
    (project / ".env").write_text("CLIPMACHINE_ANTHROPIC_API_KEY=sk-fake-not-real\n")
    fake = FakeMessages([answer()])
    keys: list[str] = []

    def new_client(api_key: str) -> Any:
        keys.append(api_key)
        return SimpleNamespace(messages=fake)

    monkeypatch.setattr(claude, "new_client", new_client)
    monkeypatch.setattr(
        claude, "load_api_key", lambda root: pytest.fail("the key must not be read")
    )
    client = ClaudeClient(run_dir, project, load_app_config(project))

    with claude.no_api(), pytest.raises(ApiRefused, match="API disabled"):
        client.call_structured(
            "find_moments:block-1", "claude-haiku-5-5", "SYSTEM", "USER", FoundMoments, 2000
        )

    assert keys == [] and fake.create_calls == [] and fake.count_calls == []
    assert ENV not in os.environ
