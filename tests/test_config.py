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
from clipmachine.models import (
    CaptionsConfig,
    HookConfig,
    RenderedClip,
    RenderedSettings,
    SafeConfig,
    SnapConfig,
    SpeakerConfig,
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
    assert resolved.app.clip.max_seconds == 90


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
        (lambda d: d["candidates"].update(preferred_max_seconds=100), "preferred"),  # > max
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
        (lambda d: d["render"].update(crf=52), "render.crf"),
        (lambda d: d["render"].update(preset="turbo"), "render.preset"),
        (lambda d: d["render"].update(audio_bitrate="fast"), "audio_bitrate"),
        (lambda d: d["render"].update(width=1080), "render.width"),  # size lives in video:
        (lambda d: d["captions"].update(font_file="../x.ttf"), "font_file"),
        (lambda d: d["captions"].update(font_file="C:/fonts/a.ttf"), "font_file"),
        (lambda d: d["captions"].update(font_file="font.exe"), "font_file"),
        (lambda d: d["captions"].update(font_size=0), "font_size"),
        (lambda d: d["captions"].update(margin_v=-1), "margin_v"),
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
    # a client range that is entirely outside the default 20-90 range still wins
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
    assert (resolved.app.clip.min_seconds, resolved.app.clip.max_seconds) == (20, 90)
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


def test_render_and_caption_defaults(project: Path) -> None:
    app = load_app_config(project)
    assert (app.render.crf, app.render.preset, app.render.audio_bitrate) == (20, "medium", "160k")
    assert (app.video.width, app.video.height) == (1080, 1920)
    captions = app.captions
    assert (captions.font_file, captions.font_size, captions.margin_v) == (
        "Montserrat-Bold.ttf",
        96,
        350,
    )


def test_config_without_render_keys_still_loads(project: Path) -> None:
    def drop(d: dict[str, Any]) -> None:
        d.pop("render")
        for key in ("font_file", "font_size", "margin_v"):
            d["captions"].pop(key)

    _default(project, drop)
    app = load_app_config(project)
    assert app.render.crf == 20
    assert app.captions.font_size == 80


def test_caption_offset_default_and_valid_values(project: Path) -> None:
    assert load_app_config(project).captions.offset_seconds == 0.15
    for value in (-1.0, -0.25, 0.15, 1.0):
        _default(project, lambda d, v=value: d["captions"].update(offset_seconds=v))
        assert load_app_config(project).captions.offset_seconds == value


@pytest.mark.parametrize("value", [1.5, -1.01, 1.0001])
def test_caption_offset_out_of_range_is_rejected(project: Path, value: float) -> None:
    _default(project, lambda d: d["captions"].update(offset_seconds=value))
    with pytest.raises(ConfigError, match="offset_seconds"):
        load_app_config(project)


def test_config_without_offset_loads(project: Path) -> None:
    _default(project, lambda d: d["captions"].pop("offset_seconds"))
    assert load_app_config(project).captions.offset_seconds == 0.15


def test_audio_fade_default_and_range(project: Path) -> None:
    assert load_app_config(project).render.audio_fade_seconds == 0.08
    data = yaml.safe_load((project / "config" / "default.yaml").read_text(encoding="utf-8"))
    assert data["render"]["audio_fade_seconds"] == 0.08
    for value in (0, 0.25, 0.5):
        _default(project, lambda d, v=value: d["render"].update(audio_fade_seconds=v))
        assert load_app_config(project).render.audio_fade_seconds == value
    for bad in (-0.01, 0.51):
        _default(project, lambda d, v=bad: d["render"].update(audio_fade_seconds=v))
        with pytest.raises(ConfigError, match="audio_fade_seconds"):
            load_app_config(project)


def test_resolved_file_from_before_the_fade_still_loads(project: Path, tmp_path: Path) -> None:
    target = tmp_path / "config.resolved.yaml"
    dump_resolved(load_resolved(project, "example-client"), target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    del data["config"]["render"]["audio_fade_seconds"]
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_resolved_file(target).app.render.audio_fade_seconds == 0.08


def test_extend_defaults_and_config_without_them_loads(project: Path) -> None:
    assert load_app_config(project).score.extend_before == 4
    assert load_app_config(project).score.extend_after == 3
    _default(project, lambda d: d["score"].pop("extend_before"))
    _default(project, lambda d: d["score"].pop("extend_after"))
    app = load_app_config(project)
    assert (app.score.extend_before, app.score.extend_after) == (4, 3)


@pytest.mark.parametrize("key", ["extend_before", "extend_after"])
@pytest.mark.parametrize("value", [-1, 11])
def test_extend_out_of_range_is_rejected(project: Path, key: str, value: int) -> None:
    _default(project, lambda d: d["score"].update({key: value}))
    with pytest.raises(ConfigError, match=key):
        load_app_config(project)


# --- Brief 8: speaker crop ------------------------------------------------------------------


def test_speaker_section_defaults_match_default_yaml(project: Path) -> None:
    assert load_app_config(project).speaker == SpeakerConfig()


def test_config_without_speaker_section_still_loads(project: Path, tmp_path: Path) -> None:
    _default(project, lambda d: d.pop("speaker"))
    app = load_app_config(project)
    assert app.speaker.sample_fps == 10
    assert app.speaker.debug_preview is False

    target = tmp_path / "config.resolved.yaml"
    dump_resolved(load_resolved(project, "example-client"), target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    del data["config"]["speaker"]
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_resolved_file(target).app.speaker.max_faces == 4


def test_brief_8_1_speaker_defaults() -> None:
    cfg = SpeakerConfig()
    assert (cfg.detect_width, cfg.landmark_width) == (960, 1920)
    assert (cfg.detect_score, cfg.face_crop_scale, cfg.cut_threshold) == (0.7, 2.0, 0.12)


def test_a_speaker_section_from_before_brief_8_1_still_loads(project: Path, tmp_path: Path) -> None:
    old = {"sample_fps": 10, "analysis_width": 640, "max_faces": 4, "min_face_height": 0.08}
    target = tmp_path / "config.resolved.yaml"
    dump_resolved(load_resolved(project, "example-client"), target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    data["config"]["speaker"] = old  # analysis_width, none of the new keys
    target.write_text(yaml.safe_dump(data), encoding="utf-8")

    speaker = load_resolved_file(target).app.speaker

    assert speaker.analysis_width == 640
    assert speaker.landmark_width == 1920 and speaker.cut_threshold == 0.12


def test_shipped_default_config_crops_to_the_speaker() -> None:
    repo_root = Path(__file__).resolve().parents[1]  # the real file, not the fixture's pinned copy
    assert load_app_config(repo_root).video.crop == "speaker"


def test_crop_speaker_is_the_default_and_center_is_accepted(project: Path) -> None:
    # the `project` fixture pins center; take it back to the shipped default first
    _default(project, lambda d: d["video"].update(crop="speaker"))
    assert load_app_config(project).video.crop == "speaker"
    _default(project, lambda d: d["video"].update(crop="center"))
    assert load_app_config(project).video.crop == "center"


def test_crop_face_is_rejected_with_a_clear_message(project: Path) -> None:
    _default(project, lambda d: d["video"].update(crop="face"))
    with pytest.raises(ConfigError, match="video.crop.*never implemented.*speaker"):
        load_app_config(project)


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"sample_fps": 0}, "speaker.sample_fps"),
        ({"sample_fps": 31}, "speaker.sample_fps"),
        ({"max_faces": 0}, "speaker.max_faces"),
        ({"max_faces": 9}, "speaker.max_faces"),
        ({"window_seconds": 0}, "speaker.window_seconds"),
        ({"min_shot_seconds": 0.25}, "min_shot_seconds"),  # below window_seconds
        ({"switch_ratio": 0.9}, "speaker.switch_ratio"),
        ({"confirm_windows": 0}, "speaker.confirm_windows"),
        ({"smoothing": 0}, "speaker.smoothing"),
        ({"dead_zone": 1.5}, "speaker.dead_zone"),
        ({"min_face_height": 0}, "speaker.min_face_height"),
        ({"link_max_distance": 0}, "speaker.link_max_distance"),
        ({"detect_width": 32}, "speaker.detect_width"),
        ({"landmark_width": 32}, "speaker.landmark_width"),
        ({"detect_score": 0}, "speaker.detect_score"),
        ({"detect_score": 1}, "speaker.detect_score"),
        ({"face_crop_scale": 0.5}, "speaker.face_crop_scale"),
        ({"face_crop_scale": 7}, "speaker.face_crop_scale"),
        ({"cut_threshold": 0}, "speaker.cut_threshold"),
        ({"cut_threshold": 1.5}, "speaker.cut_threshold"),
        ({"sample": 10}, "speaker.sample"),  # typo: unknown key
    ],
)
def test_invalid_speaker_settings_are_rejected(
    project: Path, update: dict[str, Any], expected: str
) -> None:
    _default(project, lambda d: d["speaker"].update(update))
    with pytest.raises(ConfigError, match=expected):
        load_app_config(project)


