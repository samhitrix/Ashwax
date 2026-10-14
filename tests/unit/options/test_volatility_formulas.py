"""Pure-function tests for the seller-desk volatility formulas (options_spec.md §2)."""

from __future__ import annotations

import math

from src.options.volatility_formulas import (
    classify_term_structure,
    dealer_gex,
    iv_rank,
    realized_vol,
    skew_25delta,
    vol_premium,
)


def test_iv_rank_normal() -> None:
    # current=0.20, low=0.10, high=0.30 -> (0.20-0.10)/(0.30-0.10)*100 = 50
    assert math.isclose(iv_rank(0.20, [0.10, 0.15, 0.30, 0.25]), 50.0)


def test_iv_rank_empty_history() -> None:
    assert iv_rank(0.20, []) is None


def test_iv_rank_flat_history() -> None:
    # max == min -> undefined, fail-safe None
    assert iv_rank(0.20, [0.15, 0.15, 0.15]) is None


def test_realized_vol_insufficient_closes() -> None:
    assert realized_vol([100.0, 101.0], window=30) is None


def test_realized_vol_zero_variance_is_zero() -> None:
    # constant price series -> zero log returns -> zero realized vol
    closes = [100.0] * 31
    rv = realized_vol(closes, window=30)
    assert rv is not None
    assert math.isclose(rv, 0.0, abs_tol=1e-9)


def test_realized_vol_positive_for_moving_series() -> None:
    closes = [100.0 * (1.001**i) for i in range(31)]  # steady small drift, some variance
    rv = realized_vol(closes, window=30)
    assert rv is not None
    assert rv > 0.0


def test_vol_premium() -> None:
    assert math.isclose(vol_premium(0.18, 0.12), 0.06)
    assert math.isclose(vol_premium(0.10, 0.15), -0.05)


def test_term_structure_flat() -> None:
    assert classify_term_structure({7: 0.180, 90: 0.185}) == "flat"


def test_term_structure_backwardation() -> None:
    # near (7d) = 0.22, far (90d) = 0.17 -> near - far = 0.05 > 0.03
    assert classify_term_structure({7: 0.22, 30: 0.19, 90: 0.17}) == "backwardation"


def test_term_structure_contango() -> None:
    # near (7d) = 0.15, far (90d) = 0.19 -> near < far, normal
    assert classify_term_structure({7: 0.15, 30: 0.17, 90: 0.19}) == "contango"


def test_term_structure_insufficient_buckets() -> None:
    assert classify_term_structure({30: 0.18}) is None
    assert classify_term_structure({}) is None


def test_skew_25delta() -> None:
    assert math.isclose(skew_25delta(call_iv_25d=0.16, put_iv_25d=0.21), 0.05)
    assert math.isclose(skew_25delta(call_iv_25d=0.20, put_iv_25d=0.20), 0.0)


def test_dealer_gex_symmetric_book_is_zero() -> None:
    # equal call/put gamma*OI at every strike -> net zero dealer gamma
    strikes = [(0.002, 1000, 0.002, 1000), (0.0015, 500, 0.0015, 500)]
    gex = dealer_gex(strikes, spot=24000.0, lot_size=25)
    assert math.isclose(gex, 0.0, abs_tol=1e-6)


def test_dealer_gex_call_heavy_is_positive() -> None:
    strikes = [(0.002, 2000, 0.001, 500)]
    gex = dealer_gex(strikes, spot=24000.0, lot_size=25)
    assert gex > 0.0
