"""Adapter contract and normalized market-data types.

All adapters normalize their vendor payloads into these types so the engine
layer never sees vendor-specific JSON:

* ``Candle``      — mid-price OHLCV (what the footprint engine consumes)
* ``RichCandle``  — bid / ask / mid OHLC side by side, for spread analysis
* ``Trade``       — tape print with optional aggressor flag (futures)
* ``BookSnapshot``— OANDA order book / position book buckets
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
class OHLC:
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class RichCandle:
    """A candle carrying bid, ask and mid OHLC simultaneously (OANDA price=BAM)."""

    ts: datetime
    volume: float
    complete: bool
    mid: OHLC
    bid: OHLC | None = None
    ask: OHLC | None = None

    def to_candle(self) -> Candle:
        return Candle(self.ts, self.mid.open, self.mid.high, self.mid.low,
                      self.mid.close, self.volume)

    @property
    def spread_close(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask.close - self.bid.close

    @property
    def spread_high_low(self) -> float | None:
        """Ask High minus Bid Low — the widest spread the candle could have shown."""
        if self.bid is None or self.ask is None:
            return None
        return self.ask.high - self.bid.low


@dataclass(frozen=True)
class Trade:
    ts: datetime
    price: float
    size: float
    aggressor: str | None = None  # exchange flag, e.g. 'B'/'S'; None if absent


@dataclass(frozen=True)
class BookBucket:
    """One price bucket of an order book or position book.

    Percentages are of *all* orders/positions across the whole book, so a
    bucket reading 0.35 / 0.10 holds 0.35% of all long and 0.10% of all
    short orders (or open positions).
    """

    price: float
    long_count_percent: float
    short_count_percent: float


@dataclass(frozen=True)
class BookSnapshot:
    kind: str            # "order_book" or "position_book"
    instrument: str
    ts: datetime
    price: float         # market price when the book was captured
    bucket_width: float
    buckets: tuple[BookBucket, ...]


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
