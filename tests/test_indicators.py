from datetime import datetime, timezone

import pytest

from src.adapters.base import Candle
from src.indicators.absorption import detect_absorption_exhaustion
from src.indicators.correlation import (
    DXY_WEIGHTS,
    correlation_snapshot,
    dxy_proxy_series,
    pearson,
)
from src.indicators.cvd import cumulative_delta, detect_divergence
from src.indicators.vwap import SessionVWAP, session_for


def ts(hour, minute=0):
    return datetime(2026, 9, 7, hour, minute, tzinfo=timezone.utc)


# --- CVD & divergence ---

def test_cumulative_delta_running_sum():
    assert cumulative_delta([10, -5, 3]) == [10, 5, 8]


def test_bearish_divergence_price_high_cvd_lagging():
    closes = [100, 101, 102, 101, 103]          # new high on last bar
    deltas = [50, 40, 30, -60, 5]               # CVD: 50,90,120,60,65 < peak 120
    flags = detect_divergence(closes, deltas)
    assert [f.kind for f in flags] == ["bearish"]


def test_bullish_divergence_price_low_cvd_holding():
    closes = [100, 99, 98, 99, 97]              # new low on last bar
    deltas = [-50, -40, -30, 60, -5]            # CVD: -50,-90,-120,-60,-65 > trough
    flags = detect_divergence(closes, deltas)
    assert [f.kind for f in flags] == ["bullish"]


def test_no_divergence_when_cvd_confirms():
    closes = [100, 101, 102, 103, 104]
    deltas = [10, 20, 30, 40, 50]               # CVD makes new highs with price
    assert detect_divergence(closes, deltas) == []


def test_divergence_needs_min_bars():
    assert detect_divergence([100, 101], [1, 2]) == []


# --- session VWAP ---

def test_session_boundaries():
    assert session_for(ts(0, 30)) == "tokyo"
    assert session_for(ts(6, 59)) == "tokyo"
    assert session_for(ts(7, 0)) == "london"
    assert session_for(ts(13, 30)) == "new_york"
    assert session_for(ts(20, 59)) == "new_york"
    assert session_for(ts(21, 0)) == "sydney"
    assert session_for(ts(23, 59)) == "sydney"


def test_vwap_accumulates_within_session():
    vwap = SessionVWAP()
    vwap.update(ts(8, 0), 100.0, 10)
    value = vwap.update(ts(8, 1), 110.0, 30)
    assert value == pytest.approx((100 * 10 + 110 * 30) / 40)
    assert vwap.session == "london"


def test_vwap_resets_exactly_at_session_open():
    vwap = SessionVWAP()
    vwap.update(ts(13, 29), 100.0, 1000)   # london session
    value = vwap.update(ts(13, 30), 200.0, 10)  # new york open -> reset
    assert value == pytest.approx(200.0)
    assert vwap.session == "new_york"


def test_vwap_zero_volume_is_safe():
    vwap = SessionVWAP()
    assert vwap.update(ts(8, 0), 100.0, 0) is None


# --- absorption & exhaustion ---

def base_bars():
    # calm bars: range 1.0, volume 10
    return [Candle(ts(1, i), 100, 100.5, 99.5, 100, 10) for i in range(6)]


def test_absorption_flag_high_volume_small_range():
    bars = base_bars()
    # heavy volume, tiny range
    bars.append(Candle(ts(1, 6), 100, 100.05, 99.95, 100, 100))
    deltas = [0] * len(bars)
    events = detect_absorption_exhaustion(bars, deltas)
    assert any(e.kind == "absorption" for e in events)


def test_exhaustion_flag_new_high_with_negative_delta():
    bars = base_bars()
    bars.append(Candle(ts(1, 6), 100, 103.0, 99.5, 102.5, 100))  # window high
    deltas = [0] * 6 + [-80]  # heavy volume but net selling into the high
    events = detect_absorption_exhaustion(bars, deltas)
    assert any(e.kind == "exhaustion" for e in events)


def test_quiet_tape_produces_no_events():
    bars = base_bars()
    assert detect_absorption_exhaustion(bars, [0] * len(bars)) == []


# --- correlations ---

def test_pearson_perfect_positive_and_negative():
    xs = [1, 2, 3, 4, 5]
    assert pearson(xs, [2, 4, 6, 8, 10]) == pytest.approx(1.0)
    assert pearson(xs, [10, 8, 6, 4, 2]) == pytest.approx(-1.0)


def test_pearson_degenerate_series():
    assert pearson([1, 1, 1], [1, 2, 3]) is None
    assert pearson([1], [1]) is None


def test_correlation_snapshot_inverse_relationship():
    # Every gold up-move is mirrored by a DXY down-move and vice versa.
    gold = [100, 101, 100.5, 102, 101, 103]
    dxy = [50, 49.5, 49.75, 49.0, 49.5, 48.5]
    out = correlation_snapshot(gold, {"DXY": dxy})
    assert out["DXY"] < -0.9


def test_dxy_proxy_requires_all_components():
    partial = {"EUR_USD": [1.1, 1.2]}
    assert dxy_proxy_series(partial) == []


def test_dxy_proxy_moves_inverse_to_eurusd():
    n = 5
    components = {pair: [1.0] * n for pair in DXY_WEIGHTS}
    components["EUR_USD"] = [1.00, 1.05, 1.10, 1.15, 1.20]  # euro strengthens
    series = dxy_proxy_series(components)
    assert len(series) == n
    assert series[0] > series[-1]  # dollar index falls
