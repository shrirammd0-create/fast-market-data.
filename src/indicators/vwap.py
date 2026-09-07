"""Session-anchored VWAP with resets at major global session opens.

Session boundaries are fixed UTC times (standard institutional convention;
adjust in SESSION_OPENS_UTC if you want DST-tracked local opens):

* Sydney   21:00 UTC
* Tokyo    00:00 UTC
* London   07:00 UTC
* New York 13:30 UTC (US cash equity open)
"""

from __future__ import annotations

from datetime import datetime, time, timezone

SESSION_OPENS_UTC: list[tuple[str, time]] = [
    ("tokyo", time(0, 0)),
    ("london", time(7, 0)),
    ("new_york", time(13, 30)),
    ("sydney", time(21, 0)),
]


def session_for(ts: datetime) -> str:
    """Name of the trading session a UTC timestamp falls in."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    tod = ts.time()
    current = SESSION_OPENS_UTC[-1][0]  # before 00:00 boundary → prior Sydney
    for name, opens in SESSION_OPENS_UTC:
        if tod >= opens:
            current = name
    return current


class SessionVWAP:
    """Streaming VWAP that resets exactly at each session open."""

    def __init__(self) -> None:
        self._session: str | None = None
        self._session_date = None
        self._cum_pv = 0.0
        self._cum_vol = 0.0

    def update(self, ts: datetime, price: float, volume: float) -> float | None:
        """Feed one print/bar (typical price + volume); returns current VWAP."""
        name = session_for(ts)
        day = ts.date()
        if name != self._session or day != self._session_date:
            self._session = name
            self._session_date = day
            self._cum_pv = 0.0
            self._cum_vol = 0.0
        if volume > 0:
            self._cum_pv += price * volume
            self._cum_vol += volume
        return self.value

    @property
    def value(self) -> float | None:
        if self._cum_vol <= 0:
            return None
        return self._cum_pv / self._cum_vol

    @property
    def session(self) -> str | None:
        return self._session
