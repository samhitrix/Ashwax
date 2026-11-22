"""Tests for the token-bucket API rate limiter."""

from __future__ import annotations

import pytest
from src.brokers.rate_limiter import KITE_RATE_LIMITS, RateLimiter, TokenBucket


class FakeClock:
    """Deterministic clock whose `sleep` advances virtual time."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


# ── TokenBucket ──────────────────────────────────────────────────────────────


def test_bucket_starts_full_and_consumes() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate_per_sec=10.0, clock=clock.now)
    # capacity defaults to max(1, rate) = 10 → 10 immediate acquisitions
    assert all(bucket.try_acquire() for _ in range(10))
    # 11th fails — bucket empty, no time elapsed
    assert bucket.try_acquire() is False


def test_bucket_refills_over_time() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate_per_sec=2.0, capacity=2.0, clock=clock.now)
    assert bucket.try_acquire(2.0)  # drain
    assert bucket.try_acquire() is False
    assert bucket.time_until_available() == pytest.approx(0.5)  # 1 token / 2 per sec
    clock.t += 0.5
    assert bucket.try_acquire() is True


def test_bucket_rejects_non_positive_rate() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate_per_sec=0.0)


# ── RateLimiter ──────────────────────────────────────────────────────────────


async def test_acquire_immediate_when_tokens_available() -> None:
    clock = FakeClock()
    limiter = RateLimiter({"general": 10.0}, clock=clock.now, sleep=clock.sleep)
    waited = await limiter.acquire("general")
    assert waited == 0.0


async def test_acquire_blocks_when_exhausted() -> None:
    clock = FakeClock()
    limiter = RateLimiter({"quote": 1.0, "general": 10.0}, clock=clock.now, sleep=clock.sleep)
    # quote capacity = 1 → first immediate, second must wait ~1s
    assert await limiter.acquire("quote") == 0.0
    waited = await limiter.acquire("quote")
    assert waited == pytest.approx(1.0)
    assert clock.t == pytest.approx(1.0)


async def test_unknown_category_falls_back_to_general() -> None:
    clock = FakeClock()
    limiter = RateLimiter({"general": 10.0}, clock=clock.now, sleep=clock.sleep)
    waited = await limiter.acquire("does_not_exist")
    assert waited == 0.0


def test_requires_general_category() -> None:
    with pytest.raises(ValueError):
        RateLimiter({"quote": 1.0})


def test_for_kite_has_expected_categories() -> None:
    limiter = RateLimiter.for_kite()
    assert set(KITE_RATE_LIMITS) == {"quote", "quote_priority", "historical", "order", "general"}
    # every bucket constructed
    assert limiter._buckets.keys() == KITE_RATE_LIMITS.keys()  # noqa: SLF001
