"""Environment-driven configuration.

Secrets come from environment variables (GitHub Secrets in CI). For local
runs a plain ``.env`` file at the repo root is read if present — a tiny
built-in parser, no python-dotenv dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Candles fetched per granularity for the machine-readable snapshot.
# M1 is also the source of the human-readable footprint (last ``lookback``
# bars of it). OANDA caps a single request at 5000.
DEFAULT_CANDLE_COUNTS: dict[str, int] = {"M1": 120, "M5": 96, "H4": 60}


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return values


def _parse_candle_counts(raw: str | None) -> dict[str, int]:
    """'M1:120,M5:96,H4:60' -> {'M1': 120, ...}; falls back to defaults."""
    if not raw:
        return dict(DEFAULT_CANDLE_COUNTS)
    counts: dict[str, int] = {}
    for part in raw.split(","):
        if ":" not in part:
            continue
        gran, _, count = part.partition(":")
        try:
            counts[gran.strip().upper()] = max(2, min(int(count), 5000))
        except ValueError:
            continue
    return counts or dict(DEFAULT_CANDLE_COUNTS)


@dataclass(frozen=True)
class Settings:
    oanda_api_key: str | None = None
    oanda_env: str = "practice"
    cme_api_url: str | None = None
    cme_api_key: str | None = None
    ibkr_gateway_url: str | None = None
    requests_per_minute: float = 60.0
    output_file: str = "market_updates.txt"
    json_output: str = "data/market_state_snapshot.json"
    cache_dir: str = ".cache"
    # XAU/USD spot commonly quotes in 0.01 increments; the footprint grid
    # buckets at 0.1 to keep 15-minute profiles readable.
    tick_size: float = 0.1
    gold_instrument: str = "XAU_USD"
    index_instruments: tuple[str, ...] = ("SPX500_USD", "NAS100_USD", "US30_USD")
    candle_counts: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_CANDLE_COUNTS))
    # Order/position book buckets kept within ±this % of the current price.
    book_window_pct: float = 5.0
    # Long:short (or short:long) ratio above which a bucket is flagged.
    ofi_ratio_threshold: float = 3.0
    # Spread z-score above which an M1 bar is flagged as a liquidity void.
    spread_void_z: float = 1.5


def load_settings(env: dict[str, str] | None = None) -> Settings:
    merged = dict(_load_dotenv(REPO_ROOT / ".env"))
    merged.update(env if env is not None else os.environ)

    def get(name: str, default: str | None = None) -> str | None:
        value = merged.get(name, default)
        # Secrets pasted into GitHub (or .env) often carry a trailing newline
        # or stray spaces; an API token with whitespace makes an invalid HTTP
        # header, so normalize every value here rather than at each call site.
        if isinstance(value, str):
            value = value.strip()
        return value if value not in ("", None) else default

    return Settings(
        oanda_api_key=get("OANDA_API_KEY"),
        oanda_env=get("OANDA_ENV", "practice") or "practice",
        cme_api_url=get("CME_API_URL"),
        cme_api_key=get("CME_API_KEY"),
        ibkr_gateway_url=get("IBKR_GATEWAY_URL"),
        requests_per_minute=float(get("REQUESTS_PER_MINUTE", "60") or 60),
        output_file=get("OUTPUT_FILE", "market_updates.txt") or "market_updates.txt",
        json_output=get("JSON_OUTPUT", "data/market_state_snapshot.json") or "data/market_state_snapshot.json",
        cache_dir=get("CACHE_DIR", ".cache") or ".cache",
        tick_size=float(get("TICK_SIZE", "0.1") or 0.1),
        gold_instrument=get("GOLD_INSTRUMENT", "XAU_USD") or "XAU_USD",
        candle_counts=_parse_candle_counts(get("CANDLE_COUNTS")),
        book_window_pct=float(get("BOOK_WINDOW_PCT", "5") or 5),
        ofi_ratio_threshold=float(get("OFI_RATIO_THRESHOLD", "3") or 3),
        spread_void_z=float(get("SPREAD_VOID_Z", "1.5") or 1.5),
    )
