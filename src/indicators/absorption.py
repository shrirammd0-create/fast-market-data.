"""Absorption & exhaustion detection.

* Absorption: unusually high volume with minimal price movement — passive
  limit orders soaking up aggressive flow at a level (bar volume well above
  the window norm while the bar's range is well below it).

* Exhaustion: a push to a fresh window extreme on heavy volume whose net
  delta leans *against* the direction of the push — the aggressors driving
  the move are being met and the move is running out of fuel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class AbsorptionEvent:
    kind: str  # "absorption" or "exhaustion"
    price: float
    volume: float
    description: str


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def detect_absorption_exhaustion(
    bars: Sequence,  # objects with .high, .low, .close, .volume
    bar_deltas: Sequence[float],
    volume_ratio: float = 1.5,
    range_ratio: float = 0.5,
    max_events: int = 3,
) -> list[AbsorptionEvent]:
    """Scan the window's bars and flag absorption / exhaustion candidates.

    ``volume_ratio``: bar volume must exceed this multiple of mean volume.
    ``range_ratio``: for absorption, bar range must be under this fraction
    of the mean range.
    """
    n = min(len(bars), len(bar_deltas))
    if n < 4:
        return []
    bars = list(bars[-n:])
    bar_deltas = list(bar_deltas[-n:])

    mean_vol = _mean([b.volume for b in bars])
    mean_range = _mean([b.high - b.low for b in bars])
    if mean_vol <= 0:
        return []
    window_high = max(b.high for b in bars)
    window_low = min(b.low for b in bars)

    events: list[AbsorptionEvent] = []
    for bar, delta in zip(bars, bar_deltas):
        rng = bar.high - bar.low
        heavy = bar.volume >= volume_ratio * mean_vol
        if not heavy:
            continue
        if mean_range > 0 and rng <= range_ratio * mean_range:
            events.append(AbsorptionEvent(
                "absorption", bar.close, bar.volume,
                f"{bar.volume:.0f} vol in a {rng:.2f} range at {bar.close:g} "
                f"({bar.volume / mean_vol:.1f}x avg vol, range "
                f"{rng / mean_range:.0%} of avg) — passive orders absorbing flow",
            ))
        elif bar.high >= window_high and delta < 0:
            events.append(AbsorptionEvent(
                "exhaustion", bar.high, bar.volume,
                f"push to window high {bar.high:g} on {bar.volume:.0f} vol "
                f"with negative delta ({delta:+.0f}) — buying exhaustion",
            ))
        elif bar.low <= window_low and delta > 0:
            events.append(AbsorptionEvent(
                "exhaustion", bar.low, bar.volume,
                f"flush to window low {bar.low:g} on {bar.volume:.0f} vol "
                f"with positive delta ({delta:+.0f}) — selling exhaustion",
            ))

    events.sort(key=lambda e: -e.volume)
    return events[:max_events]
