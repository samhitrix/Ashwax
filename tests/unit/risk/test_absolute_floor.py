"""Unit tests for src/risk/absolute_floor.py."""

from __future__ import annotations

import pytest
from src.risk.absolute_floor import FloorResult, check_absolute_floor


def test_no_breach_at_full_equity() -> None:
    r = check_absolute_floor(equity=200_000.0, starting_capital=200_000.0)
    assert r.breached is False
    assert r.equity_pct == 1.0


def test_no_breach_above_floor() -> None:
    r = check_absolute_floor(equity=100_000.0, starting_capital=200_000.0)
    assert r.breached is False
    assert r.equity_pct == 0.5


def test_no_breach_at_exactly_25_pct() -> None:
    r = check_absolute_floor(equity=50_000.0, starting_capital=200_000.0)
    assert r.breached is False
    assert r.equity_pct == pytest.approx(0.25)


def test_breach_just_below_floor() -> None:
    r = check_absolute_floor(equity=49_999.0, starting_capital=200_000.0)
    assert r.breached is True


def test_breach_at_zero_equity() -> None:
    r = check_absolute_floor(equity=0.0, starting_capital=200_000.0)
    assert r.breached is True
    assert r.equity_pct == 0.0


def test_breach_at_negative_equity() -> None:
    r = check_absolute_floor(equity=-5_000.0, starting_capital=200_000.0)
    assert r.breached is True
    assert r.equity_pct < 0


def test_message_contains_breached_text_when_breached() -> None:
    r = check_absolute_floor(equity=40_000.0, starting_capital=200_000.0)
    assert "BREACHED" in r.message
    assert "HALTED" in r.message


def test_message_contains_ok_text_when_clear() -> None:
    r = check_absolute_floor(equity=150_000.0, starting_capital=200_000.0)
    assert "OK" in r.message


def test_returns_frozen_dataclass() -> None:
    r = check_absolute_floor(equity=100_000.0, starting_capital=200_000.0)
    assert isinstance(r, FloorResult)
    with pytest.raises(AttributeError):
        r.breached = True  # type: ignore[misc]


def test_invalid_starting_capital_raises() -> None:
    with pytest.raises(ValueError, match="starting_capital"):
        check_absolute_floor(equity=100_000.0, starting_capital=0.0)

    with pytest.raises(ValueError, match="starting_capital"):
        check_absolute_floor(equity=100_000.0, starting_capital=-1.0)


def test_equity_pct_is_accurate() -> None:
    r = check_absolute_floor(equity=60_000.0, starting_capital=200_000.0)
    assert r.equity_pct == pytest.approx(0.30)
