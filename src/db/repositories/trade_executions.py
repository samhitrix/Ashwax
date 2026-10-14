"""Read side of the append-only `trade_executions` ledger (migration 083).

The ledger is WRITTEN by database triggers on trades / paper_trades / options_positions, so no
application code can forget to record an entry, partial, exit, stop move or adjustment. This module
only reads it and checks that it adds up.
"""

from __future__ import annotations

from typing import Any

# Closed equity trades whose ledger quantities do not add up: shares bought must equal shares sold
# in partials plus the final leg. Partials backfilled with an unknown quantity (qty_known = false)
# are excluded -- they are flagged, not wrong.
_INCONSISTENT_SQL = """
SELECT trade_id, entry_qty, partial_qty, final_qty
  FROM (
    SELECT trade_id,
           max(quantity) FILTER (WHERE event = 'ENTRY_FILL')  AS entry_qty,
           COALESCE(sum(quantity) FILTER (WHERE event = 'PARTIAL_EXIT'), 0) AS partial_qty,
           max(quantity) FILTER (WHERE event = 'FINAL_EXIT')  AS final_qty,
           COALESCE(bool_and(qty_known) FILTER (WHERE event = 'PARTIAL_EXIT'), TRUE) AS all_known
      FROM trade_executions
     WHERE instrument = 'EQUITY'
     GROUP BY trade_id
  ) t
 WHERE entry_qty IS NOT NULL AND final_qty IS NOT NULL AND all_known
   AND entry_qty <> partial_qty + final_qty
 ORDER BY trade_id
"""


class TradeExecutionsRepository:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def for_trade(self, trade_id: str) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            "SELECT * FROM trade_executions WHERE trade_id = $1 ORDER BY occurred_at, id", trade_id
        )
        return [dict(r) for r in rows]

    async def recent(
        self,
        limit: int = 200,
        event: str | None = None,
        instrument: str | None = None,
        is_paper: bool | None = None,
    ) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            """
            SELECT * FROM trade_executions
             WHERE ($2::text IS NULL OR event = $2)
               AND ($3::text IS NULL OR instrument = $3)
               AND ($4::boolean IS NULL OR is_paper = $4)
             ORDER BY occurred_at DESC, id DESC LIMIT $1
            """,
            max(1, min(int(limit), 2000)),
            event,
            instrument,
            is_paper,
        )
        return [dict(r) for r in rows]

    async def ledger_errors_after(self, last_id: int) -> list[dict[str, Any]]:
        """LEDGER_ERROR rows the triggers wrote (a ledger write that failed) after `last_id`."""
        rows = await self._pool.fetch(
            "SELECT id, trade_id, detail FROM trade_executions "
            "WHERE event = 'LEDGER_ERROR' AND id > $1 ORDER BY id",
            last_id,
        )
        return [dict(r) for r in rows]

    async def find_inconsistencies(self) -> list[dict[str, Any]]:
        return [dict(r) for r in await self._pool.fetch(_INCONSISTENT_SQL)]
