from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from clipmachine.config import (
    ConfigError,
    dump_resolved,
    load_app_config,
    load_resolved,
    load_resolved_file,
    validate_client_name,
)
from helpers import edit_yaml


def _default(project: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    edit_yaml(project / "config" / "default.yaml", mutate)


def test_real_config_loads(project: Path) -> None:
    resolved = load_resolved(project, "example-client")
    assert resolved.client_name == "example-client"
    assert resolved.app.clip.keep_top == 8
    assert resolved.app.captions.highlight_color == "#FFD400"
    assert resolved.rules.niche == "fitness / nutrition"


def test_client_clip_length_overrides_defaults(project: Path) -> None:
    resolved = load_resolved(project, "example-client")
    assert resolved.app.clip.min_seconds == 25
    assert resolved.app.clip.max_seconds == 50
    # the other clip settings come from default.yaml untouched
    assert resolved.app.clip.candidates_target == 20


def test_client_without_clip_length_keeps_defaults(project: Path) -> None:
    edit_yaml(
        project / "clients" / "example-client" / "rules.yaml",
        lambda d: d.pop("clip_length"),
    )
    resolved = load_resolved(project, "example-client")
    assert resolved.app.clip.min_seconds == 20
    assert resolved.app.clip.max_seconds == 60


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d["clip"].update(min_seconds=60, max_seconds=20), "min_seconds"),
        (lambda d: d["clip"].update(min_seconds=30, max_seconds=30), "min_seconds"),
        (lambda d: d["clip"].update(keep_top=21), "keep_top"),
        (lambda d: d["clip"].update(min_seconds=0), "clip.min_seconds"),
        (lambda d: d["clip"].update(keep_top=0), "clip.keep_top"),
        (lambda d: d["clip"].update(candidates_target=-1), "candidates_target"),
        (lambda d: d["video"].update(width=0), "video.width"),
        (lambda d: d["video"].update(height=-1080), "video.height"),
        (lambda d: d["captions"].update(words_per_line=0), "words_per_line"),
        (lambda d: d["captions"].update(highlight_color="FFD400"), "highlight_color"),
        (lambda d: d["captions"].update(highlight_color="#FFD40"), "highlight_color"),
        (lambda d: d["captions"].update(highlight_color="#GGGGGG"), "highlight_color"),
        (lambda d: d["clip"].update(min_second=20), "min_second"),  # typo: unknown key
        (lambda d: d["candidates"].update(preferred_min_seconds=10), "preferred"),  # < min
        (lambda d: d["candidates"].update(preferred_max_seconds=70), "preferred"),  # > max
        (
            lambda d: d["candidates"].update(preferred_min_seconds=50, preferred_max_seconds=50),
            "preferred",
        ),
        (
            lambda d: d["candidates"].update(preferred_min_seconds=45, preferred_max_seconds=35),
            "preferred",
        ),
        (lambda d: d["candidates"].pop("preferred_max_seconds"), "set together"),
        (lambda d: d["candidates"].update(fallback_max_words=0), "fallback_max_words"),
        (lambda d: d.update(extra_section={}), "extra_section"),
        (lambda d: d["video"].update(crop="zoom"), "video.crop"),
        (lambda d: d["fetch"].update(max_size_gb=0), "fetch.max_size_gb"),
        (lambda d: d["fetch"].update(max_duration_minutes=-1), "fetch.max_duration_minutes"),
        (lambda d: d["fetch"].update(min_duration_seconds=0), "fetch.min_duration_seconds"),
        (lambda d: d["fetch"].update(max_size_gb="big"), "fetch.max_size_gb"),
        (lambda d: d["fetch"].update(max_size=8), "fetch.max_size"),  # typo: unknown key
        (
            lambda d: d["fetch"].update(min_duration_seconds=600, max_duration_minutes=10),
            "min_duration_seconds",
        ),
    ],
)
def test_default_config_validation_rules(
    project: Path, mutate: Callable[[dict[str, Any]], None], expected: str
) -> None:
    _default(project, mutate)
    with pytest.raises(ConfigError, match=expected):
        load_app_config(project)