# --- Brief 10: snap section ----------------------------------------------------------------------


def test_snap_defaults_match_default_yaml(project: Path) -> None:
    data = yaml.safe_load((project / "config" / "default.yaml").read_text(encoding="utf-8"))
    assert set(data["snap"]) == {
        "min_pause_seconds",
        "min_cut_gap",
        "max_start_pad_seconds",
        "max_end_pad_seconds",
    }
    assert load_app_config(project).snap == SnapConfig()
    assert SnapConfig(**data["snap"]) == SnapConfig()
    assert (SnapConfig().min_pause_seconds, SnapConfig().min_cut_gap) == (0.35, 0.15)


def test_config_without_snap_section_still_loads(project: Path, tmp_path: Path) -> None:
    _default(project, lambda d: d.pop("snap"))
    assert load_app_config(project).snap == SnapConfig()

    target = tmp_path / "config.resolved.yaml"
    dump_resolved(load_resolved(project, "example-client"), target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    del data["config"]["snap"]
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_resolved_file(target).app.snap == SnapConfig()


def test_min_cut_gap_may_equal_but_not_exceed_min_pause(project: Path) -> None:
    _default(project, lambda d: d["snap"].update(min_pause_seconds=0.3, min_cut_gap=0.3))
    assert load_app_config(project).snap.min_cut_gap == 0.3
    _default(project, lambda d: d["snap"].update(min_pause_seconds=0.3, min_cut_gap=0.31))
    with pytest.raises(ConfigError, match="min_cut_gap"):
        load_app_config(project)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("min_pause_seconds", 0),
        ("min_cut_gap", -0.1),
        ("max_start_pad_seconds", -0.1),
        ("max_end_pad_seconds", -0.1),
    ],
)
def test_invalid_snap_settings_are_rejected(project: Path, key: str, value: float) -> None:
    _default(project, lambda d: d["snap"].update({key: value}))
    with pytest.raises(ConfigError, match=key):
        load_app_config(project)


