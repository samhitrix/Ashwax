"""LLMSwingJournalRepository — llm_swing_journal.

NOTE on upsert_run(): despite the name it now APPENDS one row per reasoning pass.
It used to upsert on run_date, so several passes in one day overwrote each other --
on 2026-09-22 five passes collapsed to one row and the three "no_trade_today" raw
responses (gemini) were destroyed by the later ACUTAAS proposals (groq). Those raws
are the only evidence of why the model declined, and the run that erased them was
often a different provider than the one erased. See migration 077.

Persisted "memory" for src/strategies/llm_swing/pipeline.py: one row per
run_date recording the day's market context, thesis, candidate ideas, and which
LLM produced them. Read back on later days so the agent's prompt can include a
summary of its own recent reasoning and outcomes. Never read by any CRM gate.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg


class LLMSwingJournalRepository:
    """Data access for the llm_swing_journal table."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def upsert_run(  # noqa: N802 — name kept; callers unchanged, semantics documented below
        self,
        run_date: date,
        market_context_summary: dict[str, Any],
        thesis_text: str | None,
        candidates_json: list[dict[str, Any]],
        decision: str,
        llm_model_used: str | None,
        raw_llm_response: str | None,
        session_window: str | None = None,
    ) -> int:
        """`session_window` ('market_hours' | 'after_hours' | None): computed by the
        caller from src.market.sessions against this row's real wall-clock time, not
        derived here from run_date alone. A restart-driven pass that actually ran
        after market close used to be indistinguishable from a normal one without
        checking created_at by hand (see migration alongside 077)."""
        import json

        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO llm_swing_journal
                        (run_date, market_context_summary, thesis_text,
                         candidates_json, decision, llm_model_used, raw_llm_response,
                         session_window)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                    RETURNING id
                    """,
                    run_date,
                    json.dumps(market_context_summary),
                    thesis_text,
                    json.dumps(candidates_json),
                    decision,
                    llm_model_used,
                    raw_llm_response,
                    session_window,
                )
            )

    # Decisions meaning "no LLM response was ever obtained" -- an attempt, not an
    # outcome. Kept out of has_run_today() on purpose: latching the day after ONE
    # failed attempt would just trade today's "retries forever" bug for "gives up
    # after the first hiccup," which is worse. Named to match the equivalent
    # llm_intraday_journal decisions (llm_failed / unusable_reply) rather than
    # inventing new vocabulary for the same failure shape.
    _FAILURE_DECISIONS = ("llm_failed", "unusable_reply")

    async def has_run_today(self, run_date: date) -> bool:
        """True if a pass that actually got an LLM response has already been
        journalled for run_date -- the once-daily latch for _llm_swing_scan_loop.
        Re-checked from the DB every tick rather than a file read once at loop start
        (2026-09-23): the old file was only written after a fully successful pass, so
        a pass that raised or a restart before the write left the day permanently
        unlatched -- 8 wasted origination calls on one day. A FAILED attempt does
        NOT latch the day (see failed_attempts_today) -- it should retry, bounded,
        not block the rest of the day outright."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                """SELECT 1 FROM llm_swing_journal
                    WHERE run_date = $1 AND decision != ALL($2::text[]) LIMIT 1""",
                run_date,
                list(self._FAILURE_DECISIONS),
            )
        return row is not None

    async def failed_attempts_today(self, run_date: date) -> int:
        """Count of FAILED attempts already journalled today, for bounding retries
        against the token budget rather than retrying forever (2026-09-23)."""
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(
                """SELECT count(*) FROM llm_swing_journal
                    WHERE run_date = $1 AND decision = ANY($2::text[])""",
                run_date,
                list(self._FAILURE_DECISIONS),
            )
        return int(count or 0)

    async def recent_runs(self, limit: int = 14) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    """
                    SELECT * FROM llm_swing_journal
                     ORDER BY run_date DESC, created_at DESC LIMIT $1
                    """,
                    limit,
                )
            )
