"""The execution ledger (migration 083) records every entry / partial / exit / stop move /
adjustment for EVERY instrument, written by DB triggers so no code path can forget (2026-09-28:
LAURUSLABS showed 13 shares next to P&L for 26; 13 older partial trades had no sold quantity
anywhere; options_execution_log / options_adjustment_log were never written).

Each test applies the real migration SQL inside a transaction, drives real rows, and ROLLS BACK --
your data is never touched. Skipped when no DB is reachable."""

from __future__ import annotations

import os
import pathlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

_DB = os.environ.get("DB_URL")
if not _DB:
    try:
        from dotenv import dotenv_values

        _DB = dotenv_values(pathlib.Path(__file__).parents[3] / ".env").get("DB_URL")
    except Exception:  # noqa: BLE001
        _DB = None

pytestmark = pytest.mark.skipif(not _DB, reason="no DB_URL available")
_MIGRATION = (
    pathlib.Path(__file__).parents[3] / "migrations" / "083_trade_executions_ledger.sql"
).read_text(encoding="utf-8")


@asynccontextmanager
async def _tx():
    import asyncpg

    conn = await asyncpg.connect(_DB)
    tx = conn.transaction()
    await tx.start()
    try:
        # Only apply if missing: re-running DDL locks live tables on an already-migrated database.
        if not await conn.fetchval("SELECT to_regclass('public.trade_executions')"):
            await conn.execute(_MIGRATION)
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def _events(conn, trade_id):
    return await conn.fetch(
        "SELECT event, quantity, price, pnl, reason, old_value, new_value, segment, is_paper, "
        "qty_known FROM trade_executions WHERE trade_id = $1 ORDER BY id",
        trade_id,
    )


async def _new_paper_swing(conn, tid="LEDGER-T1", qty=26):
    await conn.execute(
        "INSERT INTO paper_trades (agent_trade_id, symbol, exchange, product, direction, quantity, "
        "order_state, intended_price, strategy_name, is_paper) "
        "VALUES ($1,'TESTSYM','NSE','CNC','long',$2,'SUBMITTED',100,'test',true)",
        tid,
        qty,
    )
    await conn.execute(
        "UPDATE paper_trades SET order_state='FILLED', fill_price=100, initial_sl=95, current_sl=95 "
        "WHERE agent_trade_id=$1",
        tid,
    )


@pytest.mark.asyncio
async def test_swing_partial_then_close_keeps_the_original_size_price_and_reason() -> None:
    async with _tx() as c:
        await _new_paper_swing(c)
        await c.execute("UPDATE paper_trades SET current_sl=100 WHERE agent_trade_id='LEDGER-T1'")
        await c.execute(
            "UPDATE paper_trades SET quantity=13, partial_qty_booked=13, partial_pnl_booked=180, "
            "last_partial_price=115 WHERE agent_trade_id='LEDGER-T1'"
        )
        await c.execute(
            "UPDATE paper_trades SET order_state='CLOSED', exit_price=120, pnl_realized=420, "
            "exit_timestamp=now(), exit_reason='TARGET' WHERE agent_trade_id='LEDGER-T1'"
        )
        ev = {r["event"]: r for r in await _events(c, "LEDGER-T1")}
        assert ev["ENTRY_FILL"]["quantity"] == 26 and ev["ENTRY_FILL"]["price"] == 100
        assert ev["PARTIAL_EXIT"]["quantity"] == 13 and ev["PARTIAL_EXIT"]["price"] == 115
        assert ev["PARTIAL_EXIT"]["pnl"] == 180
        assert ev["FINAL_EXIT"]["quantity"] == 13 and ev["FINAL_EXIT"]["price"] == 120
        assert ev["FINAL_EXIT"]["pnl"] == 240 and ev["FINAL_EXIT"]["reason"] == "TARGET"
        assert ev["ENTRY_FILL"]["segment"] == "SWING" and ev["ENTRY_FILL"]["is_paper"] is True
        original = await c.fetchval(
            "SELECT original_quantity FROM paper_trades WHERE agent_trade_id='LEDGER-T1'"
        )
        assert original == 26


