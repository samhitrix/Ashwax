"""Tests for src/options/greeks.py — Black-Scholes pricing, IV, Greeks.

Values checked against standard textbook results / put-call parity rather than a
reference library, so the module stays dependency-free.
"""

from __future__ import annotations

import math

from src.options.greeks import bs_price, days_to_years, greeks, implied_vol

# A round reference contract: spot=100, strike=100 (ATM), 30 days, r=6.5%, iv=20%.
_SPOT = 100.0
_STRIKE = 100.0
_T = days_to_years(30)
_R = 0.065
_IV = 0.20


def test_atm_call_delta_near_half() -> None:
    g = greeks(_SPOT, _STRIKE, _T, _R, _IV, "CE")
    # ATM call delta is slightly above 0.5 (drift from r and variance).
    assert 0.50 <= g["delta"] <= 0.60


def test_atm_put_delta_near_minus_half() -> None:
    g = greeks(_SPOT, _STRIKE, _T, _R, _IV, "PE")
    assert -0.55 <= g["delta"] <= -0.40


def test_call_put_delta_difference_is_one() -> None:
    """delta_call - delta_put == 1 for the same strike (parity of deltas)."""
    c = greeks(_SPOT, _STRIKE, _T, _R, _IV, "CE")["delta"]
    p = greeks(_SPOT, _STRIKE, _T, _R, _IV, "PE")["delta"]
    assert abs((c - p) - 1.0) < 1e-9


def test_put_call_parity() -> None:
    """C - P == S - K*exp(-rT)."""
    c = bs_price(_SPOT, _STRIKE, _T, _R, _IV, "CE")
    p = bs_price(_SPOT, _STRIKE, _T, _R, _IV, "PE")
    rhs = _SPOT - _STRIKE * math.exp(-_R * _T)
    assert abs((c - p) - rhs) < 1e-6


def test_implied_vol_round_trips_price() -> None:
    price = bs_price(_SPOT, _STRIKE, _T, _R, _IV, "CE")
    iv = implied_vol(price, _SPOT, _STRIKE, _T, _R, "CE")
    assert iv is not None
    assert abs(iv - _IV) < 1e-3


def test_implied_vol_below_intrinsic_returns_none() -> None:
    # Deep ITM call worth less than intrinsic → invalid.
    iv = implied_vol(0.5, spot=120.0, strike=100.0, t=_T, r=_R, kind="CE")
    assert iv is None


def test_implied_vol_zero_price_returns_none() -> None:
    assert implied_vol(0.0, _SPOT, _STRIKE, _T, _R, "CE") is None


def test_price_at_expiry_is_intrinsic() -> None:
    assert bs_price(120.0, 100.0, 0.0, _R, _IV, "CE") == 20.0
    assert bs_price(90.0, 100.0, 0.0, _R, _IV, "CE") == 0.0
    assert bs_price(90.0, 100.0, 0.0, _R, _IV, "PE") == 10.0


def test_greeks_at_expiry_step_delta() -> None:
    assert greeks(120.0, 100.0, 0.0, _R, _IV, "CE")["delta"] == 1.0
    assert greeks(90.0, 100.0, 0.0, _R, _IV, "CE")["delta"] == 0.0
    assert greeks(90.0, 100.0, 0.0, _R, _IV, "PE")["delta"] == -1.0


def test_gamma_vega_positive_for_long_options() -> None:
    g = greeks(_SPOT, _STRIKE, _T, _R, _IV, "CE")
    assert g["gamma"] > 0.0
    assert g["vega"] > 0.0


def test_theta_negative_for_long_options() -> None:
    """Long options decay — theta per day is negative."""
    assert greeks(_SPOT, _STRIKE, _T, _R, _IV, "CE")["theta"] < 0.0
    assert greeks(_SPOT, _STRIKE, _T, _R, _IV, "PE")["theta"] < 0.0


def test_deep_otm_call_delta_near_zero() -> None:
    g = greeks(100.0, 200.0, _T, _R, _IV, "CE")
    assert g["delta"] < 0.05


def test_deep_itm_call_delta_near_one() -> None:
    g = greeks(200.0, 100.0, _T, _R, _IV, "CE")
    assert g["delta"] > 0.95
