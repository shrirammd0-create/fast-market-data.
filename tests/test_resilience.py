import pytest

from src.resilience.backoff import (
    RetryableError,
    RetryableHTTPError,
    is_retryable_status,
    retry_with_backoff,
)
from src.resilience.rate_limiter import RateLimitTimeout, TokenBucket
from src.resilience.schema_guard import Field, filter_valid, number_like, require_keys, validate_record


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


# --- backoff ---

def test_retry_succeeds_after_transient_failures():
    sleeps = []
    calls = {"n": 0}

    @retry_with_backoff(max_retries=5, base_delay=1.0, jitter=0.0, sleep_fn=sleeps.append)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableError("boom")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]  # exponential, no jitter


def test_retry_gives_up_and_reraises():
    @retry_with_backoff(max_retries=2, base_delay=0.01, jitter=0.0, sleep_fn=lambda s: None)
    def always_fails():
        raise RetryableHTTPError(503, "bad gateway")

    with pytest.raises(RetryableHTTPError):
        always_fails()


def test_non_retryable_errors_fail_fast():
    sleeps = []

    @retry_with_backoff(max_retries=5, sleep_fn=sleeps.append)
    def unauthorized():
        raise ValueError("401 is not transient")

    with pytest.raises(ValueError):
        unauthorized()
    assert sleeps == []


def test_retry_after_header_extends_delay():
    sleeps = []
    calls = {"n": 0}

    @retry_with_backoff(max_retries=3, base_delay=0.1, jitter=0.0, sleep_fn=sleeps.append)
    def rate_limited():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryableHTTPError(429, "slow down", retry_after=7.5)
        return "ok"

    assert rate_limited() == "ok"
    assert sleeps == [7.5]


def test_retryable_status_classification():
    assert all(is_retryable_status(s) for s in (429, 500, 502, 503, 504))
    assert not any(is_retryable_status(s) for s in (200, 400, 401, 403, 404))


# --- token bucket ---

def test_token_bucket_enforces_capacity():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_minute=60, capacity=2, time_fn=clock.time, sleep_fn=clock.sleep)
    assert bucket.try_acquire()
    assert bucket.try_acquire()
    assert not bucket.try_acquire()  # empty


def test_token_bucket_refills_over_time():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_minute=60, capacity=1, time_fn=clock.time, sleep_fn=clock.sleep)
    assert bucket.try_acquire()
    assert not bucket.try_acquire()
    clock.now += 1.0  # 60/min = 1 token per second
    assert bucket.try_acquire()


def test_token_bucket_blocking_acquire_waits():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_minute=60, capacity=1, time_fn=clock.time, sleep_fn=clock.sleep)
    bucket.acquire()
    start = clock.now
    bucket.acquire()  # must simulate-sleep ~1s for the refill
    assert clock.now - start >= 1.0


def test_token_bucket_acquire_timeout():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_minute=6, capacity=1, time_fn=clock.time, sleep_fn=clock.sleep)
    bucket.acquire()
    with pytest.raises(RateLimitTimeout):
        bucket.acquire(timeout=1.0)  # refill takes 10s


# --- schema guard ---

CANDLE = {"time": "2026-09-07T03:00:00Z", "volume": 120, "mid": {"c": "3646.1"}}
SCHEMA = {
    "time": Field((str,)),
    "volume": Field((int, float), check=lambda v: v >= 0),
    "mid": Field((dict,)),
}


def test_valid_record_passes():
    assert validate_record(CANDLE, SCHEMA) == []


def test_malformed_records_are_dropped_not_fatal():
    records = [
        CANDLE,
        {"time": "2026-09-07T03:01:00Z", "volume": "not-a-number", "mid": {}},  # bad type
        {"volume": 5, "mid": {}},                                               # missing field
        {"time": "t", "volume": -3, "mid": {}},                                 # failed check
        "garbage string",                                                       # not an object
        None,
    ]
    valid, dropped = filter_valid(records, SCHEMA)
    assert valid == [CANDLE]
    assert dropped == 5


def test_optional_fields_may_be_absent():
    schema = {"a": Field((int,)), "b": Field((str,), required=False)}
    assert validate_record({"a": 1}, schema) == []
    assert validate_record({"a": 1, "b": 2}, schema) != []


def test_number_like():
    assert number_like(1) and number_like(1.5) and number_like("3646.10")
    assert not number_like("abc") and not number_like(None) and not number_like(True)


def test_require_keys():
    assert require_keys({"candles": []}, ["candles"])
    assert not require_keys({"error": "x"}, ["candles"])
    assert not require_keys([1, 2], ["candles"])
