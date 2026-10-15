"""The Claude client: retry rules, kill switch, budget guard, cost log, key handling.

No test touches the network: claude.new_client is replaced by a scripted fake, and every error is
a real anthropic exception class built from a hand-made httpx2 response.
"""

import json
import logging
import math
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest

from claude_fakes import VALID, FakeMessages, answer, network_error, status_error, timeout_error
from clipmachine.config import load_app_config
from clipmachine.models import (
    AppConfig,
    BudgetConfig,
    CostRecord,
    CostsFile,
    FoundMoments,
    PricingConfig,
)
from clipmachine.utils import claude
from clipmachine.utils.claude import (
    ApiCallFailed,
    ApiRefused,
    ClaudeClient,
    ErrorFacts,
    InvalidModelOutput,
    classify,
)

REAL_NEW_CLIENT = claude.new_client  # captured before the autouse guard replaces it
FAKE_KEY = "sk-ant-api03-FAKE-KEY-must-never-leak-0123456789"
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
HAIKU = "claude-haiku-5-5"


class Rig:
    """A project folder with a fake key, a run folder, and a way to script the fake API."""

    def __init__(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.project = project
        self.run_dir = project / "runs" / "2026-10-12_test"
        self.run_dir.mkdir()
        (project / ".env").write_text(
            f"# comment\nOTHER=1\nCLIPMACHINE_ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8"
        )
        self.app: AppConfig = load_app_config(project)
        self.sleeps: list[float] = []
        self.keys: list[str] = []
        self.fake = FakeMessages([answer()])
        monkeypatch.delenv(claude.KILL_SWITCH_ENV, raising=False)
        monkeypatch.setattr(claude, "new_client", self._new_client)

    def _new_client(self, api_key: str) -> Any:
        self.keys.append(api_key)
        return SimpleNamespace(messages=self.fake)

    def script(self, outcomes: list[Any], count: Any = 1000) -> FakeMessages:
        self.fake = FakeMessages(outcomes, count)
        return self.fake

    def budget(self, run: float = 1.0, day: float = 5.0) -> None:
        self.app = self.app.model_copy(
            update={"budget": BudgetConfig(max_usd_per_run=run, max_usd_per_day=day)}
        )

    def client(self) -> ClaudeClient:
        return ClaudeClient(
            self.run_dir, self.project, self.app, sleep=self.sleeps.append, clock=lambda: NOW
        )

    def call(self, client: ClaudeClient | None = None, **kwargs: Any) -> Any:
        client = client or self.client()
        args: dict[str, Any] = {
            "purpose": "find_moments:block-1",
            "model": HAIKU,
            "system": "SYSTEM TEXT",
            "user": "USER TEXT",
            "schema": FoundMoments,
            "max_tokens": 2000,
        }
        args.update(kwargs)
        return client.call_structured(**args)

    def costs(self) -> CostsFile:
        return CostsFile.model_validate_json(
            (self.run_dir / "costs.json").read_text(encoding="utf-8")
        )

    def ledger(self) -> list[dict[str, Any]]:
        path = self.project / "runs" / "_ledger" / "costs.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def seed_ledger(self, day: str, booked: float) -> None:
        path = self.project / "runs" / "_ledger" / "costs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"time": f"{day}T10:00:00+00:00", "day": day, "run": "old", "booked_usd": booked}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


@pytest.fixture
def rig(project: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(project, monkeypatch)


# --- the retry table (pure) ----------------------------------------------------------------------


def facts(status: int | None, **kwargs: Any) -> ErrorFacts:
    return ErrorFacts(status=status, **kwargs)


NEVER = [
    (facts(401, error_type="authentication_error", message="invalid x-api-key"), "API key"),
    (facts(403, error_type="permission_error", message="no"), "permission"),
    (facts(402, error_type="billing_error", message="billing"), "credits"),
    (
        facts(
            400,
            error_type="invalid_request_error",
            message="Your credit balance is too low to access the Anthropic API.",
        ),
        "credits",
    ),
    (
        facts(
            400,
            error_type="invalid_request_error",
            message="You have reached your specified API usage limits. You will regain access "
            "on 2026-11-01 at 00:00 UTC.",
        ),
        "Spend limits",
    ),
    (
        facts(
            400,
            error_type="invalid_request_error",
            message="You have reached your specified workspace API usage limits.",
        ),
        "Spend limits",
    ),
    (
        facts(
            429,
            error_type="rate_limit_error",
            error_code="enforced_spend_limit_reached",
            message="You have reached your API usage limits",
        ),
        "monthly usage cap",
    ),
    (  # the code wins even when a retry-after is present
        facts(429, error_code="enforced_spend_limit_reached", retry_after=5.0),
        "monthly usage cap",
    ),
    (facts(429, error_type="rate_limit_error", message="slow down"), "no retry time"),
    (facts(429, retry_after=61.0), "try again later"),
    (facts(400, error_type="invalid_request_error", message="bad field"), "not a limit"),
    (facts(404, error_type="not_found_error"), "not a limit"),
    (facts(413, error_type="request_too_large"), "not a limit"),
    (facts(422), "not a limit"),
    (facts(None), "no answer from the API"),
]


@pytest.mark.parametrize(("error", "hint"), NEVER)
def test_never_retried(error: ErrorFacts, hint: str) -> None:
    for attempt in (1, 2):
        decision = classify(error, attempt)
        assert decision.retry is False
        assert hint in decision.message


def test_message_names_the_request_id_when_known() -> None:
    decision = classify(facts(401, request_id="req_abc"), 1)
    assert "req_abc" in decision.message


@pytest.mark.parametrize(
    ("error", "attempt", "wait"),
    [
        (facts(429, retry_after=5.0), 1, 5.0),
        (facts(429, retry_after=60.0), 2, 60.0),
        (facts(429, retry_after=0.0), 1, 0.0),
        (facts(500), 1, 2.0),
        (facts(500), 2, 8.0),
        (facts(502), 1, 2.0),
        (facts(503, retry_after=4.0), 1, 4.0),
        (facts(504), 1, 2.0),
        (facts(529, error_type="overloaded_error"), 1, 2.0),
        (facts(529, error_type="overloaded_error", retry_after=500.0), 1, 60.0),
    ],
)
def test_retried_with_backoff(error: ErrorFacts, attempt: int, wait: float) -> None:
    decision = classify(error, attempt)
    assert decision.retry is True
    assert decision.wait_seconds == wait


@pytest.mark.parametrize("error", [facts(429, retry_after=1.0), facts(500), facts(529)])
def test_third_failure_stops(error: ErrorFacts) -> None:
    decision = classify(error, 3)
    assert decision.retry is False
    assert "3 attempts" in decision.message


# --- the retry table through the client ----------------------------------------------------------


def test_429_with_retry_after_waits_then_succeeds(rig: Rig) -> None:
    fake = rig.script([status_error(429, retry_after=3), answer()])
    parsed, record = rig.call()
    assert [m.hook for m in parsed.moments] == ["A hook."]
    assert rig.sleeps == [3.0]
    assert len(fake.create_calls) == 2
    assert record.attempts == 2


def test_overloaded_twice_then_success_uses_backoff(rig: Rig) -> None:
    fake = rig.script(
        [status_error(529, error_type="overloaded_error"), status_error(500), answer()]
    )
    rig.call()
    assert rig.sleeps == [2.0, 8.0]
    assert len(fake.create_calls) == 3


def test_three_server_errors_fail_after_three_attempts(rig: Rig) -> None:
    fake = rig.script([status_error(500), status_error(502), status_error(503)])
    with pytest.raises(ApiCallFailed, match="3 attempts"):
        rig.call()
    assert len(fake.create_calls) == 3
    (record,) = rig.costs().records
    assert (record.status, record.attempts, record.booked_usd) == ("failed", 3, 0.0)


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        (status_error(401, "invalid x-api-key", error_type="authentication_error"), "API key"),
        (status_error(403, error_type="permission_error"), "permission"),
        (
            status_error(
                400,
                "Your credit balance is too low to access the Anthropic API.",
                error_type="invalid_request_error",
            ),
            "credits",
        ),
        (
            status_error(
                400,
                "You have reached your specified API usage limits.",
                error_type="invalid_request_error",
            ),
            "Spend limits",
        ),
        (
            status_error(
                429, "cap", error_type="rate_limit_error", code="enforced_spend_limit_reached"
            ),
            "monthly usage cap",
        ),
        (status_error(429, "no header", error_type="rate_limit_error"), "no retry time"),
        (status_error(400, "bad", error_type="invalid_request_error"), "not a limit"),
    ],
)
def test_never_retry_errors_stop_at_once_with_a_hint(rig: Rig, error: Exception, hint: str) -> None:
    fake = rig.script([error, answer()])
    with pytest.raises(ApiCallFailed) as caught:
        rig.call()
    assert hint in str(caught.value)
    assert len(fake.create_calls) == 1
    assert rig.sleeps == []
    (record,) = rig.costs().records
    assert record.status == "failed"
    assert record.booked_usd == 0.0


