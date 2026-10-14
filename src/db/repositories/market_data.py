"""MarketDataRepository — market_regime_log, rs_rankings, earnings_calendar."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


class MarketDataRepository:
    """Data access for market data tables."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def log_regime(
        self,
        regime: str,
        vix: Decimal | None = None,
        nifty_ma50: Decimal | None = None,
        nifty_ma200: Decimal | None = None,
        breadth_pct: Decimal | None = None,
        confidence: Decimal | None = None,
        adx: Decimal | None = None,
        atr_pct: Decimal | None = None,
        suitable_strategies: list[str] | None = None,
        avoid_strategies: list[str] | None = None,
        historical_accuracy: Decimal | None = None,
        position_size_multiplier: Decimal | None = None,
    ) -> int:
        import json as _json

        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO market_regime_log
                        (regime, vix, nifty_ma50, nifty_ma200, breadth_pct, confidence,
                         adx, atr_pct, suitable_strategies, avoid_strategies,
                         historical_accuracy, position_size_multiplier)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    RETURNING id
                    """,
                    regime,
                    vix,
                    nifty_ma50,
                    nifty_ma200,
                    breadth_pct,
                    confidence,
                    adx,
                    atr_pct,
                    _json.dumps(suitable_strategies) if suitable_strategies is not None else None,
                    _json.dumps(avoid_strategies) if avoid_strategies is not None else None,
                    historical_accuracy,
                    position_size_multiplier,
                )
            )

    async def latest_regime(self) -> asyncpg.Record | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM market_regime_log ORDER BY created_at DESC LIMIT 1"
            )

    async def upsert_rs(
        self, symbol: str, rs_score: Decimal, percentile_rank: int | None = None
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO rs_rankings (symbol, rs_score, percentile_rank) VALUES ($1, $2, $3)",
                symbol,
                rs_score,
                percentile_rank,
            )

    async def latest_rs_snapshot(self) -> list[asyncpg.Record]:
        """Most recent RS score per symbol."""
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    """
                    SELECT DISTINCT ON (symbol)
                           symbol, rs_score, percentile_rank, created_at
                      FROM rs_rankings
                     ORDER BY symbol, created_at DESC
                    """
                )
            )

    async def upsert_earnings(
        self, symbol: str, report_date: date, period: str | None = None
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO earnings_calendar (symbol, report_date, period, fetched_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (symbol, report_date) DO UPDATE
                    SET period = EXCLUDED.period, fetched_at = NOW()
                """,
                symbol,
                report_date,
                period,
            )

    async def upcoming_earnings(self, within_days: int = 5) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    """
                    SELECT * FROM earnings_calendar
                     WHERE report_date BETWEEN CURRENT_DATE AND CURRENT_DATE + $1
                     ORDER BY report_date ASC
                    """,
                    within_days,
                )
            )
