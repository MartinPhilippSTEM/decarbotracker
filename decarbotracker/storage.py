"""Čtení a zápis JSON stavu (Windows-safe: UTF-8, LF, ensure_ascii=False)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from decarbotracker.config import data_dir


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = data.model_dump(mode="json", by_alias=True) if isinstance(data, BaseModel) else data
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def items_path(week: str) -> Path:
    return data_dir() / "items" / f"{week}.json"


def week_path(week: str) -> Path:
    return data_dir() / "weeks" / f"{week}.json"


def seen_path() -> Path:
    return data_dir() / "seen.json"


def health_path() -> Path:
    return data_dir() / "source_health.json"


def briefs_dir() -> Path:
    return data_dir() / "briefs"
