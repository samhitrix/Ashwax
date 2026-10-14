"""src/risk/budget.py: config/capital.json loader, format validation, invariants, scaling.

The shared-pool, all-in-one-file risk design approved 2026-10-06 (see the plan: "one
realistic risk-budget system"). These tests cover Phase 1 only -- the loader and the
derived-rupee arithmetic. Wiring into CRM/SellerDesk/swing/intraday sizing is Phase 2/3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.risk.budget import (
    CapitalConfigError,
    load_risk_budget,
    parse_pct,
    parse_rupees,
)

ROOT = Path(__file__).resolve().parents[3]


def _base() -> dict:
    """A minimal, internally-consistent config, independent of the real file --
    these tests must keep passing even if the shipped capital.json's numbers change."""
    return {
        "account": {
            "equity_comes_from": "broker_equity",
            "fallback_equity_if_broker_unavailable": "₹20,00,000",
            "max_loss_if_everything_open_hits_stop": "2.5%",
            "stop_all_trading_if_equity_falls_below": "25%",
            "drawdown_steps": [
                {"drawdown_from_start": "3%", "position_size_becomes": "75%"},
                {"drawdown_from_start": "10%", "position_size_becomes": "0%"},
            ],
        },
        "options": {
            "max_structure_loss_per_strategy": "0.75%",
            "max_loss_per_day_per_strategy": "0.25%",
            "max_loss_per_day_all_options_strategies": "0.5%",
            "max_share_of_shared_risk": "1.5%",
            "max_open_strategies": 3,
            "max_open_strategies_per_index": 2,
            "max_open_same_direction_spreads": 2,
        },
        "swing": {
            "max_loss_per_trade": "0.2%",
            "max_share_of_shared_risk": "1%",
            "max_open_trades": 12,
            "max_new_trades_per_day": 2,
            "max_new_trades_per_day_per_strategy": 2,
            "keep_unused_cash_buffer": "10%",
        },
        "intraday": {
            "max_loss_per_trade": "0.05%",
            "max_share_of_shared_risk": "0.1%",
            "max_loss_per_day": "0.15%",
            "close_all_positions_when_daily_loss_hit": True,
            "max_open_trades": 6,
            "max_new_trades_per_day": 6,
            "max_new_trades_per_day_per_strategy": 3,
        },
    }


def _write(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "capital.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


# ── Format parsing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0.5%", 0.5), ("1%", 1.0), ("1.5%", 1.5), ("25%", 25.0), ("0.05%", 0.05)],
)
def test_parse_pct_accepts_shortest_form(raw: str, expected: float) -> None:
    assert parse_pct(raw, "k") == expected


@pytest.mark.parametrize("raw", ["0.5", "1.0%", "0.50%", 0.5, "1", "-1%", "", "1.%"])
def test_parse_pct_rejects_ambiguous_or_bare_values(raw) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(CapitalConfigError, match="k:"):
        parse_pct(raw, "k")


@pytest.mark.parametrize(
    ("raw", "expected"), [("₹20,00,000", 2_000_000.0), ("₹7,00,000", 700_000.0), ("₹750", 750.0)]
)
def test_parse_rupees_accepts_indian_grouping(raw: str, expected: float) -> None:
    assert parse_rupees(raw, "k") == expected


@pytest.mark.parametrize("raw", ["20,00,000", "Rs 20,00,000", "₹20,000,00", 2000000, "₹-5,000"])
def test_parse_rupees_rejects_bad_format(raw) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(CapitalConfigError, match="k:"):
        parse_rupees(raw, "k")


# ── Loader ──────────────────────────────────────────────────────────────────────────


def test_shipped_capital_json_loads_and_passes_invariants() -> None:
    load_risk_budget(ROOT / "config" / "capital.json")


def test_valid_config_loads(tmp_path: Path) -> None:
    b = load_risk_budget(_write(tmp_path, _base()))
    assert b.options.max_open_strategies == 3
    assert b.swing.max_new_trades_per_day == 2
    assert b.intraday.close_all_on_daily_loss is True


