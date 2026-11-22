"""LLMCallLogRepository — llm_call_log (migration 080).

One row per LLM origination call SENT, written before the call. The day's call caps
(llm_swing.max_calls_per_day, llm_intraday.trigger.max_passes_per_day) are enforced
against this table, so a restart or a mid-call kill cannot reset or hide them.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


class LLMCallLogRepository:
    """Data access for the llm_call_log table."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def record(self, purpose: str, run_date: date, note: str = "") -> None:
        """Log that a call for `purpose` is about to be sent. Call BEFORE the request."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO llm_call_log (purpose, run_date, note) VALUES ($1, $2, $3)",
                purpose,
                run_date,
                note,
            )

    async def count_today(self, purpose: str, run_date: date) -> int:
        """Calls already sent for `purpose` on `run_date`."""
        async with self._pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM llm_call_log WHERE purpose = $1 AND run_date = $2",
                purpose,
                run_date,
            )
        return int(n or 0)

    async def last_call_ts(self, purpose: str, run_date: date) -> float | None:
        """Unix time of the most recent call for `purpose` on `run_date` (None if none)."""
        async with self._pool.acquire() as conn:
            ts = await conn.fetchval(
                "SELECT max(called_at) FROM llm_call_log WHERE purpose = $1 AND run_date = $2",
                purpose,
                run_date,
            )
        return ts.timestamp() if ts is not None else None
