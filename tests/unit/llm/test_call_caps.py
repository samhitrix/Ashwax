"""Per-day LLM call caps: pure rules + the llm_call_log repo (real SQL, rolled back)."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from src.db.repositories.llm_call_log import LLMCallLogRepository
from src.llm.call_caps import startup_probe_due, swing_max_calls, sync_intraday_state
from src.strategies.llm_intraday.pipeline import TriggerState, evaluate_trigger

IST = timezone(timedelta(hours=5, minutes=30))

# ── swing cap ────────────────────────────────────────────────────────────────


def test_swing_cap_comes_from_config() -> None:
    assert swing_max_calls({"max_calls_per_day": 2}, token_budget=40000) == 2
    assert swing_max_calls({"max_calls_per_day": 5}, token_budget=None) == 5


def test_swing_cap_defaults_to_two_and_zero_budget_forces_zero() -> None:
    assert swing_max_calls({}, token_budget=40000) == 2
    assert swing_max_calls({"max_calls_per_day": 2}, token_budget=0) == 0


# ── intraday state rebuilt from the DB after a restart ───────────────────────


def test_restart_does_not_reset_intraday_pass_counter_or_gap() -> None:
    now = datetime(2026, 9, 24, 11, 30, tzinfo=IST)
    fresh_process = TriggerState(day="2026-09-24")  # what a restart gives: passes_today=0
    sync_intraday_state(fresh_process, calls_today=5, last_call_ts=now.timestamp() - 60)
    assert fresh_process.passes_today == 5
    fire, reason, _ = evaluate_trigger(
        fresh_process,
        {"max_passes_per_day": 5, "min_gap_minutes": 30},
        now,
        first_pass=datetime(2026, 9, 24, 9, 45, tzinfo=IST).time(),
        last_pass=datetime(2026, 9, 24, 14, 0, tzinfo=IST).time(),
        scores={"CIPLA": (99.0, now.timestamp())},
        traded_today=set(),
        trades_today=0,
        max_trades=2,
        tokens_left=50000,
    )
    assert (fire, reason) == (False, "pass_cap_reached")


def test_restart_does_not_refire_the_baseline_pass() -> None:
    now = datetime(2026, 9, 24, 11, 30, tzinfo=IST)
    st = TriggerState(day="2026-09-24")
    sync_intraday_state(st, calls_today=1, last_call_ts=now.timestamp() - 120)
    fire, reason, _ = evaluate_trigger(
        st,
        {"max_passes_per_day": 5, "min_gap_minutes": 30},
        now,
        first_pass=datetime(2026, 9, 24, 9, 45, tzinfo=IST).time(),
        last_pass=datetime(2026, 9, 24, 14, 0, tzinfo=IST).time(),
        scores={},
        traded_today=set(),
        trades_today=0,
        max_trades=2,
        tokens_left=50000,
    )
    assert fire is False and reason == "min_gap"  # not "baseline"


def test_sync_never_lowers_in_memory_counters() -> None:
    st = TriggerState(day="d", passes_today=3, last_pass_ts=2000.0)
    sync_intraday_state(st, calls_today=1, last_call_ts=1000.0)
    assert (st.passes_today, st.last_pass_ts) == (3, 2000.0)


# ── boot-time provider probe ─────────────────────────────────────────────────


def test_startup_probe_skipped_when_recent_check_exists() -> None:
    now = datetime(2026, 9, 24, 11, 30, tzinfo=IST)
    recent = (now - timedelta(minutes=7)).isoformat()
    assert startup_probe_due(recent, now, min_interval_minutes=240) is False


def test_startup_probe_runs_when_stale_missing_or_garbled() -> None:
    now = datetime(2026, 9, 24, 11, 30, tzinfo=IST)
    assert startup_probe_due((now - timedelta(hours=5)).isoformat(), now, 240) is True
    assert startup_probe_due(None, now, 240) is True
    assert startup_probe_due("not-a-date", now, 240) is True


# ── repo: real SQL against the real DB, inside a transaction that is rolled back ──

_DB_URL = os.environ.get("DB_URL")
if not _DB_URL:
    try:
        from dotenv import dotenv_values

        _DB_URL = dotenv_values(Path(__file__).parents[3] / ".env").get("DB_URL")
    except Exception:  # noqa: BLE001
        _DB_URL = None


class _TxPool:
    """Minimal pool whose acquire() yields the one connection under test."""

    def __init__(self, conn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


@pytest.mark.skipif(not _DB_URL, reason="no DB_URL available")
def test_call_log_counts_survive_and_are_per_purpose_per_day() -> None:
    import asyncpg

    sql = (Path(__file__).parents[3] / "migrations" / "080_llm_call_log.sql").read_text("utf-8")

    async def scenario() -> None:
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(sql)
            repo = LLMCallLogRepository(_TxPool(conn))
            d1, d2 = date(2031, 1, 1), date(2031, 1, 2)
            assert await repo.count_today("swing", d1) == 0
            assert await repo.last_call_ts("swing", d1) is None
            await repo.record("swing", d1, "a")
            await repo.record("swing", d1, "b")
            await repo.record("intraday", d1, "baseline")
            await repo.record("swing", d2, "next-day")
            assert await repo.count_today("swing", d1) == 2
            assert await repo.count_today("intraday", d1) == 1
            assert await repo.count_today("swing", d2) == 1
            assert isinstance(await repo.last_call_ts("swing", d1), float)
        finally:
            await tx.rollback()
            await conn.close()

    asyncio.run(scenario())
