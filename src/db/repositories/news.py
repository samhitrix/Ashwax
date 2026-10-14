"""NewsRepository — news_events table."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


class NewsRepository:
    """Data access for news_events."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def insert(
        self,
        headline: str,
        source: str,
        event_type: str,
        sentiment: str,
        symbol: str | None = None,
        magnitude: Decimal | None = None,
        trading_action: str | None = None,
        avoid_duration_minutes: int | None = None,
        confidence: Decimal | None = None,
        reasoning: str | None = None,
        affected_symbols: list[str] | None = None,
    ) -> int:
        import json

        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO news_events
                        (symbol, headline, source, event_type, sentiment, magnitude,
                         trading_action, avoid_duration_minutes, confidence,
                         reasoning, affected_symbols)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)
                    RETURNING id
                    """,
                    symbol,
                    headline,
                    source,
                    event_type,
                    sentiment,
                    magnitude,
                    trading_action,
                    avoid_duration_minutes,
                    confidence,
                    reasoning,
                    json.dumps(affected_symbols) if affected_symbols else None,
                )
            )

    async def recent(self, limit: int = 50) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch("SELECT * FROM news_events ORDER BY ts DESC LIMIT $1", limit)
            )

    async def for_symbol(self, symbol: str, limit: int = 20) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    "SELECT * FROM news_events WHERE symbol = $1 ORDER BY ts DESC LIMIT $2",
                    symbol,
                    limit,
                )
            )
