"""Spread Volatility Index over bid/ask candles.

For every candle: ``spread_high_low = Ask High − Bid Low`` (the widest the
market got inside the bar), plus the open/close spreads. Bars whose
spread_high_low sits ``z_threshold`` standard deviations above the window
mean are flagged as liquidity voids — price ranges where the book thinned
out and the spread mathematically expanded.
"""

from __future__ import annotations

import math
from typing import Sequence

from ..adapters.base import RichCandle


def spread_volatility_index(
    candles: Sequence[RichCandle],
    z_threshold: float = 1.5,
) -> dict:
    rows: list[dict] = []
    values: list[float] = []
    used: list[RichCandle] = []
    for c in candles:
        if c.bid is None or c.ask is None:
            continue
        s_hl = c.ask.high - c.bid.low
        used.append(c)
        rows.append({
            "time": c.ts.isoformat(),
            "mid_close": round(c.mid.close, 6),
            "spread_open": round(c.ask.open - c.bid.open, 6),
            "spread_close": round(c.ask.close - c.bid.close, 6),
            "spread_high_low": round(s_hl, 6),
            "bar_range_mid": round(c.mid.high - c.mid.low, 6),
            "volume": c.volume,
        })
        values.append(s_hl)

    if not values:
        return {"per_candle": [], "count": 0, "mean": None, "std": None,
                "min": None, "max": None, "z_threshold": z_threshold, "liquidity_voids": []}

    n = len(values)
    mean = sum(values) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / n) if n > 1 else 0.0
    voids: list[dict] = []
    for row, v, c in zip(rows, values, used):
        z = (v - mean) / std if std > 0 else 0.0
        row["z_score"] = round(z, 4)
        row["spread_ratio_to_mean"] = round(v / mean, 4) if mean > 0 else None
        row["liquidity_void"] = bool(z >= z_threshold)
        if row["liquidity_void"]:
            voids.append({
                "time": row["time"],
                "price_low": round(c.bid.low, 6),
                "price_high": round(c.ask.high, 6),
                "spread_high_low": row["spread_high_low"],
                "z_score": row["z_score"],
            })
    return {
        "per_candle": rows,
        "count": n,
        "mean": round(mean, 6),
        "std": round(std, 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "latest": rows[-1]["spread_high_low"],
        "z_threshold": z_threshold,
        "liquidity_voids": voids,
    }
