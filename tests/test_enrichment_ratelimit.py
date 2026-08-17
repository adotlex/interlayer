"""Token bucket and retry policy, driven by an injected clock."""

from __future__ import annotations

import random

import pytest

from interlayer.enrichment.ratelimit import (
    RateLimiter,
    RetryPolicy,
    TokenBucket,
    for_provider,
)


class FakeClock:
    """A clock that only advances when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_burst_is_free_then_the_rate_binds() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate_per_second=1.0, burst=3, clock=clock.time, sleeper=clock.sleep)

    for _ in range(3):
        assert bucket.acquire() == 0.0
    assert clock.slept == []

    waited = bucket.acquire()
    assert waited == pytest.approx(1.0)
    assert clock.now == pytest.approx(1.0)


def test_tokens_refill_over_time() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate_per_second=2.0, burst=2, clock=clock.time, sleeper=clock.sleep)
    bucket.acquire(2)
    clock.now += 1.0  # two tokens' worth
    assert bucket.acquire(2) == 0.0


def test_bucket_never_exceeds_its_burst() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate_per_second=5.0, burst=2, clock=clock.time, sleeper=clock.sleep)
    clock.now += 100.0
    assert bucket.acquire(2) == 0.0
    assert bucket.tokens == pytest.approx(0.0)


def test_invalid_configuration_is_refused() -> None:
    with pytest.raises(ValueError, match="rate_per_second"):
        TokenBucket(rate_per_second=0)
    with pytest.raises(ValueError, match="burst"):
        TokenBucket(burst=0)
    with pytest.raises(ValueError, match="cannot acquire"):
        TokenBucket(burst=2).acquire(3)


def test_only_429_and_5xx_are_retried() -> None:
    policy = RetryPolicy()
    assert policy.should_retry(429)
    assert policy.should_retry(500)
    assert policy.should_retry(503)
    assert not policy.should_retry(400)
    assert not policy.should_retry(401)
    assert not policy.should_retry(403)
    assert not policy.should_retry(404)
    assert not policy.should_retry(200)


def test_backoff_grows_and_stays_inside_the_jitter_band() -> None:
    policy = RetryPolicy(base_delay=1.0, factor=2.0, jitter=0.25, rng=random.Random(7))
    for attempt, expected in ((1, 1.0), (2, 2.0), (3, 4.0)):
        delay = policy.delay_for(attempt)
        assert expected * 0.75 <= delay <= expected * 1.25


def test_retry_after_beats_the_computed_backoff() -> None:
    policy = RetryPolicy()
    assert policy.delay_for(3, retry_after=0.5) == 0.5
    assert policy.delay_for(1, retry_after=0) == 0.0


def test_delay_sequence_has_one_entry_per_retry() -> None:
    policy = RetryPolicy(attempts=3)
    assert len(list(policy.delays())) == 2


def test_limiter_sleeps_between_retries() -> None:
    clock = FakeClock()
    limiter = RateLimiter(
        bucket=TokenBucket(clock=clock.time, sleeper=clock.sleep),
        policy=RetryPolicy(rng=random.Random(1)),
        sleeper=clock.sleep,
    )
    delay = limiter.wait_before_retry(1, retry_after=2.0)
    assert delay == 2.0
    assert clock.slept == [2.0]


def test_defaults_match_the_documented_policy() -> None:
    limiter = for_provider()
    assert limiter.bucket.rate_per_second == 1.0
    assert limiter.bucket.burst == 5
    assert limiter.max_concurrency == 2
    assert limiter.policy.attempts == 3


def test_seeded_policies_are_reproducible() -> None:
    left = for_provider(seed=42).policy
    right = for_provider(seed=42).policy
    assert [left.delay_for(i) for i in (1, 2, 3)] == [right.delay_for(i) for i in (1, 2, 3)]
