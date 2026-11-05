"""Tests for SurvivalMode — config-driven thresholds + hysteresis."""

from __future__ import annotations

from datetime import timedelta

import pytest
from src.infrastructure.survival_mode import OperatingLevel, SurvivalMetrics, SurvivalMode

_RISK_CFG = {
    "drawdown_staircase": {
        "tier_1_pct": 3,
        "tier_2_pct": 5,
        "tier_3_pct": 8,
        "tier_4_pct": 10,
    },
    "circuit_breakers": {
        "vix_reduce_threshold": 25,
        "vix_halt_threshold": 35,
    },
}


def make_mode(hysteresis_minutes: int = 5) -> SurvivalMode:
    return SurvivalMode(risk_cfg=_RISK_CFG, hysteresis_minutes=hysteresis_minutes)


def test_default_level_is_full_ops():
    mode = make_mode()
    assert mode.level == OperatingLevel.FULL_OPS
    assert mode.allow_new_trades()
    assert mode.size_multiplier() == 1.0


def test_reads_thresholds_from_config():
    mode = make_mode()
    assert mode._tier1_pct == pytest.approx(0.03)
    assert mode._tier2_pct == pytest.approx(0.05)
    assert mode._tier3_pct == pytest.approx(0.08)
    assert mode._tier4_pct == pytest.approx(0.10)
    assert mode._vix_reduce == 25.0
    assert mode._vix_halt == 35.0


def test_elevated_on_tier1_drawdown():
    mode = make_mode()
    mode.update(SurvivalMetrics(drawdown_pct=0.04))
    assert mode.level == OperatingLevel.ELEVATED
    assert mode.size_multiplier() == 0.75
    assert mode.allow_new_trades()


def test_reduced_on_tier2_drawdown():
    mode = make_mode()
    mode.update(SurvivalMetrics(drawdown_pct=0.06))
    assert mode.level == OperatingLevel.REDUCED
    assert mode.size_multiplier() == 0.50


def test_monitor_only_on_tier3_drawdown():
    mode = make_mode()
    mode.update(SurvivalMetrics(drawdown_pct=0.09))
    assert mode.level == OperatingLevel.MONITOR_ONLY
    assert not mode.allow_new_trades()
    assert mode.size_multiplier() == 0.0


def test_emergency_on_tier4_drawdown():
    mode = make_mode()
    mode.update(SurvivalMetrics(drawdown_pct=0.11))
    assert mode.level == OperatingLevel.EMERGENCY


def test_emergency_on_vix_halt():
    mode = make_mode()
    mode.update(SurvivalMetrics(vix=36.0))
    assert mode.level == OperatingLevel.EMERGENCY


def test_elevated_on_vix_reduce():
    mode = make_mode()
    mode.update(SurvivalMetrics(vix=26.0))
    assert mode.level == OperatingLevel.ELEVATED


def test_monitor_only_on_unknown_regime():
    mode = make_mode()
    mode.update(SurvivalMetrics(regime="UNKNOWN"))
    assert mode.level == OperatingLevel.MONITOR_ONLY


def test_monitor_only_on_db_down():
    mode = make_mode()
    mode.update(SurvivalMetrics(db_reachable=False))
    assert mode.level == OperatingLevel.MONITOR_ONLY


def test_emergency_on_broker_3_failures():
    mode = make_mode()
    mode.update(SurvivalMetrics(broker_consecutive_failures=3))
    assert mode.level == OperatingLevel.EMERGENCY


def test_step_up_immediate():
    mode = make_mode(hysteresis_minutes=60)
    changed = mode.update(SurvivalMetrics(drawdown_pct=0.09))
    assert changed
    assert mode.level == OperatingLevel.MONITOR_ONLY


def test_step_down_requires_hysteresis():
    mode = make_mode(hysteresis_minutes=5)
    mode.update(SurvivalMetrics(drawdown_pct=0.09))
    changed = mode.update(SurvivalMetrics(drawdown_pct=0.0))
    assert not changed
    assert mode.level == OperatingLevel.MONITOR_ONLY


def test_step_down_after_hysteresis():
    mode = make_mode(hysteresis_minutes=5)
    mode.update(SurvivalMetrics(drawdown_pct=0.09))
    assert mode.level == OperatingLevel.MONITOR_ONLY
    # Simulate recovery metrics — target drops to FULL_OPS, reset target_since
    mode.update(SurvivalMetrics(drawdown_pct=0.0))
    assert mode.level == OperatingLevel.MONITOR_ONLY  # hysteresis blocks step-down
    # Backdate target_since so hysteresis expires
    mode._target_since = mode._target_since - timedelta(minutes=6)
    changed = mode.update(SurvivalMetrics(drawdown_pct=0.0))
    assert changed
    assert mode.level == OperatingLevel.FULL_OPS


def test_as_dict_structure():
    mode = make_mode()
    d = mode.as_dict()
    assert d["level"] == 1
    assert d["label"] == "FULL OPS"
    assert d["allow_new_trades"] is True
    assert d["size_multiplier"] == 1.0


def test_defaults_without_config():
    mode = SurvivalMode()
    assert mode.level == OperatingLevel.FULL_OPS
    mode.update(SurvivalMetrics(drawdown_pct=0.11))
    assert mode.level == OperatingLevel.EMERGENCY
