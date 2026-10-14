"""A real position must stay monitored after the paper/live switch is flipped (2026-09-28).

The swing agent took a live LAURUSLABS trade; the registry was then set to "paper". The service
built no live broker, so the position had no monitor and the reconciler compared the real holding
to the paper adapter and reported a ghost. The switch governs NEW entries only."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from src import orchestrator as orch


class _Conn:
    def __init__(self, rows, boom=False):
        self._rows, self._boom = rows, boom

    async def fetch(self, _sql):
        if self._boom:
            raise RuntimeError("db down")
        return self._rows


class _Pool:
    def __init__(self, rows, boom=False):
        self._conn = _Conn(rows, boom)

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


def _open(*tids):
    return [{"agent_trade_id": t} for t in tids]


@pytest.mark.asyncio
async def test_open_live_trade_forces_a_live_broker_even_when_config_says_paper() -> None:
    made = MagicMock(return_value="LIVE_BROKER")
    b, ids = await orch._ensure_live_broker_for_open_positions(
        None, _Pool(_open("TRD-2825FABAD55B")), made
    )
    assert b == "LIVE_BROKER" and ids == ["TRD-2825FABAD55B"]
    made.assert_called_once()


@pytest.mark.asyncio
async def test_no_open_live_trades_and_paper_config_builds_nothing() -> None:
    made = MagicMock()
    b, ids = await orch._ensure_live_broker_for_open_positions(None, _Pool([]), made)
    assert b is None and ids == []
    made.assert_not_called()


@pytest.mark.asyncio
async def test_an_existing_live_broker_is_left_alone() -> None:
    made = MagicMock()
    b, ids = await orch._ensure_live_broker_for_open_positions("EXISTING", _Pool(_open("X")), made)
    assert b == "EXISTING" and ids == []
    made.assert_not_called()


@pytest.mark.asyncio
async def test_unreadable_db_fails_safe_by_building_the_live_broker() -> None:
    made = MagicMock(return_value="LIVE_BROKER")
    b, ids = await orch._ensure_live_broker_for_open_positions(None, _Pool([], boom=True), made)
    assert b == "LIVE_BROKER" and ids  # cannot prove nothing is open -> protect
    b2, _ = await orch._ensure_live_broker_for_open_positions(None, None, made)
    assert b2 == "LIVE_BROKER"


def test_boot_wires_the_helper_before_the_stacks_are_built() -> None:
    import inspect

    src = inspect.getsource(orch)
    call = src.index("await _ensure_live_broker_for_open_positions(")
    assert call < src.index('stacks["live"] = _build_stack(broker_live, False)')


_DB = os.environ.get("DB_URL")
if not _DB:
    try:
        from dotenv import dotenv_values

        _DB = dotenv_values(Path(__file__).parents[3] / ".env").get("DB_URL")
    except Exception:  # noqa: BLE001
        _DB = None


@pytest.mark.skipif(not _DB, reason="no DB_URL available")
def test_the_real_query_runs_against_the_real_trades_table() -> None:
    import asyncio

    import asyncpg

    async def go():
        pool = await asyncpg.create_pool(_DB, min_size=1, max_size=1)
        try:
            return await orch._ensure_live_broker_for_open_positions(
                None, pool, lambda: "LIVE_BROKER"
            )
        finally:
            await pool.close()

    b, ids = asyncio.run(go())
    assert (b is None) == (ids == [])  # consistent whichever way today's data falls
