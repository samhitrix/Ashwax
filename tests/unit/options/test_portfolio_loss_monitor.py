"""Tests for the seller-desk daily P&L circuit breaker (options_spec.md §11.5)."""

from __future__ import annotations

import math

from src.options.portfolio_loss_monitor import circuit_breaker_check, daily_loss_pct


def test_daily_loss_pct_computation() -> None:
    pct = daily_loss_pct(realized_pnl=-3000.0, unrealized_pnl=-2000.0, options_capital=100_000.0)
    assert math.isclose(pct, -5.0)


def test_daily_loss_pct_none_for_zero_capital() -> None:
    assert daily_loss_pct(-1000.0, 0.0, 0.0) is None


def test_circuit_breaker_fires_at_5pct_loss() -> None:
    halted, reason = circuit_breaker_check(-3000.0, -2000.0, 100_000.0)
    assert halted is True
    assert "CIRCUIT_BREAKER_FIRED" in reason


def test_circuit_breaker_not_fired_below_threshold() -> None:
    halted, _ = circuit_breaker_check(-1000.0, -500.0, 100_000.0)
    assert halted is False


def test_circuit_breaker_ignores_gains() -> None:
    halted, _ = circuit_breaker_check(5000.0, 2000.0, 100_000.0)
    assert halted is False


def test_circuit_breaker_combines_realized_and_unrealized() -> None:
    # Neither alone breaches 5%, but combined does.
    halted, _ = circuit_breaker_check(-3000.0, -2500.0, 100_000.0)
    assert halted is True