def test_missing_top_level_block_rejected(tmp_path: Path) -> None:
    cfg = _base()
    del cfg["swing"]
    with pytest.raises(CapitalConfigError, match="swing"):
        load_risk_budget(_write(tmp_path, cfg))


def test_missing_key_names_the_key(tmp_path: Path) -> None:
    cfg = _base()
    del cfg["options"]["max_open_strategies"]
    with pytest.raises(CapitalConfigError, match="options.max_open_strategies"):
        load_risk_budget(_write(tmp_path, cfg))


def test_count_field_rejects_a_percentage_string(tmp_path: Path) -> None:
    cfg = _base()
    cfg["options"]["max_open_strategies"] = "3"
    with pytest.raises(CapitalConfigError, match="max_open_strategies"):
        load_risk_budget(_write(tmp_path, cfg))


def test_swing_daily_loss_key_is_refused(tmp_path: Path) -> None:
    # Operator decision 2026-10-06: swing has NO daily loss limit, enforced at load.
    cfg = _base()
    cfg["swing"]["max_loss_per_day"] = "0.5%"
    with pytest.raises(CapitalConfigError, match="no daily loss limit"):
        load_risk_budget(_write(tmp_path, cfg))


def test_invalid_json_reports_the_path(tmp_path: Path) -> None:
    p = tmp_path / "capital.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(CapitalConfigError, match="invalid JSON"):
        load_risk_budget(p)


# ── Invariants ──────────────────────────────────────────────────────────────────────


def test_per_strategy_daily_must_be_below_structure_max(tmp_path: Path) -> None:
    cfg = _base()
    cfg["options"]["max_loss_per_day_per_strategy"] = "0.75%"  # equals structure max
    with pytest.raises(CapitalConfigError, match="LESS than"):
        load_risk_budget(_write(tmp_path, cfg))


def test_per_strategy_daily_must_not_exceed_all_strategies_daily(tmp_path: Path) -> None:
    cfg = _base()
    cfg["options"]["max_loss_per_day_per_strategy"] = "0.6%"
    with pytest.raises(CapitalConfigError, match="max_loss_per_day_all_options_strategies"):
        load_risk_budget(_write(tmp_path, cfg))


def test_all_strategies_daily_must_not_exceed_shared_risk_ceiling(tmp_path: Path) -> None:
    cfg = _base()
    cfg["options"]["max_loss_per_day_all_options_strategies"] = "2%"
    with pytest.raises(CapitalConfigError, match="max_share_of_shared_risk"):
        load_risk_budget(_write(tmp_path, cfg))


def test_intraday_per_trade_must_not_exceed_half_the_daily_limit(tmp_path: Path) -> None:
    cfg = _base()
    cfg["intraday"]["max_loss_per_trade"] = "0.1%"  # daily is 0.15%, half is 0.075%
    with pytest.raises(CapitalConfigError, match="max_loss_per_trade must be"):
        load_risk_budget(_write(tmp_path, cfg))


@pytest.mark.parametrize("segment", ["options", "swing", "intraday"])
def test_shared_risk_ceiling_must_cover_the_segments_own_per_trade_risk(
    tmp_path: Path, segment: str
) -> None:
    cfg = _base()
    cfg[segment]["max_share_of_shared_risk"] = "0.01%"
    with pytest.raises(CapitalConfigError, match="max_share_of_shared_risk"):
        load_risk_budget(_write(tmp_path, cfg))


@pytest.mark.parametrize("segment", ["options", "swing", "intraday"])
def test_shared_risk_ceiling_must_not_exceed_the_account_cap(tmp_path: Path, segment: str) -> None:
    cfg = _base()
    cfg[segment]["max_share_of_shared_risk"] = "10%"
    with pytest.raises(CapitalConfigError, match="max_loss_if_everything_open_hits_stop"):
        load_risk_budget(_write(tmp_path, cfg))


# ── Scaling: the whole point of the design ─────────────────────────────────────────


