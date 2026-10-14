"""Tests for the seller-desk margin monitor (options_spec.md §11)."""

from __future__ import annotations

import math

from src.options.margin_monitor import (
    cushion_pct,
    evaluate_position_margin,
    new_entries_blocked,
    position_margin_expansion,
)


def test_position_margin_expansion() -> None:
    assert math.isclose(position_margin_expansion(10000.0, 13000.0), 30.0)


def test_position_margin_expansion_none_for_zero_entry() -> None:
    assert position_margin_expansion(0.0, 5000.0) is None


def test_evaluate_position_margin_ok() -> None:
    alert = evaluate_position_margin(10000.0, 10500.0)
    assert alert.level == "OK"


def test_evaluate_position_margin_warning_above_30pct() -> None:
    alert = evaluate_position_margin(10000.0, 13500.0)
    assert alert.level == "WARNING"


def test_evaluate_position_margin_critical_above_60pct() -> None:
    alert = evaluate_position_margin(10000.0, 17000.0)
    assert alert.level == "CRITICAL"
    assert "close now" in alert.reason


def test_cushion_pct() -> None:
    assert math.isclose(cushion_pct(20000.0, 100000.0), 20.0)


def test_new_entries_blocked_below_20pct() -> None:
    blocked, reason = new_entries_blocked(15000.0, 100000.0, currently_blocked=False)
    assert blocked is True


def test_new_entries_not_blocked_above_30pct() -> None:
    blocked, _ = new_entries_blocked(40000.0, 100000.0, currently_blocked=False)
    assert blocked is False


def test_new_entries_hysteresis_stays_blocked_between_20_and_30pct() -> None:
    # Was blocked, cushion recovered to 25% — still below the 30% resume bar.
    blocked, reason = new_entries_blocked(25000.0, 100000.0, currently_blocked=True)
    assert blocked is True
    assert "resume threshold" in reason


def test_new_entries_hysteresis_not_blocked_if_never_was() -> None:
    # 25% cushion but wasn't previously blocked -> no hysteresis lock-in.
    blocked, _ = new_entries_blocked(25000.0, 100000.0, currently_blocked=False)
    assert blocked is False


def test_new_entries_blocked_honours_configured_cushion_and_resume_gap() -> None:
    from src.options.margin_monitor import new_entries_blocked

    # cushion = 40/100*100 = 40%: fine at a 20% floor, blocked at a 50% floor.
    assert new_entries_blocked(40.0, 100.0, currently_blocked=False)[0] is False
    assert new_entries_blocked(40.0, 100.0, currently_blocked=False, block_cushion_pct=50.0)[0]
    # Once blocked at a 30% floor, resume needs 40% (30% + the spec's 10-point gap).
    assert new_entries_blocked(35.0, 100.0, currently_blocked=True, block_cushion_pct=30.0)[0]
    assert not new_entries_blocked(45.0, 100.0, currently_blocked=True, block_cushion_pct=30.0)[0]
