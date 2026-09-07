"""Interactive Brokers Client Portal REST adapter (optional, local only).

Talks to a locally running IBKR Client Portal gateway (default
``https://localhost:5000/v1/api``). The gateway requires an interactive
login, so this adapter is for local/desktop runs — it cannot work inside a
serverless cron job and degrades gracefully (``is_configured`` False /
empty results) when the gateway is unreachable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from ..resilience.backoff import RetryableError, RetryableHTTPError, is_retryable_status, retry_with_backoff
from ..resilience.rate_limiter import TokenBucket
from ..resilience.schema_guard import Field, filter_valid, require_keys
from .base import AdapterError, Candle, MarketDataAdapter

logger = logging.getLogger(__name__)

_BAR_SCHEMA = {
    "t": Field((int, float)),  # epoch millis
    "o": Field((int, float)),
    "h": Field((int, float)),
    "l": Field((int, float)),
    "c": Field((int, float)),
    "v": Field((int, float), check=lambda v: v >= 0),
}


class IBKRAdapter(MarketDataAdapter):
    name = "ibkr"

    def __init__(
        self,
        gateway_url: str | None,
        rate_limiter: TokenBucket | None = None,
        timeout: float = 15.0,
        session: requests.Session | None = None,
        verify_tls: bool = True,
    ):
        self.gateway_url = (gateway_url or "").rstrip("/")
        self.rate_limiter = rate_limiter or TokenBucket(rate_per_minute=30)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.verify_tls = verify_tls

    def is_configured(self) -> bool:
        if not self.gateway_url:
            return False
        try:
            resp = self.session.get(f"{self.gateway_url}/iserver/auth/status",
                                    timeout=3, verify=self.verify_tls)
            return resp.status_code == 200 and resp.json().get("authenticated", False)
        except (requests.RequestException, ValueError):
            return False

    @retry_with_backoff(max_retries=3, base_delay=1.0, max_delay=15.0)
    def _get(self, path: str, params: dict) -> object:
        self.rate_limiter.acquire()
        try:
            resp = self.session.get(f"{self.gateway_url}{path}", params=params,
                                    timeout=self.timeout, verify=self.verify_tls)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise RetryableError(f"network failure talking to IBKR gateway: {exc}") from exc
        if is_retryable_status(resp.status_code):
            raise RetryableHTTPError(resp.status_code, resp.text[:200])
        if resp.status_code != 200:
            raise AdapterError(f"IBKR gateway returned {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise RetryableError(f"IBKR gateway returned non-JSON body: {exc}") from exc

    def fetch_candles(self, instrument: str, granularity: str = "M1", count: int = 16) -> list[Candle]:
        """``instrument`` is an IBKR conid; granularity M1 maps to bar=1min."""
        bar = {"M1": "1min", "M5": "5min", "S5": "5secs"}.get(granularity, "1min")
        payload = self._get("/iserver/marketdata/history",
                            {"conid": instrument, "period": f"{count}min", "bar": bar})
        if not require_keys(payload, ["data"], context=f"ibkr:{instrument}"):
            return []
        records, _ = filter_valid(payload["data"], _BAR_SCHEMA, context=f"ibkr:{instrument}")
        candles = [
            Candle(
                ts=datetime.fromtimestamp(r["t"] / 1000.0, tz=timezone.utc),
                open=float(r["o"]), high=float(r["h"]),
                low=float(r["l"]), close=float(r["c"]),
                volume=float(r["v"]),
            )
            for r in records
        ]
        candles.sort(key=lambda c: c.ts)
        return candles[-count:]
