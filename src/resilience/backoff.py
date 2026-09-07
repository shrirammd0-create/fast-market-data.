"""Exponential backoff with jitter for transient API failures.

Retries 429 (rate limit), 5xx gateway errors, and network disconnects.
Anything else fails fast — a 401 or a 400 will never fix itself by retrying.
"""

from __future__ import annotations

import functools
import logging
import random
import time
from typing import Callable, Iterable, Type

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class RetryableError(Exception):
    """A transient failure worth retrying (network drop, gateway hiccup)."""


class RetryableHTTPError(RetryableError):
    """An HTTP response with a retryable status code (429 / 5xx)."""

    def __init__(self, status_code: int, message: str = "", retry_after: float | None = None):
        super().__init__(f"HTTP {status_code}: {message}" if message else f"HTTP {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


def is_retryable_status(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS_CODES


def retry_with_backoff(
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: float = 0.5,
    retry_on: Iterable[Type[BaseException]] = (RetryableError,),
    sleep_fn: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
):
    """Decorator: retry the wrapped call on transient errors.

    Delay for attempt n is ``min(max_delay, base_delay * 2**n)`` plus uniform
    jitter of up to ``jitter`` * delay, so bursts of workers don't re-stampede
    the API in lockstep. A server-provided ``Retry-After`` (surfaced via
    ``RetryableHTTPError.retry_after``) takes precedence when it is longer.
    """
    retry_on = tuple(retry_on)
    _rng = rng or random.Random()

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return fn(*args, **kwargs)
                except retry_on as exc:
                    if attempt >= max_retries:
                        logger.error("%s: giving up after %d retries (%s)", fn.__name__, max_retries, exc)
                        raise
                    delay = min(max_delay, base_delay * (2 ** attempt))
                    delay += _rng.uniform(0, jitter * delay)
                    retry_after = getattr(exc, "retry_after", None)
                    if retry_after is not None:
                        delay = max(delay, float(retry_after))
                    logger.warning(
                        "%s: transient failure (%s); retry %d/%d in %.2fs",
                        fn.__name__, exc, attempt + 1, max_retries, delay,
                    )
                    sleep_fn(delay)
                    attempt += 1

        return wrapper

    return decorator
