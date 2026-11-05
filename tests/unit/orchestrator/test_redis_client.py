"""Shared, REDIS_URL-driven cache client that degrades to in-memory when Redis is down."""

from __future__ import annotations

import pytest
import src.orchestrator as orch


@pytest.fixture(autouse=True)
def _reset(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(orch, "_REDIS_CLIENT", None)


def test_client_is_a_process_wide_singleton() -> None:
    assert orch._make_redis_client() is orch._make_redis_client()


def test_redis_url_env_is_used(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, str] = {}

    class _FakeRedis:
        @classmethod
        def from_url(cls, url, **kw):  # type: ignore[no-untyped-def]
            seen["url"] = url
            return cls()

    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "Redis", _FakeRedis)
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid:6390/2")
    orch._make_redis_client()
    assert seen["url"] == "redis://example.invalid:6390/2"


def test_default_url_when_env_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, str] = {}

    class _FakeRedis:
        @classmethod
        def from_url(cls, url, **kw):  # type: ignore[no-untyped-def]
            seen["url"] = url
            return cls()

    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "Redis", _FakeRedis)
    monkeypatch.delenv("REDIS_URL", raising=False)
    orch._make_redis_client()
    assert seen["url"] == "redis://localhost:6379/0"


async def test_unreachable_redis_degrades_to_in_memory_instead_of_raising(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class _Down:
        @classmethod
        def from_url(cls, url, **kw):  # type: ignore[no-untyped-def]
            return cls()

        async def exists(self, key):  # type: ignore[no-untyped-def]
            raise ConnectionError("down")

        async def set(self, key, value, ex=None):  # type: ignore[no-untyped-def]
            raise ConnectionError("down")

        async def get(self, key):  # type: ignore[no-untyped-def]
            raise ConnectionError("down")

        async def setex(self, key, ttl, value):  # type: ignore[no-untyped-def]
            raise ConnectionError("down")

    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "Redis", _Down)
    client = orch._make_redis_client()
    await client.setex("k", 60, "v")
    assert await client.get("k") == "v"  # served from the in-memory fallback
    assert await client.exists("k") is True
