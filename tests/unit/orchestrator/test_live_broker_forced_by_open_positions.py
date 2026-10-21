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
    def __init__(self, rows, boom=False, no_exec_broker_column=False):
        self._rows, self._boom = rows, boom
        self._no_col = no_exec_broker_column

    async def fetch(self, sql):
        if self._boom:
            raise RuntimeError("db down")
        if self._no_col and "exec_broker" in sql:
            raise RuntimeError('column "exec_broker" does not exist')
        return self._rows


class _Pool:
    def __init__(self, rows, boom=False, no_exec_broker_column=False):
        self._conn = _Conn(rows, boom, no_exec_broker_column)

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


def _open(*tids, broker=None):
    return [{"agent_trade_id": t, "exec_broker": broker} for t in tids]


def _maker():
    return MagicMock(side_effect=lambda name: f"LIVE_{name}")


@pytest.mark.asyncio
async def test_open_live_trade_forces_a_live_broker_even_when_config_says_paper() -> None:
    made, live = _maker(), {}
    forced = await orch._ensure_live_broker_for_open_positions(
        live, _Pool(_open("TRD-2825FABAD55B")), made, "kite"
    )
    # exec_broker NULL means the legacy broker "kite"
    assert forced == {"kite": ["TRD-2825FABAD55B"]} and live == {"kite": "LIVE_kite"}
    made.assert_called_once_with("kite")


@pytest.mark.asyncio
async def test_no_open_live_trades_and_paper_config_builds_nothing() -> None:
    made, live = _maker(), {}
    forced = await orch._ensure_live_broker_for_open_positions(live, _Pool([]), made, "kite")
    assert forced == {} and live == {}
    made.assert_not_called()


@pytest.mark.asyncio
async def test_an_existing_live_broker_is_left_alone() -> None:
    made, live = _maker(), {"kite": "EXISTING"}
    forced = await orch._ensure_live_broker_for_open_positions(
        live, _Pool(_open("X")), made, "kite"
    )
    assert forced == {} and live == {"kite": "EXISTING"}
    made.assert_not_called()


@pytest.mark.asyncio
async def test_each_broker_with_open_positions_gets_its_own_live_broker() -> None:
    made, live = _maker(), {"kite": "EXISTING"}
    rows = _open("K1") + _open("A1", "A2", broker="angel_one")
    forced = await orch._ensure_live_broker_for_open_positions(live, _Pool(rows), made, "kite")
    assert forced == {"angel_one": ["A1", "A2"]}
    assert live == {"kite": "EXISTING", "angel_one": "LIVE_angel_one"}
    made.assert_called_once_with("angel_one")


@pytest.mark.asyncio
async def test_db_without_exec_broker_column_falls_back_to_the_legacy_broker() -> None:
    made, live = _maker(), {}
    pool = _Pool([{"agent_trade_id": "X"}], no_exec_broker_column=True)
    forced = await orch._ensure_live_broker_for_open_positions(live, pool, made, "kite")
    assert forced == {"kite": ["X"]} and live == {"kite": "LIVE_kite"}


@pytest.mark.asyncio
async def test_null_exec_broker_is_kite_in_both_query_paths_even_if_legacy_differs() -> None:
    # historical rows predate exec_broker and were all placed through Kite
    made, live = _maker(), {"angel_one": "EXISTING"}
    forced = await orch._ensure_live_broker_for_open_positions(
        live, _Pool(_open("H1")), made, "angel_one"
    )
    assert forced == {"kite": ["H1"]}
    made2, live2 = _maker(), {"angel_one": "EXISTING"}
    pool = _Pool([{"agent_trade_id": "H1"}], no_exec_broker_column=True)
    forced2 = await orch._ensure_live_broker_for_open_positions(live2, pool, made2, "angel_one")
    assert forced2 == {"kite": ["H1"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["kitee", "paper"])
@pytest.mark.parametrize("legacy", ["kite", "angel_one"])
async def test_invalid_exec_broker_is_never_built_and_its_trades_force_kite(
    bad, legacy, caplog
) -> None:
    # Same rule as NULL and as the reseed (_reseed_scope): an unusable stamp belongs to the
    # KITE stack, whatever the legacy broker is -- otherwise no stack would reseed the row.
    made, live = _maker(), {}
    rows = _open("B1", broker=bad)
    with caplog.at_level("CRITICAL"):
        forced = await orch._ensure_live_broker_for_open_positions(live, _Pool(rows), made, legacy)
    assert bad not in live and live == {"kite": "LIVE_kite"}
    assert forced == {"kite": ["B1"]}
    made.assert_called_once_with("kite")
    msgs = [r.getMessage() for r in caplog.records]
    assert any("exec_broker_invalid" in m and "tracking it with kite" in m for m in msgs)


@pytest.mark.asyncio
async def test_unreadable_db_fails_safe_by_building_the_live_broker() -> None:
    made, live = _maker(), {}
    forced = await orch._ensure_live_broker_for_open_positions(
        live, _Pool([], boom=True), made, "kite"
    )
    assert live == {"kite": "LIVE_kite"} and forced["kite"]  # cannot prove nothing is open
    live2: dict = {}
    await orch._ensure_live_broker_for_open_positions(live2, None, made, "kite")
    assert live2 == {"kite": "LIVE_kite"}


def test_boot_wires_the_helper_before_the_stacks_are_built() -> None:
    import inspect

    src = inspect.getsource(orch)
    call = src.index("await _ensure_live_broker_for_open_positions(")
    assert call < src.index(
        "stacks[stack_key(False, _lname)] = _build_stack(_lbroker, False, _lname)"
    )


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
            live: dict = {}
            forced = await orch._ensure_live_broker_for_open_positions(
                live, pool, lambda name: f"LIVE_{name}", "kite"
            )
            return live, forced
        finally:
            await pool.close()

    live, forced = asyncio.run(go())
    assert set(live) == set(forced)  # consistent whichever way today's data falls


def test_boot_refreshes_credentials_of_routed_live_brokers_before_building_them() -> None:
    import inspect

    src = inspect.getsource(orch._run)
    refresh = src.index("_routed_refresh = [")
    assert refresh < src.index("for _name in needed_live_brokers(")
