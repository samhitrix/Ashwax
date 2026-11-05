"""Unit tests for src/monitoring/expectancy_engine.py"""

from __future__ import annotations

import pytest
from src.monitoring.expectancy_engine import ExpectancyEngine, ExpectancyStatus, _expectancy


def _record(engine: ExpectancyEngine, wins: int, losses: int, strategy: str = "VCP") -> None:
    for _ in range(wins):
        engine.record(strategy, 2.0)
    for _ in range(losses):
        engine.record(strategy, -1.0)


def test_empty_is_healthy():
    r = ExpectancyEngine().evaluate("VCP")
    assert r.status == ExpectancyStatus.HEALTHY
    assert r.should_pause is False
    assert r.trade_count == 0


def test_positive_edge_healthy():
    e = ExpectancyEngine()
    _record(e, 15, 5)
    assert e.evaluate("VCP").current_e > 0
    assert e.evaluate("VCP").status == ExpectancyStatus.HEALTHY


def test_all_losses_negative_e():
    e = ExpectancyEngine()
    for _ in range(20):
        e.record("VCP", -1.0)
    assert e.evaluate("VCP").current_e < 0


def test_paused_after_consecutive_negative_windows():
    e = ExpectancyEngine()
    for _ in range(30):
        e.record("VCP", -1.0)
    r = e.evaluate("VCP")
    assert r.status == ExpectancyStatus.PAUSED
    assert r.should_pause is True


def test_warning_when_current_below_half_alltime():
    e = ExpectancyEngine()
    for _ in range(40):
        e.record("VCP", 3.0)
    for _ in range(20):
        e.record("VCP", -1.0)
    r = e.evaluate("VCP")
    assert r.status in (ExpectancyStatus.WARNING, ExpectancyStatus.PAUSED)


def test_strategies_independent():
    e = ExpectancyEngine()
    for _ in range(20):
        e.record("VCP", -1.0)
    for _ in range(20):
        e.record("ORB", 2.0)
    assert e.evaluate("VCP").current_e < 0
    assert e.evaluate("ORB").current_e > 0


def test_expectancy_helper():
    assert _expectancy([2.0, 2.0, -1.0, -1.0]) == pytest.approx(0.5)
    assert _expectancy([]) == pytest.approx(0.0)


def test_trade_count():
    e = ExpectancyEngine()
    _record(e, 3, 2)
    assert e.evaluate("VCP").trade_count == 5