def test_every_derived_rupee_value_doubles_when_equity_doubles(tmp_path: Path) -> None:
    b = load_risk_budget(_write(tmp_path, _base()))
    low, high = 2_000_000.0, 4_000_000.0
    pairs = [
        (
            b.account.max_loss_if_everything_open_hits_stop_rupees(low),
            b.account.max_loss_if_everything_open_hits_stop_rupees(high),
        ),
        (
            b.options.max_structure_loss_per_strategy_rupees(low),
            b.options.max_structure_loss_per_strategy_rupees(high),
        ),
        (
            b.options.max_loss_per_day_per_strategy_rupees(low),
            b.options.max_loss_per_day_per_strategy_rupees(high),
        ),
        (
            b.options.max_loss_per_day_all_strategies_rupees(low),
            b.options.max_loss_per_day_all_strategies_rupees(high),
        ),
        (
            b.options.max_share_of_shared_risk_rupees(low),
            b.options.max_share_of_shared_risk_rupees(high),
        ),
        (b.swing.max_loss_per_trade_rupees(low), b.swing.max_loss_per_trade_rupees(high)),
        (
            b.swing.max_share_of_shared_risk_rupees(low),
            b.swing.max_share_of_shared_risk_rupees(high),
        ),
        (b.intraday.max_loss_per_trade_rupees(low), b.intraday.max_loss_per_trade_rupees(high)),
        (
            b.intraday.max_share_of_shared_risk_rupees(low),
            b.intraday.max_share_of_shared_risk_rupees(high),
        ),
        (b.intraday.max_loss_per_day_rupees(low), b.intraday.max_loss_per_day_rupees(high)),
    ]
    for lo, hi in pairs:
        assert hi == pytest.approx(lo * 2)


def test_count_limits_do_not_scale_with_equity(tmp_path: Path) -> None:
    b = load_risk_budget(_write(tmp_path, _base()))
    # Counts are counts, not rupees -- there is no equity parameter to even pass them.
    assert b.options.max_open_strategies == 3
    assert b.swing.max_open_trades == 12
    assert b.intraday.max_open_trades == 6


def test_shipped_config_matches_the_approved_plan_numbers() -> None:
    b = load_risk_budget(ROOT / "config" / "capital.json")
    equity = 2_000_000.0
    assert b.options.max_structure_loss_per_strategy_rupees(equity) == pytest.approx(15_000)
    assert b.options.max_loss_per_day_per_strategy_rupees(equity) == pytest.approx(5_000)
    assert b.options.max_loss_per_day_all_strategies_rupees(equity) == pytest.approx(10_000)
    assert b.options.max_share_of_shared_risk_rupees(equity) == pytest.approx(60_000)
    assert b.swing.max_loss_per_trade_rupees(equity) == pytest.approx(4_000)
    assert b.swing.max_share_of_shared_risk_rupees(equity) == pytest.approx(20_000)
    assert b.intraday.max_loss_per_trade_rupees(equity) == pytest.approx(1_000)
    assert b.intraday.max_share_of_shared_risk_rupees(equity) == pytest.approx(2_000)
    assert b.intraday.max_loss_per_day_rupees(equity) == pytest.approx(3_000)
    assert b.account.max_loss_if_everything_open_hits_stop_rupees(equity) == pytest.approx(60_000)


# ── Live-equity tracking (set_current_equity / get_current_equity) ────────────────


def test_get_current_equity_falls_back_before_anything_is_set() -> None:
    from src.risk import budget as budget_mod

    budget_mod._current_equity = None
    assert budget_mod.get_current_equity() == pytest.approx(
        budget_mod.get_budget().account.fallback_equity_rupees
    )


def test_set_current_equity_is_reflected_immediately() -> None:
    from src.risk import budget as budget_mod

    budget_mod.set_current_equity(3_500_000.0)
    try:
        assert budget_mod.get_current_equity() == pytest.approx(3_500_000.0)
    finally:
        budget_mod._current_equity = None


def test_zero_or_negative_equity_falls_back_rather_than_zeroing_every_limit() -> None:
    from src.risk import budget as budget_mod

    budget_mod.set_current_equity(0.0)
    try:
        assert budget_mod.get_current_equity() > 0
    finally:
        budget_mod._current_equity = None
