"""Unit tests for src/monitoring/performance_attribution.py"""

from __future__ import annotations

from datetime import datetime

import pytest
from src.monitoring.performance_attribution import (
    PerformanceAttributor,
    TradeRecord,
    _confidence_bucket,
    _hold_duration,
    _time_window,
)


def _trade(
    strategy: str = "VCP",
    pnl_r: float = 1.5,
    entry_hour: int = 10,
    regime: str = "BULL",
    sector: str = "BANKING",
    confidence: float = 0.75,
    hold_hours: float = 2.0,
    volume: str = "AVG",
) -> TradeRecord:
    return TradeRecord(
        strategy=strategy,
        pnl_r=pnl_r,
        entry_time=datetime(2026, 1, 15, entry_hour, 0),
        regime_at_entry=regime,
        sector=sector,
        llm_confidence=confidence,
        hold_hours=hold_hours,
        volume_at_entry=volume,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_time_window_morning():
    assert _time_window(datetime(2026, 1, 1, 10, 0)) == "morning"


def test_time_window_midmorning():
    assert _time_window(datetime(2026, 1, 1, 11, 0)) == "mid_morning"


def test_time_window_midday():
    assert _time_window(datetime(2026, 1, 1, 12, 30)) == "midday"


def test_time_window_afternoon():
    assert _time_window(datetime(2026, 1, 1, 13, 45)) == "afternoon"


def test_time_window_preclose():
    assert _time_window(datetime(2026, 1, 1, 15, 0)) == "pre_close"


def test_confidence_bucket_below():
    assert _confidence_bucket(0.55) == "below_0.60"


def test_confidence_bucket_mid():
    assert _confidence_bucket(0.72).startswith("0.70")


def test_confidence_bucket_max():
    assert "1.00" in _confidence_bucket(1.0)


def test_hold_intraday():
    assert _hold_duration(3.0) == "intraday"


def test_hold_lt_3_days():
    assert _hold_duration(24.0) == "lt_3_days"


def test_hold_3_to_7():
    assert _hold_duration(4 * 24) == "3_to_7_days"


def test_hold_gt_7():
    assert _hold_duration(8 * 24) == "gt_7_days"


# ---------------------------------------------------------------------------
# PerformanceAttributor
# ---------------------------------------------------------------------------


def test_attribute_empty():
    result = PerformanceAttributor().attribute([])
    assert result.by_strategy == {}
    assert result.computed_at != ""


def test_attribute_by_strategy():
    trades = [_trade("VCP", 1.5), _trade("VCP", -1.0), _trade("ORB", 2.0)]
    result = PerformanceAttributor().attribute(trades)
    assert result.by_strategy["VCP"].count == 2
    assert result.by_strategy["ORB"].count == 1


def test_attribute_by_sector():
    trades = [_trade(sector="BANKING"), _trade(sector="IT"), _trade(sector="IT")]
    result = PerformanceAttributor().attribute(trades)
    assert result.by_sector["BANKING"].count == 1
    assert result.by_sector["IT"].count == 2


def test_attribute_by_regime():
    trades = [_trade(regime="BULL"), _trade(regime="BEAR"), _trade(regime="BULL")]
    result = PerformanceAttributor().attribute(trades)
    assert result.by_regime["BULL"].count == 2
    assert result.by_regime["BEAR"].count == 1


def test_win_rate_correct():
    trades = [_trade(pnl_r=1.5), _trade(pnl_r=-1.0), _trade(pnl_r=2.0)]
    result = PerformanceAttributor().attribute(trades)
    assert result.by_strategy["VCP"].win_rate == pytest.approx(2 / 3, rel=1e-3)


def test_profit_factor_all_wins():
    trades = [_trade(pnl_r=2.0), _trade(pnl_r=1.5)]
    result = PerformanceAttributor().attribute(trades)
    assert result.by_strategy["VCP"].profit_factor == float("inf")


def test_expectancy_positive_edge():
    trades = [_trade(pnl_r=2.0)] * 3 + [_trade(pnl_r=-1.0)]
    result = PerformanceAttributor().attribute(trades)
    assert result.by_strategy["VCP"].expectancy > 0


def test_total_r_correct():
    trades = [_trade(pnl_r=1.5), _trade(pnl_r=-0.5)]
    result = PerformanceAttributor().attribute(trades)
    assert result.by_strategy["VCP"].total_r == pytest.approx(1.0)


def test_by_hold_duration():
    trades = [_trade(hold_hours=2.0), _trade(hold_hours=24.0), _trade(hold_hours=24.0)]
    result = PerformanceAttributor().attribute(trades)
    assert result.by_hold_duration["intraday"].count == 1
    assert result.by_hold_duration["lt_3_days"].count == 2


def test_by_volume():
    trades = [_trade(volume="SURGE"), _trade(volume="AVG"), _trade(volume="SURGE")]
    result = PerformanceAttributor().attribute(trades)
    assert result.by_volume["SURGE"].count == 2
    assert result.by_volume["AVG"].count == 1


def test_confidence_bucket_grouping():
    trades = [_trade(confidence=0.72), _trade(confidence=0.72), _trade(confidence=0.85)]
    result = PerformanceAttributor().attribute(trades)
    total = sum(v.count for v in result.by_confidence_bucket.values())
    assert total == 3


def test_all_seven_dimensions():
    trades = [_trade() for _ in range(5)]
    result = PerformanceAttributor().attribute(trades)
    assert all(
        [
            result.by_strategy,
            result.by_time_window,
            result.by_regime,
            result.by_sector,
            result.by_confidence_bucket,
            result.by_hold_duration,
            result.by_volume,
        ]
    )
