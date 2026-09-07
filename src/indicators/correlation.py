"""Cross-asset correlation: Gold vs. DXY (proxy) and major indices.

OANDA has no DXY instrument, so a proxy is computed from the ICE dollar
index formula over its six component pairs:

    DXY = 50.14348112 * EURUSD^-0.576 * USDJPY^0.136 * GBPUSD^-0.119
          * USDCAD^0.091 * USDSEK^0.042 * USDCHF^0.036
"""

from __future__ import annotations

import math
from typing import Mapping, Sequence

DXY_CONSTANT = 50.14348112
DXY_WEIGHTS: dict[str, float] = {
    "EUR_USD": -0.576,
    "USD_JPY": 0.136,
    "GBP_USD": -0.119,
    "USD_CAD": 0.091,
    "USD_SEK": 0.042,
    "USD_CHF": 0.036,
}


def dxy_proxy_series(component_closes: Mapping[str, Sequence[float]]) -> list[float]:
    """Build a DXY series from aligned component close series.

    Series are aligned on their tails (last N values). Returns [] if any
    component is missing or empty.
    """
    if set(DXY_WEIGHTS) - set(component_closes):
        return []
    lengths = [len(component_closes[p]) for p in DXY_WEIGHTS]
    n = min(lengths)
    if n == 0:
        return []
    series: list[float] = []
    for i in range(n):
        value = DXY_CONSTANT
        for pair, weight in DXY_WEIGHTS.items():
            closes = component_closes[pair]
            px = closes[len(closes) - n + i]
            if px <= 0:
                return []
            value *= math.pow(px, weight)
        series.append(value)
    return series


def log_returns(closes: Sequence[float]) -> list[float]:
    return [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i] > 0 and closes[i - 1] > 0
    ]


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation of two equal-tail series; None if degenerate."""
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    xs, ys = list(xs[-n:]), list(ys[-n:])
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def correlation_snapshot(
    gold_closes: Sequence[float],
    other_closes: Mapping[str, Sequence[float]],
) -> dict[str, float | None]:
    """Correlation of gold's log-returns against each other series'."""
    gold_rets = log_returns(gold_closes)
    out: dict[str, float | None] = {}
    for name, closes in other_closes.items():
        out[name] = pearson(gold_rets, log_returns(closes))
    return out
