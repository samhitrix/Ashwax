"""
Token-bucket API rate limiter — per broker API category.

Kite Connect enforces different request caps per API category. Exceeding them
returns HTTP 429 / "Too many requests". This limiter shapes outbound calls so
the adapter never trips the broker's limit.

Kite Connect documented per-second request caps (req/sec):
  quote:       1   (LTP / quote / OHLC snapshot)
  historical:  3   (historical_data)
  order:      10   (place / modify / cancel)
  general:    10   (everything else — positions, margins, orders book, GTT)

Design:
  - One TokenBucket per category, refilled continuously at `rate` tokens/sec.
  - `RateLimiter.acquire(category)` is async: it blocks (cooperatively) until a
    token is available, then consumes it. Returns the seconds waited (0.0 if
    immediate) so callers can log throttling.
  - clock + sleep are injectable for deterministic tests.

This module holds NO broker state and makes NO network calls — it only paces.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Protocol

_log = logging.getLogger(__name__)  # "src.brokers.rate_limiter" -> brokers.log

# Kite Connect per-second request caps by category.
KITE_RATE_LIMITS: dict[str, float] = {
    "quote": 1.0,
    # Time-critical price reads (options_intraday_short_premium stop/exit polling) get their OWN lane so
    # they never queue behind the scans in the shared "quote" bucket (measured 12 s per call, 2026-10-12).
    "quote_priority": 1.0,
    "historical": 3.0,
    "order": 10.0,
    "general": 10.0,
}

# Angel One SmartAPI per-second request caps by category (documented
# smartapi.angelone.in/docs/RateLimit, verified 2026-09-22). SmartAPI also
# publishes per-minute/per-hour ceilings, but this limiter — like Kite's — only
# paces per-second; a caller that respects the per-second cap on this call
# pattern (scans/backtests, not a request-flooding loop) never approaches
# those looser windows, same assumption already made for Kite.
#   order: place/modify/cancel share ONE combined 9 req/sec bucket (SmartAPI
#          enforces this cumulatively across the three) — reused here for GTT
#          create/modify/cancel too, which SmartAPI caps separately at 9/sec.
#   general: orderBook/tradeBook/position/holding are each capped at 1 req/sec
#            (getRMS is looser at 2/sec) — 1/sec is the conservative shared
#            bucket for all of them, same pattern as "order" above.
#   quote: market-data quote/LTP batches (50 tokens per request). The docs text
#          says 1 req/sec while their table says 10; the safe 1 is used
#          (Phase 0 findings B1).
#   historical: getCandleData is also capped at 120/min and 5000/hour, which this
#          per-second limiter does not model.
ANGEL_ONE_RATE_LIMITS: dict[str, float] = {
    "quote": 1.0,
    "historical": 3.0,
    "order": 9.0,
    "general": 1.0,
}

# Flattrade Pi API v2 caps as (per_second, per_minute) — Phase 0 findings A1
# ([FT-DOC] `order:{perSecond:10,perMinute:40}`, `api:{perSecond:40,perMinute:200}`).
#   order: PlaceOrder/ModifyOrder/CancelOrder/ExitSNOOrder and the GTT/OCO
#          place/modify/cancel calls (the client routes these here).
#   general: every other endpoint.
# Both caps bind, so these are enforced with SlidingWindowBucket rather than a
# single-rate TokenBucket: a token bucket with burst 10 at 40/60 s would allow
# 10 + 40 = 50 orders in the first minute. The account's real ceiling can be
# lower (rejection "exceeds Limit N"); that surfaces as a FlattradeError.
FLATTRADE_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "order": (10, 40),
    "general": (40, 200),
}

_DEFAULT_CATEGORY = "general"


class _Bucket(Protocol):
    def try_acquire(self, tokens: float = 1.0) -> bool: ...

    def time_until_available(self, tokens: float = 1.0) -> float: ...


class TokenBucket:
    """
    A single continuously-refilled token bucket.

    Starts full. `capacity` defaults to one second's worth of tokens
    (at least 1) so short bursts are allowed up to the per-second rate.
    """

    def __init__(
        self,
        rate_per_sec: float,
        capacity: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError(f"rate_per_sec must be > 0, got {rate_per_sec}")
        self._rate = rate_per_sec
        self._capacity = capacity if capacity is not None else max(1.0, rate_per_sec)
        self._tokens = self._capacity
        self._clock = clock
        self._updated = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._updated = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Consume `tokens` if available. Returns False without consuming if not."""
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    def time_until_available(self, tokens: float = 1.0) -> float:
        """Seconds until `tokens` would be available (0.0 if already available)."""
        self._refill()
        if self._tokens >= tokens:
            return 0.0
        return (tokens - self._tokens) / self._rate


