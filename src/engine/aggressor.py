"""Aggressor-side classification.

Two regimes:

* Centralized (COMEX GC futures): the exchange tape carries an explicit
  aggressor flag — a trade matched at the ask was buyer-initiated, at the
  bid seller-initiated. ``classify_by_flag`` maps those flags directly.

* Decentralized (spot XAU/USD, forex, index CFDs): there is no consolidated
  tape, so aggressor side is inferred with the tick rule — an uptick is a
  buy, a downtick is a sell, and an unchanged price inherits the previous
  direction (the "zero-tick" convention).
"""

from __future__ import annotations

from enum import Enum


class Side(Enum):
    BUY = 1
    SELL = -1
    UNKNOWN = 0

    @property
    def sign(self) -> int:
        return self.value


_BUY_FLAGS = {"B", "BUY", "A", "ASK", "TAKE", "1"}
_SELL_FLAGS = {"S", "SELL", "BID", "HIT", "2"}


def classify_by_flag(flag: object) -> Side:
    """Map an exchange aggressor flag to a side (COMEX/CME conventions).

    Accepts 'B'/'S', 'BUY'/'SELL', 'ASK'/'BID' (trade *at* the ask = buyer
    aggressor), and CME's numeric 1/2. Unknown flags classify as UNKNOWN
    rather than raising — a bad flag must not kill the tape.
    """
    if flag is None:
        return Side.UNKNOWN
    token = str(flag).strip().upper()
    if token in _BUY_FLAGS:
        return Side.BUY
    if token in _SELL_FLAGS:
        return Side.SELL
    return Side.UNKNOWN


class TickRuleClassifier:
    """Stateful tick-rule classifier: uptick=BUY, downtick=SELL.

    An unchanged price repeats the last non-zero direction. The very first
    print (no reference price) is UNKNOWN unless a seed price is provided.
    """

    def __init__(self, seed_price: float | None = None):
        self._last_price = seed_price
        self._last_side = Side.UNKNOWN

    def classify(self, price: float) -> Side:
        if self._last_price is None:
            self._last_price = price
            return self._last_side
        if price > self._last_price:
            side = Side.BUY
        elif price < self._last_price:
            side = Side.SELL
        else:
            side = self._last_side
        self._last_price = price
        if side is not Side.UNKNOWN:
            self._last_side = side
        return side
