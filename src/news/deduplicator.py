"""
News Deduplicator â€” Redis SHA256 dedup with 24h TTL.

Key format: news:dedup:{sha256_hex}
TTL: 86400 seconds (24 hours)

Inject a RedisClient Protocol for testability.
"""

from __future__ import annotations

from typing import Protocol

from src.infrastructure.logger import get_logger

logger = get_logger(__name__)

_KEY_PREFIX = "news:dedup:"
_TTL_SECONDS = 86_400  # 24 hours


class RedisClient(Protocol):
    """Minimal Redis interface needed by deduplicator."""

    async def exists(self, key: str) -> bool: ...
    async def set(self, key: str, value: str, ex: int) -> None: ...


class Deduplicator:
    """
    Tracks seen news items by SHA256 hash in Redis.

    Usage:
        dedup = Deduplicator(redis_client)
        if not await dedup.is_seen(item.dedup_hash):
            await dedup.mark_seen(item.dedup_hash)
            # process item
    """

    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    async def is_seen(self, dedup_hash: str) -> bool:
        return await self._redis.exists(_KEY_PREFIX + dedup_hash)

    async def mark_seen(self, dedup_hash: str) -> None:
        await self._redis.set(_KEY_PREFIX + dedup_hash, "1", ex=_TTL_SECONDS)
        logger.debug("dedup_marked hash=%s", dedup_hash[:12])
