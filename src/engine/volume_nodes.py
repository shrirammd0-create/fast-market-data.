"""Volume-node analysis over a footprint: POC, Value Area, HVN, LVN."""

from __future__ import annotations

from dataclasses import dataclass

from .footprint import FootprintEngine, PriceLevel


@dataclass(frozen=True)
class VolumeProfile:
    poc: float | None
    poc_volume: float
    value_area_low: float | None
    value_area_high: float | None
    value_area_pct: float
    hvns: tuple[float, ...]
    lvns: tuple[float, ...]
    total_volume: float


def build_profile(
    engine: FootprintEngine,
    value_area_pct: float = 0.70,
    hvn_ratio: float = 1.5,
    lvn_ratio: float = 0.5,
    max_nodes: int = 5,
) -> VolumeProfile:
    """Derive POC / VA / HVN / LVN from the engine's price-level footprint.

    * POC: the single level with the most traded volume.
    * Value Area: classic expansion from the POC — repeatedly annex the
      higher-volume adjacent level until ``value_area_pct`` of total volume
      is enclosed.
    * HVN: levels trading at least ``hvn_ratio`` x the mean level volume.
    * LVN: levels trading at most ``lvn_ratio`` x the mean level volume
      (only meaningful between traded levels, so grid gaps are ignored).
    """
    levels: list[PriceLevel] = engine.levels()
    if not levels:
        return VolumeProfile(None, 0.0, None, None, value_area_pct, (), (), 0.0)

    total = sum(lv.total_volume for lv in levels)
    poc_idx = max(range(len(levels)), key=lambda i: levels[i].total_volume)
    poc_level = levels[poc_idx]

    # Value area expansion.
    included = {poc_idx}
    covered = poc_level.total_volume
    lo, hi = poc_idx, poc_idx
    while covered < value_area_pct * total and (lo > 0 or hi < len(levels) - 1):
        below = levels[lo - 1].total_volume if lo > 0 else -1.0
        above = levels[hi + 1].total_volume if hi < len(levels) - 1 else -1.0
        if above >= below:
            hi += 1
            included.add(hi)
            covered += levels[hi].total_volume
        else:
            lo -= 1
            included.add(lo)
            covered += levels[lo].total_volume

    mean_volume = total / len(levels)
    hvns = [lv.price for lv in levels
            if lv.total_volume >= hvn_ratio * mean_volume]
    hvns.sort(key=lambda p: -next(l.total_volume for l in levels if l.price == p))
    lvns = [lv.price for lv in levels
            if lv.total_volume <= lvn_ratio * mean_volume and lv is not poc_level]
    lvns.sort(key=lambda p: next(l.total_volume for l in levels if l.price == p))

    return VolumeProfile(
        poc=poc_level.price,
        poc_volume=poc_level.total_volume,
        value_area_low=levels[lo].price,
        value_area_high=levels[hi].price,
        value_area_pct=value_area_pct,
        hvns=tuple(hvns[:max_nodes]),
        lvns=tuple(lvns[:max_nodes]),
        total_volume=total,
    )
