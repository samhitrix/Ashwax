"""
Floor check — standalone capital kill-switch (Supervisor copy).

The floor percentage comes from config/capital.json account.stop_all_trading_if_equity_falls_below
(default 25%, see src/risk/floor_config.py). The check logic lives independently in
src/risk/absolute_floor.py (CRM copy); keep the two implementations separate.

This module is intentionally identical in logic to absolute_floor.py.
Do NOT consolidate them into a shared utility — the redundancy is the safety.

Usage:
    result = supervisor_floor_check(current_equity=180_000.0, starting_capital=200_000.0)
    if result.breached:
        emergency_halt(result.message)
"""

from __future__ import annotations

from dataclasses import dataclass

from src.risk.floor_config import absolute_floor_pct  # noqa: E402

_FLOOR_PCT: float = absolute_floor_pct()


@dataclass(frozen=True)
class SupervisorFloorResult:
    """Result of a supervisor floor check."""

    breached: bool
    equity: float
    starting_capital: float
    equity_pct: float
    message: str


def supervisor_floor_check(equity: float, starting_capital: float) -> SupervisorFloorResult:
    """
    Supervisor-side check: has equity dropped below 25% of starting capital?

    Args:
        equity: Current equity (rupees). Can be negative.
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
            f"SUPERVISOR FLOOR BREACHED: equity={equity:.2f} ({pct:.1%}) "
            f"< {_FLOOR_PCT:.0%} of starting capital {starting_capital:.2f}. "
            "EMERGENCY HALT REQUIRED."
        )
    else:
        message = (
            f"Supervisor floor OK: equity={equity:.2f} ({pct:.1%}) "
            f">= {_FLOOR_PCT:.0%} of {starting_capital:.2f}"
        )

    return SupervisorFloorResult(
        breached=breached,
        equity=equity,
        starting_capital=starting_capital,
        equity_pct=pct,
        message=message,
    )
