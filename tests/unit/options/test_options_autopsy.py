"""Tests for the seller-desk options autopsy (options_spec.md §18)."""

from __future__ import annotations

from src.options.options_autopsy import (
    adjustment_effectiveness,
    early_profit_taking_analysis,
    greeks_pnl_source,
    regime_correctness,
    run_autopsy,
    strike_delta_appropriateness,
    vol_thesis_correctness,
)


def test_regime_correctness_flags_range_bound_that_turned_trending() -> None:
    f = regime_correctness("HIGH_VOL_RANGE_BOUND", regime_turned_trending=True)
    assert "turned trending" in f.finding


def test_regime_correctness_no_signal_when_regime_held() -> None:
    f = regime_correctness("HIGH_VOL_RANGE_BOUND", regime_turned_trending=False)
    assert "no misclassification" in f.finding


def test_strike_delta_breached() -> None:
    f = strike_delta_appropriateness(0.25, was_breached=True)
    assert "breached" in f.finding


def test_vol_thesis_correct_when_sold_and_iv_dropped() -> None:
    f = vol_thesis_correctness(sold_expecting_iv_drop=True, iv_change_pct=-10.0)
    assert "played out" in f.finding


def test_vol_thesis_wrong_when_sold_and_iv_spiked() -> None:
    f = vol_thesis_correctness(sold_expecting_iv_drop=True, iv_change_pct=15.0)
    assert "WRONG" in f.finding


def test_early_profit_taking_left_value_on_table() -> None:
    f = early_profit_taking_analysis(
        closed_at_pct_of_max=50.0, would_have_captured_holding_to_expiry_pct=80.0
    )
    assert "left" in f.finding


def test_adjustment_effectiveness_none_made() -> None:
    f = adjustment_effectiveness(0, 0.0, 0.0)
    assert f.finding == "no adjustments made"


def test_adjustment_effectiveness_helped() -> None:
    f = adjustment_effectiveness(
        1, pnl_with_adjustments=500.0, estimated_pnl_without_adjustment=200.0
    )
    assert "helped" in f.finding


def test_adjustment_effectiveness_hurt() -> None:
    f = adjustment_effectiveness(
        1, pnl_with_adjustments=100.0, estimated_pnl_without_adjustment=400.0
    )
    assert "hurt" in f.finding


def test_greeks_pnl_source_theta_dominant_positive() -> None:
    f = greeks_pnl_source(delta_pnl=10.0, gamma_pnl=-5.0, theta_pnl=500.0, vega_pnl=-20.0)
    assert "selling edge is real" in f.finding


def test_greeks_pnl_source_vega_dominant_negative() -> None:
    f = greeks_pnl_source(delta_pnl=10.0, gamma_pnl=-5.0, theta_pnl=50.0, vega_pnl=-500.0)
    assert "vol movement ate" in f.finding


def test_run_autopsy_returns_six_findings() -> None:
    findings = run_autopsy(
        regime_at_entry="HIGH_VOL_RANGE_BOUND",
        regime_turned_trending=False,
        target_delta=0.25,
        short_strike_was_breached=False,
        sold_expecting_iv_drop=True,
        iv_change_pct=-5.0,
        closed_at_pct_of_max=50.0,
        would_have_captured_holding_to_expiry_pct=55.0,
        adjustments_made=0,
        pnl_with_adjustments=0.0,
        estimated_pnl_without_adjustment=0.0,
        delta_pnl=0.0,
        gamma_pnl=0.0,
        theta_pnl=300.0,
        vega_pnl=-20.0,
    )
    assert len(findings) == 6
    assert all(f.question for f in findings)


def test_run_autopsy_skips_questions_without_inputs() -> None:
    findings = run_autopsy(
        regime_at_entry=None,
        regime_turned_trending=None,
        target_delta=None,
        short_strike_was_breached=False,
        sold_expecting_iv_drop=True,
        iv_change_pct=None,
        closed_at_pct_of_max=None,
        would_have_captured_holding_to_expiry_pct=None,
        adjustments_made=0,
        pnl_with_adjustments=100.0,
        estimated_pnl_without_adjustment=None,
        delta_pnl=None,
        gamma_pnl=None,
        theta_pnl=None,
        vega_pnl=None,
    )
    assert [f.question for f in findings] == ["adjustment_effectiveness"]


def test_run_autopsy_adjustments_without_counterfactual_is_low_confidence() -> None:
    findings = run_autopsy(
        regime_at_entry="LOW_VOL_RANGE_BOUND",
        regime_turned_trending=True,
        target_delta=0.25,
        short_strike_was_breached=True,
        sold_expecting_iv_drop=True,
        iv_change_pct=12.0,
        closed_at_pct_of_max=None,
        would_have_captured_holding_to_expiry_pct=None,
        adjustments_made=2,
        pnl_with_adjustments=-500.0,
        estimated_pnl_without_adjustment=None,
        delta_pnl=-300.0,
        gamma_pnl=-50.0,
        theta_pnl=400.0,
        vega_pnl=-550.0,
    )
    by_q = {f.question: f for f in findings}
    assert "early_profit_taking" not in by_q
    assert by_q["adjustment_effectiveness"].confidence == 0.3
    assert "vol thesis WRONG" in by_q["vol_thesis_correctness"].finding
    assert len(findings) == 5
