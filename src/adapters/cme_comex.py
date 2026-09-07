"""CME/COMEX adapter — centralized true volume for Gold futures (GC).

COMEX trade data with aggressor flags is licensed; there is no free public
tape. This adapter targets any endpoint that serves recent GC trades as JSON
(CME DataMine exports behind a proxy, or a redistributor such as Databento's
raw-JSON gateway) configured via ``CME_API_URL`` / ``CME_API_KEY``.

Expected record shape (extra keys ignored)::

    {"ts": "2026-09-07T03:41:02.123Z", "price": 3646.1,
     "size": 3, "aggressor": "B"}

``aggressor`` follows exchange convention: "B" (buyer aggressor, matched at
the ask) or "S" (seller aggressor, matched at the bid). When unconfigured the
adapter reports so and the engine falls back to spot XAU/USD tick volume.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from ..resilience.backoff import RetryableError, RetryableHTTPError, is_retryable_status, retry_with_backoff
from ..resilience.rate_limiter import TokenBucket
from ..resilience.schema_guard import Field, filter_valid, number_like, require_keys
from .base import AdapterError, Candle, MarketDataAdapter, Trade

logger = logging.getLogger(__name__)

_TRADE_SCHEMA = {
    "ts": Field((str,)),
    "price": Field((int, float, str), check=lambda v: number_like(v) and float(v) > 0),
    "size": Field((int, float, str), check=lambda v: number_like(v) and float(v) > 0),
    "aggressor": Field((str,), required=False),
}


def _parse_time(raw: str) -> datetime:
    raw = raw.rstrip("Z")
    if "." in raw:
        head, frac = raw.split(".", 1)
        raw = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)


class CMEComexAdapter(MarketDataAdapter):
    name = "cme_comex"

    def __init__(
        self,
        api_url: str | None,
        api_key: str | None = None,
        rate_limiter: TokenBucket | None = None,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ):
        self.api_url = (api_url or "").rstrip("/")
        self.api_key = api_key
        self.rate_limiter = rate_limiter or TokenBucket(rate_per_minute=30)
        self.timeout = timeout
        self.session = session or requests.Session()

    def is_configured(self) -> bool:
        return bool(self.api_url)

    @retry_with_backoff(max_retries=4, base_delay=1.0, max_delay=30.0)
    def _get(self, path: str, params: dict) -> object:
        self.rate_limiter.acquire()
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            resp = self.session.get(f"{self.api_url}{path}", params=params,
                                    headers=headers, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise RetryableError(f"network failure talking to CME endpoint: {exc}") from exc
        if is_retryable_status(resp.status_code):
            raise RetryableHTTPError(resp.status_code, resp.text[:200])
        if resp.status_code != 200:
            raise AdapterError(f"CME endpoint returned {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise RetryableError(f"CME endpoint returned non-JSON body: {exc}") from exc

    def fetch_trades(self, instrument: str = "GC", lookback_minutes: int = 15) -> list[Trade]:
        if not self.is_configured():
            logger.info("CME_API_URL not set — skipping COMEX GC true-volume footprint")
            return []
        payload = self._get("/trades", {"symbol": instrument, "lookback": f"{lookback_minutes}m"})
        if not require_keys(payload, ["trades"], context=f"cme:{instrument}"):
            return []
        records, _ = filter_valid(payload["trades"], _TRADE_SCHEMA, context=f"cme:{instrument}")
        trades = [
            Trade(ts=_parse_time(r["ts"]), price=float(r["price"]),
                  size=float(r["size"]), aggressor=r.get("aggressor"))
            for r in records
        ]
        trades.sort(key=lambda t: t.ts)
        return trades

    def fetch_candles(self, instrument: str, granularity: str = "M1", count: int = 16) -> list[Candle]:
        raise AdapterError("CMEComexAdapter serves trades (fetch_trades), not candles")
