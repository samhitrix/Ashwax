"""Gross / charges / net for Reports rows, derived uniformly from what the DB stores.

Stored semantics differ: equity pnl_realized is NET of the costs recorded at close, options
realized_pnl is GROSS. A row with an itemised charges_breakdown is exact; one without (older
rows, or closes written by paths that never computed costs) is ESTIMATED from stored prices at
the current rate card and flagged, never silently mixed in as exact.
"""

from __future__ import annotations

from typing import Any

from src.risk.real_pnl import (
    CostBreakdown,
    ProductType,
    calculate_real_pnl,
    options_trade_charges,
)

_ZERO = {
    "brokerage": 0.0,
    "stt": 0.0,
    "exchange": 0.0,
    "sebi": 0.0,
    "stamp": 0.0,
    "gst": 0.0,
    "dp": 0.0,
}


def _parse(raw: Any) -> dict[str, float] | None:
    import json

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if isinstance(raw, dict) and raw:
        return {k: float(raw.get(k, 0) or 0) for k in _ZERO}
    return None


def _result(gross: float, charges: dict[str, float], estimated: bool) -> dict[str, Any]:
    total = sum(charges.values())
    return {
        "gross": round(gross, 2),
        "charges": {k: round(v, 2) for k, v in charges.items()},
        "chargesTotal": round(total, 2),
        "net": round(gross - total, 2),
        "estimated": estimated,
    }


def equity_costs(
    *,
    pnl_realized: float,
    charges_taxes: float | None,
    charges_breakdown: Any,
    product: str,
    direction: str,
    quantity: int,
    entry_price: float,
    exit_price: float,
) -> dict[str, Any]:
    """quantity = ORIGINAL size (remaining + partials booked)."""
    exact = _parse(charges_breakdown)
    if exact is not None:
        return _result(pnl_realized + sum(exact.values()), exact, False)
    # pnl_realized is net only if a cost was recorded at close; otherwise it is already gross.
    gross = pnl_realized + float(charges_taxes or 0)
    est = calculate_real_pnl(
        "",
        entry_price,
        exit_price,
        quantity,
        ProductType.INTRADAY if product.upper() == "MIS" else ProductType.DELIVERY,
        is_short=direction.lower() in ("short", "sell"),
    ).costs
    return _result(gross, est.as_dict(), True)


def options_costs(
    *, realized_pnl: float, charges_breakdown: Any, legs: list[dict[str, Any]]
) -> dict[str, Any]:
    """legs: _report_leg() dicts (direction, premium, exitPremium, quantity)."""
    exact = _parse(charges_breakdown)
    if exact is not None:
        return _result(realized_pnl, exact, False)
    priced = [
        (str(lg["direction"]), float(lg["premium"]), float(lg["exitPremium"]), int(lg["quantity"]))
        for lg in legs
        if lg.get("exitPremium") is not None
    ]
    est: CostBreakdown = options_trade_charges(priced)
    return _result(realized_pnl, est.as_dict(), True)
