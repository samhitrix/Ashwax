"""Tests for the seller-desk strategy selection engine (options_spec.md §4)."""

from __future__ import annotations

import math

from src.options import seller_settings
from src.options.strategy_selector import (
    StrategySelector,
    calibrated_pop,
    compute_ev,
    estimate_pop,
    sizing_multiplier,
)


def _low_ivr_cfg(**overrides):  # type: ignore[no-untyped-def]
    base = {
        "enabled": True,
        "ivr_threshold": 25.0,
        "candidacy_min_ivr": 3.0,
        "candidacy_size_multiplier": 0.25,
    }
    base.update(overrides)
    return base


def _pop_calibration_cfg(**overrides):  # type: ignore[no-untyped-def]
    base = {
        "enabled": True,
        "anchor_low_delta": 0.25,
        "anchor_low_delta_pop": 0.775,
        "anchor_high_delta": 0.35,
        "anchor_high_delta_pop": 0.716,
    }
    base.update(overrides)
    return base


def test_estimate_pop_single_leg() -> None:
    assert math.isclose(estimate_pop([0.25]), 0.75)


def test_estimate_pop_two_leg_additive() -> None:
    assert math.isclose(estimate_pop([0.25, 0.25]), 0.50)


def test_estimate_pop_floors_at_zero() -> None:
    assert estimate_pop([0.6, 0.6]) == 0.0


def test_compute_ev_positive() -> None:
    ev = compute_ev(pop=0.7, avg_win=1000.0, avg_loss=1500.0)
    assert math.isclose(ev, 0.7 * 1000.0 - 0.3 * 1500.0)


def test_compute_ev_negative_rejects() -> None:
    ev = compute_ev(pop=0.3, avg_win=500.0, avg_loss=2000.0)
    assert ev < 0.0


def test_candidates_iron_condor_needs_range_bound_and_high_ivr() -> None:
    sel = StrategySelector()
    assert "iron_condor" in sel.candidates("HIGH_VOL_RANGE_BOUND", ivr=60.0)
    assert "iron_condor" not in sel.candidates("HIGH_VOL_RANGE_BOUND", ivr=40.0)  # IVR too low
    assert "iron_condor" not in sel.candidates("LOW_VOL_TRENDING", ivr=60.0)  # wrong regime


