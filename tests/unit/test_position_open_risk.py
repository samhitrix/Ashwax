"""Open risk counts only the losing side of stop-to-fill; locked profit is 0 risk."""

from __future__ import annotations

from types import SimpleNamespace

from src.orchestrator import _position_open_risk


def _p(direction: str, fill: float, sl: float, qty: int = 10) -> SimpleNamespace:
    return SimpleNamespace(direction=direction, fill_price=fill, current_sl=sl, quantity=qty)


def test_long_and_short_initial_risk() -> None:
    assert _position_open_risk(_p("BUY", 100.0, 98.0)) == 20.0
    assert _position_open_risk(_p("SELL", 100.0, 102.0)) == 20.0
    assert _position_open_risk(_p("short", 100.0, 102.0)) == 20.0


def test_stop_past_entry_is_zero_risk_not_abs() -> None:
    assert _position_open_risk(_p("BUY", 100.0, 101.0)) == 0.0  # long, profit locked
    assert _position_open_risk(_p("SELL", 1208.38, 1199.19)) == 0.0  # short, profit locked
