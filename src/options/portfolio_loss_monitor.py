"""Seller-desk daily P&L circuit breaker (options_spec.md §11.5).

Separate from the Greeks budgets: a simultaneous adverse move in spot + IV +
time can breach overall P&L while each individual Greek budget still looks
intact (see spec's worked example — three "okay" budget signals, one 4% loss).
Only a direct P&L stop catches this.
"""

from __future__ import annotations

_DAILY_LOSS_HALT_PCT = 5.0


def daily_loss_pct(
    realized_pnl: float, unrealized_pnl: float, options_capital: float
) -> float | None:
    """Negative = loss, as a percentage of options_capital. None if capital<=0."""
    if options_capital <= 0.0:
        return None
    return (realized_pnl + unrealized_pnl) / options_capital * 100.0


def circuit_breaker_check(
    realized_pnl: float,
    unrealized_pnl: float,
    options_capital: float,
    *,
    halt_pct: float = _DAILY_LOSS_HALT_PCT,
) -> tuple[bool, str]:
    """(halted, reason). True once today's combined options loss reaches halt_pct."""
    loss_pct = daily_loss_pct(realized_pnl, unrealized_pnl, options_capital)
    if loss_pct is None:
        return False, "options_capital unavailable"
    if -loss_pct >= halt_pct:
        return (
            True,
            f"OPTIONS_DAILY_PNL_CIRCUIT_BREAKER_FIRED loss={-loss_pct:.1f}% >= {halt_pct:.0f}%",
        )
    return False, f"loss={-loss_pct:.1f}% within limit"
