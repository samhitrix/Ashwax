"""Black-Scholes option pricing, implied volatility, and Greeks.

Pure, deterministic, dependency-free (normal CDF/PDF via ``math``). No exchange
behaviour is invented here — these are the standard European Black-Scholes
formulas. Indian index/stock options are European-style, so this is appropriate
for NFO. Time is in years; rates and vols are annualized decimals.

Used by the NFO chain provider to back out implied vol from a live option mid
price and to compute per-leg delta for the ``max_delta_exposure`` risk gate.
"""

from __future__ import annotations

import math

# Day-count: NSE options expire on a fixed calendar; 365-day year is the market
# convention for time-to-expiry in days → years.
_DAYS_PER_YEAR = 365.0

# Default risk-free rate (annualized) when caller does not supply one. Indian
# T-bill / MIBOR neighbourhood; the selector passes an explicit rate in practice.
DEFAULT_RISK_FREE_RATE = 0.065


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function (no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard normal probability density."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def days_to_years(days: float) -> float:
    """Convert calendar days-to-expiry to a year fraction."""
    return max(days, 0.0) / _DAYS_PER_YEAR


def _d1_d2(spot: float, strike: float, t: float, r: float, iv: float) -> tuple[float, float]:
    vol_sqrt_t = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    return d1, d2


def bs_price(spot: float, strike: float, t: float, r: float, iv: float, kind: str) -> float:
    """Black-Scholes European option price.

    Args:
        spot: Underlying spot price.
        strike: Option strike.
        t: Time to expiry in years (> 0).
        r: Annualized risk-free rate (decimal).
        iv: Annualized implied volatility (decimal, > 0).
        kind: "CE" (call) or "PE" (put).

    Returns:
        Theoretical premium. At/just past expiry (t<=0) returns intrinsic value.
    """
    kind = kind.upper()
    if t <= 0.0 or iv <= 0.0:
        intrinsic = (spot - strike) if kind == "CE" else (strike - spot)
        return max(intrinsic, 0.0)

    d1, d2 = _d1_d2(spot, strike, t, r, iv)
    disc = math.exp(-r * t)
    if kind == "CE":
        return spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
    return strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(
    price: float,
    spot: float,
    strike: float,
    t: float,
    r: float,
    kind: str,
    *,
    tol: float = 1e-4,
    max_iter: int = 100,
) -> float | None:
    """Back out implied volatility from an observed option price via bisection.

    Bisection (not Newton) for robustness — vega collapses to ~0 for deep
    ITM/OTM options and would make Newton diverge. Returns ``None`` when the
    price is below intrinsic (arbitrage / stale quote) or no root in [1e-4, 5.0].
    """
    kind = kind.upper()
    if price <= 0.0 or t <= 0.0:
        return None

    intrinsic = max((spot - strike) if kind == "CE" else (strike - spot), 0.0)
    if price < intrinsic - tol:
        return None  # price below intrinsic — not a valid quote

    lo, hi = 1e-4, 5.0  # 0.01% .. 500% annualized vol
    price_lo = bs_price(spot, strike, t, r, lo, kind)
    price_hi = bs_price(spot, strike, t, r, hi, kind)
    if not (price_lo <= price <= price_hi):
        return None  # target price not bracketed

    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        diff = bs_price(spot, strike, t, r, mid, kind) - price
        if abs(diff) < tol:
            return mid
        if diff > 0.0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def greeks(
    spot: float, strike: float, t: float, r: float, iv: float, kind: str
) -> dict[str, float]:
    """Return delta, gamma, theta (per-day), vega (per 1% vol), rho (per 1% rate).

    Theta and rho are scaled to the conventional trader units (theta per
    calendar day, vega/rho per 1 percentage-point move).
    """
    kind = kind.upper()
    if t <= 0.0 or iv <= 0.0:
        # At expiry: delta is a step function; other greeks vanish.
        if kind == "CE":  # noqa: SIM108 — clearer than a nested ternary
            delta = 1.0 if spot > strike else 0.0
        else:
            delta = -1.0 if spot < strike else 0.0
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

    d1, d2 = _d1_d2(spot, strike, t, r, iv)
    disc = math.exp(-r * t)
    pdf_d1 = _norm_pdf(d1)

    if kind == "CE":
        delta = _norm_cdf(d1)
        theta = -(spot * pdf_d1 * iv) / (2.0 * math.sqrt(t)) - r * strike * disc * _norm_cdf(d2)
        rho = strike * t * disc * _norm_cdf(d2)
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = -(spot * pdf_d1 * iv) / (2.0 * math.sqrt(t)) + r * strike * disc * _norm_cdf(-d2)
        rho = -strike * t * disc * _norm_cdf(-d2)

    gamma = pdf_d1 / (spot * iv * math.sqrt(t))
    vega = spot * pdf_d1 * math.sqrt(t)

    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta / _DAYS_PER_YEAR,  # per calendar day
        "vega": vega / 100.0,  # per 1% vol move
        "rho": rho / 100.0,  # per 1% rate move
    }
