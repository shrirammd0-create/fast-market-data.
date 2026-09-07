"""Price-level footprint: buy/sell volume and net delta per discrete tick.

Every trade (or classified candle) is bucketed onto a price grid quantized to
``tick_size``. Each level tracks total volume, buy volume, sell volume, and
net delta (buy - sell), which is the raw material for POC / value area /
HVN-LVN analysis and CVD.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .aggressor import Side, TickRuleClassifier


@dataclass
class PriceLevel:
    price: float
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    unknown_volume: float = 0.0

    @property
    def total_volume(self) -> float:
        return self.buy_volume + self.sell_volume + self.unknown_volume

    @property
    def delta(self) -> float:
        return self.buy_volume - self.sell_volume


@dataclass
class BarDelta:
    """Per-bar aggressor summary, feeds CVD and divergence detection."""

    close: float
    volume: float
    delta: float


class FootprintEngine:
    def __init__(self, tick_size: float):
        if tick_size <= 0:
            raise ValueError("tick_size must be positive")
        self.tick_size = tick_size
        self._levels: dict[int, PriceLevel] = {}
        self.cumulative_delta = 0.0
        self.total_volume = 0.0
        self.trade_count = 0
        self.bar_deltas: list[BarDelta] = []

    def _key(self, price: float) -> int:
        return int(round(price / self.tick_size))

    def add_trade(self, price: float, size: float, side: Side) -> None:
        """Record one matched trade at a price level."""
        if size <= 0:
            return
        key = self._key(price)
        level = self._levels.get(key)
        if level is None:
            level = PriceLevel(price=round(key * self.tick_size, 10))
            self._levels[key] = level
        if side is Side.BUY:
            level.buy_volume += size
        elif side is Side.SELL:
            level.sell_volume += size
        else:
            level.unknown_volume += size
        self.cumulative_delta += side.sign * size
        self.total_volume += size
        self.trade_count += 1

    def ingest_trades(self, trades: Iterable[tuple[float, float, Side]]) -> None:
        for price, size, side in trades:
            self.add_trade(price, size, side)

    def ingest_candles(self, candles: Sequence, classifier: TickRuleClassifier | None = None) -> None:
        """Approximate a footprint from OHLCV candles (decentralized feeds).

        With no per-trade tape available, each candle's volume is classified
        by the tick rule on its close: an up-close is buyer pressure, a
        down-close seller pressure; a flat close falls back to the candle
        body direction, then to the previous direction. Volume is booked at
        the candle close, quantized to the tick grid.
        """
        classifier = classifier or TickRuleClassifier()
        for candle in candles:
            side = classifier.classify(candle.close)
            if side is Side.UNKNOWN:
                # No prior close yet: use the candle body as the tiebreak.
                if candle.close > candle.open:
                    side = Side.BUY
                elif candle.close < candle.open:
                    side = Side.SELL
            self.add_trade(candle.close, candle.volume, side)
            self.bar_deltas.append(BarDelta(close=candle.close, volume=candle.volume,
                                            delta=side.sign * candle.volume))

    def levels(self) -> list[PriceLevel]:
        """All touched levels, lowest price first."""
        return [self._levels[k] for k in sorted(self._levels)]

    @property
    def is_empty(self) -> bool:
        return not self._levels
