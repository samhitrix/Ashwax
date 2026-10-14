"""Tests for the seller-desk position manager (options_spec.md §10)."""

from __future__ import annotations

from src.options.position_manager import (
    check_breakeven_lock,
    check_delta_breach,
    check_delta_breach_any_leg,
    check_dte,
    check_iv_crush,
    check_profit_target,
    check_stop_loss,
    evaluate_position,
)
from src.options.structure_selector import OptionLeg


def _income_leg(instrument_type: str, delta: float) -> OptionLeg:
    return OptionLeg(
        tradingsymbol=f"NFO:NIFTY24000{instrument_type}",
        instrument_type=instrument_type,
        strike=24000.0,
        direction="SELL",
        lots=1,
        lot_size=25,
        premium=50.0,
        delta=delta,
    )


def test_delta_breach_normal_threshold() -> None:
    assert check_delta_breach(0.40, blackout_active=False).action == "ADJUST"
    assert check_delta_breach(0.30, blackout_active=False).action == "NONE"


def test_delta_breach_blackout_tighter_threshold() -> None:
    assert (
        check_delta_breach(0.30, blackout_active=True).action == "ADJUST"
    )  # blocked at 0.25 in blackout
    assert check_delta_breach(0.30, blackout_active=False).action == "NONE"


def test_delta_breach_any_leg_checks_both_income_legs() -> None:
    # 2026-09-11 bug: the caller only ever checked income_legs[0], so an Iron
    # Condor's call side breaching was invisible whenever the put leg was
    # listed first (which build_iron_condor always does).
    put_leg = _income_leg("PE", -0.20)  # untested, healthy
    call_leg = _income_leg("CE", 0.40)  # tested — breach
    action = check_delta_breach_any_leg([put_leg, call_leg], blackout_active=False)
    assert action.action == "ADJUST"
    assert action.breached_instrument_type == "CE"


def test_delta_breach_any_leg_no_breach() -> None:
    put_leg = _income_leg("PE", -0.20)
    call_leg = _income_leg("CE", 0.22)
    action = check_delta_breach_any_leg([put_leg, call_leg], blackout_active=False)
    assert action.action == "NONE"
    assert action.breached_instrument_type is None


def test_delta_breach_any_leg_picks_worst_when_both_breach() -> None:
    put_leg = _income_leg("PE", -0.50)  # worse breach
    call_leg = _income_leg("CE", 0.40)
    action = check_delta_breach_any_leg([put_leg, call_leg], blackout_active=False)
    assert action.breached_instrument_type == "PE"


def test_profit_target_credit_hits_50pct() -> None:
    action = check_profit_target("credit", unrealized_pnl=600.0, max_potential=1000.0)
    assert action.action == "CLOSE"


def test_profit_target_credit_blackout_tighter() -> None:
    action = check_profit_target(
        "credit", unrealized_pnl=400.0, max_potential=1000.0, blackout_active=True
    )
    assert action.action == "CLOSE"  # 40% >= 35% blackout target, below normal 50%


def test_profit_target_debit_60pct() -> None:
    assert (
        check_profit_target("debit", unrealized_pnl=700.0, max_potential=1000.0).action == "CLOSE"
    )
    assert check_profit_target("debit", unrealized_pnl=500.0, max_potential=1000.0).action == "NONE"


def test_stop_loss_credit_150pct_of_premium() -> None:
    action = check_stop_loss("credit", unrealized_pnl=-1600.0, premium=1000.0)
    assert action.action == "CLOSE"
    action_ok = check_stop_loss("credit", unrealized_pnl=-800.0, premium=1000.0)
    assert action_ok.action == "NONE"


def test_stop_loss_debit_100pct_of_premium() -> None:
    action = check_stop_loss("debit", unrealized_pnl=-1000.0, premium=1000.0)
    assert action.action == "CLOSE"


def test_dte_double_check_triggers_at_3() -> None:
    assert check_dte(3).action == "CLOSE"
    assert check_dte(4).action == "NONE"


def test_iv_crush_only_applies_to_debit() -> None:
    assert check_iv_crush("credit", entry_iv=0.20, current_iv=0.10).action == "NONE"
    assert check_iv_crush("debit", entry_iv=0.20, current_iv=0.10).action == "CLOSE"  # 50% drop
    assert check_iv_crush("debit", entry_iv=0.20, current_iv=0.18).action == "NONE"  # 10% drop


