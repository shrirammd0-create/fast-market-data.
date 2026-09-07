"""OFI matrix, trapped positions, spread volatility index, M5 tick delta."""

from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.base import BookBucket, OHLC, RichCandle
from src.indicators.book_analytics import book_summary, imbalance_matrix, trapped_positions
from src.indicators.spread import spread_volatility_index
from src.indicators.tick_delta import tick_volume_delta

T0 = datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)


# --- order flow imbalance ---

def test_ofi_flags_three_to_one_and_one_sided():
    buckets = [
        BookBucket(99.0, 0.30, 0.10),   # 3:1 long  -> flagged
        BookBucket(100.0, 0.20, 0.10),  # 2:1       -> not flagged
        BookBucket(101.0, 0.00, 0.25),  # one-sided -> flagged
        BookBucket(102.0, 0.00, 0.00),  # empty     -> not flagged
    ]
    rows = imbalance_matrix(buckets, reference_price=100.0, ratio_threshold=3.0)
    by_price = {r["price"]: r for r in rows}

    assert by_price[99.0]["flagged"] and by_price[99.0]["ratio"] == 3.0
    assert by_price[99.0]["net_imbalance"] == pytest.approx(0.2)
    assert by_price[99.0]["dominant_side"] == "long"
    assert not by_price[100.0]["flagged"]
    assert by_price[101.0]["flagged"] and by_price[101.0]["one_sided"]
    assert by_price[101.0]["ratio"] is None
    assert not by_price[102.0]["flagged"] and by_price[102.0]["dominant_side"] == "balanced"
    assert by_price[101.0]["distance_from_price"] == pytest.approx(1.0)
    assert by_price[101.0]["distance_pct"] == pytest.approx(1.0)


def test_ofi_window_filters_far_buckets():
    buckets = [BookBucket(p, 0.1, 0.1) for p in (90.0, 99.0, 100.0, 101.0, 110.0)]
    rows = imbalance_matrix(buckets, reference_price=100.0, window_pct=2.0)
    assert [r["price"] for r in rows] == [99.0, 100.0, 101.0]


def test_book_summary_splits_above_and_below():
    rows = imbalance_matrix(
        [BookBucket(99.0, 0.1, 0.3), BookBucket(101.0, 0.4, 0.1)], reference_price=100.0
    )
    s = book_summary(rows)
    assert s["bucket_count"] == 2
    assert s["long_pct_above_price"] == pytest.approx(0.4)
    assert s["short_pct_below_price"] == pytest.approx(0.3)


# --- trapped positions ---

def test_trapped_positions_longs_above_shorts_below():
    buckets = [
        BookBucket(105.0, 0.50, 0.05),  # heavy longs far above price -> trapped longs
        BookBucket(101.0, 0.20, 0.05),  # longs slightly above          -> trapped, lower score
        BookBucket(99.0, 0.30, 0.02),   # longs below price             -> in profit, not trapped
        BookBucket(95.0, 0.02, 0.40),   # shorts below price            -> trapped shorts
    ]
    rows = imbalance_matrix(buckets, reference_price=100.0)
    trapped = trapped_positions(rows)
    assert [r["price"] for r in trapped["trapped_longs"]] == [105.0, 101.0]
    assert trapped["trapped_longs"][0]["trapped_score"] > trapped["trapped_longs"][1]["trapped_score"]
    assert [r["price"] for r in trapped["trapped_shorts"]] == [95.0]
    assert trapped["trapped_long_pct_total"] == pytest.approx(0.7)


# --- spread volatility index ---

def make_candle(i, bid_low, ask_high, mid=100.0, vol=10):
    return RichCandle(
        ts=T0 + timedelta(minutes=i), volume=vol, complete=True,
        mid=OHLC(mid, mid + 0.1, mid - 0.1, mid),
        bid=OHLC(mid - 0.05, mid + 0.05, bid_low, mid - 0.05),
        ask=OHLC(mid + 0.05, ask_high, mid - 0.05, mid + 0.05),
    )


def test_spread_index_flags_liquidity_void():
    calm = [make_candle(i, 99.9, 100.1) for i in range(8)]     # spread_hl = 0.2
    void = make_candle(8, 99.5, 101.0)                          # spread_hl = 1.5
    result = spread_volatility_index(calm + [void], z_threshold=1.5)
    assert result["count"] == 9
    assert result["max"] == pytest.approx(1.5)
    assert len(result["liquidity_voids"]) == 1
    v = result["liquidity_voids"][0]
    assert v["price_low"] == 99.5 and v["price_high"] == 101.0
    assert result["per_candle"][-1]["liquidity_void"] is True
    assert result["per_candle"][0]["liquidity_void"] is False


def test_spread_index_skips_candles_without_bid_ask():
    c = RichCandle(T0, 5, True, OHLC(1, 1, 1, 1))
    result = spread_volatility_index([c])
    assert result["count"] == 0 and result["per_candle"] == []


# --- tick delta ---

def bar(i, o, h, l, c, vol):
    return RichCandle(T0 + timedelta(minutes=5 * i), vol, True, OHLC(o, h, l, c))


def test_tick_delta_tick_rule_and_body_weighting():
    bars = [
        bar(0, 100.0, 100.5, 99.5, 100.4, 100),   # first bar: up body -> +100
        bar(1, 100.4, 100.6, 100.0, 100.2, 60),   # downtick vs prev close -> -60
        bar(2, 100.2, 100.3, 100.1, 100.2, 40),   # flat close -> inherits -1 -> -40
        bar(3, 100.2, 100.2, 100.2, 100.2, 20),   # zero range -> body-weighted 0
    ]
    result = tick_volume_delta(bars)
    rows = result["per_candle"]
    assert [r["direction"] for r in rows] == [1, -1, -1, -1]
    assert [r["delta_tick_rule"] for r in rows] == [100, -60, -40, -20]
    assert result["cumulative_delta"] == -20
    assert rows[0]["delta_body_weighted"] == pytest.approx(100 * 0.4 / 1.0)
    assert rows[3]["delta_body_weighted"] == 0.0
    assert rows[0]["tick_change"] is None and rows[1]["tick_change"] == -40
    assert result["delta_ratio"] == pytest.approx(-20 / 220)
