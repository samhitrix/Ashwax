"""
Absolute floor — standalone capital kill-switch (CRM copy).

The floor percentage comes from config/capital.json account.stop_all_trading_if_equity_falls_below
(default 25%, see src/risk/floor_config.py). The check logic lives independently in
src/supervisor/floor_check.py; keep the two implementations separate.

Usage:
    result = check_absolute_floor(current_equity=180_000.0, starting_capital=200_000.0)
    if result.breached:
        halt_all_trading(result.message)
"""

from __future__ import annotations

from dataclasses import dataclass

from src.risk.floor_config import absolute_floor_pct  # noqa: E402

_FLOOR_PCT: float = absolute_floor_pct()


@dataclass(frozen=True)
class FloorResult:
    """Result of an absolute-floor check."""

    breached: bool
    equity: float
    starting_capital: float
    equity_pct: float
    message: str


def check_absolute_floor(equity: float, starting_capital: float) -> FloorResult:
    """
    Return FloorResult indicating whether equity has dropped below 25% of starting capital.

    Args:
        equity: Current equity (rupees). Can be negative if losses exceed capital.
        starting_capital: Capital at session/day start (rupees). Must be > 0.

    Raises:
        ValueError: If starting_capital is not positive.
    """
    if starting_capital <= 0:
        raise ValueError(f"starting_capital must be > 0, got {starting_capital}")

    pct = equity / starting_capital
    breached = pct < _FLOOR_PCT

    if breached:
        message = (
            f"ABSOLUTE FLOOR BREACHED: equity={equity:.2f} ({pct:.1%}) "
            f"< {_FLOOR_PCT:.0%} of starting capital {starting_capital:.2f}. "
            "ALL TRADING HALTED."
        )
    else:
        message = (
            f"Floor OK: equity={equity:.2f} ({pct:.1%}) "
            f">= {_FLOOR_PCT:.0%} of {starting_capital:.2f}"
        )

    return FloorResult(
        breached=breached,
        equity=equity,
        starting_capital=starting_capital,
        equity_pct=pct,
        message=message,
    )
