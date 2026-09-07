"""Atomic JSON writer for the machine-readable market-state snapshot."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


def _sanitize(value: Any) -> Any:
    """JSON has no NaN/Infinity; map them to null so consumers never choke."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


def write_json(path: str | Path, payload: dict, indent: int | None = 2) -> Path:
    """Write ``payload`` atomically (tmp file + rename). Returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_sanitize(payload), indent=indent, sort_keys=False) + "\n",
                   encoding="utf-8")
    tmp.replace(path)
    return path
