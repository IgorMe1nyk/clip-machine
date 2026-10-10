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
