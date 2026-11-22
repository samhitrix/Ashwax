"""Unit tests for src/llm/calibration.py"""

from __future__ import annotations

import pytest
from src.llm.calibration import CalibrationEngine, CalibrationRecord, _brier_score


def _add(e: CalibrationEngine, conf: float, wins: int, losses: int, strategy: str = "VCP") -> None:
    for _ in range(wins):
        e.add(strategy, conf, was_win=True)
    for _ in range(losses):
        e.add(strategy, conf, was_win=False)


def test_empty():
    r = CalibrationEngine().evaluate("VCP")
    assert r.brier_score == pytest.approx(0.0)
    assert r.buckets == []
    assert r.overall_miscalibrated is False


def test_well_calibrated():
    e = CalibrationEngine()
    _add(e, 0.75, wins=75, losses=25)
    r = e.evaluate("VCP")
    assert r.worst_gap < 0.15
    assert r.overall_miscalibrated is False


def test_miscalibrated_overconfident():
    e = CalibrationEngine()
    _add(e, 0.90, wins=5, losses=5)
    r = e.evaluate("VCP")
    assert r.overall_miscalibrated is True
    assert r.worst_gap > 0.15


def test_brier_perfect():
    assert _brier_score([CalibrationRecord(1.0, True)] * 10) == pytest.approx(0.0)


def test_brier_worst():
    assert _brier_score([CalibrationRecord(1.0, False)] * 10) == pytest.approx(1.0)


def test_buckets_same_range():
    e = CalibrationEngine()
    _add(e, 0.71, wins=5, losses=5)  # int(0.71/0.05)=14 → bucket 0.70
    _add(e, 0.73, wins=5, losses=5)  # int(0.73/0.05)=14 → bucket 0.70
    r = e.evaluate("VCP")
    assert len(r.buckets) == 1
    assert r.buckets[0].count == 20


def test_buckets_separate_ranges():
    e = CalibrationEngine()
    _add(e, 0.65, wins=5, losses=5)
    _add(e, 0.80, wins=5, losses=5)
    assert len(e.evaluate("VCP").buckets) == 2


def test_strategies_independent():
    e = CalibrationEngine()
    _add(e, 0.90, wins=5, losses=5, strategy="VCP")
    _add(e, 0.75, wins=75, losses=25, strategy="ORB")
    assert e.evaluate("VCP").overall_miscalibrated is True
    assert e.evaluate("ORB").overall_miscalibrated is False


def test_bucket_miscalibrated_flag():
    e = CalibrationEngine()
    _add(e, 0.90, wins=1, losses=9)
    r = e.evaluate("VCP")
    assert any(b.is_miscalibrated for b in r.buckets)
