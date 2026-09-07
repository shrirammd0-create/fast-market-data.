"""Order-book / position-book analytics.

* Order Flow Imbalance (OFI) matrix — per bucket, ``longCountPercent`` minus
  ``shortCountPercent`` and the long:short ratio; buckets past a 3:1 ratio
  are flagged as one-sided clusters of resting orders.

* Trapped positions — the same matrix over the *position* book, plus each
  bucket's distance from the current price. Long-dominant buckets sitting
  above the market are longs under water (trapped longs); short-dominant
  buckets below the market are trapped shorts. ``trapped_score`` weights the
  dominant side's participation by how far under water it is.
"""

from __future__ import annotations

from typing import Sequence

from ..adapters.base import BookBucket


def _ratio(a: float, b: float) -> float | None:
    """max/min of two percentages; None when one side is empty (one-sided)."""
    lo, hi = min(a, b), max(a, b)
    if lo <= 0:
        return None
    return hi / lo


def imbalance_matrix(
    buckets: Sequence[BookBucket],
    reference_price: float,
    ratio_threshold: float = 3.0,
    window_pct: float | None = None,
) -> list[dict]:
    """Flat OFI rows for every bucket (optionally within ±window_pct of price).

    Each row: price, long_pct, short_pct, total_pct, net_imbalance
    (long − short), ratio (dominant:minor, None if one side is 0),
    dominant_side, one_sided, flagged (ratio ≥ threshold, or one-sided with
    non-zero participation), distance_from_price, distance_pct.
    """
    rows: list[dict] = []
    for b in buckets:
        distance = b.price - reference_price
        distance_pct = (distance / reference_price * 100.0) if reference_price else 0.0
        if window_pct is not None and abs(distance_pct) > window_pct:
            continue
        long_pct, short_pct = b.long_count_percent, b.short_count_percent
        total = long_pct + short_pct
        net = long_pct - short_pct
        ratio = _ratio(long_pct, short_pct)
        one_sided = total > 0 and ratio is None
        if net > 0:
            dominant = "long"
        elif net < 0:
            dominant = "short"
        else:
            dominant = "balanced"
        # Tolerance so an exact 3:1 (e.g. 0.30/0.10 = 2.999…) still counts.
        flagged = one_sided or (ratio is not None and ratio >= ratio_threshold - 1e-9)
        rows.append({
            "price": round(b.price, 6),
            "long_pct": round(long_pct, 6),
            "short_pct": round(short_pct, 6),
            "total_pct": round(total, 6),
            "net_imbalance": round(net, 6),
            "ratio": round(ratio, 4) if ratio is not None else None,
            "dominant_side": dominant,
            "one_sided": one_sided,
            "flagged": bool(flagged),
            "distance_from_price": round(distance, 6),
            "distance_pct": round(distance_pct, 4),
        })
    return rows


def trapped_positions(matrix: Sequence[dict], top_n: int = 15) -> dict:
    """Split a position-book matrix into trapped longs / trapped shorts.

    Trapped longs: long-dominant buckets *above* the current price.
    Trapped shorts: short-dominant buckets *below* the current price.
    Sorted by ``trapped_score`` = dominant side pct × |distance_pct|.
    """
    longs: list[dict] = []
    shorts: list[dict] = []
    for row in matrix:
        if row["total_pct"] <= 0:
            continue
        if row["dominant_side"] == "long" and row["distance_from_price"] > 0:
            longs.append({**row, "trapped_side": "long",
                          "trapped_score": round(row["long_pct"] * abs(row["distance_pct"]), 6)})
        elif row["dominant_side"] == "short" and row["distance_from_price"] < 0:
            shorts.append({**row, "trapped_side": "short",
                           "trapped_score": round(row["short_pct"] * abs(row["distance_pct"]), 6)})
    longs.sort(key=lambda r: -r["trapped_score"])
    shorts.sort(key=lambda r: -r["trapped_score"])
    return {
        "trapped_longs": longs[:top_n],
        "trapped_shorts": shorts[:top_n],
        "trapped_long_pct_total": round(sum(r["long_pct"] for r in longs), 6),
        "trapped_short_pct_total": round(sum(r["short_pct"] for r in shorts), 6),
    }


def book_summary(matrix: Sequence[dict]) -> dict:
    """Aggregate participation above vs below price and the flagged buckets."""
    above = [r for r in matrix if r["distance_from_price"] > 0]
    below = [r for r in matrix if r["distance_from_price"] < 0]
    return {
        "bucket_count": len(matrix),
        "flagged_count": sum(1 for r in matrix if r["flagged"]),
        "long_pct_above_price": round(sum(r["long_pct"] for r in above), 6),
        "short_pct_above_price": round(sum(r["short_pct"] for r in above), 6),
        "long_pct_below_price": round(sum(r["long_pct"] for r in below), 6),
        "short_pct_below_price": round(sum(r["short_pct"] for r in below), 6),
        "net_imbalance_total": round(sum(r["net_imbalance"] for r in matrix), 6),
    }