@pytest.mark.asyncio
async def test_stop_moves_are_recorded_but_a_plain_pnl_refresh_writes_nothing() -> None:
    async with _tx() as c:
        await _new_paper_swing(c)
        count_sql = "SELECT count(*) FROM trade_executions WHERE trade_id='LEDGER-T1'"
        before = await c.fetchval(count_sql)
        await c.execute("UPDATE paper_trades SET pnl_unrealized=5 WHERE agent_trade_id='LEDGER-T1'")
        assert await c.fetchval(count_sql) == before
        await c.execute("UPDATE paper_trades SET current_sl=101 WHERE agent_trade_id='LEDGER-T1'")
        moves = [r for r in await _events(c, "LEDGER-T1") if r["event"] == "SL_MOVE"]
        assert moves[-1]["old_value"] == "95.00" and moves[-1]["new_value"] == "101.00"


@pytest.mark.asyncio
async def test_live_intraday_is_recorded_as_live_and_intraday() -> None:
    async with _tx() as c:
        await c.execute(
            "INSERT INTO trades (agent_trade_id, symbol, exchange, product, direction, quantity, "
            "order_state, intended_price, strategy_name) "
            "VALUES ('LEDGER-T2','TESTLIVE','NSE','MIS','short',10,'FILLED',50,'test')"
        )
        await c.execute(
            "UPDATE trades SET order_state='CLOSED', exit_price=49, pnl_realized=10, "
            "exit_timestamp=now(), exit_reason='EOD' WHERE agent_trade_id='LEDGER-T2'"
        )
        ev = await _events(c, "LEDGER-T2")
        assert {r["segment"] for r in ev} == {"INTRADAY"} and {r["is_paper"] for r in ev} == {False}
        final = next(r for r in ev if r["event"] == "FINAL_EXIT")
        assert final["reason"] == "EOD" and final["pnl"] == 10  # the live table keeps a reason too


@pytest.mark.asyncio
async def test_options_open_adjust_close_are_recorded() -> None:
    async with _tx() as c:
        await c.execute(
            "INSERT INTO options_positions (position_id, underlying, strategy_type, status, legs, "
            "expiry, dte_at_entry, lot_size_at_entry, net_credit, max_loss, max_profit, entry_margin, "
            "entry_delta, entry_gamma, entry_theta, entry_vega, adjustments_count, opened_at, is_paper) "
            "VALUES ('OPT-T','NSE:NIFTY','iron_condor','OPEN','{\"income\":[]}',current_date,10,65,"
            "1000,2000,1000,5000,0,0,0,0,0,now(),true)"
        )
        await c.execute(
            'UPDATE options_positions SET legs=\'{"income":[{"strike":1}]}\', adjustments_count=1 '
            "WHERE position_id='OPT-T'"
        )
        await c.execute(
            "UPDATE options_positions SET status='CLOSED', closed_at=now(), realized_pnl=750, "
            "exit_reason='TARGET' WHERE position_id='OPT-T'"
        )
        rows = await _events(c, "OPT-T")
        assert [r["event"] for r in rows] == [
            "ENTRY_FILL",
            "ADJUSTMENT",
            "STATE_CHANGE",
            "FINAL_EXIT",
        ]
        final = rows[-1]
        assert final["pnl"] == 750 and final["reason"] == "TARGET"


@pytest.mark.asyncio
async def test_a_manual_edit_of_a_closed_row_is_itself_recorded() -> None:
    async with _tx() as c:
        await _new_paper_swing(c)
        await c.execute(
            "UPDATE paper_trades SET order_state='CLOSED', exit_price=120, pnl_realized=100, "
            "exit_timestamp=now() WHERE agent_trade_id='LEDGER-T1'"
        )
        await c.execute("UPDATE paper_trades SET pnl_realized=999 WHERE agent_trade_id='LEDGER-T1'")
        assert "CLOSED_ROW_EDITED" in [r["event"] for r in await _events(c, "LEDGER-T1")]


@pytest.mark.asyncio
async def test_a_broken_ledger_never_blocks_the_trade_write_and_is_reported() -> None:
    async with _tx() as c:
        await _new_paper_swing(c)
        await c.execute(
            "ALTER TABLE trade_executions ADD CONSTRAINT boom CHECK (event <> 'STATE_CHANGE') NOT VALID"
        )
        await c.execute(
            "UPDATE paper_trades SET order_state='CLOSED', exit_price=1, pnl_realized=1, "
            "exit_timestamp=now() WHERE agent_trade_id='LEDGER-T1'"
        )
        state = await c.fetchval(
            "SELECT order_state FROM paper_trades WHERE agent_trade_id='LEDGER-T1'"
        )
        assert state == "CLOSED"  # the trade write went through
        assert "LEDGER_ERROR" in [r["event"] for r in await _events(c, "LEDGER-T1")]


