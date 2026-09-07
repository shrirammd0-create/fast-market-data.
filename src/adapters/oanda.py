"""OANDA v20 REST adapter — spot XAU/USD, forex pairs, and index CFDs.

Endpoints covered:

* ``/v3/instruments/{i}/candles``      — Bid / Ask / Mid candles (price=BAM)
  at any granularity; tick ``volume`` is the number of price updates.
* ``/v3/instruments/{i}/orderBook``    — pending-order clusters per price
  bucket (``longCountPercent`` / ``shortCountPercent``).
* ``/v3/instruments/{i}/positionBook`` — open-position clusters per bucket.

Every request goes through the shared token bucket and the backoff
decorator, and every payload through the malformed-packet guard.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from ..resilience.backoff import RetryableError, RetryableHTTPError, is_retryable_status, retry_with_backoff
from ..resilience.rate_limiter import TokenBucket
from ..resilience.schema_guard import Field, filter_valid, number_like, require_keys, validate_record
from .base import AdapterError, BookBucket, BookSnapshot, Candle, MarketDataAdapter, OHLC, RichCandle

logger = logging.getLogger(__name__)

_HOSTS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}

# One OANDA candle record, as served by /v3/instruments/{i}/candles.
# ``mid`` / ``bid`` / ``ask`` are present according to the ``price`` param;
# at least one must be there (checked after the schema pass).
_CANDLE_SCHEMA = {
    "time": Field((str,)),
    "volume": Field((int, float), check=lambda v: v >= 0),
    "complete": Field((bool,), required=False),
    "mid": Field((dict,), required=False),
    "bid": Field((dict,), required=False),
    "ask": Field((dict,), required=False),
}
_OHLC_SCHEMA = {
    "o": Field((str, int, float), check=number_like),
    "h": Field((str, int, float), check=number_like),
    "l": Field((str, int, float), check=number_like),
    "c": Field((str, int, float), check=number_like),
}
_BUCKET_SCHEMA = {
    "price": Field((str, int, float), check=number_like),
    "longCountPercent": Field((str, int, float), check=number_like),
    "shortCountPercent": Field((str, int, float), check=number_like),
}


def _parse_time(raw: str) -> datetime:
    # OANDA returns RFC3339 with nanosecond precision; trim to microseconds.
    raw = raw.rstrip("Z")
    if "." in raw:
        head, frac = raw.split(".", 1)
        raw = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)


def _parse_ohlc(block: dict | None, label: str) -> OHLC | None:
    if block is None:
        return None
    errors = validate_record(block, _OHLC_SCHEMA)
    if errors:
        logger.warning("dropping oanda %s block: %s", label, "; ".join(errors))
        return None
    return OHLC(float(block["o"]), float(block["h"]), float(block["l"]), float(block["c"]))


class OandaAdapter(MarketDataAdapter):
    name = "oanda"

    def __init__(
        self,
        api_key: str | None,
        environment: str = "practice",
        rate_limiter: TokenBucket | None = None,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ):
        self.api_key = api_key
        if environment not in _HOSTS:
            raise AdapterError(f"unknown OANDA environment '{environment}' (use practice/live)")
        self.environment = environment
        self.base_url = _HOSTS[environment]
        self.rate_limiter = rate_limiter or TokenBucket(rate_per_minute=60)
        self.timeout = timeout
        self.session = session or requests.Session()

    def is_configured(self) -> bool:
        return bool(self.api_key)

    @retry_with_backoff(max_retries=4, base_delay=1.0, max_delay=30.0)
    def _get(self, path: str, params: dict) -> dict:
        self.rate_limiter.acquire()
        try:
            resp = self.session.get(
                f"{self.base_url}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise RetryableError(f"network failure talking to OANDA: {exc}") from exc
        if is_retryable_status(resp.status_code):
            retry_after = resp.headers.get("Retry-After")
            raise RetryableHTTPError(
                resp.status_code,
                resp.text[:200],
                retry_after=float(retry_after) if retry_after else None,
            )
        if resp.status_code != 200:
            raise AdapterError(f"OANDA {path} returned {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            # Truncated/garbled body mid-transfer is transient — retry it.
            raise RetryableError(f"OANDA returned non-JSON body: {exc}") from exc

    # ------------------------------------------------------------------ candles

    def fetch_rich_candles(
        self,
        instrument: str,
        granularity: str = "M1",
        count: int = 16,
        price: str = "BAM",
    ) -> list[RichCandle]:
        """Fetch candles carrying bid, ask and mid OHLC together.

        ``price`` is any combination of B/A/M (OANDA convention). Records
        whose mid block is unusable are dropped; a bad bid or ask block only
        drops that side, so the mid-based engine keeps working.
        """
        if not self.is_configured():
            raise AdapterError("OANDA_API_KEY is not set")
        count = max(1, min(int(count), 5000))  # OANDA hard limit
        payload = self._get(
            f"/v3/instruments/{instrument}/candles",
            {"granularity": granularity, "count": count, "price": price},
        )
        if not require_keys(payload, ["candles"], context=f"oanda:{instrument}:{granularity}"):
            return []
        records, _ = filter_valid(payload["candles"], _CANDLE_SCHEMA,
                                  context=f"oanda:{instrument}:{granularity}")

        candles: list[RichCandle] = []
        for rec in records:
            mid = _parse_ohlc(rec.get("mid"), "mid")
            bid = _parse_ohlc(rec.get("bid"), "bid")
            ask = _parse_ohlc(rec.get("ask"), "ask")
            if mid is None:
                if bid is not None and ask is not None:
                    # Reconstruct mid from bid/ask when only those were requested.
                    mid = OHLC((bid.open + ask.open) / 2, (bid.high + ask.high) / 2,
                               (bid.low + ask.low) / 2, (bid.close + ask.close) / 2)
                else:
                    logger.warning("dropping oanda candle without a usable mid block")
                    continue
            candles.append(RichCandle(
                ts=_parse_time(rec["time"]),
                volume=float(rec["volume"]),
                complete=bool(rec.get("complete", True)),
                mid=mid, bid=bid, ask=ask,
            ))
        candles.sort(key=lambda c: c.ts)
        return candles

    def fetch_candles(self, instrument: str, granularity: str = "M1", count: int = 16) -> list[Candle]:
        """Mid-price OHLCV candles (engine-facing view)."""
        return [c.to_candle() for c in self.fetch_rich_candles(instrument, granularity, count, price="M")]

    # ------------------------------------------------------------------- books

    def _fetch_book(self, kind: str, instrument: str, at: datetime | None) -> BookSnapshot | None:
        if not self.is_configured():
            raise AdapterError("OANDA_API_KEY is not set")
        path_key = "orderBook" if kind == "order_book" else "positionBook"
        params = {}
        if at is not None:
            params["time"] = at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = self._get(f"/v3/instruments/{instrument}/{path_key}", params)
        if not require_keys(payload, [path_key], context=f"oanda:{instrument}:{kind}"):
            return None
        book = payload[path_key]
        if not isinstance(book, dict) or not isinstance(book.get("buckets"), list):
            logger.error("oanda %s for %s has no bucket list", kind, instrument)
            return None
        for key in ("price", "bucketWidth"):
            if not number_like(book.get(key)):
                logger.error("oanda %s for %s: bad header field %s=%r", kind, instrument, key, book.get(key))
                return None
        records, _ = filter_valid(book["buckets"], _BUCKET_SCHEMA, context=f"oanda:{instrument}:{kind}")
        buckets = tuple(sorted(
            (BookBucket(float(r["price"]), float(r["longCountPercent"]), float(r["shortCountPercent"]))
             for r in records),
            key=lambda b: b.price,
        ))
        try:
            ts = _parse_time(str(book["time"]))
        except (KeyError, ValueError):
            ts = datetime.now(timezone.utc)
        return BookSnapshot(
            kind=kind,
            instrument=instrument,
            ts=ts,
            price=float(book["price"]),
            bucket_width=float(book["bucketWidth"]),
            buckets=buckets,
        )

    def fetch_order_book(self, instrument: str, at: datetime | None = None) -> BookSnapshot | None:
        """Pending limit/stop order clusters per price bucket."""
        return self._fetch_book("order_book", instrument, at)

    def fetch_position_book(self, instrument: str, at: datetime | None = None) -> BookSnapshot | None:
        """Open-position clusters per price bucket."""
        return self._fetch_book("position_book", instrument, at)
