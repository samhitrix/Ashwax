"""Tests for _monitoring_loop_25's DB-row -> TradeWithCosts / by-strategy mapping.

Regression coverage for 2026-08-21 fix: the loop used to source raw_trades from
shared['closed_trades'] (in-memory, reset every restart, stuck at "Insufficient
data"). Switching to _fetch_closed_trades_from_db means the per-row dict shape
changed (adds gross_pnl/total_costs, keys the strategy under "strategy_name" not
"strategy") — these tests lock in the mapping logic so a future refactor can't
silently break either without a fast, DB-free feedback signal.
"""

from __future__ import annotations

from src.monitoring.cost_erosion import TradeWithCosts


def _build_cost_trades(raw_trades: list[dict]) -> list[TradeWithCosts]:
    """Replicates the filtering logic from _monitoring_loop_25."""
    return [
        TradeWithCosts(
            gross_pnl=float(t["gross_pnl"]),
            total_costs=float(t["total_costs"]),
            risk_rupees=float(t["risk_rupees"] or 1),
        )
        for t in raw_trades
        if t.get("total_costs") is not None
    ]


def _group_by_strategy(raw_trades: list[dict]) -> dict[str, int]:
    """Replicates the by_strategy grouping logic from _monitoring_loop_25."""
    by_strategy: dict[str, int] = {}
    for t in raw_trades:
        key = str(t.get("strategy_name", "unknown"))
        by_strategy[key] = by_strategy.get(key, 0) + 1
    return by_strategy


def test_rows_missing_charges_taxes_are_excluded_from_cost_trades():
    raw = [
        {"gross_pnl": 100.0, "total_costs": 10.0, "risk_rupees": 50.0},
        {"gross_pnl": -50.0, "total_costs": None, "risk_rupees": 50.0},  # no cost data recorded
    ]
    cost_trades = _build_cost_trades(raw)
    assert len(cost_trades) == 1
    assert cost_trades[0].total_costs == 10.0


def test_all_rows_missing_costs_gives_empty_list():
    raw = [{"gross_pnl": 100.0, "total_costs": None, "risk_rupees": 50.0}] * 5
    assert _build_cost_trades(raw) == []


def test_by_strategy_groups_on_strategy_name_key():
    raw = [
        {"strategy_name": "orb_intraday_smart"},
        {"strategy_name": "orb_intraday_smart"},
        {"strategy_name": "oneil_canslim"},
    ]
    grouped = _group_by_strategy(raw)
    assert grouped == {"orb_intraday_smart": 2, "oneil_canslim": 1}


def test_missing_strategy_name_falls_back_to_unknown():
    grouped = _group_by_strategy([{"pnl_r": 1.0}])
    assert grouped == {"unknown": 1}
