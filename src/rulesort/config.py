from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Config, ConfigurationError


def load_config(path: Path) -> Config:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    return Config.from_dict(raw, path.resolve().parent)