@pytest.mark.parametrize("error", [network_error(), timeout_error()])
def test_network_errors_are_not_retried_and_are_booked_as_unconfirmed(
    rig: Rig, error: Exception
) -> None:
    fake = rig.script([error, answer()])
    with pytest.raises(ApiCallFailed, match="no answer from the API"):
        rig.call()
    assert len(fake.create_calls) == 1
    (record,) = rig.costs().records
    assert record.status == "unconfirmed"
    assert record.booked_usd == record.estimated_usd > 0
    assert rig.ledger()[0]["booked_usd"] == record.booked_usd
    assert rig.client().spent_run() == record.booked_usd


# --- kill switch ---------------------------------------------------------------------------------


def test_kill_switch_refuses_before_anything_else(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(claude.KILL_SWITCH_ENV, "1")
    (rig.project / ".env").unlink()  # proves .env is not even read
    fake = rig.script([answer()])
    with pytest.raises(ApiRefused, match="API disabled"):
        rig.call()
    assert fake.create_calls == [] and fake.count_calls == [] and rig.keys == []
    costs = rig.costs()
    assert (costs.totals.calls, costs.totals.refusals) == (0, 1)
    assert costs.records[0].status == "refused"
    assert rig.ledger() == []  # refusals are not spend


def test_kill_switch_off_values_do_not_block(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(claude.KILL_SWITCH_ENV, "0")
    parsed, _ = rig.call()
    assert parsed.moments


# --- budget guard --------------------------------------------------------------------------------


def test_run_budget_refuses_before_any_network_use(rig: Rig) -> None:
    rig.budget(run=0.000001)
    fake = rig.script([answer()])
    with pytest.raises(ApiRefused, match=r"budget.max_usd_per_run"):
        rig.call()
    assert fake.create_calls == [] and fake.count_calls == [] and rig.keys == []
    (record,) = rig.costs().records
    assert record.status == "refused"
    assert record.estimated_usd == pytest.approx(0.001)  # 2000 output tokens at $0.50 / M


def test_run_budget_is_checked_again_with_the_counted_input(rig: Rig) -> None:
    rig.budget(run=0.0012)  # the output floor ($0.001) fits; the counted input does not
    fake = rig.script([answer()], count=5000)
    with pytest.raises(ApiRefused, match=r"budget.max_usd_per_run"):
        rig.call()
    assert len(fake.count_calls) == 1
    assert fake.create_calls == []
    (record,) = rig.costs().records
    assert (record.status, record.estimate_source) == ("refused", "count_tokens")
    assert record.estimated_input_tokens == math.ceil(5000 * 1.1)


def test_day_budget_counts_todays_ledger_and_ignores_other_days(rig: Rig) -> None:
    rig.seed_ledger("2026-10-11", 100.0)  # yesterday: ignored
    rig.seed_ledger("2026-10-12", 4.9995)
    fake = rig.script([answer()])
    with pytest.raises(ApiRefused, match=r"budget.max_usd_per_day"):
        rig.call()
    assert fake.create_calls == []

    rig.script([answer()])
    rig.budget(day=6.0)
    parsed, _ = rig.call()
    assert parsed.moments


def test_run_spend_accumulates_across_calls_and_processes(rig: Rig) -> None:
    rig.script([answer(), answer()])
    rig.budget(run=0.0016)  # one call is about $0.0003 actual but $0.0015 worst case
    client = rig.client()
    rig.call(client)  # allowed: nothing spent yet
    spent = client.spent_run()
    assert spent == pytest.approx(0.0002)  # 1000 in * $0.10 + 200 out * $0.50, per million
    rig.budget(run=spent + 0.0005)
    fresh = rig.client()  # a new process: reads costs.json
    assert fresh.spent_run() == pytest.approx(spent)
    with pytest.raises(ApiRefused, match=r"max_usd_per_run"):
        rig.call(fresh)
    assert len(rig.costs().records) == 2  # the first call and the refusal


def test_unreadable_ledger_refuses_instead_of_guessing(rig: Rig) -> None:
    path = rig.project / "runs" / "_ledger" / "costs.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"day": "2026-10-12", "booked_usd": 0.1}\nnot json\n', encoding="utf-8")
    fake = rig.script([answer()])
    with pytest.raises(ApiRefused, match="line 2 is unreadable"):
        rig.call()
    assert fake.create_calls == []


def test_unreadable_costs_json_refuses(rig: Rig) -> None:
    (rig.run_dir / "costs.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ApiRefused, match="unreadable"):
        rig.client()


def test_prompt_over_the_price_tier_is_refused(rig: Rig) -> None:
    fake = rig.script([answer()], count=95_000)  # x1.1 = 104,500 > 100,000
    with pytest.raises(ApiRefused, match="over the 100000"):
        rig.call()
    assert fake.create_calls == []


def test_model_without_a_price_is_refused(rig: Rig) -> None:
    rig.app = rig.app.model_copy(update={"pricing": PricingConfig()})
    fake = rig.script([answer()])
    with pytest.raises(ApiRefused, match="no price for model"):
        rig.call()
    assert fake.create_calls == [] and rig.keys == []


def test_counting_falls_back_to_characters_over_three(rig: Rig) -> None:
    fake = rig.script([answer()], count=status_error(500))
    _, record = rig.call(system="s" * 300, user="u" * 600)
    assert len(fake.count_calls) == 1
    assert record.estimate_source == "chars_div_3"
    schema_chars = len(json.dumps(fake.create_calls[0]["output_config"]))
    assert record.estimated_input_tokens == math.ceil((900 + schema_chars) / 3)


def test_counting_rejects_a_bad_key_without_a_paid_call(rig: Rig) -> None:
    fake = rig.script([answer()], count=status_error(401, error_type="authentication_error"))
    with pytest.raises(ApiCallFailed, match="API key"):
        rig.call()
    assert fake.create_calls == []


# --- cost log ------------------------------------------------------------------------------------


def test_success_is_recorded_in_costs_json_and_the_ledger(rig: Rig) -> None:
    rig.script([answer(input_tokens=2000, output_tokens=500, thinking_tokens=50)], count=1800)
    parsed, record = rig.call(purpose="find_moments:block-2")
    assert parsed.moments[0].type == "story"

    expected = (2000 * 0.10 + 500 * 0.50) / 1e6
    worst = (math.ceil(1800 * 1.1) * 0.10 + 2000 * 0.50) / 1e6
    assert record.actual_usd == pytest.approx(expected)
    assert record.estimated_usd == pytest.approx(worst)
    assert record.estimated_usd >= record.actual_usd

    costs = rig.costs()
    assert costs.records == [record]
    saved = costs.records[0]
    assert saved.purpose == "find_moments:block-2"
    assert saved.model == HAIKU
    assert (saved.input_tokens, saved.output_tokens, saved.thinking_tokens) == (2000, 500, 50)
    assert (saved.cache_creation_tokens, saved.cache_read_tokens) == (0, 0)
    assert saved.request_id == "req_ok"
    assert saved.stop_reason == "end_turn"
    assert saved.duration_seconds >= 0
    assert saved.estimate_source == "count_tokens"
    assert saved.status == "ok" and saved.attempts == 1
    totals = costs.totals
    assert (totals.calls, totals.refusals, totals.input_tokens) == (1, 0, 2000)
    assert totals.actual_usd == pytest.approx(expected)

    (line,) = rig.ledger()
    assert line["day"] == "2026-10-12"
    assert line["run"] == rig.run_dir.name
    assert line["booked_usd"] == pytest.approx(expected)
    assert line["request_id"] == "req_ok"
    assert "SYSTEM TEXT" not in json.dumps(line) and "USER TEXT" not in json.dumps(line)


def test_request_shape(rig: Rig) -> None:
    fake = rig.script([answer()])
    rig.call(thinking="disabled", effort="low")
    request = fake.create_calls[0]
    assert request["model"] == HAIKU
    assert request["max_tokens"] == 2000
    assert request["system"] == "SYSTEM TEXT"
    assert request["messages"] == [{"role": "user", "content": "USER TEXT"}]
    assert request["thinking"] == {"type": "disabled"}
    config = request["output_config"]
    assert config["effort"] == "low"
    assert config["format"]["type"] == "json_schema"
    assert config["format"]["schema"]["additionalProperties"] is False
    # the free count sees the same request, minus max_tokens
    counted = fake.count_calls[0]
    assert "max_tokens" not in counted
    assert counted["output_config"] == config and counted["thinking"] == request["thinking"]


def test_thinking_and_effort_are_left_out_when_not_given(rig: Rig) -> None:
    fake = rig.script([answer()])
    rig.call()
    request = fake.create_calls[0]
    assert "thinking" not in request and "effort" not in request["output_config"]


def test_schema_sent_to_the_api_has_no_unsupported_constraints(rig: Rig) -> None:
    schema = anthropic_schema()
    text = json.dumps(schema)
    for banned in ("minimum", "maximum", "minLength", "maxLength", "multipleOf", "pattern"):
        assert banned not in text
    objects = [schema, *schema["$defs"].values()]
    assert all(o["additionalProperties"] is False for o in objects)
    assert set(schema["$defs"]["FoundMoment"]["required"]) == {
        "start",
        "end",
        "hook",
        "reason",
        "type",
    }


def anthropic_schema() -> dict[str, Any]:
    return anthropic.transform_schema(FoundMoments)


def test_thinking_tokens_are_recorded(rig: Rig) -> None:
    rig.script([answer(thinking_tokens=123)])
    _, record = rig.call()
    assert record.thinking_tokens == 123


def test_costs_json_is_only_appended_to(rig: Rig) -> None:
    rig.script([answer(), answer()])
    rig.call(rig.client(), purpose="a")
    rig.call(rig.client(), purpose="b")
    assert [r.purpose for r in rig.costs().records] == ["a", "b"]
    assert len(rig.ledger()) == 2


def test_session_records_cover_only_this_client(rig: Rig) -> None:
    rig.script([answer(), answer()])
    rig.call(rig.client(), purpose="old")
    client = rig.client()
    rig.call(client, purpose="new")
    assert [r.purpose for r in client.session_records] == ["new"]
    assert [r.purpose for r in client.records] == ["old", "new"]
    assert client.session_totals().calls == 1


# --- invalid model output ------------------------------------------------------------------------


def test_invalid_output_gets_one_retry_naming_the_problem(rig: Rig) -> None:
    fake = rig.script([answer('{"moments": "nope"}'), answer()])
    parsed, record = rig.call()
    assert parsed.moments
    assert len(fake.create_calls) == 2
    first, second = (c["messages"][0]["content"] for c in fake.create_calls)
    assert first == "USER TEXT"
    assert second.startswith("USER TEXT") and "rejected" in second and "moments" in second
    assert "nope" not in second  # the bad answer itself is not echoed back
    assert [r.purpose for r in rig.costs().records] == [
        "find_moments:block-1",
        "find_moments:block-1:repair",
    ]
    assert record.purpose.endswith(":repair")


def test_a_validator_failure_is_named_with_its_message_in_the_repair(rig: Rig) -> None:
    # Brief 12.1 fix: "value_error" alone gave the model nothing to fix; the validator's own
    # words go into the repair note (never the offending value, which pydantic keeps out of msg).
    from pydantic import ValidationError

    from clipmachine.models import ScoreBatch

    bad = {
        "clips": [
            {
                "id": "c01",
                "score": 7,
                "verdict": "keep",
                "reject_reason": None,
                "hook_line": "",
                "suggested_title": "SECRET TITLE",
                "reason": "r",
                "keep_sentences": None,
                "topic": None,
                "caption": None,
            }
        ]
    }
    with pytest.raises(ValidationError) as caught:
        ScoreBatch.model_validate(bad)
    problem = claude._summarise_problem(caught.value)
    assert problem == (
        "clips.0: value_error (Value error, hook_line and suggested_title must not be empty)"
    )
    assert "SECRET TITLE" not in problem


def test_invalid_output_twice_fails_the_call_and_books_both(rig: Rig) -> None:
    fake = rig.script([answer("not json"), answer('{"moments": [{"start": 1}]}')])
    with pytest.raises(InvalidModelOutput, match="still invalid"):
        rig.call()
    assert len(fake.create_calls) == 2
    assert len(rig.ledger()) == 2  # both calls cost money and are on the ledger
    assert rig.costs().totals.calls == 2


def test_check_failure_uses_the_one_repair_call(rig: Rig) -> None:
    seen: list[int] = []

    def check(parsed: FoundMoments) -> str | None:
        seen.append(len(parsed.moments))
        return "missing ids: c03" if len(seen) == 1 else None

    fake = rig.script([answer(), answer()])
    parsed, record = rig.call(check=check)
    assert parsed.moments and record.purpose.endswith(":repair")
    assert len(fake.create_calls) == 2
    second = fake.create_calls[1]["messages"][0]["content"]
    assert "rejected" in second and "missing ids: c03" in second


def test_check_failing_twice_raises_and_books_both(rig: Rig) -> None:
    fake = rig.script([answer(), answer()])
    with pytest.raises(InvalidModelOutput, match="still invalid after one retry: bad ids"):
        rig.call(check=lambda parsed: "bad ids")
    assert len(fake.create_calls) == 2
    assert rig.costs().totals.calls == 2


def test_a_soft_check_asks_for_the_repair_but_never_fails_the_call(rig: Rig) -> None:
    fake = rig.script([answer(), answer()])
    parsed, record = rig.call(soft_check=lambda parsed: "hook too long")
    assert parsed.moments and record.purpose.endswith(":repair")  # the repair's answer, taken
    assert "hook too long" in fake.create_calls[1]["messages"][0]["content"]
    # the first answer fails the schema: the repair is the last answer and is taken as it is
    fake = rig.script([answer("not json"), answer()])
    parsed, _ = rig.call(soft_check=lambda parsed: "hook too long")
    assert parsed.moments and len(fake.create_calls) == 2
    # a strict problem still fails, and the soft one rides along in the repair
    fake = rig.script([answer(), answer()])
    with pytest.raises(InvalidModelOutput, match="still invalid after one retry: bad ids$"):
        rig.call(check=lambda parsed: "bad ids", soft_check=lambda parsed: "hook too long")
    assert "bad ids; hook too long" in fake.create_calls[1]["messages"][0]["content"]
    # the first answer broke only the soft rule and the repair is invalid: the first is taken
    fake = rig.script([answer(), answer("not json")])
    parsed, record = rig.call(soft_check=lambda parsed: "hook too long")
    assert parsed.moments and not record.purpose.endswith(":repair")
    assert len(fake.create_calls) == 2 and rig.costs().totals.calls >= 2


def test_check_that_passes_costs_no_extra_call(rig: Rig) -> None:
    fake = rig.script([answer()])
    rig.call(check=lambda parsed: None)
    assert len(fake.create_calls) == 1


def test_zero_repairs_means_one_call_and_then_failure(rig: Rig) -> None:
    fake = rig.script([answer("not json"), answer()])
    with pytest.raises(InvalidModelOutput, match="was invalid"):
        rig.call(repairs=0)
    assert len(fake.create_calls) == 1


def test_repairs_other_than_zero_or_one_is_refused(rig: Rig) -> None:
    with pytest.raises(ValueError, match="repairs"):
        rig.call(repairs=2)


def test_truncated_or_refused_answers_are_invalid(rig: Rig) -> None:
    rig.script([answer(VALID, stop_reason="max_tokens"), answer(VALID, stop_reason="refusal")])
    with pytest.raises(InvalidModelOutput, match="refused"):
        rig.call()


def test_enum_capitalisation_changes_are_accepted(rig: Rig) -> None:
    text = json.dumps(
        {"moments": [{"start": 1, "end": 2, "hook": "h", "reason": "r", "type": "Story"}]}
    )
    rig.script([answer(text)])
    parsed, _ = rig.call()
    assert parsed.moments[0].type == "story"


# --- the key -------------------------------------------------------------------------------------


def test_key_is_passed_explicitly_and_the_environment_key_is_never_used(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ENV-KEY-MUST-NOT-BE-USED")
    rig.call()
    assert rig.keys == [FAKE_KEY]
    import os

    assert os.environ["ANTHROPIC_API_KEY"] == "ENV-KEY-MUST-NOT-BE-USED"  # untouched


def test_environment_key_is_not_a_fallback(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    (rig.project / ".env").write_text("OTHER=1\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ENV-KEY-MUST-NOT-BE-USED")
    monkeypatch.setenv("CLIPMACHINE_ANTHROPIC_API_KEY", "PROCESS-ENV-IS-NOT-USED-EITHER")
    with pytest.raises(ApiRefused, match="missing or empty"):
        rig.call()
    assert rig.keys == []


def test_missing_env_file_is_a_clear_refusal(rig: Rig) -> None:
    (rig.project / ".env").unlink()
    with pytest.raises(ApiRefused, match=r"\.env\.example"):
        rig.call()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("CLIPMACHINE_ANTHROPIC_API_KEY=abc", "abc"),
        ("  export CLIPMACHINE_ANTHROPIC_API_KEY = abc  ", "abc"),
        ('CLIPMACHINE_ANTHROPIC_API_KEY="abc def"', "abc def"),
        ("CLIPMACHINE_ANTHROPIC_API_KEY='abc'", "abc"),
        ("CLIPMACHINE_ANTHROPIC_API_KEY=abc # my key", "abc"),
        ("# CLIPMACHINE_ANTHROPIC_API_KEY=commented\nX=1", None),
        ("CLIPMACHINE_ANTHROPIC_API_KEY_OLD=abc", None),
        ("CLIPMACHINE_ANTHROPIC_API_KEY=", ""),
        ("nothing here", None),
    ],
)
def test_parse_env_value(text: str, expected: str | None) -> None:
    assert claude.parse_env_value(text, "CLIPMACHINE_ANTHROPIC_API_KEY") == expected


def test_key_with_spaces_is_rejected_without_echoing_it(rig: Rig) -> None:
    (rig.project / ".env").write_text(
        'CLIPMACHINE_ANTHROPIC_API_KEY="two words SECRETPART"\n', encoding="utf-8"
    )
    with pytest.raises(ApiRefused) as caught:
        rig.call()
    assert "SECRETPART" not in str(caught.value)


def test_the_key_never_appears_in_logs_exceptions_or_files(
    rig: Rig, project: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def attempt(outcomes: list[Any], **kwargs: Any) -> None:
        rig.script(outcomes, **kwargs)
        try:
            rig.call(rig.client())
        except Exception as exc:
            seen.append(f"{type(exc).__name__}: {exc}")
            seen.append(repr(exc))

    with caplog.at_level(logging.DEBUG):
        attempt([answer()])
        attempt([status_error(401, f"bad key {FAKE_KEY}", error_type="authentication_error")])
        attempt([status_error(400, f"echo {FAKE_KEY}", error_type="invalid_request_error")])
        attempt([network_error()])
        attempt([answer("garbage"), answer("garbage")])
        attempt([status_error(500), status_error(500), status_error(500)])
        rig.budget(run=0.000001)
        attempt([answer()])
        monkeypatch.setenv(claude.KILL_SWITCH_ENV, "1")
        attempt([answer()])

    assert any("ApiRefused" in s for s in seen) and any("ApiCallFailed" in s for s in seen)
    blobs = [caplog.text, *seen]
    for path in project.rglob("*"):
        if path.is_file() and path.name != ".env":
            blobs.append(path.read_bytes().decode("utf-8", errors="replace"))
    assert len(blobs) > 8
    for blob in blobs:
        assert FAKE_KEY not in blob
        assert "must-never-leak" not in blob


def test_prompts_are_not_logged_or_stored(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        rig.call(system="SECRET SYSTEM", user="SECRET TRANSCRIPT WORDS")
    assert "SECRET" not in caplog.text
    for path in rig.project.rglob("*"):
        if path.is_file() and path.name != ".env":
            assert b"SECRET TRANSCRIPT" not in path.read_bytes()


# --- the real SDK client factory (no network is used to build it) --------------------------------


def test_real_client_factory_has_no_sdk_retries_and_ignores_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ENV-KEY-MUST-NOT-BE-USED")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.invalid")
    client = REAL_NEW_CLIENT("explicit-key")
    assert client.max_retries == 0
    assert client.api_key == "explicit-key"
    assert str(client.base_url).rstrip("/") == "https://api.anthropic.com"


def test_the_guard_stops_tests_from_building_the_real_client() -> None:
    with pytest.raises(AssertionError, match="fake claude.new_client"):
        claude.new_client("k")


def test_record_model_roundtrips() -> None:
    record = CostRecord(time="t", purpose="p", model="m", status="ok")
    assert CostRecord.model_validate(record.model_dump(mode="json")) == record
