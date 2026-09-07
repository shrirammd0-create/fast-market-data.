"""Token-bucket rate limiter governing outbound REST calls.

The bucket refills continuously at ``rate_per_minute / 60`` tokens per second
up to ``capacity``. Every REST call must acquire a token first, so the engine
can never exceed the configured per-minute budget no matter how many
instruments a snapshot fans out to.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class RateLimitTimeout(Exception):
    """Raised when a token could not be acquired within the caller's timeout."""


class TokenBucket:
    def __init__(
        self,
        rate_per_minute: float,
        capacity: float | None = None,
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self.rate = rate_per_minute / 60.0  # tokens per second
        self.capacity = float(capacity) if capacity is not None else max(1.0, rate_per_minute / 6.0)
        self._tokens = self.capacity
        self._time_fn = time_fn
        self._sleep_fn = sleep_fn
        self._last_refill = time_fn()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._time_fn()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_refill = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Take ``tokens`` if available right now; never blocks."""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> None:
        """Block until ``tokens`` are available (or ``timeout`` elapses)."""
        deadline = None if timeout is None else self._time_fn() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            if deadline is not None:
                remaining = deadline - self._time_fn()
                if remaining <= 0:
                    raise RateLimitTimeout(f"could not acquire {tokens} token(s) within {timeout}s")
                wait = min(wait, remaining)
            self._sleep_fn(max(wait, 0.01))

    @property
    def available(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens
