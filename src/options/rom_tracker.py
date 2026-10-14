"""Seller-desk Return on Margin tracker (options_spec.md §15).

Pre-trade gate, not a monitoring tool: every credit strategy must clear the
minimum annualized ROM before order submission — below it, premium doesn't
adequately compensate for tail risk.
"""

from __future__ import annotations

_MIN_ANNUALIZED_ROM_PCT = 24.0


def annualized_rom(premium_collected: float, margin_blocked: float, dte: int) -> float | None:
    """(premium/margin) * (365/DTE) * 100. ``None`` if margin<=0 or dte<=0."""
    if margin_blocked <= 0.0 or dte <= 0:
        return None
    return (premium_collected / margin_blocked) * (365.0 / dte) * 100.0


def rom_gate(
    premium_collected: float,
    margin_blocked: float,
    dte: int,
    *,
    min_pct: float = _MIN_ANNUALIZED_ROM_PCT,
) -> tuple[bool, str]:
    """(passed, reason)."""
    rom = annualized_rom(premium_collected, margin_blocked, dte)
    if rom is None:
        return False, "ROM undefined (margin<=0 or dte<=0)"
    if rom < min_pct:
        return False, f"rom={rom:.1f}% < min={min_pct:.0f}%"
    return True, f"rom={rom:.1f}%"
