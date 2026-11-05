"""Tests for src.orchestrator._fetch_closed_trades_from_db's `mode` parameter.

Added 2026-09-04: cost_erosion (quality_raise), expectancy (STRATEGY_DECAY
pause) and health (SUSPENDED block / size multiplier) all gate LIVE trading
from this function's output. Before this fix they all called it with the
implicit "all" (paper_trades UNION trades) — a paper strategy's simulated
losses could raise the live quality bar, hard-pause a live strategy via
STRATEGY_DECAY, or drag down its live health tier, and vice versa. Same bug
class already fixed for /api/pnl_breakdown on 2026-09-02.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
import src.orchestrator as orch_mod


def _row(table: str) -> dict[str, Any]:
    return {
        "id": 1,
        "agent_trade_id": f"T-{table}",
        "strategy_name": "orb_intraday_smart",
        "pnl_realized": 100.0 if table == "trades" else -999.0,
        "initial_sl": 90.0,
        "fill_price": 100.0,
        "quantity": 10,
        "entry_timestamp": datetime(2026, 9, 4, 9, 0),
        "exit_timestamp": datetime(2026, 9, 4, 10, 0),
        "regime_at_entry": "TRENDING",
        "llm_confidence": 0.8,
        "charges_taxes": 5.0,
    }


class _FakePool:
    """Records which table(s) each mode actually queries. mode="all" issues a
    single UNION ALL query (both substrings present) — not two separate calls."""

    def __init__(self) -> None:
        self.tables_queried: list[str] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "UNION ALL" in query:
            self.tables_queried.extend(["paper_trades", "trades"])
            return [_row("paper_trades"), _row("trades")]
        table = "paper_trades" if "FROM paper_trades" in query else "trades"
        self.tables_queried.append(table)
        return [_row(table)]


@pytest.mark.asyncio
async def test_mode_live_queries_trades_table_only():
    pool = _FakePool()
    out = await orch_mod._fetch_closed_trades_from_db(pool, mode="live")
    assert pool.tables_queried == ["trades"]
    assert out[0]["net_pnl"] == 100.0


@pytest.mark.asyncio
async def test_mode_paper_queries_paper_trades_table_only():
    pool = _FakePool()
    out = await orch_mod._fetch_closed_trades_from_db(pool, mode="paper")
    assert pool.tables_queried == ["paper_trades"]
    assert out[0]["net_pnl"] == -999.0


@pytest.mark.asyncio
async def test_mode_all_queries_both_tables():
    pool = _FakePool()
    out = await orch_mod._fetch_closed_trades_from_db(pool, mode="all")
    assert set(pool.tables_queried) == {"paper_trades", "trades"}
    assert len(out) == 2


@pytest.mark.asyncio
async def test_default_mode_is_all_back_compat():
    pool = _FakePool()
    out = await orch_mod._fetch_closed_trades_from_db(pool)
    assert set(pool.tables_queried) == {"paper_trades", "trades"}
    assert len(out) == 2


@pytest.mark.asyncio
async def test_none_pool_returns_empty_regardless_of_mode():
    assert await orch_mod._fetch_closed_trades_from_db(None, mode="live") == []


@pytest.mark.asyncio
async def test_pnl_r_risk_basis_is_original_size_after_partials():
    """quantity is what remained after partials; pnl_realized is the whole trade, so
    the risk denominator must cover quantity + partial_qty_booked."""

    class _Pool:
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            row = _row("trades")
            row["quantity"] = 5
            row["partial_qty_booked"] = 5
            row["pnl_realized"] = 100.0
            return [row]

    out = await orch_mod._fetch_closed_trades_from_db(_Pool(), mode="live")
    # risk = |100-90| * (5+5) = 100  ->  100/100 = 1.0R (not 2.0R over the 5 remaining)
    assert out[0]["risk_rupees"] == 100.0
    assert out[0]["pnl_r"] == 1.0
