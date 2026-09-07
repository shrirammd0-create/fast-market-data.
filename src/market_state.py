"""Machine-readable market-state pipeline (OANDA v20, full depth).

Fetches — concurrently, through the shared rate limiter — M1 / M5 / H4
Bid+Ask+Mid candles plus the order book and position book for one
instrument, runs the pre-processing matrices an external model needs for
POI / POC work, and assembles a flat JSON payload:

    {
      "schema_version", "generated_at", "instrument", "source",
      "current_price":  {bid, ask, mid, spread, time},
      "candles":        {"M1": [row...], "M5": [row...], "H4": [row...]},
      "order_book":     {time, captured_price, bucket_width, buckets: [row...]},
      "position_book":  {…same shape…},
      "analytics": {
        "order_flow_imbalance":    {matrix: [row...], flagged_buckets, summary},
        "position_imbalance":      {matrix, trapped_longs, trapped_shorts, summary},
        "spread_volatility_index": {per_candle: [row...], mean, std, liquidity_voids},
        "volume_delta_m5":         {per_candle: [row...], cumulative_delta, …},
        "volume_profile_m1":       filled in by the snapshot runner (footprint)
      },
      "errors": {section: message}   # partial failures never abort the run
    }

Every array holds flat records (no nesting inside a row), so a consumer can
load any of them straight into a dataframe.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping

from config.settings import Settings

from .adapters.base import BookSnapshot, RichCandle
from .adapters.oanda import OandaAdapter
from .indicators.book_analytics import book_summary, imbalance_matrix, trapped_positions
from .indicators.spread import spread_volatility_index
from .indicators.tick_delta import tick_volume_delta

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"


@dataclass
class MarketDepth:
    candles: dict[str, list[RichCandle]] = field(default_factory=dict)
    order_book: BookSnapshot | None = None
    position_book: BookSnapshot | None = None
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class MarketState:
    payload: dict
    depth: MarketDepth

    @property
    def m1(self) -> list[RichCandle]:
        return self.depth.candles.get("M1", [])


# ------------------------------------------------------------------ fetching

def fetch_market_depth(
    oanda: OandaAdapter,
    instrument: str,
    candle_counts: Mapping[str, int],
    max_workers: int = 5,
) -> MarketDepth:
    """Pull all candle granularities and both books in parallel.

    The token bucket is thread-safe, so concurrency never breaches the
    per-minute budget — it only removes the serial latency. Each section
    fails independently; failures land in ``errors`` instead of raising.
    """
    depth = MarketDepth()
    tasks: dict[str, Callable[[], object]] = {}
    for granularity, count in candle_counts.items():
        tasks[f"candles:{granularity}"] = (
            lambda g=granularity, n=count: oanda.fetch_rich_candles(instrument, g, n, price="BAM")
        )
    tasks["order_book"] = lambda: oanda.fetch_order_book(instrument)
    tasks["position_book"] = lambda: oanda.fetch_position_book(instrument)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {name: pool.submit(fn) for name, fn in tasks.items()}
        for name, future in futures.items():
            try:
                result = future.result()
            except Exception as exc:  # one failed section must not sink the rest
                logger.warning("market depth section %s failed: %s", name, exc)
                depth.errors[name] = f"{type(exc).__name__}: {exc}"
                continue
            if name.startswith("candles:"):
                depth.candles[name.split(":", 1)[1]] = result or []
            elif name == "order_book":
                depth.order_book = result
            elif name == "position_book":
                depth.position_book = result
            if result is None or result == []:
                depth.errors.setdefault(name, "empty response")
    return depth


# --------------------------------------------------------------- serializing

def _r(value: float | None, places: int = 6) -> float | None:
    return round(value, places) if value is not None else None


def candle_rows(candles: list[RichCandle]) -> list[dict]:
    rows = []
    for c in candles:
        b, a, m = c.bid, c.ask, c.mid
        rows.append({
            "time": c.ts.isoformat(),
            "complete": c.complete,
            "volume": c.volume,
            "bid_open": _r(b.open) if b else None,
            "bid_high": _r(b.high) if b else None,
            "bid_low": _r(b.low) if b else None,
            "bid_close": _r(b.close) if b else None,
            "ask_open": _r(a.open) if a else None,
            "ask_high": _r(a.high) if a else None,
            "ask_low": _r(a.low) if a else None,
            "ask_close": _r(a.close) if a else None,
            "mid_open": _r(m.open),
            "mid_high": _r(m.high),
            "mid_low": _r(m.low),
            "mid_close": _r(m.close),
            "range_mid": _r(m.high - m.low),
            "spread_open": _r(a.open - b.open) if a and b else None,
            "spread_close": _r(c.spread_close),
            "spread_high_low": _r(c.spread_high_low),
        })
    return rows


def book_rows(book: BookSnapshot, reference_price: float, window_pct: float | None) -> dict:
    kept = []
    for bucket in book.buckets:
        if window_pct is not None and reference_price:
            if abs(bucket.price - reference_price) / reference_price * 100.0 > window_pct:
                continue
        kept.append({
            "price": _r(bucket.price),
            "long_count_percent": _r(bucket.long_count_percent),
            "short_count_percent": _r(bucket.short_count_percent),
        })
    return {
        "time": book.ts.isoformat(),
        "captured_price": _r(book.price),
        "bucket_width": _r(book.bucket_width),
        "window_pct": window_pct,
        "total_buckets_in_book": len(book.buckets),
        "buckets": kept,
    }


def _current_price(candles: list[RichCandle]) -> dict | None:
    if not candles:
        return None
    c = candles[-1]
    return {
        "time": c.ts.isoformat(),
        "bid": _r(c.bid.close) if c.bid else None,
        "ask": _r(c.ask.close) if c.ask else None,
        "mid": _r(c.mid.close),
        "spread": _r(c.spread_close),
        "candle_complete": c.complete,
    }


def build_market_state(
    oanda: OandaAdapter,
    settings: Settings,
    lookback_minutes: int,
    now: datetime | None = None,
) -> MarketState:
    """Fetch full depth for the gold instrument and assemble the payload."""
    now = now or datetime.now(timezone.utc)
    instrument = settings.gold_instrument
    counts = dict(settings.candle_counts)
    # The footprint needs lookback+1 M1 bars; never fetch fewer than that.
    counts["M1"] = max(counts.get("M1", 0), lookback_minutes + 1)

    depth = fetch_market_depth(oanda, instrument, counts)

    m1 = depth.candles.get("M1", [])
    reference = None
    for gran in ("M1", "M5", "H4"):
        if depth.candles.get(gran):
            reference = depth.candles[gran][-1].mid.close
            break
    if reference is None and depth.order_book is not None:
        reference = depth.order_book.price

    analytics: dict = {}
    order_book_section = position_book_section = None

    if depth.order_book is not None and reference:
        ob = depth.order_book
        order_book_section = book_rows(ob, reference, settings.book_window_pct)
        matrix = imbalance_matrix(ob.buckets, reference, settings.ofi_ratio_threshold,
                                  settings.book_window_pct)
        analytics["order_flow_imbalance"] = {
            "book_time": ob.ts.isoformat(),
            "reference_price": _r(reference),
            "ratio_threshold": settings.ofi_ratio_threshold,
            "matrix": matrix,
            "flagged_buckets": [row for row in matrix if row["flagged"]],
            "summary": book_summary(matrix),
        }

    if depth.position_book is not None and reference:
        pb = depth.position_book
        position_book_section = book_rows(pb, reference, settings.book_window_pct)
        matrix = imbalance_matrix(pb.buckets, reference, settings.ofi_ratio_threshold,
                                  settings.book_window_pct)
        underwater = [
            {**row, "underwater_side": (
                "long" if row["dominant_side"] == "long" and row["distance_from_price"] > 0 else
                "short" if row["dominant_side"] == "short" and row["distance_from_price"] < 0 else
                None)}
            for row in matrix
        ]
        analytics["position_imbalance"] = {
            "book_time": pb.ts.isoformat(),
            "reference_price": _r(reference),
            "ratio_threshold": settings.ofi_ratio_threshold,
            "matrix": underwater,
            "flagged_buckets": [row for row in underwater if row["flagged"]],
            "summary": book_summary(matrix),
            **trapped_positions(matrix),
        }

    if m1:
        analytics["spread_volatility_index"] = spread_volatility_index(m1, settings.spread_void_z)
    m5 = depth.candles.get("M5", [])
    if m5:
        analytics["volume_delta_m5"] = tick_volume_delta(m5)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "instrument": instrument,
        "source": {
            "provider": "oanda_v20",
            "environment": oanda.environment,
            "price_components": "bid,ask,mid",
            "volume_semantics": "tick_count",
        },
        "lookback_minutes": lookback_minutes,
        "candle_counts_requested": counts,
        "current_price": _current_price(m1) or _current_price(m5),
        "candles": {gran: candle_rows(cs) for gran, cs in depth.candles.items()},
        "order_book": order_book_section,
        "position_book": position_book_section,
        "analytics": analytics,
        "errors": depth.errors,
    }
    return MarketState(payload=payload, depth=depth)
