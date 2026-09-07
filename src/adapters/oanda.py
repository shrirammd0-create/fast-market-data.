"""OANDA v20 REST adapter — spot XAU/USD, forex pairs, and index CFDs.

Decentralized feed: candle ``volume`` is tick volume (number of price
updates), which the engine classifies with the tick rule. Every request goes
through the shared token bucket and the backoff decorator, and every payload
through the malformed-packet guard.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from ..resilience.backoff import RetryableError, RetryableHTTPError, is_retryable_status, retry_with_backoff
from ..resilience.rate_limiter import TokenBucket
from ..resilience.schema_guard import Field, filter_valid, number_like, require_keys, validate_record
from .base import AdapterError, Candle, MarketDataAdapter

logger = logging.getLogger(__name__)

_HOSTS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}

# One OANDA candle record, as served by /v3/instruments/{i}/candles.
_CANDLE_SCHEMA = {
    "time": Field((str,)),
    "volume": Field((int, float), check=lambda v: v >= 0),
    "complete": Field((bool,), required=False),
    "mid": Field((dict,)),
}
_MID_SCHEMA = {
    "o": Field((str, int, float), check=number_like),
    "h": Field((str, int, float), check=number_like),
    "l": Field((str, int, float), check=number_like),
    "c": Field((str, int, float), check=number_like),
}


def _parse_time(raw: str) -> datetime:
    # OANDA returns RFC3339 with nanosecond precision; trim to microseconds.
    raw = raw.rstrip("Z")
    if "." in raw:
        head, frac = raw.split(".", 1)
        raw = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)


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

    def fetch_candles(self, instrument: str, granularity: str = "M1", count: int = 16) -> list[Candle]:
        if not self.is_configured():
            raise AdapterError("OANDA_API_KEY is not set")
        payload = self._get(
            f"/v3/instruments/{instrument}/candles",
            {"granularity": granularity, "count": count, "price": "M"},
        )
        if not require_keys(payload, ["candles"], context=f"oanda:{instrument}"):
            return []
        records, _ = filter_valid(payload["candles"], _CANDLE_SCHEMA, context=f"oanda:{instrument}")

        candles: list[Candle] = []
        for rec in records:
            errors = validate_record(rec["mid"], _MID_SCHEMA)
            if errors:
                logger.warning("dropping oanda candle with bad mid block: %s", "; ".join(errors))
                continue
            mid = rec["mid"]
            candles.append(Candle(
                ts=_parse_time(rec["time"]),
                open=float(mid["o"]),
                high=float(mid["h"]),
                low=float(mid["l"]),
                close=float(mid["c"]),
                volume=float(rec["volume"]),
            ))
        candles.sort(key=lambda c: c.ts)
        return candles
