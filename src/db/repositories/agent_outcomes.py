"""Recent closed-trade outcomes for an LLM agent's prompt memory.

Shared by llm_intraday_journal and llm_swing_journal. Reads BOTH paper_trades and
trades and tags each row with its mode: an agent that executes live used to read
paper_trades only, so its prompt showed stale paper results and never its real
fills. R uses the same formula as
orchestrator._fetch_closed_trades_from_db (risk over the ORIGINAL size, incl. partials).
Prompt context only — never read by any CRM gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

_SQL = """
SELECT * FROM (
    SELECT 'PAPER' AS mode, entry_timestamp::date AS entry_date, exit_timestamp, symbol,
           direction, exit_reason,
           pnl_realized / NULLIF(ABS(fill_price - initial_sl)
               * (quantity + COALESCE(partial_qty_booked, 0)), 0) AS r
      FROM paper_trades
     WHERE strategy_name = $1 AND order_state = 'CLOSED'
       AND exit_reason IS DISTINCT FROM 'manual_dup_entry_cleanup'
    UNION ALL
    SELECT 'LIVE' AS mode, entry_timestamp::date AS entry_date, exit_timestamp, symbol,
           direction, exit_reason,
           pnl_realized / NULLIF(ABS(fill_price - initial_sl)
               * (quantity + COALESCE(partial_qty_booked, 0)), 0) AS r
      FROM trades
     WHERE strategy_name = $1 AND order_state = 'CLOSED'
) x
ORDER BY exit_timestamp DESC NULLS LAST
LIMIT $2
"""


async def fetch_recent_outcomes(
    pool: asyncpg.Pool, strategy: str, limit: int
) -> list[asyncpg.Record]:
    """Newest-first closed trades of `strategy` from both modes, each tagged
    mode='PAPER'|'LIVE'. limit<=0 returns [] (rollback switch: memory off)."""
    if limit <= 0:
        return []
    async with pool.acquire() as conn:
        return list(await conn.fetch(_SQL, strategy, limit))
