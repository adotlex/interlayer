"""Token bucket and retry policy for the network adapters.

Defaults are deliberately slow — 1 request/second, burst 5, concurrency 2 — because
nothing here is time-critical. The binding constraint on this whole project is a
monthly quota, not throughput, so there is no prize for going faster and there is
a real cost to hammering a vendor.

Clock and sleep are injected so the tests are deterministic and instant. A rate
limiter you cannot test without waiting is a rate limiter nobody tests.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

DEFAULT_REQUESTS_PER_SECOND = 1.0
DEFAULT_BURST = 5
DEFAULT_MAX_CONCURRENCY = 2

#: Retry these and nothing else. A 400 or a 403 will be a 400 or a 403 next time
#: too, and retrying it just spends the budget faster.
RETRYABLE_STATUSES: frozenset[int] = frozenset({429})


@dataclass
class TokenBucket:
    """Classic token bucket with an injectable clock."""

    rate_per_second: float = DEFAULT_REQUESTS_PER_SECOND
    burst: int = DEFAULT_BURST
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    _tokens: float = field(init=False, default=0.0)
    _last: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        if self.rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if self.burst < 1:
            raise ValueError("burst must be at least 1")
        self._tokens = float(self.burst)
        self._last = self.clock()

    @property
    def tokens(self) -> float:
        return self._tokens

    def _refill(self) -> None:
        now = self.clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate_per_second)

    def acquire(self, tokens: int = 1) -> float:
        """Block until ``tokens`` are available. Returns the seconds waited."""
        if tokens > self.burst:
            raise ValueError(f"cannot acquire {tokens} tokens from a burst of {self.burst}")
        waited = 0.0
        while True:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return waited
            deficit = tokens - self._tokens
            pause = deficit / self.rate_per_second
            self.sleeper(pause)
            waited += pause


@dataclass
class RetryPolicy:
    """Bounded exponential backoff with jitter, honouring ``Retry-After``."""

    attempts: int = 3
    base_delay: float = 1.0
    factor: float = 2.0
    jitter: float = 0.25
    rng: random.Random = field(default_factory=lambda: random.Random(0))

    def should_retry(self, status: int) -> bool:
        """429 and 5xx only."""
        return status in RETRYABLE_STATUSES or 500 <= status < 600

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to wait before ``attempt`` (1-based).

        A server-supplied ``Retry-After`` always wins over the computed backoff:
        the vendor knows when it will be ready and we do not.
        """
        if retry_after is not None and retry_after >= 0:
            return float(retry_after)
        base = self.base_delay * (self.factor ** max(0, attempt - 1))
        spread = base * self.jitter
        return max(0.0, base + self.rng.uniform(-spread, spread))

    def delays(self, retry_after: float | None = None) -> Iterator[float]:
        """The full delay sequence for one operation."""
        for attempt in range(1, self.attempts):
            yield self.delay_for(attempt, retry_after)


@dataclass
class RateLimiter:
    """A bucket plus a policy — what a provider is handed."""

    bucket: TokenBucket = field(default_factory=TokenBucket)
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    sleeper: Callable[[float], None] = time.sleep

    def acquire(self, tokens: int = 1) -> float:
        return self.bucket.acquire(tokens)

    def wait_before_retry(self, attempt: int, retry_after: float | None = None) -> float:
        delay = self.policy.delay_for(attempt, retry_after)
        if delay > 0:
            self.sleeper(delay)
        return delay


def for_provider(
    *,
    requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
    burst: int = DEFAULT_BURST,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    seed: int = 0,
) -> RateLimiter:
    """Build a limiter with this project's defaults."""
    return RateLimiter(
        bucket=TokenBucket(
            rate_per_second=requests_per_second,
            burst=burst,
            clock=clock,
            sleeper=sleeper,
        ),
        policy=RetryPolicy(rng=random.Random(seed)),
        max_concurrency=max_concurrency,
        sleeper=sleeper,
    )


__all__ = [
    "DEFAULT_BURST",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_REQUESTS_PER_SECOND",
    "RETRYABLE_STATUSES",
    "RateLimiter",
    "RetryPolicy",
    "TokenBucket",
    "for_provider",
]