def test_preferred_range_in_the_default_config(project: Path) -> None:
    app = load_app_config(project)
    assert (app.candidates.preferred_min_seconds, app.candidates.preferred_max_seconds) == (30, 50)
    assert app.candidates.fallback_max_words == 40
    assert app.preferred_range() == (30, 50)


def test_preferred_range_may_equal_the_clip_range(project: Path) -> None:
    _default(
        project,
        lambda d: d["candidates"].update(preferred_min_seconds=20, preferred_max_seconds=60),
    )
    assert load_app_config(project).preferred_range() == (20, 60)


def test_client_clip_length_clamps_the_preferred_range(project: Path) -> None:
    # example-client is 25-50: 30-50 already fits
    assert load_resolved(project, "example-client").app.preferred_range() == (30, 50)
    edit_yaml(
        project / "clients" / "example-client" / "rules.yaml",
        lambda d: d.update(clip_length={"min": 40, "max": 70}),
    )
    app = load_resolved(project, "example-client").app
    assert (app.candidates.preferred_min_seconds, app.candidates.preferred_max_seconds) == (40, 50)


def test_client_range_outside_the_preferred_range_uses_the_whole_range(project: Path) -> None:
    edit_yaml(
        project / "clients" / "example-client" / "rules.yaml",
        lambda d: d.update(clip_length={"min": 10, "max": 20}),
    )
    app = load_resolved(project, "example-client").app
    assert (app.candidates.preferred_min_seconds, app.candidates.preferred_max_seconds) == (10, 20)


def test_missing_preferred_keys_use_clamped_defaults(project: Path) -> None:
    def drop(d: dict[str, Any]) -> None:
        d["candidates"].pop("preferred_min_seconds")
        d["candidates"].pop("preferred_max_seconds")
        d["candidates"].pop("fallback_max_words")

    _default(project, drop)
    app = load_app_config(project)
    assert app.candidates.preferred_min_seconds is None
    assert app.candidates.fallback_max_words == 40
    assert app.preferred_range() == (30, 50)


