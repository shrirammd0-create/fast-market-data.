"""Cumulative Volume Delta (CVD) and price/CVD divergence flags."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import accumulate
from typing import Sequence


@dataclass(frozen=True)
class Divergence:
    kind: str  # "bearish" or "bullish"
    description: str


def cumulative_delta(bar_deltas: Sequence[float]) -> list[float]:
    """Running sum of per-bar delta (buy volume minus sell volume)."""
    return list(accumulate(bar_deltas))


def detect_divergence(
    closes: Sequence[float],
    bar_deltas: Sequence[float],
    min_bars: int = 5,
) -> list[Divergence]:
    """Flag CVD-vs-price divergences over the window.

    * Bearish: price prints a new window high while CVD fails to — buyers
      are pushing price without net aggressive volume behind it.
    * Bullish: price prints a new window low while CVD holds above its own
      low — sellers are pressing price without real aggression.
    """
    n = min(len(closes), len(bar_deltas))
    if n < min_bars:
        return []
    closes = list(closes[-n:])
    cvd = cumulative_delta(bar_deltas[-n:])

    flags: list[Divergence] = []
    last_close, last_cvd = closes[-1], cvd[-1]
    prior_high, prior_low = max(closes[:-1]), min(closes[:-1])
    prior_cvd_high, prior_cvd_low = max(cvd[:-1]), min(cvd[:-1])

    if last_close >= prior_high and last_cvd < prior_cvd_high:
        flags.append(Divergence(
            "bearish",
            f"price at window high ({last_close:g}) but CVD "
            f"({last_cvd:+.0f}) below its prior peak ({prior_cvd_high:+.0f})",
        ))
    if last_close <= prior_low and last_cvd > prior_cvd_low:
        flags.append(Divergence(
            "bullish",
            f"price at window low ({last_close:g}) but CVD "
            f"({last_cvd:+.0f}) above its prior trough ({prior_cvd_low:+.0f})",
        ))
    return flags