# --- Brief 8.2: the mix signal -------------------------------------------------------------------


def test_brief_8_2_speaker_defaults() -> None:
    cfg = SpeakerConfig()
    assert (cfg.signal, cfg.min_turn_seconds, cfg.min_turn_motion_seconds) == ("mouth", 1.0, 1.5)
    assert cfg.min_shot_seconds == 1.5  # for signal "mouth", the default


def test_default_yaml_has_the_mix_keys(project: Path) -> None:
    speaker = load_app_config(project).speaker
    assert (speaker.signal, speaker.min_turn_seconds, speaker.min_turn_motion_seconds) == (
        "mouth",
        1.0,
        1.5,
    )


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"signal": "loudness"}, "speaker.signal"),
        ({"min_turn_seconds": 0}, "speaker.min_turn_seconds"),
        ({"min_turn_motion_seconds": 0}, "speaker.min_turn_motion_seconds"),
        ({"min_turn_seconds": 0.25}, "min_turn_seconds"),  # below window_seconds
        ({"min_turn_motion_seconds": 0.25}, "min_turn_motion_seconds"),
    ],
)
def test_invalid_mix_settings_are_rejected(
    project: Path, update: dict[str, Any], expected: str
) -> None:
    _default(project, lambda d: d["speaker"].update(update))
    with pytest.raises(ConfigError, match=expected):
        load_app_config(project)


def test_a_speaker_section_from_before_brief_8_2_still_loads(project: Path, tmp_path: Path) -> None:
    target = tmp_path / "config.resolved.yaml"
    dump_resolved(load_resolved(project, "example-client"), target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    for key in ("signal", "min_turn_seconds", "min_turn_motion_seconds"):
        del data["config"]["speaker"][key]
    target.write_text(yaml.safe_dump(data), encoding="utf-8")

    speaker = load_resolved_file(target).app.speaker

    assert (speaker.signal, speaker.min_turn_seconds, speaker.min_turn_motion_seconds) == (
        "mouth",
        1.0,
        1.5,
    )


# --- Brief 8.2 part 3: the camera glide ----------------------------------------------------------


def test_camera_glide_defaults() -> None:
    cfg = SpeakerConfig()
    assert (cfg.dead_zone, cfg.smooth_seconds, cfg.glide_seconds, cfg.max_speed) == (
        0.15,
        0.5,
        0.6,
        0.8,
    )
    assert SpeakerConfig(smooth_seconds=0).smooth_seconds == 0  # 0 = no smoothing


def test_default_yaml_has_the_glide_keys(project: Path) -> None:
    speaker = load_app_config(project).speaker
    assert (speaker.dead_zone, speaker.smooth_seconds) == (0.15, 0.5)
    assert (speaker.glide_seconds, speaker.max_speed) == (0.6, 0.8)


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"smooth_seconds": -0.1}, "speaker.smooth_seconds"),
        ({"glide_seconds": 0}, "speaker.glide_seconds"),
        ({"max_speed": 0}, "speaker.max_speed"),
        ({"dead_zone": -0.1}, "speaker.dead_zone"),
    ],
)
def test_invalid_glide_settings_are_rejected(
    project: Path, update: dict[str, Any], expected: str
) -> None:
    _default(project, lambda d: d["speaker"].update(update))
    with pytest.raises(ConfigError, match=expected):
        load_app_config(project)


