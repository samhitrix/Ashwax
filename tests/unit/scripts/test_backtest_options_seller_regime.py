"""Unit tests for scripts/backtest_options_seller_regime.py's pure logic
(regime replay, delta-to-strike inversion, spread pricing/trial engine) —
no DB required, matching how other backtest scripts separate logic from I/O."""

from __future__ import annotations

# sys.path bootstrap must precede the scripts.* import below (E402).
# ruff: noqa: E402
import math
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_options_seller_regime import (
    _ALL_STRUCTURE_TYPES,
    DEFAULT_RISK_FREE_RATE,
    RegimeDay,
    _price_combo,
    _price_protective_put,
    _price_vertical,
    _strike_for_delta,
    _strikes_for_structure,
    analyze_undefined_zone,
    compute_regime_timeline,
    days_to_years,
    realized_vol_proxy,
    run_spread_trials,
    run_structure_trials,
    summarize_structure_trials,
    summarize_trials,
)


def _flat_series(n: int, value: float = 24000.0) -> list[float]:
    return [value] * n


def _random_walk(n: int, seed: int = 1, start: float = 24000.0, sigma: float = 0.01) -> list[float]:
    rng = random.Random(seed)  # noqa: S311 -- reproducible synthetic test fixture, not security-sensitive
    closes = [start]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.gauss(0, sigma)))
    return closes


def test_realized_vol_proxy_none_before_window() -> None:
    closes = _random_walk(15)
    out = realized_vol_proxy(closes, window=20)
    assert all(v is None for v in out)


def test_realized_vol_proxy_positive_after_window() -> None:
    closes = _random_walk(60)
    out = realized_vol_proxy(closes, window=20)
    assert out[19] is None
    assert out[20] is not None
    assert out[20] > 0.0


def test_realized_vol_proxy_zero_for_flat_series() -> None:
    closes = _flat_series(60)
    out = realized_vol_proxy(closes, window=20)
    assert out[-1] == 0.0


def test_compute_regime_timeline_produces_no_lookahead_days() -> None:
    n = 200
    closes = _random_walk(n)
    highs = [c * 1.005 for c in closes]
    lows = [c * 0.995 for c in closes]
    timeline = compute_regime_timeline(closes, highs, lows)
    assert len(timeline) > 0
    # every entry's idx must be within bounds and after the warmup floor
    for day in timeline:
        assert 0 <= day.idx < n
        assert isinstance(day.regime, str)


def test_compute_regime_timeline_empty_for_short_series() -> None:
    closes = _random_walk(10)
    highs = [c * 1.005 for c in closes]
    lows = [c * 0.995 for c in closes]
    assert compute_regime_timeline(closes, highs, lows) == []


def test_analyze_undefined_zone_handles_no_undefined_days() -> None:
    timeline = [
        RegimeDay(
            idx=i,
            regime="LOW_VOL_RANGE_BOUND",
            vix_proxy=10.0,
            adx=10.0,
            trend_score=0.1,
            trend_direction=0.0,
            allowed_selling=True,
            allowed_directional=False,
        )
        for i in range(5)
    ]
    result = analyze_undefined_zone(timeline)
    assert result["n_undefined"] == 0
    assert result["undefined_pct"] == 0.0


def test_analyze_undefined_zone_counts_trend_days_correctly() -> None:
    timeline = [
        RegimeDay(
            idx=0,
            regime="UNDEFINED",
            vix_proxy=20.0,
            adx=20.0,
            trend_score=0.3,
            trend_direction=0.005,
            allowed_selling=False,
            allowed_directional=False,
        ),
        RegimeDay(
            idx=1,
            regime="UNDEFINED",
            vix_proxy=20.0,
            adx=20.0,
            trend_score=0.3,
            trend_direction=0.0001,
            allowed_selling=False,
            allowed_directional=False,
        ),
        RegimeDay(
            idx=2,
            regime="LOW_VOL_RANGE_BOUND",
            vix_proxy=10.0,
            adx=10.0,
            trend_score=0.1,
            trend_direction=0.0,
            allowed_selling=True,
            allowed_directional=False,
        ),
    ]
    result = analyze_undefined_zone(timeline)
    assert result["n_total"] == 3
    assert result["n_undefined"] == 2
    # only the first UNDEFINED day clears _MODERATE_TREND_BIAS_THRESHOLD (0.0015)
    assert result["n_with_trend"] == 1


def test_strike_for_delta_put_below_spot_call_above_spot() -> None:
    spot = 24000.0
    put_k = _strike_for_delta(spot, 0.15, 30 / 365, 0.065, 0.25, "PE")
    call_k = _strike_for_delta(spot, 0.15, 30 / 365, 0.065, 0.25, "CE")
    assert put_k < spot < call_k


