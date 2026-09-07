import json
from unittest.mock import MagicMock

import pytest
import requests

from src.adapters.base import AdapterError
from src.adapters.cme_comex import CMEComexAdapter
from src.adapters.oanda import OandaAdapter, _parse_time
from src.engine.aggressor import Side, classify_by_flag
from src.resilience.rate_limiter import TokenBucket


def make_response(status=200, payload=None, text="", headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text or json.dumps(payload or {})
    resp.headers = headers or {}
    if payload is not None:
        resp.json.return_value = payload
    else:
        resp.json.side_effect = ValueError("no json")
    return resp


def fast_bucket():
    return TokenBucket(rate_per_minute=6000)


OANDA_PAYLOAD = {
    "candles": [
        {
            "time": "2026-09-07T03:00:00.000000000Z",
            "volume": 120,
            "complete": True,
            "mid": {"o": "3645.0", "h": "3646.5", "l": "3644.8", "c": "3646.1"},
        },
        {   # malformed: volume is a string -> must be dropped, not fatal
            "time": "2026-09-07T03:01:00.000000000Z",
            "volume": "garbage",
            "mid": {"o": "1", "h": "1", "l": "1", "c": "1"},
        },
        {   # malformed mid block -> dropped
            "time": "2026-09-07T03:02:00.000000000Z",
            "volume": 10,
            "mid": {"o": "x", "h": "1", "l": "1", "c": "1"},
        },
    ]
}


def test_oanda_parses_and_drops_malformed_candles():
    adapter = OandaAdapter("key", "practice", rate_limiter=fast_bucket())
    adapter.session = MagicMock()
    adapter.session.get.return_value = make_response(payload=OANDA_PAYLOAD)

    candles = adapter.fetch_candles("XAU_USD", "M1", 3)
    assert len(candles) == 1
    c = candles[0]
    assert c.close == 3646.1 and c.volume == 120
    assert c.ts.isoformat().startswith("2026-09-07T03:00")


def test_oanda_retries_on_429_then_succeeds():
    adapter = OandaAdapter("key", "practice", rate_limiter=fast_bucket())
    adapter.session = MagicMock()
    adapter.session.get.side_effect = [
        make_response(status=429, payload={}, headers={"Retry-After": "0"}),
        make_response(payload=OANDA_PAYLOAD),
    ]
    candles = adapter.fetch_candles("XAU_USD", "M1", 3)
    assert len(candles) == 1
    assert adapter.session.get.call_count == 2


def test_oanda_fails_fast_on_401():
    adapter = OandaAdapter("bad-key", "practice", rate_limiter=fast_bucket())
    adapter.session = MagicMock()
    adapter.session.get.return_value = make_response(status=401, payload={"error": "unauthorized"})
    with pytest.raises(AdapterError):
        adapter.fetch_candles("XAU_USD")
    assert adapter.session.get.call_count == 1  # no retry loop on auth errors


def test_oanda_unconfigured():
    adapter = OandaAdapter(None)
    assert not adapter.is_configured()
    with pytest.raises(AdapterError):
        adapter.fetch_candles("XAU_USD")


def test_oanda_rejects_unknown_environment():
    with pytest.raises(AdapterError):
        OandaAdapter("key", "staging")


def test_parse_time_nanoseconds():
    ts = _parse_time("2026-09-07T03:00:00.123456789Z")
    assert ts.microsecond == 123456


def test_cme_unconfigured_returns_empty_tape():
    adapter = CMEComexAdapter(None)
    assert not adapter.is_configured()
    assert adapter.fetch_trades("GC", 15) == []


def test_cme_parses_trades_with_aggressor_flags():
    adapter = CMEComexAdapter("https://feed.example.com", "key", rate_limiter=fast_bucket())
    adapter.session = MagicMock()
    adapter.session.get.return_value = make_response(payload={
        "trades": [
            {"ts": "2026-09-07T03:00:01Z", "price": 3646.1, "size": 3, "aggressor": "B"},
            {"ts": "2026-09-07T03:00:02Z", "price": 3646.0, "size": 2, "aggressor": "S"},
            {"ts": "2026-09-07T03:00:03Z", "price": -1, "size": 2},  # malformed
        ]
    })
    trades = adapter.fetch_trades("GC", 15)
    assert len(trades) == 2
    assert classify_by_flag(trades[0].aggressor) is Side.BUY
    assert classify_by_flag(trades[1].aggressor) is Side.SELL