def test_a_speaker_section_from_before_the_glide_keys_still_loads(
    project: Path, tmp_path: Path
) -> None:
    target = tmp_path / "config.resolved.yaml"
    dump_resolved(load_resolved(project, "example-client"), target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    for key in ("smooth_seconds", "glide_seconds", "max_speed"):
        del data["config"]["speaker"][key]
    data["config"]["speaker"]["smoothing"] = 0.25  # the old key is still accepted
    target.write_text(yaml.safe_dump(data), encoding="utf-8")

    speaker = load_resolved_file(target).app.speaker

    assert (speaker.smooth_seconds, speaker.glide_seconds, speaker.max_speed) == (0.5, 0.6, 0.8)
    assert speaker.smoothing == 0.25


# --- Brief R1: captions v2, hook and safe zones ---------------------------------------------------


R1_CAPTION_KEYS = (
    "style",
    "max_words",
    "max_width_px",
    "word_gap_em",
    "letter_spacing_px",
    "line_gap_em",
    "spoken_color",
    "upcoming_color",
    "box_opacity",
    "box_padding_px",
)


def _without_r1_keys(d: dict[str, Any]) -> None:
    for key in R1_CAPTION_KEYS:
        d["captions"].pop(key)
    del d["hook"], d["safe"]


def test_the_shipped_default_is_box_karaoke_with_the_hook_on(project: Path) -> None:
    data = yaml.safe_load((project / "config" / "default.yaml").read_text(encoding="utf-8"))
    assert set(R1_CAPTION_KEYS) <= set(data["captions"])
    assert set(data["hook"]) == set(HookConfig.model_fields)
    assert set(data["safe"]) == set(SafeConfig.model_fields)
    app = load_app_config(project)
    assert (app.captions.style, app.captions.font_size) == ("box_karaoke", 96)
    assert app.hook == HookConfig(enabled=True)
    assert app.safe == SafeConfig()
    assert app.captions.model_dump(include=set(R1_CAPTION_KEYS)) == CaptionsConfig(
        words_per_line=1, font="x", highlight_color="#FFD400", style="box_karaoke"
    ).model_dump(include=set(R1_CAPTION_KEYS))  # the shipped values are the model's defaults


def test_config_without_the_r1_keys_loads_as_legacy(project: Path) -> None:
    _default(project, _without_r1_keys)
    app = load_app_config(project)
    assert app.captions.style == "legacy"
    assert (app.captions.max_words, app.captions.max_width_px) == (4, 760)
    assert (app.captions.word_gap_em, app.captions.letter_spacing_px) == (0.45, 1.0)
    assert (app.captions.spoken_color, app.captions.upcoming_color) == ("#FFD400", "#FFFFFF")
    assert (app.captions.box_opacity, app.captions.box_padding_px) == (0.75, 18)
    assert app.hook == HookConfig() and app.hook.enabled is False
    assert (app.safe.top, app.safe.bottom, app.safe.left, app.safe.right) == (290, 560, 120, 192)


def _box_karaoke(d: dict[str, Any]) -> None:
    d["captions"]["style"] = "box_karaoke"


def test_box_karaoke_with_the_default_margins_is_valid(project: Path) -> None:
    _default(project, _box_karaoke)
    _default(project, lambda d: d.update(hook={"enabled": True}))
    app = load_app_config(project)
    assert app.captions.style == "box_karaoke" and app.hook.enabled is True


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d["captions"].update(spoken_color="FFD400"), "spoken_color"),
        (lambda d: d["captions"].update(upcoming_color="#FFF"), "upcoming_color"),
        (lambda d: d["captions"].update(style="neon"), "captions.style"),
        (lambda d: d["captions"].update(max_words=0), "captions.max_words"),
        (lambda d: d["captions"].update(max_words=13), "captions.max_words"),
        (lambda d: d["captions"].update(max_width_px=0), "captions.max_width_px"),
        (lambda d: d["captions"].update(word_gap_em=0.05), "captions.word_gap_em"),
        (lambda d: d["captions"].update(letter_spacing_px=-1), "captions.letter_spacing_px"),
        (lambda d: d["captions"].update(line_gap_em=3), "captions.line_gap_em"),
        (lambda d: d["captions"].update(box_opacity=1.5), "captions.box_opacity"),
        (lambda d: d["captions"].update(box_padding_px=101), "captions.box_padding_px"),
        (lambda d: d.update(hook={"seconds": 0}), "hook.seconds"),
        (lambda d: d.update(hook={"fade_ms": -1}), "hook.fade_ms"),
        (lambda d: d.update(hook={"enable": True}), "hook.enable"),  # typo: unknown key
        (lambda d: d.update(safe={"top": -1}), "safe.top"),
        # margins that leave no room
        (lambda d: d.update(safe={"left": 600, "right": 480}), "safe.left"),
        (lambda d: d.update(safe={"top": 1000, "bottom": 920}), "safe.top"),
        # hook needs the new caption style
        (lambda d: d["captions"].update(style="legacy"), "hook.enabled"),  # the hook is on
    ],
)
def test_invalid_r1_settings_are_rejected(
    project: Path, mutate: Callable[[dict[str, Any]], None], expected: str
) -> None:
    _default(project, mutate)
    with pytest.raises(ConfigError, match=expected):
        load_app_config(project)