class SlidingWindowBucket:
    """
    Enforces several caps at once: at most `max_calls` acquisitions in ANY
    trailing `window_secs`, for every (max_calls, window_secs) pair in `caps`.

    Same try_acquire/time_until_available interface as TokenBucket, so
    RateLimiter can hold either. `tokens` is rounded up to a whole call count.
    """

    # Minimum positive wait, so a float-rounding residue can never stall the
    # acquire loop with a zero-length sleep.
    _MIN_WAIT = 1e-6

    def __init__(
        self,
        caps: list[tuple[int, float]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not caps or any(n < 1 or w <= 0 for n, w in caps):
            raise ValueError(f"caps must be non-empty (max_calls>=1, window_secs>0), got {caps}")
        self._caps = list(caps)
        self._horizon = max(w for _, w in caps)
        self._stamps: deque[float] = deque()
        self._clock = clock

    def time_until_available(self, tokens: float = 1.0) -> float:
        """Seconds until `tokens` calls would fit under every cap (0.0 if now)."""
        now = self._clock()
        while self._stamps and self._stamps[0] <= now - self._horizon:
            self._stamps.popleft()
        n = math.ceil(tokens)
        wait = 0.0
        for max_calls, window in self._caps:
            if n > max_calls:
                raise ValueError(f"{n} calls can never fit a cap of {max_calls}/{window}s")
            recent = [s for s in self._stamps if s > now - window]
            excess = len(recent) + n - max_calls
            if excess > 0:
                # The oldest `excess` calls in this window must age out first.
                wait = max(wait, recent[excess - 1] + window - now, self._MIN_WAIT)
        return wait

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Record `tokens` calls if every cap allows them now; else False."""
        if self.time_until_available(tokens) > 0:
            return False
        now = self._clock()
        self._stamps.extend([now] * math.ceil(tokens))
        return True


class RateLimiter:
    """
    Per-category token-bucket limiter.

    Usage:
        limiter = RateLimiter.for_kite()
        await limiter.acquire("order")   # blocks until an order slot is free
        broker.place_order(...)
    """

    def __init__(
        self,
        limits: dict[str, float],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        buckets: dict[str, _Bucket] | None = None,
    ) -> None:
        if _DEFAULT_CATEGORY not in limits:
            raise ValueError(f"limits must include a {_DEFAULT_CATEGORY!r} category")
        self._buckets: dict[str, _Bucket] = {
            cat: TokenBucket(rate, clock=clock) for cat, rate in limits.items()
        }
        # Optional per-category replacement buckets (e.g. SlidingWindowBucket for
        # brokers with several binding windows); categories must exist in `limits`.
        if buckets:
            unknown = set(buckets) - set(limits)
            if unknown:
                raise ValueError(f"buckets for unknown categories: {sorted(unknown)}")
            self._buckets.update(buckets)
        self._locks = {cat: asyncio.Lock() for cat in limits}
        self._sleep = sleep

    async def acquire(self, category: str = _DEFAULT_CATEGORY, tokens: float = 1.0) -> float:
        """
        Block until a token is available for `category`, then consume it.

        Unknown categories fall back to the 'general' bucket.
        Returns the total seconds waited (0.0 if acquired immediately).
        """
        bucket = self._buckets.get(category, self._buckets[_DEFAULT_CATEGORY])
        lock = self._locks.get(category, self._locks[_DEFAULT_CATEGORY])

        async with lock:
            waited = 0.0
            while not bucket.try_acquire(tokens):
                wait = bucket.time_until_available(tokens)
                _log.debug(
                    "rate_limit_found category=%s action=throttled wait_secs=%.3f",
                    category,
                    wait,
                )
                await self._sleep(wait)
                waited += wait
            if waited > 0:
                _log.debug(
                    "rate_limit_action category=%s action=acquired_after_wait total_wait_secs=%.3f",
                    category,
                    waited,
                )
            return waited

    @classmethod
    def for_kite(
        cls,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> RateLimiter:
        """Construct a limiter pre-loaded with Kite Connect's category caps."""
        return cls(KITE_RATE_LIMITS, clock=clock, sleep=sleep)

    @classmethod
    def for_angel_one(
        cls,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> RateLimiter:
        """Construct a limiter pre-loaded with Angel One SmartAPI's category caps."""
        return cls(ANGEL_ONE_RATE_LIMITS, clock=clock, sleep=sleep)

    @classmethod
    def for_flattrade(
        cls,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> RateLimiter:
        """Construct a limiter enforcing Flattrade's per-second AND per-minute caps."""
        return cls(
            {cat: per_min / 60.0 for cat, (_per_s, per_min) in FLATTRADE_RATE_LIMITS.items()},
            clock=clock,
            sleep=sleep,
            buckets={
                cat: SlidingWindowBucket([(per_s, 1.0), (per_min, 60.0)], clock=clock)
                for cat, (per_s, per_min) in FLATTRADE_RATE_LIMITS.items()
            },
        )
