"""End-to-end: full-depth pull -> analytics -> data/market_state_snapshot.json."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from config.settings import Settings
from src.snapshot import run_snapshot

START = datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)


def bam_candles(n, step_minutes, base=3640.0, step=0.5, volume=100, spread=0.3):
    out = []
    for i in range(n):
        o = base + i * step
        c = o + step
        h, l = c + 0.2, o - 0.2
        # Bar 5 gets a wide ask-high / bid-low gap -> liquidity void
        wide = 2.0 if (i == 5 and step_minutes == 1) else 0.0
        ts = START + timedelta(minutes=i * step_minutes)
        out.append({
            "time": ts.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
            "volume": volume + i,
            "complete": True,
            "bid": {"o": f"{o - spread / 2:.2f}", "h": f"{h - spread / 2:.2f}",
                    "l": f"{l - spread / 2 - wide:.2f}", "c": f"{c - spread / 2:.2f}"},
            "ask": {"o": f"{o + spread / 2:.2f}", "h": f"{h + spread / 2 + wide:.2f}",
                    "l": f"{l + spread / 2:.2f}", "c": f"{c + spread / 2:.2f}"},
            "mid": {"o": f"{o:.2f}", "h": f"{h:.2f}", "l": f"{l:.2f}", "c": f"{c:.2f}"},
        })
    return {"candles": out}


def book(key, price):
    buckets = []
    # Buckets from -10% to +10% at width 0.5; heavy longs above, shorts below.
    p = price * 0.9
    while p <= price * 1.1:
        above = p > price
        buckets.append({
            "price": f"{p:.1f}",
            "longCountPercent": "0.40" if above else "0.05",
            "shortCountPercent": "0.05" if above else "0.40",
        })
        p += 0.5
    return {key: {"instrument": "XAU_USD", "time": "2026-09-07T03:14:00Z",
                  "price": f"{price:.2f}", "bucketWidth": "0.5", "buckets": buckets}}


def router(url, params=None, **kwargs):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}
    if url.endswith("/orderBook"):
        payload = book("orderBook", 3648.0)
    elif url.endswith("/positionBook"):
        payload = book("positionBook", 3648.0)
    elif "/candles" in url:
        gran = params["granularity"]
        n = params["count"]
        step = {"M1": 1, "M5": 5, "H4": 240}[gran]
        payload = bam_candles(n, step)
    else:
        payload = {}
    resp.text = json.dumps(payload)
    resp.json.return_value = payload
    return resp


def test_full_depth_snapshot_writes_machine_payload(tmp_path):
    settings = Settings(
        oanda_api_key="test-key",
        output_file=str(tmp_path / "market_updates.txt"),
        json_output=str(tmp_path / "data" / "market_state_snapshot.json"),
        cache_dir=str(tmp_path / ".cache"),
        requests_per_minute=60000,
        candle_counts={"M1": 30, "M5": 12, "H4": 6},
    )
    with patch("requests.Session.get", side_effect=router):
        assert run_snapshot(settings, lookback_minutes=15) == 0

    payload = json.loads((tmp_path / "data" / "market_state_snapshot.json").read_text())

    # Envelope
    assert payload["schema_version"] == "1.0"
    assert payload["instrument"] == "XAU_USD"
    assert payload["source"]["price_components"] == "bid,ask,mid"
    assert payload["errors"] == {}

    # Candles: all three granularities, flat rows carrying bid/ask/mid
    assert set(payload["candles"]) == {"M1", "M5", "H4"}
    assert len(payload["candles"]["M1"]) == 30
    assert len(payload["candles"]["M5"]) == 12
    assert len(payload["candles"]["H4"]) == 6
    row = payload["candles"]["M1"][0]
    for key in ("bid_open", "ask_high", "mid_close", "spread_close", "spread_high_low", "volume"):
        assert key in row
    assert all(not isinstance(v, (dict, list)) for v in row.values())  # flat

    # Current price with spread
    cp = payload["current_price"]
    assert cp["ask"] > cp["mid"] > cp["bid"]
    assert cp["spread"] > 0

    # Books: buckets within the ±window, flat rows
    ob = payload["order_book"]
    assert ob["bucket_width"] == 0.5
    assert ob["total_buckets_in_book"] > len(ob["buckets"]) > 0
    assert set(ob["buckets"][0]) == {"price", "long_count_percent", "short_count_percent"}
    assert payload["position_book"]["buckets"]

    # OFI matrix: 8:1 buckets are all flagged
    ofi = payload["analytics"]["order_flow_imbalance"]
    assert ofi["ratio_threshold"] == 3.0
    assert len(ofi["flagged_buckets"]) == len(ofi["matrix"])
    assert ofi["summary"]["flagged_count"] == len(ofi["matrix"])
    above = [r for r in ofi["matrix"] if r["distance_from_price"] > 0]
    assert all(r["dominant_side"] == "long" for r in above)

    # Position book: longs above price are trapped, shorts below are trapped
    pos = payload["analytics"]["position_imbalance"]
    assert pos["trapped_longs"] and pos["trapped_shorts"]
    assert all(r["distance_from_price"] > 0 for r in pos["trapped_longs"])
    assert all(r["distance_from_price"] < 0 for r in pos["trapped_shorts"])
    assert any(r["underwater_side"] == "long" for r in pos["matrix"])

    # Spread volatility index over M1 with the injected void
    svi = payload["analytics"]["spread_volatility_index"]
    assert svi["count"] == 30
    assert len(svi["liquidity_voids"]) == 1
    assert svi["liquidity_voids"][0]["time"].startswith("2026-09-07T03:05")

    # M5 delta proxy
    vd = payload["analytics"]["volume_delta_m5"]
    assert vd["count"] == 12
    assert vd["cumulative_delta"] > 0  # every bar closes up
    assert all(r["delta_body_weighted"] > 0 for r in vd["per_candle"])

    # Footprint carried into the machine payload as flat level rows
    vp = payload["analytics"]["volume_profile_m1"]
    assert vp["poc"] is not None and vp["levels"]
    assert set(vp["levels"][0]) == {"price", "buy_volume", "sell_volume",
                                    "unknown_volume", "total_volume", "delta"}
    assert payload["analytics"]["session_context"]["session"] == "tokyo"

    # Human-readable block gained the depth summary lines
    text = (tmp_path / "market_updates.txt").read_text()
    for marker in ("Spread SVI:", "Order book:", "Positions:", "M5 delta:", "JSON:"):
        assert marker in text


def test_book_failure_degrades_to_partial_payload(tmp_path):
    def flaky_router(url, params=None, **kwargs):
        if url.endswith("Book"):
            resp = MagicMock()
            resp.status_code = 404
            resp.headers = {}
            resp.text = '{"errorMessage":"no book"}'
            resp.json.return_value = {"errorMessage": "no book"}
            return resp
        return router(url, params, **kwargs)

    settings = Settings(
        oanda_api_key="test-key",
        output_file=str(tmp_path / "market_updates.txt"),
        json_output=str(tmp_path / "snapshot.json"),
        cache_dir=str(tmp_path / ".cache"),
        requests_per_minute=60000,
        candle_counts={"M1": 20, "M5": 6, "H4": 3},
    )
    with patch("requests.Session.get", side_effect=flaky_router):
        assert run_snapshot(settings, lookback_minutes=15) == 0

    payload = json.loads((tmp_path / "snapshot.json").read_text())
    assert payload["order_book"] is None and payload["position_book"] is None
    assert "order_book" in payload["errors"] and "position_book" in payload["errors"]
    assert "order_flow_imbalance" not in payload["analytics"]
    # candle-derived analytics still present
    assert payload["analytics"]["spread_volatility_index"]["count"] == 20
    assert payload["analytics"]["volume_delta_m5"]["count"] == 6
    assert payload["analytics"]["volume_profile_m1"]["poc"] is not None


def test_no_append_writes_nothing(tmp_path):
    settings = Settings(
        oanda_api_key="test-key",
        output_file=str(tmp_path / "market_updates.txt"),
        json_output=str(tmp_path / "snapshot.json"),
        cache_dir=str(tmp_path / ".cache"),
        requests_per_minute=60000,
        candle_counts={"M1": 20, "M5": 6, "H4": 3},
    )
    with patch("requests.Session.get", side_effect=router):
        assert run_snapshot(settings, lookback_minutes=15, no_append=True) == 0
    assert not (tmp_path / "snapshot.json").exists()
    assert not (tmp_path / "market_updates.txt").exists()