@pytest.mark.parametrize(
    ("captions", "expected"),
    [
        ({"max_width_px": 769}, "safe width"),  # 1080 - 120 - 192 = 768
        ({"max_width_px": 36}, "box_padding_px"),  # not more than twice the padding
    ],
)
def test_box_karaoke_width_must_fit_the_safe_area(
    project: Path, captions: dict[str, Any], expected: str
) -> None:
    _default(project, _box_karaoke)
    _default(project, lambda d: d["captions"].update(captions))
    with pytest.raises(ConfigError, match=expected):
        load_app_config(project)


def test_legacy_style_ignores_the_box_width(project: Path) -> None:
    _default(project, lambda d: d["captions"].update(max_width_px=5000, style="legacy"))
    _default(project, lambda d: d["hook"].update(enabled=False))
    assert load_app_config(project).captions.max_width_px == 5000


def test_resolved_file_from_before_r1_still_loads(project: Path, tmp_path: Path) -> None:
    target = tmp_path / "config.resolved.yaml"
    dump_resolved(load_resolved(project, "example-client"), target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    for key in ("hook", "safe"):
        del data["config"][key]
    for key in CaptionsConfig.model_fields.keys() - {
        "words_per_line",
        "font",
        "highlight_color",
        "font_file",
        "font_size",
        "margin_v",
        "offset_seconds",
    }:
        del data["config"]["captions"][key]
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    app = load_resolved_file(target).app
    assert app.captions.style == "legacy" and app.hook.enabled is False
    assert app.safe == SafeConfig()


def test_rendered_settings_and_clip_json_from_before_r1_still_load() -> None:
    old_settings = {
        "width": 1080,
        "height": 1920,
        "crf": 20,
        "preset": "medium",
        "audio_bitrate": "160k",
        "font": "Montserrat",
        "font_file": "Montserrat-Bold.ttf",
        "font_size": 80,
        "margin_v": 350,
        "words_per_line": 3,
        "highlight_color": "#FFD400",
    }
    settings = RenderedSettings.model_validate(old_settings)
    assert settings.caption_style == "legacy" and settings.hook_enabled is False
    assert (settings.safe_top, settings.safe_bottom, settings.max_words) == (0, 0, 0)
    assert settings.spoken_color == "" and settings.box_opacity == 0.0

    old_clip = {
        "id": "c01",
        "rank": 1,
        "score": 8,
        "start": 1.0,
        "end": 31.0,
        "duration": 30.0,
        "source": {
            "width": 1920,
            "height": 1080,
            "rotation": 0,
            "r_frame_rate": "30/1",
            "avg_frame_rate": "30/1",
            "variable_frame_rate": False,
        },
        "crop": {"w": 608, "h": 1080, "x": 656, "y": 0},
        "settings": old_settings,
        "caption_events": 12,
        "output": {
            "width": 1080,
            "height": 1920,
            "video_codec": "h264",
            "audio_codec": "aac",
            "duration_seconds": 30.0,
            "size_bytes": 1000,
        },
        "render_seconds": 4.0,
    }
    clip = RenderedClip.model_validate(old_clip)
    assert clip.notes == [] and clip.caption_lines == 0 and clip.caption_max_words == 0
    assert (clip.hook_shown, clip.hook_moved, clip.outside_safe_zone) == (False, False, 0)
