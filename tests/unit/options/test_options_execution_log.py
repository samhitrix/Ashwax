"""Every options execute_structure() attempt is recorded in options_execution_log -- including the
failed ones (rejected protection leg, emergency close), which used to leave no trace outside the log
file (2026-09-28: the table had zero writers). Real table, rolled-back transaction."""

from __future__ import annotations

import os
import pathlib
from contextlib import asynccontextmanager
from typing import Any

import pytest
from src.db.repositories.options import OptionsRepository
from src.options.options_leg_executor import ExecutionResult, LegFillState
from src.options.structure_selector import OptionLeg

_DB = os.environ.get("DB_URL")
if not _DB:
    try:
        from dotenv import dotenv_values

        _DB = dotenv_values(pathlib.Path(__file__).parents[3] / ".env").get("DB_URL")
    except Exception:  # noqa: BLE001
        _DB = None

pytestmark = pytest.mark.skipif(not _DB, reason="no DB_URL available")


class _ConnAsPool:
    def __init__(self, conn: Any) -> None:
        self._c = conn

    def acquire(self) -> Any:
        conn = self._c

        class _Ctx:
            async def __aenter__(self_inner) -> Any:
                return conn

            async def __aexit__(self_inner, *a: Any) -> bool:
                return False

        return _Ctx()


@asynccontextmanager
async def _tx():
    import asyncpg

    conn = await asyncpg.connect(_DB)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


def _leg(sym: str, direction: str, prem: float) -> OptionLeg:
    return OptionLeg(sym, "CE", 100.0, direction, 1, 50, prem)


@pytest.mark.asyncio
async def test_a_failed_execution_is_recorded_with_what_was_intended() -> None:
    prot, inc = [_leg("NFO:A", "BUY", 12.5)], [_leg("NFO:B", "SELL", 30.0)]
    res = ExecutionResult(
        position_id="OPT-EXEC-T",
        outcome="PARTIAL_EMERGENCY_CLOSED",
        protection_legs=[LegFillState(prot[0], "o1", filled=True)],
        income_legs=[LegFillState(inc[0], "o2", rejected=True)],
    )
    async with _tx() as c:
        await OptionsRepository(_ConnAsPool(c)).record_execution(
            res, prot, inc, naked_detected=True
        )
        row = await c.fetchrow("SELECT * FROM options_execution_log WHERE position_id='OPT-EXEC-T'")
        assert row["leg_count"] == 2 and row["legs_filled"] == 1 and row["legs_rejected"] == 1
        assert row["partial_fill"] is True and row["naked_exposure_detected"] is True
        assert row["emergency_action_taken"] == "MARKET_CLOSE_FILLED_LEGS"
        assert '"NFO:A": 12.5' in str(row["intended_prices"])


@pytest.mark.asyncio
async def test_a_clean_fill_is_recorded_as_such() -> None:
    prot, inc = [_leg("NFO:A", "BUY", 12.5)], [_leg("NFO:B", "SELL", 30.0)]
    res = ExecutionResult(
        position_id="OPT-EXEC-OK",
        outcome="ALL_FILLED",
        protection_legs=[LegFillState(prot[0], "o1", filled=True)],
        income_legs=[LegFillState(inc[0], "o2", filled=True)],
    )
    async with _tx() as c:
        await OptionsRepository(_ConnAsPool(c)).record_execution(res, prot, inc)
        row = await c.fetchrow(
            "SELECT * FROM options_execution_log WHERE position_id='OPT-EXEC-OK'"
        )
        assert row["legs_filled"] == 2 and row["partial_fill"] is False
        assert row["emergency_action_taken"] is None and row["naked_exposure_detected"] is False


def test_the_seller_desk_records_every_attempt_before_judging_the_outcome() -> None:
    import inspect

    from src.options import seller_desk

    src = inspect.getsource(seller_desk)
    assert src.index("record_execution(") < src.index("seller_desk_execution_failed")
