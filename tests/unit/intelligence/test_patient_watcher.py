"""Tests for PatientWatcher (spec §10)."""

from __future__ import annotations

from datetime import date

from src.intelligence.patient_watcher import PatientWatcher


def _watcher() -> PatientWatcher:
    return PatientWatcher(min_score=65.0)


def test_empty_below_sample_returns_ok():
    result = _watcher().evaluate()
    assert result.recommendation == "OK"
    assert result.trade_rate == 0.0


def test_trade_rate_goldilocks():
    w = _watcher()
    for i in range(20):
        w.record_signal(f"SYM{i}", score=70.0)
    # 2 trades out of 20 = 10% — goldilocks
    w.record_trade("SYM1")
    w.record_trade("SYM2")
    result = w.evaluate()
    assert result.recommendation == "OK"
    assert result.trade_rate == 0.1


def test_trade_rate_too_high_raises_floor():
    w = _watcher()
    for i in range(10):
        w.record_signal(f"SYM{i}", score=70.0)
        w.record_trade(f"SYM{i}")  # 100% trade rate
    result = w.evaluate()
    assert result.recommendation == "RAISE_QUALITY_FLOOR"
    assert result.auto_raise_score == 5.0
    assert w.effective_min_score == 70.0  # 65 + 5


def test_trade_rate_too_low_review_filter():
    w = _watcher()
    for i in range(20):
        w.record_signal(f"SYM{i}", score=70.0)
    # 0 trades = 0% — below goldilocks
    result = w.evaluate()
    assert result.recommendation == "REVIEW_FILTER"


def test_correct_behavior_days_counted():
    w = _watcher()
    w.record_day_end(today=date(2026, 6, 9), had_qualifying_setups=False)
    w.record_day_end(today=date(2026, 6, 10), had_qualifying_setups=True)
    result = w.evaluate()
    assert result.days_correct_patience == 1


def test_auto_raise_caps_at_20():
    w = _watcher()
    for _ in range(5):
        for i in range(10):
            w.record_signal(f"SYM{i}", score=70.0)
            w.record_trade(f"SYM{i}")
        w.evaluate()
    assert w.effective_min_score <= 85.0  # 65 + 20 cap


def test_signals_and_trades_tracked():
    w = _watcher()
    for i in range(15):
        w.record_signal(f"S{i}", score=72.0)
    w.record_trade("S1")
    w.record_trade("S2")
    w.record_trade("S3")
    r = w.evaluate()
    assert r.signals_seen == 15
    assert r.trades_taken == 3
