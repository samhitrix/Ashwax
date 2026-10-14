"""Orchestrator wiring of the liquid / not-recently-shown menu (2026-09-28)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from src import orchestrator as orch
from src.strategies.llm_intraday.pipeline import TriggerState

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=IST)
CFG = {"universe_size": 10, "min_adv_crores": 50}
TCFG = {"re_offer_minutes": 60}


def _f(chg: float, volx: float) -> dict:
    return {"ltp": 500.0, "chg_pct": chg, "vol_ratio20": volx}


FEATS = {"LIQ1": _f(-3, 3), "LIQ2": _f(-2, 2), "THIN": _f(-5, 9), "SEEN": _f(-4, 4)}
ADV = {"LIQ1": 600.0, "LIQ2": 100.0, "THIN": 19.0, "SEEN": 300.0}


def _shared(offered=None):
    st = TriggerState(day="2026-09-28")
    st.offered.update(offered or {})
    return {
        "llm_intraday_trigger_state": st,
        "intraday_adv": {"day": "2026-09-28", "map": dict(ADV)},
    }


def test_menu_is_liquid_and_skips_what_the_llm_was_just_shown() -> None:
    shared = _shared({"SEEN": NOW.timestamp() - 600})
    menu, stats = orch._intraday_menu(shared, FEATS, CFG, TCFG, NOW)
    assert menu == ["LIQ1", "LIQ2"]  # THIN is illiquid, SEEN was shown 10 minutes ago
    assert stats["excluded_illiquid"] == 1 and stats["excluded_recent"] == 1
    assert stats["considered"] == 4


def test_choosing_a_menu_marks_those_stocks_as_shown() -> None:
    shared = _shared()
    orch._intraday_menu(shared, FEATS, CFG, TCFG, NOW)
    offered = shared["llm_intraday_trigger_state"].offered
    assert set(offered) >= {"LIQ1", "LIQ2", "SEEN"} and "THIN" not in offered
    # so the very next pass cannot show the same names
    again, _ = orch._intraday_menu(shared, FEATS, CFG, TCFG, NOW + timedelta(minutes=5))
    assert again == []


def test_missing_liquidity_data_fails_closed_not_open() -> None:
    shared = _shared()
    shared["intraday_adv"] = {"day": "2026-09-28", "map": {}}
    menu, stats = orch._intraday_menu(shared, FEATS, CFG, TCFG, NOW)
    assert menu == [] and stats["excluded_illiquid"] == 4


@pytest.mark.asyncio
async def test_offered_memory_is_rebuilt_from_todays_journal_after_a_restart() -> None:
    ts = NOW - timedelta(minutes=20)
    row = {
        "run_ts": ts,
        "market_context": json.dumps(
            {"candidate_universe": [{"symbol": "AAA"}, {"symbol": "BBB"}]}
        ),
    }
    repo = MagicMock()
    repo.todays_runs = AsyncMock(return_value=[row])
    shared = {
        "llm_intraday_journal_repo": repo,
        "llm_intraday_trigger_state": TriggerState(day="2026-09-28"),
    }
    await orch._reseed_llm_offered(shared, NOW)
    assert shared["llm_intraday_trigger_state"].offered == {
        "AAA": ts.timestamp(),
        "BBB": ts.timestamp(),
    }
    await orch._reseed_llm_offered(shared, NOW)  # idempotent: once per day
    repo.todays_runs.assert_awaited_once()


@pytest.mark.asyncio
async def test_liquidity_map_is_loaded_once_per_day_from_the_repo() -> None:
    repo = MagicMock()
    repo.adv_crores = AsyncMock(return_value={"AXISBANK": 604.0})
    shared = {"ohlcv_repo": repo}
    await orch._ensure_intraday_adv(shared, NOW)
    await orch._ensure_intraday_adv(shared, NOW + timedelta(minutes=30))
    repo.adv_crores.assert_awaited_once()
    assert shared["intraday_adv"]["map"] == {"AXISBANK": 604.0}


# ── the repo query itself (real SQL, read-only) ──────────────────────────────────────────

_DB = os.environ.get("DB_URL")
if not _DB:
    try:
        from dotenv import dotenv_values

        _DB = dotenv_values(Path(__file__).parents[3] / ".env").get("DB_URL")
    except Exception:  # noqa: BLE001
        _DB = None


@pytest.mark.skipif(not _DB, reason="no DB_URL available")
def test_adv_crores_returns_a_positive_average_traded_value_per_symbol() -> None:
    import asyncio

    import asyncpg
    from src.db.repositories.ohlcv import OHLCVRepository

    async def go():
        pool = await asyncpg.create_pool(_DB, min_size=1, max_size=1)
        try:
            return await OHLCVRepository(pool).adv_crores(20)
        finally:
            await pool.close()

    m = asyncio.run(go())
    assert len(m) > 100 and all(v >= 0 for v in m.values())
    assert m.get("HDFCBANK", 0) > 500  # a genuinely liquid name


def test_the_stock_that_triggered_the_call_leads_its_own_menu() -> None:
    """2026-09-28 14:14: the tick marks the trigger's fresh symbols as 'offered' the moment it fires,
    then the menu skipped anything offered in the last hour -- so UNOMINDA triggered a call whose
    menu did not contain UNOMINDA (and the answer was PASS on ten other stocks)."""
    just_marked = {"SEEN": NOW.timestamp()}  # exactly what the tick does before the pass
    menu_before_fix, _ = orch._intraday_menu(_shared(just_marked), FEATS, CFG, TCFG, NOW)
    assert "SEEN" not in menu_before_fix  # the old behaviour: excluded from its own menu

    menu, stats = orch._intraday_menu(
        _shared(just_marked), FEATS, CFG, TCFG, NOW, must_show=["SEEN"]
    )
    assert menu[0] == "SEEN" and menu == ["SEEN", "LIQ1", "LIQ2"]
    assert "THIN" not in menu  # illiquid names are still never shown


def test_a_trigger_symbol_that_is_not_priced_or_known_is_ignored_not_invented() -> None:
    menu, _ = orch._intraday_menu(_shared(), FEATS, CFG, TCFG, NOW, must_show=["NOPE"])
    assert "NOPE" not in menu


def test_the_tick_hands_its_fresh_symbols_to_the_pass() -> None:
    import inspect

    assert "must_show=fresh" in inspect.getsource(orch._llm_intraday_signal_tick)
    assert "must_show=must_show" in inspect.getsource(orch._run_llm_intraday_pass)
