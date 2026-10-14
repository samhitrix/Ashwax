"""Tests for the seller-desk adjustment engine (options_spec.md §10)."""

from __future__ import annotations

import math

from src.options.adjustment_engine import estimate_adjustment_cost, evaluate_adjustment


def test_estimate_adjustment_cost() -> None:
    # 4 legs, spread=2.0 each -> (2/2)*4=4.0 spread cost; notional=100000 -> impact=100
    cost = estimate_adjustment_cost([(98.0, 100.0)] * 4, notional=100_000.0)
    assert math.isclose(cost, 4.0 + 80.0 + 100.0)


def test_evaluate_adjustment_max_count_reached() -> None:
    d = evaluate_adjustment(
        dte=20, adjustments_so_far=2, adjustment_cost=10.0, remaining_premium=1000.0
    )
    assert d.action == "CLOSE"
    assert d.reason == "MAX_ADJUSTMENTS_REACHED"


def test_evaluate_adjustment_blocked_low_dte() -> None:
    d = evaluate_adjustment(
        dte=5, adjustments_so_far=0, adjustment_cost=10.0, remaining_premium=1000.0
    )
    assert d.action == "CLOSE"
    assert d.reason == "ADJUSTMENT_BLOCKED_LOW_DTE"


def test_evaluate_adjustment_cost_exceeds_benefit() -> None:
    d = evaluate_adjustment(
        dte=20, adjustments_so_far=0, adjustment_cost=400.0, remaining_premium=1000.0
    )
    assert d.action == "CLOSE"
    assert "EXCEEDS_BENEFIT" in d.reason


def test_evaluate_adjustment_allowed() -> None:
    d = evaluate_adjustment(
        dte=20, adjustments_so_far=1, adjustment_cost=100.0, remaining_premium=1000.0
    )
    assert d.action == "ADJUST"


def test_evaluate_adjustment_no_remaining_premium_closes() -> None:
    d = evaluate_adjustment(
        dte=20, adjustments_so_far=0, adjustment_cost=10.0, remaining_premium=0.0
    )
    assert d.action == "CLOSE"


def test_max_count_checked_before_dte() -> None:
    # Even with plenty of DTE, hitting max adjustments closes first.
    d = evaluate_adjustment(
        dte=30, adjustments_so_far=2, adjustment_cost=1.0, remaining_premium=10000.0
    )
    assert d.reason == "MAX_ADJUSTMENTS_REACHED"
