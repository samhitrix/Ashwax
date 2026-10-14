"""LLM swing agent must not be offered a stock it exited a few days ago (2026-09-28: LAURUSLABS was
exited for +Rs3,624 on 24 Sep and re-bought on 28 Sep because the only guard is 'currently held' and
the day's top-20 relative-strength list contains the same leaders every day)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from src.strategies.llm_swing.pipeline import exclude_recent_exits


def test_recently_exited_symbols_are_removed_from_the_candidate_list() -> None:
    kept, dropped = exclude_recent_exits({"LAURUSLABS", "SONACOMS", "MCX"}, {"LAURUSLABS"})
    assert kept == {"SONACOMS", "MCX"} and dropped == ["LAURUSLABS"]


def test_symbols_with_an_exchange_prefix_are_matched_too() -> None:
    kept, dropped = exclude_recent_exits({"ANTHEM", "MCX"}, {"NSE:ANTHEM"})
    assert kept == {"MCX"} and dropped == ["ANTHEM"]


def test_nothing_recent_changes_nothing() -> None:
    kept, dropped = exclude_recent_exits({"A", "B"}, set())
    assert kept == {"A", "B"} and dropped == []


_DB = os.environ.get("DB_URL")
if not _DB:
    try:
        from dotenv import dotenv_values

        _DB = dotenv_values(Path(__file__).parents[3] / ".env").get("DB_URL")
    except Exception:  # noqa: BLE001
        _DB = None


@pytest.mark.skipif(not _DB, reason="no DB_URL available")
def test_recently_exited_query_runs_against_both_trade_tables_and_normalises_symbols() -> None:
    import asyncio

    import asyncpg
    from src import orchestrator as orch

    async def go():
        pool = await asyncpg.create_pool(_DB, min_size=1, max_size=1)
        try:
            return await orch._recently_exited_symbols(pool, 3650)  # everything ever exited
        finally:
            await pool.close()

    s = asyncio.run(go())
    assert isinstance(s, set) and len(s) > 0
    assert all(not x.startswith("NSE:") for x in s)  # normalised to bare symbols


def test_a_resting_entry_order_is_reported_as_resting_not_refused() -> None:
    import inspect

    from src import orchestrator as orch

    src = inspect.getsource(orch._llm_swing_scan_loop)
    assert '"resting" if reason == "not_filled"' in src
