"""Tests for CalibrationEngine — Phase 4 Item 2."""

from __future__ import annotations

from src.llm.calibration import CalibrationEngine


def test_empty_returns_zero_brier() -> None:
    engine = CalibrationEngine()
    result = engine.evaluate("VCP")
    assert result.brier_score == 0.0
    assert result.overall_miscalibrated is False
    assert result.buckets == []


def test_perfect_calibration() -> None:
    """All predictions at 0.75 with 75% actual win rate → near-zero gap."""
    engine = CalibrationEngine()
    for _ in range(75):
        engine.add("VCP", llm_confidence=0.75, was_win=True)
    for _ in range(25):
        engine.add("VCP", llm_confidence=0.75, was_win=False)
    result = engine.evaluate("VCP")
    assert result.overall_miscalibrated is False
    assert result.worst_gap < 0.15


def test_overconfident_flagged_as_miscalibrated() -> None:
    """Predicts 0.90 confidence but only 50% win rate → gap > 0.15."""
    engine = CalibrationEngine()
    for _ in range(5):
        engine.add("VCP", llm_confidence=0.90, was_win=True)
    for _ in range(5):
        engine.add("VCP", llm_confidence=0.90, was_win=False)
    result = engine.evaluate("VCP")
    assert result.overall_miscalibrated is True
    assert result.worst_gap > 0.15


def test_brier_score_known_value() -> None:
    """All 0.7 confidence, all wins → Brier = (0.7-1)^2 = 0.09."""
    engine = CalibrationEngine()
    for _ in range(10):
        engine.add("S1", llm_confidence=0.7, was_win=True)
    result = engine.evaluate("S1")
    assert abs(result.brier_score - 0.09) < 0.001


def test_fewer_than_5_records_skipped_in_report() -> None:
    """Loop skips strategies with <5 records during weekly eval."""
    engine = CalibrationEngine()
    for _ in range(3):
        engine.add("SMALL", llm_confidence=0.6, was_win=True)
    for _ in range(10):
        engine.add("BIG", llm_confidence=0.6, was_win=True)

    report = {}
    for strat, records in engine._records.items():
        if len(records) >= 5:
            report[strat] = engine.evaluate(strat)

    assert "BIG" in report
    assert "SMALL" not in report


def test_seen_ids_dedup_logic() -> None:
    """Same trade_id should only be recorded once."""
    seen: set[str] = set()
    trades = [
        {"trade_id": "X1", "strategy_name": "VCP", "pnl_r": 1.0, "llm_confidence": 0.8},
        {"trade_id": "X1", "strategy_name": "VCP", "pnl_r": 1.0, "llm_confidence": 0.8},  # dup
        {"trade_id": "X2", "strategy_name": "VCP", "pnl_r": -0.5, "llm_confidence": 0.6},
    ]
    engine = CalibrationEngine()
    for t in trades:
        tid = str(t.get("trade_id", ""))
        if not tid or tid in seen:
            continue
        seen.add(tid)
        engine.add(
            str(t["strategy_name"]),
            llm_confidence=float(t["llm_confidence"]),
            was_win=float(t["pnl_r"]) > 0,
        )
    assert len(engine._records["VCP"]) == 2  # X1 + X2 only
