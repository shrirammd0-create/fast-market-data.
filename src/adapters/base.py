"""Adapter contract and normalized market-data types.

All adapters normalize their vendor payloads into ``Candle`` (OHLCV) and
``Trade`` (tape print with optional aggressor flag) so the engine layer never
sees vendor-specific JSON.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float  # tick volume on decentralized feeds, contracts on futures


@dataclass(frozen=True)
class Trade:
    ts: datetime
    price: float
    size: float
    aggressor: str | None = None  # exchange flag, e.g. 'B'/'S'; None if absent


class AdapterError(Exception):
    """Non-transient adapter failure (bad credentials, unknown instrument)."""


class MarketDataAdapter(ABC):
    """A REST market-data source usable in snapshot (cron) mode."""

    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """True when required credentials/endpoints are present."""

    @abstractmethod
    def fetch_candles(self, instrument: str, granularity: str, count: int) -> list[Candle]:
        """Fetch the most recent ``count`` completed candles, oldest first."""

    def fetch_trades(self, instrument: str, lookback_minutes: int) -> list[Trade]:
        """Fetch the recent tape. Default: not supported by this adapter."""
        return []
