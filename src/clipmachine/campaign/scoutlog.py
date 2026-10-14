"""campaigns/<slug>/scout.json (Brief 12): what the scout proposed, for the report and for Igor.

A small module of its own so the report can read it without importing the scout command.
"""

from pathlib import Path

from pydantic import ValidationError

from clipmachine.campaign.models import ScoutFile, ScoutRecord
from clipmachine.config import ConfigError
from clipmachine.runs import write_json_atomic

SCOUT_FILE = "scout.json"


def load_scout_file(folder: Path) -> ScoutFile:
    """The file, or an empty one when there is none. An unreadable file is an error, never
    silently reset: it holds what the scout proposed and why."""
    path = folder / SCOUT_FILE
    if not path.is_file():
        return ScoutFile()
    try:
        return ScoutFile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as exc:
        raise ConfigError(
            f"{SCOUT_FILE} in {folder.name} cannot be read ({type(exc).__name__}); fix it by "
            "hand or remove it"
        ) from exc


def save_scout_file(folder: Path, data: ScoutFile) -> None:
    write_json_atomic(folder / SCOUT_FILE, data.model_dump(mode="json"))


def latest_records(data: ScoutFile) -> dict[str, ScoutRecord]:
    """The newest record per moment id."""
    return {record.id: record for record in data.records}
