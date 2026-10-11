"""Load, validate and merge configuration."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from clipmachine.models import AppConfig, ClientRules, clamp_preferred

CLIENT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")


class ConfigError(Exception):
    """A configuration problem with a message fit to show the user."""


@dataclass(frozen=True)
class ResolvedConfig:
    app: AppConfig
    client_name: str
    rules: ClientRules


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: expected a mapping at the top level")
    return data


M = TypeVar("M", bound=BaseModel)


def _validate(model: type[M], data: dict[str, Any], source: str) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            where = ".".join(str(p) for p in err["loc"])
            msg = err["msg"].removeprefix("Value error, ")
            problems.append(f"{where}: {msg}" if where else msg)
        raise ConfigError(f"{source}: " + "; ".join(problems)) from exc


def validate_client_name(root: Path, name: str) -> Path:
    """Return the client's folder, or raise ConfigError."""
    if not CLIENT_NAME_RE.fullmatch(name):
        raise ConfigError(
            f"invalid client name {name!r}: use lowercase letters, digits and '-', "
            "starting with a letter or digit, at most 41 characters"
        )
    folder = root / "clients" / name
    if not folder.is_dir():
        raise ConfigError(f"unknown client {name!r}: no folder {folder}")
    return folder


def load_app_config(root: Path) -> AppConfig:
    path = root / "config" / "default.yaml"
    return _validate(AppConfig, _read_yaml(path), "config/default.yaml")


def load_client_rules(root: Path, name: str) -> ClientRules:
    folder = validate_client_name(root, name)
    return _validate(ClientRules, _read_yaml(folder / "rules.yaml"), f"clients/{name}/rules.yaml")


def merge_client(app: AppConfig, rules: ClientRules) -> AppConfig:
    """Client clip_length.min/max override clip.min_seconds/max_seconds. The preferred range is
    clamped into the new range, so a client's length never makes the default preferred range
    invalid."""
    if rules.clip_length is None:
        return app
    data = app.model_dump()
    data["clip"]["min_seconds"] = rules.clip_length.min
    data["clip"]["max_seconds"] = rules.clip_length.max
    preferred_min, preferred_max = app.preferred_range()
    low, high = clamp_preferred(
        preferred_min, preferred_max, rules.clip_length.min, rules.clip_length.max
    )
    data["candidates"]["preferred_min_seconds"] = low
    data["candidates"]["preferred_max_seconds"] = high
    return _validate(AppConfig, data, f"config merged with client {rules.name!r}")


def load_resolved(root: Path, client: str) -> ResolvedConfig:
    rules = load_client_rules(root, client)
    app = merge_client(load_app_config(root), rules)
    return ResolvedConfig(app=app, client_name=client, rules=rules)


def resolved_to_dict(resolved: ResolvedConfig) -> dict[str, Any]:
    return {
        "config": resolved.app.model_dump(mode="json"),
        "client": {
            "name": resolved.client_name,
            "rules": resolved.rules.model_dump(mode="json"),
        },
    }


def dump_resolved(resolved: ResolvedConfig, path: Path) -> None:
    text = yaml.safe_dump(resolved_to_dict(resolved), sort_keys=False, allow_unicode=True)
    path.write_text(text, encoding="utf-8")


def load_resolved_file(path: Path) -> ResolvedConfig:
    """Read a config.resolved.yaml written by dump_resolved (already merged)."""
    data = _read_yaml(path)
    client = data.get("client")
    if not isinstance(client, dict) or "config" not in data:
        raise ConfigError(f"{path.name}: not a resolved config file")
    app = _validate(AppConfig, data["config"], path.name)
    rules = _validate(ClientRules, client.get("rules", {}), path.name)
    return ResolvedConfig(app=app, client_name=str(client.get("name", "")), rules=rules)
