"""Unit tests for src/monitoring/slippage_tracker.py"""

from __future__ import annotations

import pytest
from src.monitoring.slippage_tracker import (
    ADVBucket,
    OrderType,
    SlippageTracker,
    TimeOfDay,
    _compute_slippage,
)


def _fill(
    tracker: SlippageTracker,
    expected: float = 100.0,
    fill: float = 100.3,
    order_type: OrderType = OrderType.LIMIT,
    time: TimeOfDay = TimeOfDay.MORNING,
    adv: ADVBucket = ADVBucket.MID,
) -> None:
    tracker.record_fill(
        order_id="ORD001",
        symbol="RELIANCE",
        order_type=order_type,
        expected_price=expected,
        fill_price=fill,
        time_of_day=time,
        adv_bucket=adv,
    )


# ---------------------------------------------------------------------------
# _compute_slippage
# ---------------------------------------------------------------------------


def test_slippage_positive():
    assert _compute_slippage(100.0, 100.5) == pytest.approx(0.005)


def test_slippage_negative_better_fill():
    assert _compute_slippage(100.0, 99.5) == pytest.approx(-0.005)


def test_slippage_zero_expected():
    assert _compute_slippage(0.0, 100.0) == pytest.approx(0.0)


def test_slippage_exact():
    assert _compute_slippage(100.0, 100.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# record_fill
# ---------------------------------------------------------------------------


def test_record_fill_correct_slippage():
    tracker = SlippageTracker()
    rec = tracker.record_fill(
        "O1", "INFY", OrderType.LIMIT, 1000.0, 1003.0, TimeOfDay.MORNING, ADVBucket.MID
    )
    assert rec.slippage_pct == pytest.approx(0.003)
    assert rec.symbol == "INFY"


def test_record_fill_negative_slippage():
    tracker = SlippageTracker()
    rec = tracker.record_fill(
        "O2", "TCS", OrderType.MARKET, 3000.0, 2998.0, TimeOfDay.MORNING, ADVBucket.HIGH
    )
    assert rec.slippage_pct < 0


# ---------------------------------------------------------------------------
# stats_for
# ---------------------------------------------------------------------------


def test_stats_empty_bucket():
    stats = SlippageTracker().stats_for("LIMIT", "morning", "mid", 0.002)
    assert stats.count == 0
    assert stats.auto_adjust_triggered is False


def test_stats_below_threshold_no_adjust():
    tracker = SlippageTracker()
    for _ in range(10):
        _fill(tracker, expected=100.0, fill=100.1)  # 0.1%
    stats = tracker.stats_for("LIMIT", "morning", "mid", 0.002)
    assert stats.auto_adjust_triggered is False
    assert stats.suggested_buffer_pct == pytest.approx(0.002)


def test_stats_above_threshold_triggers_adjust():
    tracker = SlippageTracker()
    for _ in range(10):
        _fill(tracker, expected=100.0, fill=100.5)  # 0.5%
    stats = tracker.stats_for("LIMIT", "morning", "mid", 0.002)
    assert stats.auto_adjust_triggered is True
    assert stats.suggested_buffer_pct == pytest.approx(0.003)


def test_stats_rolling_window_uses_last_10():
    tracker = SlippageTracker()
    for _ in range(5):
        _fill(tracker, expected=100.0, fill=101.0)  # 1% (old)
    for _ in range(10):
        _fill(tracker, expected=100.0, fill=100.1)  # 0.1% (recent)
    stats = tracker.stats_for("LIMIT", "morning", "mid", 0.002)
    assert stats.auto_adjust_triggered is False


def test_market_no_auto_adjust():
    tracker = SlippageTracker()
    for _ in range(10):
        tracker.record_fill(
            "X", "HDFC", OrderType.MARKET, 1500.0, 1510.0, TimeOfDay.MORNING, ADVBucket.MID
        )
    assert SlippageTracker().stats_for("MARKET", "morning", "mid").auto_adjust_triggered is False


def test_stats_count():
    tracker = SlippageTracker()
    for _ in range(5):
        _fill(tracker)
    assert tracker.stats_for("LIMIT", "morning", "mid").count == 5


def test_stats_avg_correct():
    tracker = SlippageTracker()
    _fill(tracker, expected=100.0, fill=100.2)  # 0.2%
    _fill(tracker, expected=100.0, fill=100.4)  # 0.4%
    stats = tracker.stats_for("LIMIT", "morning", "mid")
    assert stats.avg_slippage_pct == pytest.approx(0.003, rel=1e-2)


def test_buckets_independent():
    tracker = SlippageTracker()
    for _ in range(10):
        _fill(tracker, expected=100.0, fill=100.5, time=TimeOfDay.MORNING)
    for _ in range(10):
        _fill(tracker, expected=100.0, fill=100.1, time=TimeOfDay.AFTERNOON)
    assert tracker.stats_for("LIMIT", "morning", "mid").auto_adjust_triggered is True
    assert tracker.stats_for("LIMIT", "afternoon", "mid").auto_adjust_triggered is False


# ---------------------------------------------------------------------------
# check_limit_auto_adjust
# ---------------------------------------------------------------------------


def test_check_auto_adjust_true():
    tracker = SlippageTracker()
    for _ in range(10):
        _fill(tracker, expected=100.0, fill=100.5)
    assert tracker.check_limit_auto_adjust("morning", "mid", 0.002) is True


def test_check_auto_adjust_false():
    tracker = SlippageTracker()
    for _ in range(10):
        _fill(tracker, expected=100.0, fill=100.1)
    assert tracker.check_limit_auto_adjust("morning", "mid", 0.002) is False
