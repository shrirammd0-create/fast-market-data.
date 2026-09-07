"""Small JSON file cache with TTL, for carrying state between cron runs.

Used to persist things like the previous snapshot's close (so run-over-run
change can be reported) without any database dependency. Corrupt cache files
are treated as a miss, never as a crash.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class JsonCache:
    def __init__(self, cache_dir: str | Path = ".cache"):
        self.cache_dir = Path(cache_dir)

    def _path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return self.cache_dir / f"{safe}.json"

    def get(self, key: str, max_age_seconds: float | None = None) -> Any | None:
        path = self._path(key)
        try:
            raw = path.read_text(encoding="utf-8")
            entry = json.loads(raw)
            if max_age_seconds is not None and time.time() - entry["ts"] > max_age_seconds:
                return None
            return entry["value"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def set(self, key: str, value: Any) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "value": value}), encoding="utf-8")
        tmp.replace(path)