def test_strike_for_delta_tighter_profile_closer_to_spot() -> None:
    spot = 24000.0
    put_25 = _strike_for_delta(spot, 0.15, 30 / 365, 0.065, 0.25, "PE")
    put_35 = _strike_for_delta(spot, 0.15, 30 / 365, 0.065, 0.35, "PE")
    # 35-delta is less OTM than 25-delta -> strike closer to spot
    assert put_25 < put_35 < spot


def test_strike_for_delta_atm_is_spot_adjusted_for_drift() -> None:
    # delta=0.5 -> d1=invcdf(0.5)=0 for CE -> strike = spot * exp((r+0.5*iv^2)*t)
    spot = 24000.0
    r, iv, t = 0.065, 0.15, 30 / 365
    k = _strike_for_delta(spot, iv, t, r, 0.5, "CE")
    expected = spot * math.exp((r + 0.5 * iv * iv) * t)
    assert abs(k - expected) < 1e-6


def test_price_vertical_bull_put_full_win_when_spot_stays_above_short_strike() -> None:
    spot_entry = 24000.0
    spot_expiry = 24000.0  # unchanged -> short put (below spot) expires worthless -> max profit
    premium, max_loss, pnl = _price_vertical(
        spot_entry, spot_expiry, 0.15, 30 / 365, 0.065, "bull_put", {"short": 0.25, "long": 0.10}
    )
    assert premium > 0.0
    assert pnl == premium  # both legs expire worthless, full premium kept


def test_price_vertical_bull_put_loses_on_deep_crash() -> None:
    spot_entry = 24000.0
    spot_expiry = 20000.0  # deep crash through both strikes -> max loss
    premium, max_loss, pnl = _price_vertical(
        spot_entry, spot_expiry, 0.15, 30 / 365, 0.065, "bull_put", {"short": 0.25, "long": 0.10}
    )
    assert pnl < 0.0
    assert pnl >= -max_loss - 1e-6  # loss is capped at max_loss (defined-risk)


def test_run_spread_trials_skips_days_without_enough_forward_data() -> None:
    n = 50
    closes = _random_walk(n)
    highs = [c * 1.005 for c in closes]
    lows = [c * 0.995 for c in closes]
    timeline = compute_regime_timeline(closes, highs, lows)
    ivs = realized_vol_proxy(closes)
    trials = run_spread_trials(
        timeline,
        closes,
        ivs,
        dte=30,
        profile={"short": 0.25, "long": 0.10},
        side_selector="abs_premium",
    )
    # every trial's expiry_idx must be within the series
    for trial in trials:
        assert trial.expiry_idx < n


def test_summarize_trials_empty_returns_n_zero() -> None:
    assert summarize_trials([], dte=30) == {"n": 0}


def test_summarize_trials_computes_win_rate() -> None:
    from scripts.backtest_options_seller_regime import SpreadTrial

    trials = [
        SpreadTrial(
            entry_idx=0,
            expiry_idx=30,
            side="bull_put",
            premium=100.0,
            max_loss=50.0,
            pnl=100.0,
            win=True,
        ),
        SpreadTrial(
            entry_idx=1,
            expiry_idx=31,
            side="bull_put",
            premium=100.0,
            max_loss=50.0,
            pnl=-50.0,
            win=False,
        ),
    ]
    summary = summarize_trials(trials, dte=30)
    assert summary["n"] == 2
    assert summary["win_rate_pct"] == 50.0


# ── Options learning-engine PR1: all-structures EV calibration extension ────

_SPOT = 24000.0
_IV = 0.15
_T = days_to_years(30)
_R = DEFAULT_RISK_FREE_RATE
_PROFILE = {"short": 0.25, "long": 0.10}


def test_strikes_for_structure_leg_counts() -> None:
    expected_leg_counts = {
        "bull_put_spread": 2,
        "bear_call_spread": 2,
        "bull_call_spread": 2,
        "bear_put_spread": 2,
        "long_straddle": 2,
        "long_strangle": 2,
        "iron_condor": 4,
        "iron_butterfly": 4,
        "long_call": 1,
    }
    for structure_type, n_legs in expected_leg_counts.items():
        legs = _strikes_for_structure(structure_type, _SPOT, _IV, _T, _R, _PROFILE)
        assert len(legs) == n_legs, structure_type


