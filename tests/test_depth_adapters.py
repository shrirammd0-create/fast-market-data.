"""OANDA bid/ask/mid candles and order/position book parsing."""

import json
from unittest.mock import MagicMock

import pytest

from src.adapters.base import AdapterError
from src.adapters.oanda import OandaAdapter
from src.resilience.rate_limiter import TokenBucket


def make_response(status=200, payload=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.text = json.dumps(payload or {})
    resp.json.return_value = payload if payload is not None else {}
    return resp


def adapter():
    a = OandaAdapter("key", "practice", rate_limiter=TokenBucket(rate_per_minute=6000))
    a.session = MagicMock()
    return a


BAM_PAYLOAD = {
    "candles": [
        {
            "time": "2026-09-07T03:00:00.000000000Z", "volume": 120, "complete": True,
            "bid": {"o": "3645.0", "h": "3646.5", "l": "3644.8", "c": "3646.0"},
            "ask": {"o": "3645.3", "h": "3646.9", "l": "3645.1", "c": "3646.3"},
            "mid": {"o": "3645.15", "h": "3646.7", "l": "3644.95", "c": "3646.15"},
        },
        {   # ask block corrupt -> candle kept, ask dropped
            "time": "2026-09-07T03:01:00.000000000Z", "volume": 80, "complete": True,
            "bid": {"o": "3646.0", "h": "3646.4", "l": "3645.5", "c": "3646.1"},
            "ask": {"o": "bad", "h": "1", "l": "1", "c": "1"},
            "mid": {"o": "3646.15", "h": "3646.55", "l": "3645.65", "c": "3646.25"},
        },
        {   # no mid, but bid+ask present -> mid reconstructed
            "time": "2026-09-07T03:02:00.000000000Z", "volume": 50,
            "bid": {"o": "3646.0", "h": "3646.0", "l": "3646.0", "c": "3646.0"},
            "ask": {"o": "3646.4", "h": "3646.4", "l": "3646.4", "c": "3646.4"},
        },
        {   # nothing usable -> dropped
            "time": "2026-09-07T03:03:00.000000000Z", "volume": 10,
            "mid": {"o": "x", "h": "1", "l": "1", "c": "1"},
        },
    ]
}


def test_rich_candles_parse_bid_ask_mid():
    a = adapter()
    a.session.get.return_value = make_response(payload=BAM_PAYLOAD)
    candles = a.fetch_rich_candles("XAU_USD", "M1", 4)
    assert len(candles) == 3

    first = candles[0]
    assert first.bid.close == 3646.0 and first.ask.close == 3646.3 and first.mid.close == 3646.15
    assert first.spread_close == pytest.approx(0.3)
    assert first.spread_high_low == pytest.approx(3646.9 - 3644.8)

    second = candles[1]
    assert second.ask is None and second.bid is not None
    assert second.spread_close is None

    third = candles[2]
    assert third.mid.close == pytest.approx(3646.2)
    assert third.complete is True  # default when absent

    # request asked for all three price components
    params = a.session.get.call_args.kwargs["params"]
    assert params["price"] == "BAM" and params["granularity"] == "M1"


def test_rich_candles_count_is_clamped_to_oanda_limit():
    a = adapter()
    a.session.get.return_value = make_response(payload={"candles": []})
    a.fetch_rich_candles("XAU_USD", "H4", 99999)
    assert a.session.get.call_args.kwargs["params"]["count"] == 5000


BOOK_PAYLOAD = {
    "orderBook": {
        "instrument": "XAU_USD",
        "time": "2026-09-07T03:00:00Z",
        "unixTime": "1788706800",
        "price": "3646.10",
        "bucketWidth": "0.5",
        "buckets": [
            {"price": "3644.0", "longCountPercent": "0.4", "shortCountPercent": "0.1"},
            {"price": "3648.0", "longCountPercent": "0.05", "shortCountPercent": "0.35"},
            {"price": "3646.0", "longCountPercent": "0.2", "shortCountPercent": "0.2"},
            {"price": "oops", "longCountPercent": "0.1", "shortCountPercent": "0.1"},  # dropped
        ],
    }
}


def test_order_book_parses_and_sorts_buckets():
    a = adapter()
    a.session.get.return_value = make_response(payload=BOOK_PAYLOAD)
    book = a.fetch_order_book("XAU_USD")
    assert book.kind == "order_book"
    assert book.price == 3646.10 and book.bucket_width == 0.5
    assert [b.price for b in book.buckets] == [3644.0, 3646.0, 3648.0]
    assert book.buckets[0].long_count_percent == 0.4
    assert "orderBook" in a.session.get.call_args.args[0]


def test_position_book_uses_its_own_endpoint_and_key():
    a = adapter()
    payload = {"positionBook": BOOK_PAYLOAD["orderBook"]}
    a.session.get.return_value = make_response(payload=payload)
    book = a.fetch_position_book("XAU_USD")
    assert book.kind == "position_book"
    assert len(book.buckets) == 3
    assert "positionBook" in a.session.get.call_args.args[0]


def test_book_with_bad_envelope_returns_none_not_crash():
    a = adapter()
    a.session.get.return_value = make_response(payload={"orderBook": {"price": "x", "buckets": []}})
    assert a.fetch_order_book("XAU_USD") is None
    a.session.get.return_value = make_response(payload={"unexpected": 1})
    assert a.fetch_order_book("XAU_USD") is None


def test_book_unavailable_instrument_raises_adapter_error():
    a = adapter()
    a.session.get.return_value = make_response(status=404, payload={"errorMessage": "no book"})
    with pytest.raises(AdapterError):
        a.fetch_order_book("NOPE_USD")
