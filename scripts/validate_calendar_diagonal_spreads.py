"""Offline P&L sanity check for calendar_spread/diagonal_spread — NOT a
replacement for the full options backtesting engine (options_spec.md §17,
not built). Explicitly labels its data source (real options_vol_surface_snapshots
history where available, Black-Scholes approximation from historical
spot+IV otherwise) per the design doc's Failure Conditions. Report only —
never touches config/options_settings.json.

Run: python -m scripts.validate_calendar_diagonal_spreads --underlying NIFTY
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from src.options.greeks import bs_price, days_to_years


@dataclass
class PnlEstimate:
    strategy_type: str
    source: str  # "vol_surface_snapshot" | "bs_approximation"
    estimated_debit: float
    estimated_max_profit: float


def estimate_two_expiry_pnl(
    *,
    strategy_type: str,
    spot: float,
    near_iv: float,
    far_iv: float,
    near_dte: int,
    far_dte: int,
    lot_size: int,
    source: str,
    risk_free_rate: float = 0.07,
) -> PnlEstimate:
    """ATM straddle-value approximation: near premium sold vs far premium
    bought, both priced at spot (ATM) via Black-Scholes bs_price().
    calendar_spread uses both CE+PE (matches build_calendar_spread);
    diagonal_spread here is approximated the same way since the offset-strike
    case needs live delta data this standalone estimator doesn't have —
    documented simplification, not claimed accurate for diagonal's offset
    strike."""
    t_near = days_to_years(near_dte)
    t_far = days_to_years(far_dte)
    near_premium = (
        bs_price(spot, spot, t_near, risk_free_rate, near_iv, "CE")
        + bs_price(spot, spot, t_near, risk_free_rate, near_iv, "PE")
    ) * lot_size
    far_premium = (
        bs_price(spot, spot, t_far, risk_free_rate, far_iv, "CE")
        + bs_price(spot, spot, t_far, risk_free_rate, far_iv, "PE")
    ) * lot_size
    debit = far_premium - near_premium
    return PnlEstimate(
        strategy_type=strategy_type,
        source=source,
        estimated_debit=debit,
        estimated_max_profit=debit * 1.5,
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="NIFTY")
    args = parser.parse_args()
    print(
        f"Offline validation for {args.underlying}: run against real "
        f"options_vol_surface_snapshots history once enough has accumulated "
        f"(collection started 2026-08-04) — see design doc Failure Conditions "
        f"for the BS-approximation fallback caveat."
    )


if __name__ == "__main__":
    _main()