def test_candidates_bull_put_spread_any_regime_with_high_ivr(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Explicit disabled low_ivr_mode -- independent of the live operator
    # config (see test_candidacy_floor_* below for the enabled behavior),
    # this test verifies the pre-low_ivr_mode candidacy floor still holds.
    monkeypatch.setattr(seller_settings, "low_ivr_mode", lambda settings=None: {"enabled": False})
    sel = StrategySelector()
    assert "bull_put_spread" in sel.candidates("LOW_VOL_TRENDING", ivr=60.0)
    assert "bull_put_spread" in sel.candidates("HIGH_VOL_TRENDING", ivr=60.0)
    assert "bull_put_spread" not in sel.candidates(
        "LOW_VOL_TRENDING", ivr=20.0
    )  # below reduced-size floor


def test_candidates_bull_put_spread_reduced_size_band_now_included() -> None:
    # 2026-08-20: IVR 25-55 used to be excluded outright (min_ivr=55). It's
    # now a candidate at reduced size rather than skipped entirely.
    sel = StrategySelector()
    assert "bull_put_spread" in sel.candidates("LOW_VOL_TRENDING", ivr=40.0)
    assert "bear_call_spread" in sel.candidates("HIGH_VOL_TRENDING", ivr=40.0)


def test_sizing_multiplier_full_size_above_55_ivr() -> None:
    assert sizing_multiplier("bull_put_spread", 60.0) == 1.0
    assert sizing_multiplier("bear_call_spread", 55.0) == 1.0


def test_sizing_multiplier_half_size_in_moderate_band() -> None:
    assert sizing_multiplier("bull_put_spread", 40.0) == 0.5
    assert sizing_multiplier("bear_call_spread", 25.0) == 0.5


def test_sizing_multiplier_unaffected_below_band_or_other_types(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Explicit disabled low_ivr_mode -- see test_candidates_bull_put_spread_
    # any_regime_with_high_ivr's comment; below the reduced-size band with
    # low_ivr_mode off, sizing must stay full (1.0), not discounted.
    monkeypatch.setattr(seller_settings, "low_ivr_mode", lambda settings=None: {"enabled": False})
    assert (
        sizing_multiplier("bull_put_spread", 20.0) == 1.0
    )  # below band — not this lever's concern
    assert sizing_multiplier("iron_condor", 40.0) == 1.0  # unscoped type, unaffected
    assert sizing_multiplier("bull_put_spread", None) == 1.0


def test_candidates_moderate_trend_override_picks_bullish_spread_on_positive_bias() -> None:
    # regime is NOT a clean TRENDING regime, but a real signed drift exists.
    sel = StrategySelector()
    out = sel.candidates("LOW_VOL_RANGE_BOUND", ivr=40.0, directional_bias=0.003)
    assert "bull_call_spread" in out
    assert "bear_put_spread" not in out


def test_candidates_moderate_trend_override_picks_bearish_spread_on_negative_bias() -> None:
    sel = StrategySelector()
    out = sel.candidates("UNDEFINED", ivr=40.0, directional_bias=-0.003)
    assert "bear_put_spread" in out
    assert "bull_call_spread" not in out


def test_candidates_moderate_trend_override_ignores_noise_level_bias() -> None:
    # below the calibrated threshold — not a real enough signal to trade.
    sel = StrategySelector()
    out = sel.candidates("LOW_VOL_RANGE_BOUND", ivr=40.0, directional_bias=0.0005)
    assert "bull_call_spread" not in out
    assert "bear_put_spread" not in out


def test_candidates_moderate_trend_override_still_respects_ivr_band() -> None:
    sel = StrategySelector()
    out = sel.candidates("LOW_VOL_RANGE_BOUND", ivr=10.0, directional_bias=0.003)
    assert "bull_call_spread" not in out  # ivr below bull_call_spread's own 30-55 band


def test_candidates_clean_trending_regime_unaffected_by_override() -> None:
    # In a clean TRENDING regime the normal menu path already adds both
    # directional debit types (existing behavior) — the override must not
    # remove either of them.
    sel = StrategySelector()
    out = sel.candidates("LOW_VOL_TRENDING", ivr=40.0, directional_bias=0.003)
    assert "bull_call_spread" in out
    assert "bear_put_spread" in out


def test_sizing_multiplier_full_size_for_directional_debit_in_trending_regime() -> None:
    assert sizing_multiplier("bull_call_spread", 40.0, regime="LOW_VOL_TRENDING") == 1.0


def test_sizing_multiplier_half_size_for_directional_debit_via_moderate_override() -> None:
    assert sizing_multiplier("bull_call_spread", 40.0, regime="LOW_VOL_RANGE_BOUND") == 0.5
    assert sizing_multiplier("bear_put_spread", 40.0, regime="UNDEFINED") == 0.5


def test_candidates_long_straddle_needs_low_ivr_and_trending() -> None:
    sel = StrategySelector()
    assert "long_straddle" in sel.candidates("LOW_VOL_TRENDING", ivr=20.0)
    assert "long_straddle" not in sel.candidates("LOW_VOL_TRENDING", ivr=50.0)  # too rich


def test_candidates_none_when_ivr_missing() -> None:
    sel = StrategySelector()
    assert sel.candidates("HIGH_VOL_RANGE_BOUND", ivr=None) == []


def test_candidates_backwardation_adds_calendar_spread() -> None:
    sel = StrategySelector()
    out = sel.candidates("HIGH_VOL_RANGE_BOUND", ivr=60.0, term_structure="backwardation")
    assert "calendar_spread" in out


def test_diagonal_spread_requires_backwardation_and_directional_bias() -> None:
    sel = StrategySelector()
    out = sel.candidates(
        "LOW_VOL_RANGE_BOUND",
        20.0,
        term_structure="backwardation",
        directional_bias=0.20,
    )
    assert "diagonal_spread" in out


def test_diagonal_spread_excluded_below_bias_threshold() -> None:
    sel = StrategySelector()
    out = sel.candidates(
        "LOW_VOL_RANGE_BOUND",
        20.0,
        term_structure="backwardation",
        directional_bias=0.05,
    )
    assert "diagonal_spread" not in out


def test_diagonal_spread_excluded_without_backwardation() -> None:
    sel = StrategySelector()
    out = sel.candidates(
        "LOW_VOL_RANGE_BOUND",
        20.0,
        term_structure="contango",
        directional_bias=0.30,
    )
    assert "diagonal_spread" not in out


def test_diagonal_spread_excluded_when_bias_none() -> None:
    sel = StrategySelector()
    out = sel.candidates(
        "LOW_VOL_RANGE_BOUND",
        20.0,
        term_structure="backwardation",
        directional_bias=None,
    )
    assert "diagonal_spread" not in out


def test_select_rejects_negative_ev() -> None:
    sel = StrategySelector()
    rec = sel.select(
        "HIGH_VOL_RANGE_BOUND",
        ivr=60.0,
        term_structure="contango",
        short_deltas_by_strategy={"iron_condor": [0.5, 0.5]},  # POP=0 -> EV always <=0
        avg_win_by_strategy={"iron_condor": 1000.0},
        avg_loss_by_strategy={"iron_condor": 3000.0},
        selling_blocked=False,
    )
    assert rec is None


def test_select_picks_best_positive_ev_candidate() -> None:
    sel = StrategySelector()
    rec = sel.select(
        "HIGH_VOL_RANGE_BOUND",
        ivr=60.0,
        term_structure="contango",
        short_deltas_by_strategy={
            "iron_condor": [0.20, 0.20],
            "bull_put_spread": [0.20],
        },
        avg_win_by_strategy={"iron_condor": 1000.0, "bull_put_spread": 800.0},
        avg_loss_by_strategy={"iron_condor": 1500.0, "bull_put_spread": 1200.0},
        selling_blocked=False,
    )
    assert rec is not None
    assert rec.direction == "credit"
    assert rec.expected_value > 0.0


def test_select_blocks_credit_strategies_when_selling_blocked() -> None:
    # ivr=55 in a trending regime: bull_put_spread (credit) and bull_call_spread
    # (debit) are BOTH otherwise-valid candidates at this exact band overlap.
    sel = StrategySelector()
    rec = sel.select(
        "LOW_VOL_TRENDING",
        ivr=55.0,
        term_structure="contango",
        short_deltas_by_strategy={
            "bull_put_spread": [0.10],  # would be very high-EV credit if allowed
            "bull_call_spread": [0.50],
        },
        avg_win_by_strategy={"bull_put_spread": 900.0, "bull_call_spread": 2000.0},
        avg_loss_by_strategy={"bull_put_spread": 100.0, "bull_call_spread": 800.0},
        selling_blocked=True,
    )
    assert rec is not None
    assert rec.strategy_type != "bull_put_spread"
    assert rec.direction == "debit"


# ── low_ivr_mode candidacy floor (2026-09-07) ───────────────────────────────
#
# Found live: on a real day (BANKNIFTY regime correctly classified
# LOW_VOL_RANGE_BOUND, ivr 4.1-6.8%), candidates=[] every cycle because
# _menu()'s own floor (reduced_size_min_ivr, 25 by default) gated candidacy
# for bull_put_spread/bear_call_spread BEFORE low_ivr_mode's strike_deltas/
# dte_range in structure_builder.py ever got consulted -- that tighter
# profile was unreachable dead code without a candidacy-level fix too.


def test_candidacy_floor_disabled_low_ivr_still_excluded(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Explicit disabled config, not the live config's current value (which
    # is operator-controlled and expected to change -- see
    # config/options_settings.json low_ivr_mode.enabled) -- this test's job
    # is to verify the disabled CODE PATH, independent of today's setting.
    monkeypatch.setattr(seller_settings, "low_ivr_mode", lambda settings=None: {"enabled": False})
    sel = StrategySelector()
    assert sel.candidates("LOW_VOL_RANGE_BOUND", ivr=5.0) == []


def test_candidacy_floor_enabled_admits_deep_low_ivr(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(seller_settings, "low_ivr_mode", lambda settings=None: _low_ivr_cfg())
    sel = StrategySelector()
    # exact IVR values logged live for BANKNIFTY on 2026-09-07
    for ivr in (4.1, 5.0, 6.8):
        out = sel.candidates("LOW_VOL_RANGE_BOUND", ivr=ivr)
        assert "bull_put_spread" in out, f"ivr={ivr}"
        assert "bear_call_spread" in out, f"ivr={ivr}"


def test_candidacy_floor_enabled_still_excludes_below_its_own_floor(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(seller_settings, "low_ivr_mode", lambda settings=None: _low_ivr_cfg())
    sel = StrategySelector()
    out = sel.candidates("LOW_VOL_RANGE_BOUND", ivr=2.0)  # below candidacy_min_ivr=3.0
    assert "bull_put_spread" not in out
    assert "bear_call_spread" not in out


def test_candidacy_floor_enabled_iron_condor_unaffected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(seller_settings, "low_ivr_mode", lambda settings=None: _low_ivr_cfg())
    sel = StrategySelector()
    # iron_condor's own floor (iv_thresholds.iron_condor_min_ivr, 55) is a
    # completely separate config value -- low_ivr_mode must not touch it.
    out = sel.candidates("LOW_VOL_RANGE_BOUND", ivr=5.0)
    assert "iron_condor" not in out


def test_sizing_multiplier_deep_low_ivr_band_when_enabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(seller_settings, "low_ivr_mode", lambda settings=None: _low_ivr_cfg())
    assert sizing_multiplier("bull_put_spread", 5.0) == 0.25
    assert sizing_multiplier("bear_call_spread", 6.8) == 0.25


def test_sizing_multiplier_moderate_band_unaffected_by_low_ivr_mode(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Regression check: the pre-existing 25-55 reduced-size band (0.5x) must
    # be unchanged by low_ivr_mode being enabled -- they're independent tiers.
    monkeypatch.setattr(seller_settings, "low_ivr_mode", lambda settings=None: _low_ivr_cfg())
    assert sizing_multiplier("bull_put_spread", 40.0) == 0.5
    assert sizing_multiplier("bear_call_spread", 25.0) == 0.5


def test_sizing_multiplier_still_1x_below_candidacy_floor_when_enabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Below candidacy_min_ivr, the strategy isn't even a candidate (see
    # test_candidacy_floor_enabled_still_excludes_below_its_own_floor) so
    # sizing_multiplier is never consulted for it in the real pipeline --
    # but called directly it must still report neutral, not fabricate a
    # discount for an IVR level nothing will ever trade at.
    monkeypatch.setattr(seller_settings, "low_ivr_mode", lambda settings=None: _low_ivr_cfg())
    assert sizing_multiplier("bull_put_spread", 2.0) == 1.0


def test_candidacy_floor_uses_custom_config_value(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        seller_settings,
        "low_ivr_mode",
        lambda settings=None: _low_ivr_cfg(candidacy_min_ivr=15.0, candidacy_size_multiplier=0.1),
    )
    sel = StrategySelector()
    assert "bull_put_spread" not in sel.candidates("LOW_VOL_RANGE_BOUND", ivr=10.0)
    assert "bull_put_spread" in sel.candidates("LOW_VOL_RANGE_BOUND", ivr=16.0)
    assert sizing_multiplier("bull_put_spread", 16.0) == 0.1


# ── calibrated_pop (2026-09-08, backtested EV-gate recalibration) ───────────


def test_calibrated_pop_disabled_falls_back_to_crude_estimate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        seller_settings, "pop_calibration", lambda settings=None: {"enabled": False}
    )
    assert calibrated_pop("bull_put_spread", [0.25]) == estimate_pop([0.25])


def test_calibrated_pop_uncovered_strategy_falls_back_to_crude_estimate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        seller_settings, "pop_calibration", lambda settings=None: _pop_calibration_cfg()
    )
    # iron_condor is not one of the two backtested types -- must stay on the
    # original crude proxy even with pop_calibration enabled.
    assert calibrated_pop("iron_condor", [0.25]) == estimate_pop([0.25])


def test_calibrated_pop_at_low_anchor_matches_backtested_default_delta(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        seller_settings, "pop_calibration", lambda settings=None: _pop_calibration_cfg()
    )
    pop = calibrated_pop("bull_put_spread", [0.25])
    assert math.isclose(pop, 0.775)
    # this is the whole point of the fix: the default delta must now clear
    # the EV gate's pop>0.75 requirement (avg_win=0.5x, avg_loss=1.5x premium).
    assert compute_ev(pop, 0.5, 1.5) > 0.0


def test_calibrated_pop_at_high_anchor_matches_backtested_tight_delta(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        seller_settings, "pop_calibration", lambda settings=None: _pop_calibration_cfg()
    )
    pop = calibrated_pop("bear_call_spread", [0.35])
    assert math.isclose(pop, 0.716)
    # low_ivr_mode's tight delta genuinely measured a lower real win rate --
    # it must still fail the EV gate even after recalibration, which is why
    # structure_builder no longer applies it (override_deltas:false).
    assert compute_ev(pop, 0.5, 1.5) < 0.0


def test_calibrated_pop_extrapolates_and_clamps_to_valid_range(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        seller_settings, "pop_calibration", lambda settings=None: _pop_calibration_cfg()
    )
    # far outside the two measured anchors -- must clamp into [0, 1], never
    # raise or return a nonsensical probability.
    assert 0.0 <= calibrated_pop("bull_put_spread", [0.9]) <= 1.0
    assert 0.0 <= calibrated_pop("bull_put_spread", [0.0]) <= 1.0


def test_calibrated_pop_malformed_config_falls_back_to_crude_estimate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(seller_settings, "pop_calibration", lambda settings=None: {"enabled": True})
    assert calibrated_pop("bull_put_spread", [0.25]) == estimate_pop([0.25])


# ── debit-vertical probability of profit (2026-09-28) ──────────────────────────────
# The EV gate used pop = 1 - |short delta| for EVERY structure. For a debit spread that
# is the wrong event: you win only if price ends beyond BREAKEVEN. A BANKNIFTY
# bear_put_spread (long 54900 PE @898, short 53300 PE @380, spot 54567, 29d, IV 15.4%)
# was logged pop=0.746 / EV=+10,586 when the real probability is ~0.43 (EV slightly < 0).


def _debit_legs(
    kind: str, long_strike: float, short_strike: float, long_px: float, short_px: float
):  # type: ignore[no-untyped-def]
    from src.options.structure_selector import OptionLeg

    def leg(strike: float, direction: str, px: float) -> OptionLeg:
        return OptionLeg(
            tradingsymbol=f"NFO:X{int(strike)}{kind}",
            instrument_type=kind,
            strike=strike,
            direction=direction,
            lots=1,
            lot_size=30,
            premium=px,
            delta=0.0,
        )

    return [leg(long_strike, "BUY", long_px)], [leg(short_strike, "SELL", short_px)]


def test_debit_vertical_pop_bear_put_uses_breakeven_not_short_delta() -> None:
    from src.options.strategy_selector import debit_vertical_pop

    prot, inc = _debit_legs("PE", 54900.0, 53300.0, 898.175, 379.75)
    pop = debit_vertical_pop("bear_put_spread", prot, inc, spot=54567.2, iv=0.154, dte=29)
    assert pop is not None and 0.40 < pop < 0.46  # NOT the 0.746 the short delta implied


def test_debit_vertical_pop_bull_call_is_the_mirror_image() -> None:
    from src.options.strategy_selector import debit_vertical_pop

    prot, inc = _debit_legs("CE", 54000.0, 55600.0, 900.0, 380.0)
    pop = debit_vertical_pop("bull_call_spread", prot, inc, spot=54567.2, iv=0.154, dte=29)
    # breakeven 54520 is just below spot -> a bit better than a coin flip
    assert pop is not None and 0.48 < pop < 0.56


def test_debit_vertical_pop_falls_as_the_debit_grows() -> None:
    from src.options.strategy_selector import debit_vertical_pop

    cheap = debit_vertical_pop(
        "bull_call_spread",
        *_debit_legs("CE", 54000.0, 55600.0, 700.0, 380.0),
        spot=54567.2,
        iv=0.154,
        dte=29,
    )
    dear = debit_vertical_pop(
        "bull_call_spread",
        *_debit_legs("CE", 54000.0, 55600.0, 1000.0, 380.0),
        spot=54567.2,
        iv=0.154,
        dte=29,
    )
    assert cheap is not None and dear is not None and cheap > dear


def test_debit_vertical_pop_is_none_for_other_structures() -> None:
    from src.options.strategy_selector import debit_vertical_pop

    prot, inc = _debit_legs("PE", 54900.0, 53300.0, 898.0, 380.0)
    assert debit_vertical_pop("bull_put_spread", prot, inc, spot=54567.2, iv=0.154, dte=29) is None


def test_debit_vertical_pop_fails_safe_to_zero_without_usable_iv_or_time() -> None:
    from src.options.strategy_selector import debit_vertical_pop

    prot, inc = _debit_legs("PE", 54900.0, 53300.0, 898.0, 380.0)
    assert debit_vertical_pop("bear_put_spread", prot, inc, spot=54567.2, iv=None, dte=29) == 0.0
    assert debit_vertical_pop("bear_put_spread", prot, inc, spot=54567.2, iv=0.154, dte=0) == 0.0
