"""The score answer shape: wire schema limits and our own validation."""

import json
from typing import Any

import anthropic
import pytest
from pydantic import ValidationError

from clipmachine.models import REJECT_REASONS, ClipScore, ScoreBatch

BANNED = {"minimum", "maximum", "minLength", "maxLength", "multipleOf", "maxItems"}


def clip(**update: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "c01",
        "score": 8,
        "verdict": "keep",
        "reject_reason": None,
        "hook_line": "A hook",
        "suggested_title": "A title",
        "reason": "because",
        "keep_sentences": None,
    }
    data.update(update)
    return data


def walk(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(walk(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(walk(value))
    return found


def test_wire_schema_stays_inside_the_documented_limits() -> None:
    schema = anthropic.transform_schema(ScoreBatch)
    nodes = walk(schema)
    for node in nodes:
        assert not BANNED & node.keys()
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["properties"]) == set(node["required"])  # nothing optional
    unions = sum(1 for n in nodes if "anyOf" in n)
    assert unions == 2  # reject_reason and keep_sentences; the API allows 16
    assert set(REJECT_REASONS) == {
        "no_payoff",
        "needs_context",
        "weak_hook",
        "too_long_or_rambling",
        "off_brand",
        "below_threshold",
    }
    assert json.dumps(schema).count("below_threshold") == 1


def test_valid_keep_and_reject() -> None:
    assert ClipScore.model_validate(clip()).verdict == "keep"
    rejected = clip(verdict="reject", reject_reason="weak_hook", score=3)
    assert ClipScore.model_validate(rejected).reject_reason == "weak_hook"


def test_enums_are_case_insensitive() -> None:
    parsed = ClipScore.model_validate(clip(verdict=" REJECT ", reject_reason="No_Payoff", score=2))
    assert (parsed.verdict, parsed.reject_reason) == ("reject", "no_payoff")


@pytest.mark.parametrize("score", [0, 11, -3])
def test_score_outside_1_to_10_is_invalid(score: int) -> None:
    with pytest.raises(ValidationError):
        ClipScore.model_validate(clip(score=score))


@pytest.mark.parametrize(
    "update",
    [
        {"verdict": "keep", "reject_reason": "weak_hook"},
        {"verdict": "reject", "reject_reason": None},
        {"reject_reason": "made_up", "verdict": "reject"},
        {"hook_line": "  "},
        {"suggested_title": ""},
        {"unexpected": 1},
    ],
)
def test_inconsistent_or_unknown_answers_are_invalid(update: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ClipScore.model_validate(clip(**update))
