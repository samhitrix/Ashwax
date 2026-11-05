"""Tests for the seller-desk expiry guardian (options_spec.md §6)."""

from __future__ import annotations

from datetime import time

from src.options.expiry_guardian import evaluate, otm_long_near_money


def test_layer4_before_stage1_on_expiry_day() -> None:
    action = evaluate(dte=0, now_time=time(9, 20), is_expiry_day=True)
    assert action.close_now is True
    assert action.layer == "LAYER4"
    assert action.reason == "EXPIRY_DAY_ABSOLUTE_CLOSE"


def test_layer0_stage1_orderly_close() -> None:
    action = evaluate(dte=0, now_time=time(15, 5), is_expiry_day=True)
    assert action.close_now is True
    assert action.order_type == "LIMIT"
    assert action.layer == "LAYER0_STAGE1"


def test_layer0_stage2_forced_close() -> None:
    action = evaluate(dte=0, now_time=time(15, 20), is_expiry_day=True)
    assert action.close_now is True
    assert action.order_type == "MARKET"
    assert action.reason == "EXPIRY_DAY_STT_PREVENTION_FORCE_CLOSE"


def test_layer3_emergency_at_1_dte() -> None:
    action = evaluate(dte=1, now_time=time(11, 0), is_expiry_day=False)
    assert action.close_now is True
    assert action.reason == "EXPIRY_EMERGENCY_DTE1"


def test_layer2_invariant_at_2_dte() -> None:
    action = evaluate(dte=2, now_time=time(11, 0), is_expiry_day=False)
    assert action.close_now is True
    assert action.reason == "DTE2_INVARIANT_FORCE_CLOSE"


def test_layer1_planned_exit_at_3_dte() -> None:
    action = evaluate(dte=3, now_time=time(11, 0), is_expiry_day=False)
    assert action.close_now is True
    assert action.order_type == "LIMIT"
    assert action.reason == "GAMMA_RISK_DTE3_EXIT"


def test_no_action_outside_guardian_window() -> None:
    action = evaluate(dte=10, now_time=time(11, 0), is_expiry_day=False)
    assert action.close_now is False
    assert action.layer == "NONE"


def test_otm_long_call_near_money() -> None:
    assert otm_long_near_money(spot=24000.0, strike=24100.0, kind="CE") is True  # 0.42% OTM
    assert otm_long_near_money(spot=24000.0, strike=25000.0, kind="CE") is False  # far OTM


def test_otm_long_put_near_money() -> None:
    assert otm_long_near_money(spot=24000.0, strike=23900.0, kind="PE") is True  # 0.42% OTM
    assert otm_long_near_money(spot=24000.0, strike=22000.0, kind="PE") is False


def test_otm_check_false_when_already_itm() -> None:
    assert otm_long_near_money(spot=24000.0, strike=23900.0, kind="CE") is False  # ITM call
