"""FundamentalsRepository — fundamentals_snapshot.

MVP fundamentals store (src/market_data/fundamentals_fetcher.py, yfinance
source). Log-only: nothing here is read by a CRM gate until
scripts/backtest_fundamentals_filter.py validates an edge.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

_log = logging.getLogger(__name__)


class FundamentalsRepository:
    """Data access for the fundamentals_snapshot table."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def upsert_snapshot(self, snapshot_date: date, row: dict[str, Any]) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO fundamentals_snapshot
                    (symbol, snapshot_date, trailing_pe, forward_pe, trailing_eps,
                     forward_eps, earnings_growth, revenue_growth, return_on_equity,
                     debt_to_equity, market_cap, dividend_yield, yahoo_sector)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                ON CONFLICT (symbol, snapshot_date) DO UPDATE
                  SET trailing_pe      = EXCLUDED.trailing_pe,
                      forward_pe       = EXCLUDED.forward_pe,
                      trailing_eps     = EXCLUDED.trailing_eps,
                      forward_eps      = EXCLUDED.forward_eps,
                      earnings_growth  = EXCLUDED.earnings_growth,
                      revenue_growth   = EXCLUDED.revenue_growth,
                      return_on_equity = EXCLUDED.return_on_equity,
                      debt_to_equity   = EXCLUDED.debt_to_equity,
                      market_cap       = EXCLUDED.market_cap,
                      dividend_yield   = EXCLUDED.dividend_yield,
                      yahoo_sector     = EXCLUDED.yahoo_sector,
                      fetched_at       = now()
                """,
                row.get("symbol"),
                snapshot_date,
                row.get("trailingPE"),
                row.get("forwardPE"),
                row.get("trailingEps"),
                row.get("forwardEps"),
                row.get("earningsGrowth"),
                row.get("revenueGrowth"),
                row.get("returnOnEquity"),
                row.get("debtToEquity"),
                row.get("marketCap"),
                row.get("dividendYield"),
                row.get("sector"),
            )

    async def upsert_snapshot_batch(self, snapshot_date: date, rows: list[dict[str, Any]]) -> int:
        n = 0
        for row in rows:
            try:
                await self.upsert_snapshot(snapshot_date, row)
                n += 1
            except Exception as exc:
                _log.warning(
                    "fundamentals_upsert_failed symbol=%s err=%s: %s",
                    row.get("symbol"),
                    type(exc).__name__,
                    exc,
                )
        if n < len(rows):
            _log.warning("fundamentals_upsert_partial persisted=%d/%d", n, len(rows))
        return n

    async def latest_market_caps(self, symbols: list[str]) -> dict[str, float]:
        """Latest market cap per symbol, in INR (yfinance units). Symbols with no
        snapshot or a NULL cap are absent from the result."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (symbol) symbol, market_cap
                  FROM fundamentals_snapshot
                 WHERE symbol = ANY($1::text[]) AND market_cap IS NOT NULL
                 ORDER BY symbol, snapshot_date DESC
                """,
                symbols,
            )
        return {r["symbol"]: float(r["market_cap"]) for r in rows}

    async def latest_for_symbol(self, symbol: str) -> asyncpg.Record | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT * FROM fundamentals_snapshot
                 WHERE symbol = $1
                 ORDER BY snapshot_date DESC LIMIT 1
                """,
                symbol,
            )
