"""
TradeRepository — CRUD for trades and paper_trades tables.

Both tables share identical columns (paper_trades adds is_paper + simulated_fill_ts).
The is_paper flag selects the target table; callers never reference table names directly.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

_LOG = logging.getLogger(__name__)


class TradeRepository:
    """Data access for trades and paper_trades."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        # table -> whether it has exec_broker (migration 089); checked once per process.
        self._exec_broker_col: dict[str, bool] = {}

    def _table(self, is_paper: bool) -> str:
        return "paper_trades" if is_paper else "trades"

    async def insert(self, data: dict[str, Any], is_paper: bool = False) -> int:
        """
        Insert a new trade record.

        Args:
            data: Column to value mapping. Must include agent_trade_id, symbol,
                  direction, quantity.
            is_paper: Write to paper_trades when True.

        Returns:
            The new row id.
        """
        table = self._table(is_paper)
        if "exec_broker" in data and not await self._has_exec_broker(table):
            # A DB without migration 089 must still get the row: entry persistence is what
            # every exit/reseed path reads, and NULL already means legacy "kite".
            _LOG.error(
                "trade_exec_broker_column_missing table=%s trade=%s exec_broker=%s -- "
                "migration 089 not applied; row written without it",
                table,
                data.get("agent_trade_id"),
                data["exec_broker"],
            )
            if data["exec_broker"] != "kite":
                # Stripped, the row reads back as NULL = a Kite row: its reseed/exits would go
                # to the kite stack. Still written -- losing the entry row is worse.
                _LOG.critical(
                    "trade_exec_broker_lost table=%s trade=%s exec_broker=%s -- row now "
                    "indistinguishable from a Kite row",
                    table,
                    data.get("agent_trade_id"),
                    data["exec_broker"],
                )
                try:
                    from src.infrastructure import operator_alerts as _oa

                    _oa.raise_alert(
                        f"Trade {data.get('agent_trade_id')} placed on {data['exec_broker']} "
                        "was saved without exec_broker (migration 089 missing): it will be "
                        "treated as a Kite position. Apply migration 089 and fix the row.",
                        source="trade_exec_broker_lost",
                        tier="CRITICAL",
                        dedupe_seconds=600,
                    )
                except Exception as exc:  # noqa: BLE001 -- the CRITICAL log already fired
                    _LOG.error("trade_exec_broker_lost_alert_failed err=%s", exc)
            data = {k: v for k, v in data.items() if k != "exec_broker"}
        cols = list(data.keys())
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        col_list = ", ".join(cols)
        sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) RETURNING id"  # noqa: S608
        async with self._pool.acquire() as conn:
            return int(await conn.fetchval(sql, *data.values()))

    async def _has_exec_broker(self, table: str) -> bool:
        """Whether `table` has the exec_broker column; cached once known. An unreadable
        catalog is not cached and reported as present (the INSERT then decides)."""
        if table not in self._exec_broker_col:
            try:
                async with self._pool.acquire() as conn:
                    found = await conn.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = $1 "
                        "AND column_name = 'exec_broker')",
                        table,
                    )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("exec_broker_column_check_failed table=%s err=%s", table, exc)
                return True
            self._exec_broker_col[table] = bool(found)
        return self._exec_broker_col[table]

    async def get_by_agent_id(
        self, agent_trade_id: str, is_paper: bool = False
    ) -> asyncpg.Record | None:
        """Fetch a single trade by the internal agent_trade_id."""
        table = self._table(is_paper)
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                f"SELECT * FROM {table} WHERE agent_trade_id = $1",  # noqa: S608
                agent_trade_id,
            )

    async def update_state(
        self,
        agent_trade_id: str,
        order_state: str,
        is_paper: bool = False,
        **kwargs: Any,
    ) -> None:
        """Update order_state and any additional columns atomically."""
        table = self._table(is_paper)
        updates: dict[str, Any] = {"order_state": order_state, **kwargs}
        set_clauses = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(updates.keys()))
        values = list(updates.values())
        sql = f"UPDATE {table} SET {set_clauses}, updated_at = NOW() WHERE agent_trade_id = $1"  # noqa: S608
        async with self._pool.acquire() as conn:
            await conn.execute(sql, agent_trade_id, *values)

    async def update_fill(
        self,
        agent_trade_id: str,
        fill_price: Decimal,
        fill_timestamp: datetime,
        actual_risk_rupees: Decimal | None = None,
        actual_rr_ratio: Decimal | None = None,
        is_paper: bool = False,
    ) -> None:
        """Record actual fill price after broker confirmation.

        `WHERE ... AND order_state != 'CLOSED'` (2026-09-23): this is also the
        target of _price_truth_phase2_sweep's late entry-fill correction, which
        iterates a position snapshot taken at the top of a monitor cycle. If the
        position closes (a different async loop's SmartExitTracker calling
        force_exit) in the gap between that snapshot and this call landing, the
        unconditional `order_state = 'FILLED'` here silently reopened an
        already-CLOSED trade -- exit_price/exit_reason/pnl_realized stayed
        correct (written by update_pnl/update_state, an entirely separate call),
        only order_state reverted, so the dashboard kept showing a fully
        resolved trade as active. Observed live: AUBANK exit at 10:51:18 UTC,
        clobbered back to FILLED by an update_fill at 10:51:52 UTC. A CLOSED
        trade's entry fill price is never legitimately re-confirmed after the
        fact, so this is a pure no-op for that case, not a narrowing of intent."""
        table = self._table(is_paper)
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                UPDATE {table}
                   SET fill_price = $2,
                       fill_timestamp = $3,
                       actual_risk_rupees = COALESCE($4, actual_risk_rupees),
                       actual_rr_ratio = COALESCE($5, actual_rr_ratio),
                       order_state = 'FILLED',
                       updated_at = NOW()
                 WHERE agent_trade_id = $1 AND order_state != 'CLOSED'
                """,  # noqa: S608
                agent_trade_id,
                fill_price,
                fill_timestamp,
                actual_risk_rupees,
                actual_rr_ratio,
            )

    async def update_pnl(
        self,
        agent_trade_id: str,
        pnl_realized: Decimal,
        charges_taxes: Decimal,
        exit_price: Decimal,
        exit_timestamp: datetime,
        is_paper: bool = False,
        partial_pnl_booked: Decimal | None = None,
        partial_qty_booked: int | None = None,
        exit_reason: str | None = None,
        charges_breakdown: str | None = None,
        broker: str | None = None,
    ) -> None:
        """Record realized P&L and exit details on trade close.

        charges_breakdown is the whole-trade itemised cost JSON (partials + last leg); broker is
        the rate card it was computed from.

        `pnl_realized` is the LAST LEG's net; the stored pnl_realized is that plus
        partial_pnl_booked (whatever book_partial() already banked), added inside this one
        UPDATE so no caller can record only the last leg — the 2026-09-24 LAURUSLABS bug,
        where a partial-then-close trade lost half its P&L. Pass the partial_* args to also
        (re)write the running partial columns atomically, covering a failed earlier
        best-effort partial persist; omit them to use what the row already holds.
        """
        table = self._table(is_paper)
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                UPDATE {table}
                   SET pnl_realized = $2 + COALESCE($6, partial_pnl_booked),
                       partial_pnl_booked = COALESCE($6, partial_pnl_booked),
                       partial_qty_booked = COALESCE($7, partial_qty_booked),
                       exit_reason = COALESCE($8, exit_reason),
                       charges_breakdown = COALESCE($9::jsonb, charges_breakdown),
                       broker = COALESCE($10, broker),
                       charges_taxes = $3,
                       exit_price = $4,
                       exit_timestamp = $5,
                       order_state = 'CLOSED',
                       updated_at = NOW()
                 WHERE agent_trade_id = $1
                """,  # noqa: S608
                agent_trade_id,
                pnl_realized,
                charges_taxes,
                exit_price,
                exit_timestamp,
                partial_pnl_booked,
                partial_qty_booked,
                exit_reason,
                charges_breakdown,
                broker,
            )

    async def open_trades(
        self, is_paper: bool = False, exec_broker: str | None = None
    ) -> list[asyncpg.Record]:
        """Return all trades not in a terminal state.

        Args:
            is_paper: Read paper_trades when True.
            exec_broker: Only rows held by this broker (NULL exec_broker = "kite").
                None = every row, as before.
        """
        table = self._table(is_paper)
        terminal = ("CLOSED", "REJECTED", "CANCELLED", "TIMED_OUT")
        placeholders = ", ".join(f"${i + 1}" for i in range(len(terminal)))
        args: list[Any] = list(terminal)
        broker_sql = ""
        if exec_broker is not None:
            args.append(exec_broker)
            broker_sql = f" AND COALESCE(exec_broker, 'kite') = ${len(args)}"
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    f"SELECT * FROM {table} WHERE order_state NOT IN ({placeholders})"  # noqa: S608
                    f"{broker_sql} ORDER BY entry_timestamp ASC",
                    *args,
                )
            )

    async def closed_trades(
        self,
        limit: int = 100,
        offset: int = 0,
        is_paper: bool = False,
    ) -> list[asyncpg.Record]:
        """Return closed trades, newest first."""
        table = self._table(is_paper)
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    f"SELECT * FROM {table} WHERE order_state = 'CLOSED'"  # noqa: S608
                    " ORDER BY exit_timestamp DESC LIMIT $1 OFFSET $2",
                    limit,
                    offset,
                )
            )

    async def daily_realized_pnl(self, date_str: str, is_paper: bool = False) -> Decimal:
        """Sum of realized P&L for trades closed on the given date (YYYY-MM-DD)."""
        table = self._table(is_paper)
        async with self._pool.acquire() as conn:
            val = await conn.fetchval(
                f"""
                SELECT COALESCE(SUM(pnl_realized), 0)
                  FROM {table}
                 WHERE order_state = 'CLOSED'
                   AND exit_timestamp::date = $1::date
                """,  # noqa: S608
                date_str,
            )
        return Decimal(str(val))