def test_evaluate_position_aggregates_all_triggered_actions() -> None:
    actions = evaluate_position(
        direction="credit",
        short_strike_delta=0.40,  # breach
        unrealized_pnl=-1600.0,  # stop loss too
        max_potential=1000.0,
        premium=1000.0,
        dte=10,  # no dte trigger
        entry_iv=0.20,
        current_iv=0.20,  # no iv crush (not debit anyway)
        blackout_active=False,
    )
    reasons = {a.action for a in actions}
    assert "ADJUST" in reasons
    assert "CLOSE" in reasons


def test_evaluate_position_no_triggers_when_healthy() -> None:
    actions = evaluate_position(
        direction="credit",
        short_strike_delta=0.15,
        unrealized_pnl=100.0,
        max_potential=1000.0,
        premium=1000.0,
        dte=20,
        entry_iv=0.20,
        current_iv=0.20,
    )
    assert actions == []


def test_profit_target_and_stop_read_from_config(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.options import position_manager as pm

    monkeypatch.setattr(
        pm.seller_settings,
        "profit_and_stop",
        lambda: {"credit_profit_target_pct": 30, "credit_stop_loss_pct": 80},
    )
    assert pm.check_profit_target("credit", 35.0, 100.0).action == "CLOSE"  # 35% >= 30%
    assert pm.check_stop_loss("credit", -85.0, 100.0).action == "CLOSE"  # -85 <= -80


def test_profit_target_falls_back_to_spec_defaults_when_config_empty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.options import position_manager as pm

    monkeypatch.setattr(pm.seller_settings, "profit_and_stop", lambda: {})
    assert pm.check_profit_target("credit", 35.0, 100.0).action == "NONE"  # default target 50%
    assert pm.check_profit_target("credit", 55.0, 100.0).action == "CLOSE"


def test_breakeven_lock_closes_debit_once_armed_and_pnl_back_to_zero() -> None:
    # Was +40% of the debit (armed at 35%), has now given it all back: lock out at 0.
    assert (
        check_breakeven_lock(
            "debit", unrealized_pnl=-50.0, peak_unrealized_pnl=400.0, premium=1000.0
        ).action
        == "CLOSE"
    )


def test_breakeven_lock_not_armed_below_threshold() -> None:
    # Peak only +30% -- the lock never armed, so a dip below zero is left to the stop.
    assert (
        check_breakeven_lock(
            "debit", unrealized_pnl=-50.0, peak_unrealized_pnl=300.0, premium=1000.0
        ).action
        == "NONE"
    )


def test_breakeven_lock_holds_while_still_in_profit() -> None:
    assert (
        check_breakeven_lock(
            "debit", unrealized_pnl=100.0, peak_unrealized_pnl=400.0, premium=1000.0
        ).action
        == "NONE"
    )


def test_breakeven_lock_is_debit_only_and_needs_a_peak() -> None:
    assert (
        check_breakeven_lock(
            "credit", unrealized_pnl=-50.0, peak_unrealized_pnl=400.0, premium=1000.0
        ).action
        == "NONE"
    )
    assert (
        check_breakeven_lock(
            "debit", unrealized_pnl=-50.0, peak_unrealized_pnl=None, premium=1000.0
        ).action
        == "NONE"
    )


def test_breakeven_lock_arm_pct_read_from_config(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.options import position_manager as pm

    monkeypatch.setattr(
        pm.seller_settings, "profit_and_stop", lambda: {"debit_breakeven_lock_arm_pct": 20}
    )
    assert pm.check_breakeven_lock("debit", -10.0, 250.0, 1000.0).action == "CLOSE"  # 25% >= 20%


def test_evaluate_position_includes_breakeven_lock() -> None:
    actions = evaluate_position(
        direction="debit",
        short_strike_delta=None,
        unrealized_pnl=-10.0,
        max_potential=1000.0,
        premium=1000.0,
        dte=20,
        entry_iv=0.0,
        current_iv=0.0,
        peak_unrealized_pnl=400.0,
    )
    assert [a.reason.split()[0] for a in actions] == ["BREAKEVEN_LOCK"]


def test_shipped_debit_stop_is_not_a_noop() -> None:
    # Regression (2026-09-28): debit_stop_loss_pct was 100 = "stop at total loss" = no stop.
    # A bull_call_spread bled to -55..-70% of its debit with no exit. The shipped config
    # must stop a debit spread well before max loss.
    from src.options import position_manager as pm

    assert pm.check_stop_loss("debit", unrealized_pnl=-600.0, premium=1000.0).action == "CLOSE"
    assert pm.check_stop_loss("debit", unrealized_pnl=-400.0, premium=1000.0).action == "NONE"
