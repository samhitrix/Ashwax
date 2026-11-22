"""Position sizing — hardcoded safety rule #2.

Position size cannot exceed capital at risk limit.
This is the only entry point for quantity calculation in the system.
"""

from __future__ import annotations


def calculate_quantity(entry: float, sl: float, capital_at_risk_rupees: float) -> int:
    """Return the maximum quantity whose risk does not exceed capital_at_risk_rupees.

    Args:
        entry: Intended entry price.
        sl: Stop-loss price (above or below entry for short/long).
        capital_at_risk_rupees: Maximum rupees to risk on this trade.

    Returns:
        Integer quantity, minimum 1.

    Raises:
        ValueError: If entry == sl (zero SL distance is invalid).
    """
    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        raise ValueError("SL distance must be positive")
    qty = int(capital_at_risk_rupees / sl_distance)
    return max(1, qty)


def intraday_position_size(
    entry: float, sl: float, max_risk_rupees: float, available_funds_rupees: float
) -> int:
    """Smart intraday (MIS) quantity: risk-capped first, then capped by affordable funds.

    Two independent ceilings, take the smaller:
      • risk cap  — never lose more than ``max_risk_rupees`` if SL hits (safety rule #2).
      • affordability — cannot deploy more than ``available_funds_rupees`` (caller folds
        in any MIS leverage, e.g. available_cash * mis_leverage).

    Returns 0 when funds cannot cover even one risk-sized share (caller must skip the
    trade). Never sizes up beyond either ceiling.
    """
    if entry <= 0 or sl <= 0:
        return 0
    risk_qty = calculate_quantity(entry, sl, max_risk_rupees)  # >= 1
    afford_qty = int(max(0.0, available_funds_rupees) / entry)
    return max(0, min(risk_qty, afford_qty))
