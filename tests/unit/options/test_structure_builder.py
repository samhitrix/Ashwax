"""Tests for the seller-desk structure builders — the missing link between
strategy_selector (WHAT to trade) and options_leg_executor (placing it)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.brokers.base import OptionQuote
from src.options import seller_settings
from src.options.nfo_chain import ChainStrike, OptionChain
from src.options.structure_builder import (
    build_bear_call_spread,
    build_bull_put_spread,
    build_calendar_spread,
    build_diagonal_spread,
    build_iron_butterfly,
    build_iron_condor,
    build_long_straddle,
    build_long_strangle,
    build_structure,
    build_two_expiry_structure,
    implemented_strategy_types,
)

_IST = ZoneInfo("Asia/Kolkata")


def _quote(mid: float) -> OptionQuote:
    return OptionQuote("SYM", ltp=mid, bid=mid - 0.5, ask=mid + 0.5, oi=5000, volume=2000)


def _rich_chain(days: int = 30) -> OptionChain:
    """A chain wide enough (11 strikes) to have real candidates near every
    target delta (0.10, 0.25, 0.50)."""
    expiry = datetime.now(_IST) + timedelta(days=days)
    # Strikes symmetric around spot=24000, deltas roughly tracking distance.
    strikes_data = [
        (23500.0, 0.90, -0.10),
        (23600.0, 0.82, -0.15),
        (23700.0, 0.72, -0.22),
        (23800.0, 0.62, -0.28),
        (23900.0, 0.55, -0.45),
        (24000.0, 0.50, -0.50),
        (24100.0, 0.45, -0.55),
        (24200.0, 0.28, -0.62),
        (24300.0, 0.22, -0.72),
        (24400.0, 0.15, -0.82),
        (24500.0, 0.10, -0.90),
    ]
    rows = [
        ChainStrike(
            strike=strike,
            call=_quote(50.0),
            put=_quote(50.0),
            call_iv=0.18,
            put_iv=0.19,
            call_delta=call_d,
            put_delta=put_d,
            call_symbol=f"NIFTY{int(strike)}CE",
            put_symbol=f"NIFTY{int(strike)}PE",
        )
        for strike, call_d, put_d in strikes_data
    ]
    return OptionChain(
        underlying="NIFTY",
        spot=24000.0,
        expiry=expiry,
        days_to_expiry=days,
        lot_size=25,
        strikes=rows,
    )


def test_build_bull_put_spread_short_near_25delta_long_near_10delta() -> None:
    result = build_bull_put_spread(_rich_chain(), lots=1)
    assert result is not None
    protection, income = result
    assert len(protection) == 1 and len(income) == 1
    assert protection[0].direction == "BUY"
    assert income[0].direction == "SELL"
    # short leg's |delta| should be nearer 0.25 than the long leg's nearer 0.10
    assert abs(abs(income[0].delta) - 0.25) < abs(abs(protection[0].delta) - 0.25)


def test_build_bear_call_spread_directions() -> None:
    result = build_bear_call_spread(_rich_chain(), lots=1)
    assert result is not None
    protection, income = result
    assert protection[0].instrument_type == "CE"
    assert income[0].instrument_type == "CE"
    assert protection[0].direction == "BUY"
    assert income[0].direction == "SELL"


def test_build_iron_condor_has_4_legs_total() -> None:
    result = build_iron_condor(_rich_chain(), lots=1)
    assert result is not None
    protection, income = result
    assert len(protection) == 2  # long put wing + long call wing
    assert len(income) == 2  # short put + short call
    assert all(leg.direction == "BUY" for leg in protection)
    assert all(leg.direction == "SELL" for leg in income)


def test_build_iron_butterfly_income_legs_are_atm() -> None:
    result = build_iron_butterfly(_rich_chain(), lots=1)
    assert result is not None
    protection, income = result
    assert len(income) == 2
    assert all(leg.strike == 24000.0 for leg in income)  # ATM
    assert len(protection) == 2
    assert all(leg.strike != 24000.0 for leg in protection)  # wings, not ATM


def test_build_long_straddle_no_income_legs() -> None:
    result = build_long_straddle(_rich_chain(), lots=1)
    assert result is not None
    protection, income = result
    assert len(protection) == 2
    assert income == []
    assert all(leg.direction == "BUY" for leg in protection)
    assert all(leg.strike == 24000.0 for leg in protection)


def test_build_long_strangle_both_otm_no_income() -> None:
    result = build_long_strangle(_rich_chain(), lots=1)
    assert result is not None
    protection, income = result
    assert len(protection) == 2
    assert income == []
    call_leg = next(leg for leg in protection if leg.instrument_type == "CE")
    put_leg = next(leg for leg in protection if leg.instrument_type == "PE")
    assert call_leg.strike > 24000.0  # OTM call above spot
    assert put_leg.strike < 24000.0  # OTM put below spot


def test_build_structure_dispatches_by_type() -> None:
    chain = _rich_chain()
    result = build_structure("bull_put_spread", chain, lots=1)
    assert result is not None


def test_build_structure_unknown_type_returns_none() -> None:
    chain = _rich_chain()
    assert build_structure("collar", chain, lots=1) is None  # not yet implemented, honest None


def test_build_bull_put_spread_none_when_chain_too_thin() -> None:
    expiry = datetime.now(_IST) + timedelta(days=30)
    thin_chain = OptionChain(
        underlying="NIFTY",
        spot=24000.0,
        expiry=expiry,
        days_to_expiry=30,
        lot_size=25,
        strikes=[ChainStrike(strike=24000.0, put=_quote(50.0), put_delta=-0.50)],
    )
    assert build_bull_put_spread(thin_chain, lots=1) is None


def test_build_calendar_spread_sells_near_buys_far_atm() -> None:
    near_chain = _rich_chain(days=7)
    far_chain = _rich_chain(days=35)
    result = build_calendar_spread(near_chain, far_chain, lots=1)
    assert result is not None
    protection, income = result
    assert {leg.instrument_type for leg in protection} == {"CE", "PE"}
    assert all(leg.direction == "BUY" for leg in protection)
    assert {leg.instrument_type for leg in income} == {"CE", "PE"}
    assert all(leg.direction == "SELL" for leg in income)
    assert all(leg.strike == 24000.0 for leg in protection + income)  # ATM both sides


def test_build_diagonal_spread_bullish_uses_call_side() -> None:
    near_chain = _rich_chain(days=7)
    far_chain = _rich_chain(days=35)
    result = build_diagonal_spread(near_chain, far_chain, lots=1, directional_bias=0.30)
    assert result is not None
    protection, income = result
    assert protection[0].instrument_type == "CE"
    assert protection[0].direction == "BUY"
    assert income[0].instrument_type == "CE"
    assert income[0].direction == "SELL"


def test_build_diagonal_spread_bearish_uses_put_side() -> None:
    near_chain = _rich_chain(days=7)
    far_chain = _rich_chain(days=35)
    result = build_diagonal_spread(near_chain, far_chain, lots=1, directional_bias=-0.30)
    assert result is not None
    protection, income = result
    assert protection[0].instrument_type == "PE"
    assert income[0].instrument_type == "PE"


def test_implemented_strategy_types_includes_two_expiry_types() -> None:
    types = implemented_strategy_types()
    assert "calendar_spread" in types
    assert "diagonal_spread" in types
    assert "iron_condor" in types


def test_build_two_expiry_structure_dispatches_calendar() -> None:
    near_chain = _rich_chain(days=7)
    far_chain = _rich_chain(days=35)
    result = build_two_expiry_structure("calendar_spread", near_chain, far_chain, lots=1)
    assert result is not None


def test_build_two_expiry_structure_diagonal_needs_bias() -> None:
    near_chain = _rich_chain(days=7)
    far_chain = _rich_chain(days=35)
    result = build_two_expiry_structure(
        "diagonal_spread", near_chain, far_chain, lots=1, directional_bias=None
    )
    assert result is None


# ── low_ivr_mode (2026-09-04, off by default) ───────────────────────────────


def test_low_ivr_mode_disabled_ivr_has_no_effect(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Explicit disabled config, not the live config's current value (which
    # is operator-controlled and expected to change over time -- see
    # config/options_settings.json low_ivr_mode.enabled) -- this test's job
    # is to verify the disabled CODE PATH, independent of today's setting.
    monkeypatch.setattr(seller_settings, "low_ivr_mode", lambda settings=None: {"enabled": False})
    chain = _rich_chain()
    default = build_bull_put_spread(chain, lots=1)
    low_ivr = build_bull_put_spread(chain, lots=1, ivr=10.0)
    assert default is not None and low_ivr is not None
    assert default[1][0].strike == low_ivr[1][0].strike  # same short strike -- config disabled


def test_low_ivr_mode_enabled_and_below_threshold_changes_short_strike(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        seller_settings,
        "low_ivr_mode",
        lambda settings=None: {
            "enabled": True,
            "ivr_threshold": 25.0,
            # 0.28/0.15 (not the real 0.35/0.15 default) -- 0.28 chosen
            # because it resolves to a DIFFERENT strike than the default
            # 0.25's nearest match (23700) in _rich_chain()'s coarse delta
            # ladder, within find_strike_by_delta's 0.03 tolerance (0.35
            # doesn't resolve at all here -- nearest is 0.07 away, over
            # tolerance). This test verifies the config-override WIRING, not
            # the production default values (those live in
            # config/options_settings.json, asserted separately).
            "strike_deltas": {"credit_short": 0.28, "credit_long": 0.15},
        },
    )
    chain = _rich_chain()
    default = build_bull_put_spread(chain, lots=1, ivr=30.0)  # above threshold -- unaffected
    tight = build_bull_put_spread(chain, lots=1, ivr=10.0)  # below threshold -- tighter profile
    assert default is not None and tight is not None
    # tighter (0.28-delta) short put is closer to spot (higher strike) than
    # the default 0.25-delta short put for this chain's monotonic layout.
    assert tight[1][0].strike > default[1][0].strike


def test_low_ivr_mode_override_deltas_false_keeps_default_delta(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # 2026-09-08: real config now sets override_deltas:false -- the backtest
    # that justified the tighter delta also showed it has a genuinely lower
    # real win rate, which fails the EV gate even after pop_calibration.
    # DTE-shortening/candidacy floor/size reduction still apply (untouched by
    # this flag) -- only the delta override is suppressed.
    monkeypatch.setattr(
        seller_settings,
        "low_ivr_mode",
        lambda settings=None: {
            "enabled": True,
            "ivr_threshold": 25.0,
            "override_deltas": False,
            "strike_deltas": {"credit_short": 0.28, "credit_long": 0.15},
        },
    )
    chain = _rich_chain()
    default = build_bull_put_spread(chain, lots=1)
    below_threshold = build_bull_put_spread(chain, lots=1, ivr=10.0)
    assert default is not None and below_threshold is not None
    assert default[1][0].strike == below_threshold[1][0].strike


def test_low_ivr_mode_enabled_but_above_threshold_is_unaffected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        seller_settings,
        "low_ivr_mode",
        lambda settings=None: {
            "enabled": True,
            "ivr_threshold": 25.0,
            # 0.22/0.15 (not the real 0.35/0.15 default) -- chosen only to
            # exist within _rich_chain()'s coarse delta ladder + find_strike_by_delta's
            # 0.03 tolerance; this test verifies the config-override WIRING,
            # not the production default values (those are asserted directly
            # against config/options_settings.json, not this synthetic chain).
            "strike_deltas": {"credit_short": 0.22, "credit_long": 0.15},
        },
    )
    chain = _rich_chain()
    normal_ivr = build_bull_put_spread(chain, lots=1, ivr=40.0)
    no_ivr = build_bull_put_spread(chain, lots=1)
    assert normal_ivr is not None and no_ivr is not None
    assert normal_ivr[1][0].strike == no_ivr[1][0].strike


def test_low_ivr_mode_does_not_affect_debit_spreads(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.options.structure_builder import build_bull_call_spread

    monkeypatch.setattr(
        seller_settings,
        "low_ivr_mode",
        lambda settings=None: {
            "enabled": True,
            "ivr_threshold": 25.0,
            # 0.22/0.15 (not the real 0.35/0.15 default) -- chosen only to
            # exist within _rich_chain()'s coarse delta ladder + find_strike_by_delta's
            # 0.03 tolerance; this test verifies the config-override WIRING,
            # not the production default values (those are asserted directly
            # against config/options_settings.json, not this synthetic chain).
            "strike_deltas": {"credit_short": 0.22, "credit_long": 0.15},
        },
    )
    chain = _rich_chain()
    low_ivr = build_bull_call_spread(chain, lots=1, ivr=5.0)
    no_ivr = build_bull_call_spread(chain, lots=1)
    assert low_ivr is not None and no_ivr is not None
    assert low_ivr[1][0].strike == no_ivr[1][0].strike  # debit spreads ignore low_ivr_mode


def test_build_structure_threads_ivr_through_to_bull_put(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        seller_settings,
        "low_ivr_mode",
        lambda settings=None: {
            "enabled": True,
            "ivr_threshold": 25.0,
            # 0.22/0.15 (not the real 0.35/0.15 default) -- chosen only to
            # exist within _rich_chain()'s coarse delta ladder + find_strike_by_delta's
            # 0.03 tolerance; this test verifies the config-override WIRING,
            # not the production default values (those are asserted directly
            # against config/options_settings.json, not this synthetic chain).
            "strike_deltas": {"credit_short": 0.22, "credit_long": 0.15},
        },
    )
    chain = _rich_chain()
    via_dispatch = build_structure("bull_put_spread", chain, 1, ivr=10.0)
    direct = build_bull_put_spread(chain, lots=1, ivr=10.0)
    assert via_dispatch is not None and direct is not None
    assert via_dispatch[1][0].strike == direct[1][0].strike


# ── risk-budget width cap for credit verticals (2026-09-28) ─────────────────────────
# The 0.25/0.10-delta wing pair makes BANKNIFTY spreads ~1,800 points wide: ~₹47k max loss
# per 30-lot against a ₹20k limit, so sizing rejected them EVERY cycle ("SIZE_TOO_SMALL").
# Width is now capped by the per-lot risk budget instead of being fixed by the wing delta.


def _bank_chain() -> OptionChain:
    """BANKNIFTY-like ladder priced from Black-Scholes (spot 54,567, 28d, IV 15.4%, 100-pt
    steps): liquid, tight, with real deltas — the realistic shape of the failing chain."""
    from src.options.greeks import bs_price, greeks

    spot, t, iv, r = 54567.0, 28 / 365.0, 0.154, 0.07
    rows = []
    for k in range(50000, 59100, 100):
        strike = float(k)
        c, p = bs_price(spot, strike, t, r, iv, "CE"), bs_price(spot, strike, t, r, iv, "PE")

        def q(mid: float) -> OptionQuote:
            m = max(mid, 1.0)
            return OptionQuote("SYM", ltp=m, bid=m * 0.995, ask=m * 1.005, oi=20000, volume=9000)

        rows.append(
            ChainStrike(
                strike=strike,
                call=q(c),
                put=q(p),
                call_iv=iv,
                put_iv=iv,
                call_delta=greeks(spot, strike, t, r, iv, "CE")["delta"],
                put_delta=greeks(spot, strike, t, r, iv, "PE")["delta"],
                call_symbol=f"BANKNIFTY{k}CE",
                put_symbol=f"BANKNIFTY{k}PE",
            )
        )
    return OptionChain(
        underlying="BANKNIFTY",
        spot=spot,
        expiry=datetime.now(_IST) + timedelta(days=28),
        days_to_expiry=28,
        lot_size=30,
        strikes=rows,
    )


def _loss_per_lot(built) -> float:  # type: ignore[no-untyped-def]
    (long_leg,), (short_leg,) = built
    width = abs(short_leg.strike - long_leg.strike)
    credit = short_leg.premium - long_leg.premium
    return (width - credit) * short_leg.lot_size


def test_credit_vertical_without_a_cap_keeps_the_delta_wing_and_is_too_wide() -> None:
    built = build_bull_put_spread(_bank_chain(), 1)
    assert built is not None
    assert _loss_per_lot(built) > 30_000  # the failing shape: far above a ₹20k budget


def test_credit_vertical_width_is_capped_to_the_risk_budget() -> None:
    chain = _bank_chain()
    uncapped = build_bull_put_spread(chain, 1)
    capped = build_bull_put_spread(chain, 1, max_loss_per_lot=20_000.0)
    assert capped is not None and uncapped is not None
    assert _loss_per_lot(capped) <= 20_000.0
    # same short strike (the 0.25-delta premium seller), only the protective wing moved in
    assert capped[1][0].strike == uncapped[1][0].strike
    assert capped[0][0].strike > uncapped[0][0].strike
    # and it is the WIDEST that fits: one strike wider would breach the budget
    wider = capped[0][0].strike - 100.0
    row = next(s for s in chain.strikes if s.strike == wider)
    assert row.put is not None
    step_loss = ((capped[1][0].strike - wider) - (capped[1][0].premium - row.put.mid)) * 30
    assert step_loss > 20_000.0


def test_bear_call_spread_is_capped_the_same_way() -> None:
    chain = _bank_chain()
    capped = build_bear_call_spread(chain, 1, max_loss_per_lot=20_000.0)
    assert capped is not None and _loss_per_lot(capped) <= 20_000.0
    assert capped[0][0].strike < build_bear_call_spread(chain, 1)[0][0].strike  # type: ignore[index]


def test_a_cap_that_the_delta_structure_already_meets_changes_nothing() -> None:
    chain = _bank_chain()
    assert build_bull_put_spread(chain, 1, max_loss_per_lot=1_000_000.0) == build_bull_put_spread(
        chain, 1
    )


def test_cap_too_small_for_any_width_returns_none() -> None:
    assert build_bull_put_spread(_bank_chain(), 1, max_loss_per_lot=500.0) is None


def test_cap_skips_illiquid_protective_strikes() -> None:
    chain = _bank_chain()
    capped = build_bull_put_spread(chain, 1, max_loss_per_lot=20_000.0)
    assert capped is not None
    bad = capped[0][0].strike
    for s in chain.strikes:
        if s.strike == bad:
            s.put = OptionQuote("SYM", ltp=5.0, bid=0.0, ask=9.0, oi=0, volume=0)
    again = build_bull_put_spread(chain, 1, max_loss_per_lot=20_000.0)
    assert again is not None and again[0][0].strike != bad
    assert _loss_per_lot(again) <= 20_000.0


def test_build_structure_passes_the_cap_only_to_credit_verticals() -> None:
    chain = _rich_chain()
    # a debit vertical / condor must simply ignore the keyword, not crash
    for st in ("bull_call_spread", "iron_condor", "long_straddle"):
        assert build_structure(st, chain, 1, max_loss_per_lot=1_000.0) == build_structure(
            st, chain, 1
        )