def test_resolved_file_from_before_preferred_range_still_loads(
    project: Path, tmp_path: Path
) -> None:
    target = tmp_path / "config.resolved.yaml"
    dump_resolved(load_resolved(project, "example-client"), target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    for key in ("preferred_min_seconds", "preferred_max_seconds", "fallback_max_words"):
        del data["config"]["candidates"][key]
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_resolved_file(target).app.preferred_range() == (30, 50)


def test_fetch_section_values(project: Path) -> None:
    fetch = load_app_config(project).fetch
    assert fetch.max_size_gb == 8
    assert fetch.max_duration_minutes == 240
    assert fetch.min_duration_seconds == 60


def test_missing_fetch_section_uses_defaults(project: Path) -> None:
    _default(project, lambda d: d.pop("fetch"))
    fetch = load_app_config(project).fetch
    assert (fetch.max_size_gb, fetch.max_duration_minutes, fetch.min_duration_seconds) == (
        8,
        240,
        60,
    )


def test_resolved_file_from_before_fetch_section_still_loads(project: Path, tmp_path: Path) -> None:
    target = tmp_path / "config.resolved.yaml"
    dump_resolved(load_resolved(project, "example-client"), target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    del data["config"]["fetch"]
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_resolved_file(target).app.fetch.max_size_gb == 8


def test_resolved_config_carries_fetch_section(project: Path, tmp_path: Path) -> None:
    edit_yaml(project / "config" / "default.yaml", lambda d: d["fetch"].update(max_size_gb=2))
    target = tmp_path / "config.resolved.yaml"
    dump_resolved(load_resolved(project, "example-client"), target)
    assert load_resolved_file(target).app.fetch.max_size_gb == 2


def test_missing_required_key(project: Path) -> None:
    _default(project, lambda d: d["clip"].pop("keep_top"))
    with pytest.raises(ConfigError, match="keep_top"):
        load_app_config(project)


def test_missing_default_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_app_config(tmp_path)


def test_invalid_yaml(project: Path) -> None:
    (project / "config" / "default.yaml").write_text("clip: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_app_config(project)


def test_client_rules_reject_unknown_key(project: Path) -> None:
    edit_yaml(
        project / "clients" / "example-client" / "rules.yaml",
        lambda d: d.update(tonee="oops"),
    )
    with pytest.raises(ConfigError, match="tonee"):
        load_resolved(project, "example-client")


def test_client_clip_length_must_be_ordered(project: Path) -> None:
    edit_yaml(
        project / "clients" / "example-client" / "rules.yaml",
        lambda d: d.update(clip_length={"min": 50, "max": 25}),
    )
    with pytest.raises(ConfigError, match="clip_length"):
        load_resolved(project, "example-client")


def test_client_override_replaces_both_bounds(project: Path) -> None:
    # a client range that is entirely outside the default 20-60 range still wins
    edit_yaml(
        project / "clients" / "example-client" / "rules.yaml",
        lambda d: d.update(clip_length={"min": 10, "max": 20}),
    )
    resolved = load_resolved(project, "example-client")
    assert resolved.app.clip.max_seconds == 20


@pytest.mark.parametrize(
    "name",
    [
        "../evil",
        "..",
        ".",
        "",
        "Example-Client",
        "-leading-dash",
        "has space",
        "slash/inside",
        "back\\slash",
        "a" * 42,
        "example-client\n",
        "ünïcode",
    ],
)
def test_bad_client_names_rejected(project: Path, name: str) -> None:
    with pytest.raises(ConfigError, match="invalid client name"):
        validate_client_name(project, name)


def test_longest_allowed_client_name(project: Path) -> None:
    name = "a" * 41
    (project / "clients" / name).mkdir()
    assert validate_client_name(project, name).name == name


def test_unknown_client_folder(project: Path) -> None:
    with pytest.raises(ConfigError, match="unknown client"):
        validate_client_name(project, "nobody")


def test_resolved_config_roundtrip(project: Path, tmp_path: Path) -> None:
    resolved = load_resolved(project, "example-client")
    target = tmp_path / "config.resolved.yaml"
    dump_resolved(resolved, target)
    again = load_resolved_file(target)
    assert again == resolved


# --- pricing, budget and candidates (Brief 4) ----------------------------------------------------


def test_default_yaml_has_every_new_key(project: Path) -> None:
    from clipmachine.models import BudgetConfig, CandidatesConfig, ScoreConfig

    data = yaml.safe_load((project / "config" / "default.yaml").read_text(encoding="utf-8"))
    assert set(data["budget"]) == set(BudgetConfig.model_fields)
    assert set(data["candidates"]) == set(CandidatesConfig.model_fields)
    assert set(data["score"]) == set(ScoreConfig.model_fields)
    assert set(data["pricing"]) == {"checked", "source", "models"}


def test_default_pricing_and_limits_match_the_brief(project: Path) -> None:
    app = load_app_config(project)
    assert app.models.find == "claude-haiku-5-5"
    assert app.models.score == "claude-sonnet-5-5"
    haiku = app.pricing.models["claude-haiku-5-5"]
    sonnet = app.pricing.models["claude-sonnet-5-5"]
    assert (haiku.input_per_mtok, haiku.output_per_mtok, haiku.max_prompt_tokens) == (
        0.10,
        0.50,
        100_000,
    )
    assert (sonnet.input_per_mtok, sonnet.output_per_mtok, sonnet.max_prompt_tokens) == (
        2.0,
        10.0,
        None,
    )
    assert app.pricing.checked == "2026-10-10"
    assert app.pricing.source.startswith("https://platform.claude.com/")
    assert (app.budget.max_usd_per_run, app.budget.max_usd_per_day) == (1.0, 5.0)
    cand = app.candidates
    assert (cand.block_minutes, cand.per_block_max, cand.max_output_tokens) == (10, 8, 2000)
    assert (cand.snap_padding_seconds, cand.dedupe_overlap) == (0.2, 0.5)
    assert (cand.thinking, cand.effort) == ("disabled", "low")
    score = app.score
    assert (score.max_per_call, score.max_output_tokens) == (20, 8000)
    assert (score.thinking, score.effort) == ("between_tools", "medium")
    assert (score.min_score, score.repair_attempts) == (6, 1)


def test_general_client_loads_with_its_clip_length(project: Path) -> None:
    resolved = load_resolved(project, "general")
    assert resolved.rules.niche == "general interviews"
    assert (resolved.app.clip.min_seconds, resolved.app.clip.max_seconds) == (20, 60)
    assert "sponsor reads" in resolved.rules.avoid


def test_resolved_file_from_before_brief_4_still_loads(project: Path, tmp_path: Path) -> None:
    target = tmp_path / "config.resolved.yaml"
    dump_resolved(load_resolved(project, "example-client"), target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    for key in ("pricing", "budget", "candidates", "score"):
        del data["config"][key]
    target.write_text(yaml.safe_dump(data), encoding="utf-8")

    app = load_resolved_file(target).app
    assert app.pricing.models == {}  # no guessed prices: a call is refused instead
    assert app.budget.max_usd_per_run == 1.0
    assert app.candidates.per_block_max == 8  # the model default, no value in the file
    assert (app.score.max_per_call, app.score.thinking) == (20, "between_tools")


@pytest.mark.parametrize(
    ("section", "update", "expected"),
    [
        ("budget", {"max_usd_per_run": 0}, "budget.max_usd_per_run"),
        ("budget", {"max_usd_per_day": -1}, "budget.max_usd_per_day"),
        ("candidates", {"block_minutes": 0}, "candidates.block_minutes"),
        ("candidates", {"per_block_max": 0}, "candidates.per_block_max"),
        ("candidates", {"max_output_tokens": 0}, "candidates.max_output_tokens"),
        ("candidates", {"snap_padding_seconds": -0.1}, "candidates.snap_padding_seconds"),
        ("candidates", {"dedupe_overlap": 1.5}, "candidates.dedupe_overlap"),
        ("candidates", {"thinking": "enabled"}, "candidates.thinking"),
        ("candidates", {"effort": "extreme"}, "candidates.effort"),
        ("candidates", {"blocks": 3}, "candidates.blocks"),  # typo: unknown key
        ("score", {"max_per_call": 0}, "score.max_per_call"),
        ("score", {"max_output_tokens": 0}, "score.max_output_tokens"),
        ("score", {"min_score": 0}, "score.min_score"),
        ("score", {"min_score": 11}, "score.min_score"),
        ("score", {"repair_attempts": 2}, "score.repair_attempts"),
        ("score", {"repair_attempts": -1}, "score.repair_attempts"),
        ("score", {"thinking": "disabled"}, "score.thinking"),  # Sonnet 5.5 rejects it
        ("score", {"effort": "extreme"}, "score.effort"),
        ("score", {"effort": "max"}, "between_tools"),  # not accepted with between_tools
        ("score", {"min": 6}, "score.min"),  # typo: unknown key
    ],
)
def test_invalid_budget_and_candidates_settings_are_rejected(
    project: Path, section: str, update: dict[str, Any], expected: str
) -> None:
    _default(project, lambda d: d[section].update(update))
    with pytest.raises(ConfigError, match=expected):
        load_app_config(project)


def test_price_without_a_positive_rate_is_rejected(project: Path) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["pricing"]["models"]["claude-haiku-5-5"]["output_per_mtok"] = 0

    _default(project, mutate)
    with pytest.raises(ConfigError, match="output_per_mtok"):
        load_app_config(project)


def test_old_resolved_file_keeps_its_own_per_block_max(project: Path, tmp_path: Path) -> None:
    target = tmp_path / "config.resolved.yaml"
    dump_resolved(load_resolved(project, "general"), target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    data["config"]["candidates"]["per_block_max"] = 5  # a run folder made before Brief 5.1
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_resolved_file(target).app.candidates.per_block_max == 5