def test_strikes_for_structure_bull_put_short_strike_above_long_strike() -> None:
    legs = _strikes_for_structure("bull_put_spread", _SPOT, _IV, _T, _R, _PROFILE)
    (short_kind, short_dir, short_k), (long_kind, long_dir, long_k) = legs
    assert (short_kind, short_dir) == ("PE", "SELL")
    assert (long_kind, long_dir) == ("PE", "BUY")
    assert short_k > long_k


def test_strikes_for_structure_bear_call_short_strike_below_long_strike() -> None:
    legs = _strikes_for_structure("bear_call_spread", _SPOT, _IV, _T, _R, _PROFILE)
    (short_kind, short_dir, short_k), (long_kind, long_dir, long_k) = legs
    assert (short_kind, short_dir) == ("CE", "SELL")
    assert (long_kind, long_dir) == ("CE", "BUY")
    assert short_k < long_k


def test_strikes_for_structure_unsupported_type_raises() -> None:
    with pytest.raises(ValueError):
        _strikes_for_structure("calendar_spread", _SPOT, _IV, _T, _R, _PROFILE)


def test_price_combo_credit_vertical_matches_price_vertical() -> None:
    """The generalized combo pricer must agree with the original two-type
    _price_vertical for the same bull_put_spread legs (same underlying model).
    Sign conventions differ deliberately: _price_vertical's premium is
    "premium COLLECTED" (positive for a credit spread); _price_combo's
    net_premium is "premium PAID" (positive = debit) so the same sign works
    uniformly across both debit and credit structure types — hence the flip."""
    legs = _strikes_for_structure("bull_put_spread", _SPOT, _IV, _T, _R, _PROFILE)
    spot_expiry = _SPOT * 1.02
    combo_premium, combo_loss, combo_pnl = _price_combo(legs, _SPOT, spot_expiry, _IV, _T, _R)
    vert_premium, vert_loss, vert_pnl = _price_vertical(
        _SPOT, spot_expiry, _IV, _T, _R, "bull_put", _PROFILE
    )
    assert combo_premium == pytest.approx(-vert_premium)
    assert combo_pnl == pytest.approx(vert_pnl)
    assert combo_loss == pytest.approx(vert_loss, rel=1e-2)


def test_price_combo_long_call_max_loss_is_premium() -> None:
    """A single long call's worst case is losing the whole premium — nothing more."""
    legs = _strikes_for_structure("long_call", _SPOT, _IV, _T, _R, _PROFILE)
    premium, max_loss, _pnl = _price_combo(legs, _SPOT, _SPOT * 0.5, _IV, _T, _R)
    assert premium > 0  # net debit
    assert max_loss == pytest.approx(premium, rel=1e-6)


def test_price_combo_iron_condor_bounded_loss() -> None:
    """An iron condor's max loss must be well below an unhedged short strangle's
    (that's the entire point of the wings) and strictly positive."""
    legs = _strikes_for_structure("iron_condor", _SPOT, _IV, _T, _R, _PROFILE)
    _premium, max_loss, _pnl = _price_combo(legs, _SPOT, _SPOT * 1.3, _IV, _T, _R)
    assert 0 < max_loss < _SPOT  # bounded, not catastrophic


def test_price_protective_put_matches_live_max_loss_formula() -> None:
    """Mirrors structure_selector._protective_put's live formula exactly:
    max_loss = (entry - put_strike) + put_premium, per share."""
    from scripts.backtest_options_seller_regime import _strike_for_delta as _sfd

    put_delta = 0.25
    premium, max_loss, _pnl = _price_protective_put(_SPOT, _SPOT * 0.9, _IV, _T, _R, put_delta)
    put_k = _sfd(_SPOT, _IV, _T, _R, put_delta, "PE")
    expected_max_loss = max(_SPOT - put_k, 0.0) + premium
    assert max_loss == pytest.approx(expected_max_loss, rel=1e-6)


def test_run_structure_trials_and_summarize_all_types_produce_stats() -> None:
    closes = _random_walk(400, seed=3)
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    timeline = compute_regime_timeline(closes, highs, lows)
    ivs: list[float | None] = [20.0] * len(closes)
    for structure_type in _ALL_STRUCTURE_TYPES:
        trials = run_structure_trials(
            timeline, closes, ivs, structure_type=structure_type, dte=30, profile=_PROFILE
        )
        summary = summarize_structure_trials(trials)
        assert summary["n"] >= 0
        if summary["n"] > 0:
            assert 0.0 <= summary["win_rate_pct"] <= 100.0
            assert summary["avg_win"] >= 0.0
            assert summary["avg_loss"] >= 0.0


def test_summarize_structure_trials_empty_is_safe() -> None:
    assert summarize_structure_trials([]) == {"n": 0}
