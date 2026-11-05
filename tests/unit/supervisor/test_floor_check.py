"""Unit tests for src/supervisor/floor_check.py."""

from __future__ import annotations

import pytest
from src.supervisor.floor_check import SupervisorFloorResult, supervisor_floor_check


def test_no_breach_at_full_equity() -> None:
    r = supervisor_floor_check(equity=200_000.0, starting_capital=200_000.0)
    assert r.breached is False


def test_no_breach_above_floor() -> None:
    r = supervisor_floor_check(equity=100_000.0, starting_capital=200_000.0)
    assert r.breached is False


def test_no_breach_at_exactly_25_pct() -> None:
    r = supervisor_floor_check(equity=50_000.0, starting_capital=200_000.0)
    assert r.breached is False
    assert r.equity_pct == pytest.approx(0.25)


def test_breach_just_below_25_pct() -> None:
    r = supervisor_floor_check(equity=49_999.0, starting_capital=200_000.0)
    assert r.breached is True


def test_breach_at_zero_equity() -> None:
    r = supervisor_floor_check(equity=0.0, starting_capital=200_000.0)
    assert r.breached is True


def test_breach_at_negative_equity() -> None:
    r = supervisor_floor_check(equity=-1_000.0, starting_capital=200_000.0)
    assert r.breached is True


def test_breach_message_contains_emergency_text() -> None:
    r = supervisor_floor_check(equity=40_000.0, starting_capital=200_000.0)
    assert "BREACHED" in r.message
    assert "EMERGENCY" in r.message


def test_ok_message_when_clear() -> None:
    r = supervisor_floor_check(equity=180_000.0, starting_capital=200_000.0)
    assert "OK" in r.message


def test_returns_frozen_dataclass() -> None:
    r = supervisor_floor_check(equity=100_000.0, starting_capital=200_000.0)
    assert isinstance(r, SupervisorFloorResult)
    with pytest.raises(AttributeError):
        r.breached = True  # type: ignore[misc]


def test_invalid_starting_capital_raises() -> None:
    with pytest.raises(ValueError, match="starting_capital"):
        supervisor_floor_check(equity=100_000.0, starting_capital=0.0)


def test_equity_pct_accuracy() -> None:
    r = supervisor_floor_check(equity=75_000.0, starting_capital=200_000.0)
    assert r.equity_pct == pytest.approx(0.375)
