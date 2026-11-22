"""Tests for Phase 3 news loop helpers: _InMemoryRedis, news_avoid TTL logic."""

from __future__ import annotations

import time

import pytest
from src.orchestrator import _InMemoryRedis, _NullLLM, _NullNewsRepo


class TestInMemoryRedis:
    @pytest.fixture
    def redis(self) -> _InMemoryRedis:
        return _InMemoryRedis()

    async def test_exists_returns_false_for_missing_key(self, redis: _InMemoryRedis) -> None:
        assert not await redis.exists("missing")

    async def test_set_then_exists_returns_true(self, redis: _InMemoryRedis) -> None:
        await redis.set("k1", "v", ex=60)
        assert await redis.exists("k1")

    async def test_expired_key_returns_false(self, redis: _InMemoryRedis) -> None:
        await redis.set("k2", "v", ex=1)
        redis._data["k2"] = ("v", time.time() - 1)  # force-expire
        assert not await redis.exists("k2")

    async def test_expired_key_is_cleaned_from_data(self, redis: _InMemoryRedis) -> None:
        await redis.set("k3", "v", ex=1)
        redis._data["k3"] = ("v", time.time() - 1)
        await redis.exists("k3")
        assert "k3" not in redis._data

    async def test_set_overwrites_existing(self, redis: _InMemoryRedis) -> None:
        await redis.set("k4", "old", ex=60)
        await redis.set("k4", "new", ex=60)
        assert redis._data["k4"][0] == "new"

    async def test_multiple_independent_keys(self, redis: _InMemoryRedis) -> None:
        await redis.set("a", "1", ex=60)
        await redis.set("b", "2", ex=60)
        assert await redis.exists("a")
        assert await redis.exists("b")


class TestNullLLM:
    async def test_complete_raises(self) -> None:
        with pytest.raises(RuntimeError):
            await _NullLLM().complete("any prompt")


class TestNullNewsRepo:
    async def test_insert_does_not_raise(self) -> None:
        await _NullNewsRepo().insert({"symbol": "TEST", "scope": "COMPANY"})


class TestNewsAvoidTTL:
    def test_active_symbol_survives_prune(self) -> None:
        now = time.time()
        avoid = {"RELIANCE": now + 60, "EXPIRED": now - 1}
        pruned = {s: exp for s, exp in avoid.items() if exp > now}
        assert "RELIANCE" in pruned
        assert "EXPIRED" not in pruned

    def test_all_expired_gives_empty(self) -> None:
        now = time.time()
        avoid = {"A": now - 10, "B": now - 1}
        pruned = {s: exp for s, exp in avoid.items() if exp > now}
        assert pruned == {}


class TestNewsLoopNonTradingDay:
    async def test_no_polling_or_classification_on_a_holiday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        import aiohttp
        from src import orchestrator as orch

        stop = asyncio.Event()
        polled: list[str] = []

        class _NoHttp:
            def __init__(self, *a: object, **k: object) -> None:
                polled.append("session")

        async def _sleep(_: float) -> None:
            stop.set()  # one skipped cycle, then end the loop

        monkeypatch.setattr(orch, "is_trading_day", lambda d: False)
        monkeypatch.setattr(orch, "_make_redis_client", lambda: _InMemoryRedis())
        monkeypatch.setattr(aiohttp, "ClientSession", _NoHttp)
        monkeypatch.setattr(orch.asyncio, "sleep", _sleep)

        await orch._news_loop({"news_avoid_symbols": {}}, {}, stop)
        assert polled == []
