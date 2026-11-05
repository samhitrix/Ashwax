"""LLMIntradayJournalRepository — llm_intraday_journal.

Persisted "memory" for src/strategies/llm_intraday/pipeline.py: one row per
reasoning pass (several per day) recording the market snapshot, thesis, the
validated ideas and which LLM produced them. Outcomes are read straight from
paper_trades so the agent's prompt sees its real closed-trade R, not just what
it proposed. Never read by any CRM gate.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

_STRATEGY = "llm_intraday_agent"


class LLMIntradayJournalRepository:
    """Data access for the llm_intraday_journal table (+ its paper_trades outcomes)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def insert_run(
        self,
        run_ts: datetime,
        market_context: dict[str, Any],
        thesis_text: str | None,
        ideas_json: list[dict[str, Any]],
        decision: str,
        llm_model_used: str | None,
        raw_llm_response: str | None,
        rejected_json: list[dict[str, Any]] | None = None,
    ) -> int:
        import json

        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO llm_intraday_journal
                        (run_ts, run_date, market_context, thesis_text,
                         ideas_json, decision, llm_model_used, raw_llm_response,
                         rejected_json)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    ON CONFLICT (run_ts) DO UPDATE
                      SET market_context   = EXCLUDED.market_context,
                          thesis_text      = EXCLUDED.thesis_text,
                          ideas_json       = EXCLUDED.ideas_json,
                          decision         = EXCLUDED.decision,
                          llm_model_used   = EXCLUDED.llm_model_used,
                          raw_llm_response = EXCLUDED.raw_llm_response,
                          rejected_json    = EXCLUDED.rejected_json
                    RETURNING id
                    """,
                    run_ts,
                    run_ts.date(),
                    json.dumps(market_context),
                    thesis_text,
                    json.dumps(ideas_json),
                    decision,
                    llm_model_used,
                    raw_llm_response,
                    json.dumps(rejected_json or []),
                )
            )

    async def todays_runs(self, run_date: date) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    "SELECT * FROM llm_intraday_journal WHERE run_date = $1 ORDER BY run_ts",
                    run_date,
                )
            )

    async def recent_outcomes(self, limit: int = 20) -> list[asyncpg.Record]:
        """Most recent closed paper trades of this agent with realised R — same
        R formula as orchestrator._fetch_closed_trades_from_db (attribution)."""
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    """
                    SELECT entry_timestamp::date AS entry_date, symbol, direction, exit_reason,
                           pnl_realized / NULLIF(ABS(fill_price - initial_sl) * quantity, 0) AS r
                      FROM paper_trades
                     WHERE strategy_name = $1 AND order_state = 'CLOSED'
                     ORDER BY exit_timestamp DESC NULLS LAST
                     LIMIT $2
                    """,
                    _STRATEGY,
                    limit,
                )
            )