class _ConnAsPool:
    """Lets repository code that expects a pool run on the test's transaction connection."""

    def __init__(self, conn):
        self._c = conn

    async def fetch(self, *a):
        return await self._c.fetch(*a)

    def acquire(self):
        conn = self._c

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_consistency_check_flags_shares_that_do_not_add_up() -> None:
    from src.db.repositories.trade_executions import TradeExecutionsRepository

    async with _tx() as c:
        await _new_paper_swing(c, "LEDGER-OK")
        await c.execute(
            "UPDATE paper_trades SET quantity=13, partial_qty_booked=13, partial_pnl_booked=1, "
            "last_partial_price=110 WHERE agent_trade_id='LEDGER-OK'"
        )
        await c.execute(
            "UPDATE paper_trades SET order_state='CLOSED', exit_price=120, pnl_realized=2, "
            "exit_timestamp=now() WHERE agent_trade_id='LEDGER-OK'"
        )
        await c.execute(  # a trade whose sold shares are missing from the ledger
            "INSERT INTO trade_executions (instrument, segment, is_paper, trade_id, event, quantity) "
            "VALUES ('EQUITY','SWING',true,'LEDGER-BAD','ENTRY_FILL',10), "
            "       ('EQUITY','SWING',true,'LEDGER-BAD','FINAL_EXIT',4)"
        )
        repo = TradeExecutionsRepository(_ConnAsPool(c))
        bad = {r["trade_id"] for r in await repo.find_inconsistencies()}
        assert "LEDGER-BAD" in bad and "LEDGER-OK" not in bad


@pytest.mark.asyncio
async def test_backfill_recovers_the_laurus_original_size() -> None:
    async with _tx() as c:
        ev = await c.fetch(
            "SELECT event, quantity, qty_known FROM trade_executions "
            "WHERE trade_id='TRD-5D7C28E9D9E0' ORDER BY id"
        )
        if not ev:
            pytest.skip("LAURUSLABS row not present in this database")
        d = {r["event"]: r for r in ev}
        assert d["ENTRY_FILL"]["quantity"] == 26  # not the 13 left after the partial
        assert d["PARTIAL_EXIT"]["quantity"] == 13 and d["PARTIAL_EXIT"]["qty_known"] is True


@pytest.mark.asyncio
async def test_paper_engine_close_uses_remaining_quantity_plus_the_banked_partial() -> None:
    """The paper tracker never shrinks on a partial, so its own pnl covers the ORIGINAL size."""
    from src.paper_trading.paper_engine import PaperTradingEngine as PaperEngine

    async with _tx() as c:
        await _new_paper_swing(c)
        await c.execute(
            "UPDATE paper_trades SET quantity=13, partial_qty_booked=13, partial_pnl_booked=180, "
            "last_partial_price=115 WHERE agent_trade_id='LEDGER-T1'"
        )
        fake = SimpleNamespace(_pool=_ConnAsPool(c))
        await PaperEngine._persist_close(fake, "LEDGER-T1", 120.0, 520.0, "TARGET")  # 520 = 26*20
        pnl = await c.fetchval(
            "SELECT pnl_realized FROM paper_trades WHERE agent_trade_id='LEDGER-T1'"
        )
        assert float(pnl) == 13 * 20 + 180  # not 520


@pytest.mark.asyncio
async def test_notifications_are_actually_saved_and_deduped_in_the_real_table() -> None:
    """notifications_sent stayed at 0 rows: ON CONFLICT (dedupe_key) did not match the PARTIAL
    unique index (WHERE dedupe_key IS NOT NULL), so every insert failed (2026-09-28)."""
    from src.db.repositories.notifications import NotificationRepository

    async with _tx() as c:
        repo = NotificationRepository(_ConnAsPool(c))
        kw = {"kind": "ENTRY_FILL", "body": "x", "is_paper": True}
        assert await repo.record(dedupe_key="dk-1", **kw) is True
        assert await repo.record(dedupe_key="dk-1", **kw) is False  # duplicate is skipped
        assert await repo.record(dedupe_key=None, **kw) is True  # no key -> never a conflict
        assert await repo.record(dedupe_key=None, **kw) is True
        assert await c.fetchval("SELECT count(*) FROM notifications_sent WHERE body='x'") == 3
