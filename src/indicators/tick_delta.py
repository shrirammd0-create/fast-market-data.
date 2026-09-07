"""Volume delta / tick proxy over a candle series (designed for M5).

Decentralized feeds only expose tick *counts*, so aggressor volume is
approximated two ways per bar and both are emitted so the consumer can pick:

* ``delta_tick_rule``     — direction by the tick rule on closes
  (uptick = buy, downtick = sell, flat inherits), times tick volume.
* ``delta_body_weighted`` — tick volume × (close − open) / (high − low):
  the share of the bar's range the body covered, signed by direction. A
  full-body bar books all its ticks to one side; a doji books ~0.

``tick_change`` is the tick-count difference from the previous bar
(activity acceleration), and ``cumulative_delta`` runs the tick-rule delta.
"""

from __future__ import annotations

from typing import Sequence

from ..adapters.base import RichCandle


def tick_volume_delta(candles: Sequence[RichCandle]) -> dict:
    rows: list[dict] = []
    prev_close: float | None = None
    prev_volume: float | None = None
    last_direction = 0
    cumulative = 0.0
    cumulative_body = 0.0

    for c in candles:
        m = c.mid
        if prev_close is None:
            direction = 1 if m.close > m.open else -1 if m.close < m.open else 0
        elif m.close > prev_close:
            direction = 1
        elif m.close < prev_close:
            direction = -1
        else:
            direction = last_direction
        if direction != 0:
            last_direction = direction

        bar_range = m.high - m.low
        body_weighted = c.volume * (m.close - m.open) / bar_range if bar_range > 0 else 0.0
        delta_tick = direction * c.volume
        cumulative += delta_tick
        cumulative_body += body_weighted
        rows.append({
            "time": c.ts.isoformat(),
            "open": round(m.open, 6),
            "high": round(m.high, 6),
            "low": round(m.low, 6),
            "close": round(m.close, 6),
            "volume": c.volume,
            "tick_change": (c.volume - prev_volume) if prev_volume is not None else None,
            "direction": direction,
            "delta_tick_rule": delta_tick,
            "delta_body_weighted": round(body_weighted, 4),
            "cumulative_delta": cumulative,
            "cumulative_delta_body_weighted": round(cumulative_body, 4),
        })
        prev_close = m.close
        prev_volume = c.volume

    total_volume = sum(c.volume for c in candles)
    return {
        "per_candle": rows,
        "count": len(rows),
        "total_volume": total_volume,
        "cumulative_delta": cumulative,
        "cumulative_delta_body_weighted": round(cumulative_body, 4),
        "delta_ratio": round(cumulative / total_volume, 6) if total_volume > 0 else None,
    }
