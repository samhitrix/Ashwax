"""Real-Postgres check of the exec_broker SQL (migration 089, phase 2 task 5): the per-broker
reseed filter, the repository's column check and open_trades filter.

Runs only when DB_URL is set in the ENVIRONMENT (never read from .env), inside one transaction
that is always ROLLED BACK. The test that drops the column to simulate a pre-089 database also
requires "scratch" in DB_URL, so it can never take a table lock on a real database."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

_DB = os.environ.get("DB_URL")
pytestmark = pytest.mark.skipif(not _DB, reason="DB_URL not set in the environment")

_ROWS = [("T5-KITE", "kite"), ("T5-ANGEL", "angel_one"), ("T5-NULL", None), ("T5-BAD", "kitee")]
_STACKS = {"paper": object(), "live:kite": object(), "live:angel_one": object()}
_ANGEL_ONLY = {"paper": object(), "live:angel_one": object()}


@asynccontextmanager
async def _tx():
    import asyncpg

    conn = await asyncpg.connect(_DB)
    tx = conn.transaction()
    await tx.start()
    try:
        if not await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='trades' AND column_name='exec_broker'"
        ):
            pytest.skip("migration 089 not applied on this DB")
        # isolate from any other open rows on this DB
        await conn.execute("UPDATE trades SET order_state='CLOSED' WHERE order_state <> 'CLOSED'")
        for tid, eb in _ROWS:
            await conn.execute(
                "INSERT INTO trades (agent_trade_id, symbol, direction, quantity, order_state, "
                "fill_price, product, exec_broker) VALUES ($1,'INFY','long',1,'FILLED',100,'CNC',$2)",
                tid,
                eb,
            )
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


class _Pool:
    """asyncpg connection posing as a pool (fetch + acquire)."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def fetch(self, sql: str, *args: Any) -> Any:
        # a savepoint per statement, like a pool's independent autocommit statements: a failed
        # query must not abort the outer (rolled-back) test transaction
        async with self._conn.transaction():
            return await self._conn.fetch(sql, *args)

    def acquire(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self) -> Any:
                return conn

            async def __aexit__(self, *a: Any) -> bool:
                return False

        return _Ctx()


async def _reseeded(conn: Any, key: str, legacy: str = "kite", stacks: Any = None) -> list[str]:
    from src import orchestrator
    from src.execution.order_lifecycle import OrderMonitor

    mon = OrderMonitor(MagicMock())
    await orchestrator._reseed_order_monitor(
        mon, _Pool(conn), False, **orchestrator._reseed_scope(key, stacks or _STACKS, legacy)
    )
    return sorted(p.agent_trade_id for p in mon.get_monitored())


@pytest.mark.asyncio
async def test_reseed_filter_on_real_postgres() -> None:
    async with _tx() as conn:
        for legacy in ("kite", "angel_one"):  # unstamped/unknown rows are kite's, never primary's
            assert await _reseeded(conn, "live:kite", legacy) == ["T5-BAD", "T5-KITE", "T5-NULL"]
            assert await _reseeded(conn, "live:angel_one", legacy) == ["T5-ANGEL"]
        # angel_one primary with no kite stack: never the unstamped (Kite) rows
        assert await _reseeded(conn, "live:angel_one", "angel_one", _ANGEL_ONLY) == ["T5-ANGEL"]
        # kite-only: every kite/NULL/unknown row as before (an angel_one-stamped row is not
        # kite's -- it is orphaned and reported, see the orphan test)
        single = {"paper": object(), "live:kite": object()}
        assert await _reseeded(conn, "live:kite", "kite", single) == [
            "T5-BAD",
            "T5-KITE",
            "T5-NULL",
        ]


@pytest.mark.asyncio
async def test_orphan_report_on_real_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.infrastructure.operator_alerts as oa
    from src import orchestrator

    monkeypatch.setattr(oa, "raise_alert", lambda msg, **kw: True)
    orchestrator._ORPHANS_LOGGED.clear()
    async with _tx() as conn:
        assert await orchestrator._report_orphaned_live_rows(_Pool(conn), _STACKS) == 0
        # no kite stack: NULL + kite + unknown are orphaned
        assert await orchestrator._report_orphaned_live_rows(_Pool(conn), _ANGEL_ONLY) == 3


@pytest.mark.asyncio
async def test_repo_column_check_and_open_trades_filter_on_real_postgres() -> None:
    from src.db.repositories.trades import TradeRepository

    async with _tx() as conn:
        repo = TradeRepository(_Pool(conn))  # type: ignore[arg-type]
        assert await repo._has_exec_broker("trades") is True
        await repo.insert(
            {
                "agent_trade_id": "T5-NEW",
                "symbol": "TCS",
                "direction": "long",
                "quantity": 1,
                "exec_broker": "angel_one",
            }
        )
        assert (
            await conn.fetchval("SELECT exec_broker FROM trades WHERE agent_trade_id='T5-NEW'")
            == "angel_one"
        )
        got = sorted(r["agent_trade_id"] for r in await repo.open_trades(exec_broker="kite"))
        assert got == ["T5-KITE", "T5-NULL"]
        assert len(await repo.open_trades()) == 5


@pytest.mark.asyncio
@pytest.mark.skipif("scratch" not in (_DB or ""), reason="DROP COLUMN only on the scratch DB")
async def test_pre_089_database_on_real_postgres() -> None:
    from src.db.repositories.trades import TradeRepository

    async with _tx() as conn:
        await conn.execute("ALTER TABLE trades DROP COLUMN exec_broker")
        # kite stack: unfiltered (all 4); any other live stack: nothing, even as primary
        assert len(await _reseeded(conn, "live:kite")) == 4
        assert await _reseeded(conn, "live:angel_one") == []
        assert await _reseeded(conn, "live:angel_one", "angel_one", _ANGEL_ONLY) == []
        import src.infrastructure.operator_alerts as oa
        from src import orchestrator

        orchestrator._ORPHANS_LOGGED.clear()
        _orig = oa.raise_alert
        oa.raise_alert = lambda msg, **kw: True  # type: ignore[assignment]
        try:
            assert await orchestrator._report_orphaned_live_rows(_Pool(conn), _ANGEL_ONLY) == 4
        finally:
            oa.raise_alert = _orig
        repo = TradeRepository(_Pool(conn))  # type: ignore[arg-type]
        await repo.insert(
            {
                "agent_trade_id": "T5-OLD",
                "symbol": "TCS",
                "direction": "long",
                "quantity": 1,
                "exec_broker": "kite",
            }
        )
        assert await conn.fetchval("SELECT 1 FROM trades WHERE agent_trade_id='T5-OLD'") == 1
