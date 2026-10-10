from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml


def edit_yaml(path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
