"""Tests for src/news/token_budget.py.

Built 2026-09-15 after a 3.5-day idle gap outlived the news dedup TTL (24h):
the first poll after restart reclassified 411 "fresh" items, and 658 Groq
calls in 90 minutes exhausted Groq's whole 200k/day cap — starving the
llm_intraday_agent / llm_swing_agent G9 reviews (also Groq-first) of
their only non-Gemini reviewer for the rest of the day. This budget caps how
many tokens the (non-trade-critical) news classifier may spend per day.
"""

from __future__ import annotations

import pytest
from src.news.token_budget import DailyTokenBudget


class _FakeRedis:
    """Mirrors the get/setex subset orchestrator._make_redis_client() adapters
    (both the real-Redis _Adapter and _InMemoryRedis) already expose."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._data[key] = value


@pytest.mark.asyncio  # type: ignore[misc]
async def test_remaining_is_full_limit_when_nothing_spent() -> None:
    budget = DailyTokenBudget(_FakeRedis(), "news:llm_tokens", daily_limit=1000)
    assert await budget.remaining("2026-09-15") == 1000


@pytest.mark.asyncio  # type: ignore[misc]
async def test_add_reduces_remaining() -> None:
    redis = _FakeRedis()
    budget = DailyTokenBudget(redis, "news:llm_tokens", daily_limit=1000)
    await budget.add("2026-09-15", 300)
    assert await budget.remaining("2026-09-15") == 700
    await budget.add("2026-09-15", 250)
    assert await budget.remaining("2026-09-15") == 450


@pytest.mark.asyncio  # type: ignore[misc]
async def test_remaining_floors_at_zero_never_negative() -> None:
    redis = _FakeRedis()
    budget = DailyTokenBudget(redis, "news:llm_tokens", daily_limit=1000)
    await budget.add("2026-09-15", 1500)  # a single call bigger than the cap
    assert await budget.remaining("2026-09-15") == 0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_days_are_independent() -> None:
    redis = _FakeRedis()
    budget = DailyTokenBudget(redis, "news:llm_tokens", daily_limit=1000)
    await budget.add("2026-09-15", 900)
    assert await budget.remaining("2026-09-16") == 1000  # a new day, untouched


@pytest.mark.asyncio  # type: ignore[misc]
async def test_add_ignores_non_positive_amounts() -> None:
    redis = _FakeRedis()
    budget = DailyTokenBudget(redis, "news:llm_tokens", daily_limit=1000)
    await budget.add("2026-09-15", 0)
    await budget.add("2026-09-15", -50)
    assert await budget.remaining("2026-09-15") == 1000


@pytest.mark.asyncio  # type: ignore[misc]
async def test_keys_are_namespaced_by_prefix() -> None:
    redis = _FakeRedis()
    a = DailyTokenBudget(redis, "news:llm_tokens", daily_limit=1000)
    b = DailyTokenBudget(redis, "other:budget", daily_limit=1000)
    await a.add("2026-09-15", 400)
    assert await b.remaining("2026-09-15") == 1000  # unaffected by a different budget
