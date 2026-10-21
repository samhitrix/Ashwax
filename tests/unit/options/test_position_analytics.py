"""compute_leg_pnl / compute_breakevens / compute_pop -- dashboard analytics
derived from already-persisted legs + net_credit, no new storage."""

from __future__ import annotations

import pytest
from src.options.position_analytics import compute_breakevens, compute_leg_pnl, compute_pop


def _leg(sym, kind, strike, direction, premium, lots=1, lot_size=75, delta=None):
    d = {
        "tradingsymbol": sym,
        "instrument_type": kind,
        "strike": strike,
        "direction": direction,
        "lots": lots,
        "lot_size": lot_size,
        "premium": premium,
    }
    if delta is not None:
        d["delta"] = delta
    return d


# ── compute_leg_pnl ──────────────────────────────────────────────────────────


def test_leg_pnl_buy_gains_as_price_rises():
    legs = [_leg("A", "PE", 22000, "BUY", 50.0)]
    total, per_leg = compute_leg_pnl(legs, {"A": {"last_price": 60.0}})
    assert total == (60.0 - 50.0) * 75
    assert per_leg == [750.0]


def test_leg_pnl_sell_gains_as_price_falls():
    legs = [_leg("A", "PE", 22000, "SELL", 50.0)]
    total, _ = compute_leg_pnl(legs, {"A": {"last_price": 40.0}})
    assert total == (50.0 - 40.0) * 75


def test_leg_pnl_unknown_ltp_makes_total_none():
    legs = [_leg("A", "PE", 22000, "BUY", 50.0), _leg("B", "CE", 23000, "SELL", 20.0)]
    total, per_leg = compute_leg_pnl(legs, {"A": {"last_price": 55.0}})
    assert total is None
    assert per_leg == [375.0, None]


# ── compute_breakevens ───────────────────────────────────────────────────────


def test_breakeven_vertical_put_credit_spread():
    # bull_put_spread: short 22250 PE, long 22000 PE, net credit 42.15/share x 75
    legs = [
        _leg("L", "PE", 22000, "BUY", 50.63),
        _leg("S", "PE", 22250, "SELL", 92.78),
    ]
    out = compute_breakevens(legs, net_credit=(92.78 - 50.63) * 75, spot=22569.55)
    assert len(out) == 1
    assert out[0]["strike"] == 22250 - round(42.15, 2)
    assert out[0]["pctFromSpot"] == pytest.approx(-1.6, abs=0.05)


def test_breakeven_without_spot_has_no_pct():
    legs = [_leg("L", "PE", 22000, "BUY", 50.0), _leg("S", "PE", 22250, "SELL", 92.78)]
    out = compute_breakevens(legs, net_credit=(92.78 - 50.0) * 75)
    assert out[0]["pctFromSpot"] is None


def test_breakeven_condor_two_sides():
    legs = [
        _leg("LP", "PE", 21950, "BUY", 15.22),
        _leg("LC", "CE", 23150, "BUY", 9.85),
        _leg("SP", "PE", 22150, "SELL", 32.33),
        _leg("SC", "CE", 22950, "SELL", 28.20),
    ]
    net_credit = (32.33 + 28.20 - 15.22 - 9.85) * 75
    out = compute_breakevens(legs, net_credit, spot=22569.55)
    assert len(out) == 2
    assert out[0]["strike"] < 22150  # put-side breakeven below the short put
    assert out[1]["strike"] > 22950  # call-side breakeven above the short call
    assert out[0]["pctFromSpot"] < 0
    assert out[1]["pctFromSpot"] > 0


def test_breakeven_empty_for_pure_debit_structure():
    legs = [_leg("L", "CE", 22000, "BUY", 50.0)]
    assert compute_breakevens(legs, net_credit=-50.0 * 75) == []


def test_breakeven_empty_when_no_legs():
    assert compute_breakevens([], net_credit=0.0) == []


# ── compute_pop ───────────────────────────────────────────────────────────────


def test_pop_calibrated_for_bull_put_spread():
    legs = [
        _leg("L", "PE", 22000, "BUY", 50.63, delta=-0.08),
        _leg("S", "PE", 22250, "SELL", 92.78, delta=-0.15),
    ]
    pop = compute_pop("bull_put_spread", legs)
    assert pop is not None
    assert 0.0 < pop < 1.0


def test_pop_uses_crude_proxy_for_uncalibrated_strategy_type():
    # iron_butterfly has no backtested calibration -> calibrated_pop falls
    # back to its own crude 1-|delta| proxy, still a real (if less precise) number.
    legs = [_leg("S", "PE", 22150, "SELL", 32.33, delta=-0.15)]
    pop = compute_pop("iron_butterfly", legs)
    assert pop == pytest.approx(1.0 - 0.15)


def test_pop_none_when_short_leg_has_no_delta():
    legs = [_leg("S", "PE", 22250, "SELL", 92.78)]
    assert compute_pop("bull_put_spread", legs) is None


def test_pop_none_when_no_sell_legs():
    legs = [_leg("L", "PE", 22000, "BUY", 50.63, delta=-0.08)]
    assert compute_pop("bull_put_spread", legs) is None


# --- compute_exit_levels: target / stop amounts follow the live config -----------------------------


def _patch_settings(monkeypatch, **blocks):
    from src.options import seller_settings

    monkeypatch.setattr(
        seller_settings, "profit_and_stop", lambda settings=None: blocks.get("pas", {})
    )
    monkeypatch.setattr(seller_settings, "expiry_day", lambda settings=None: blocks.get("ed", {}))


def test_exit_levels_credit_spread_follow_config(monkeypatch) -> None:
    from src.options.position_analytics import compute_exit_levels

    _patch_settings(monkeypatch, pas={"credit_profit_target_pct": 50, "credit_stop_loss_pct": 150})
    out = compute_exit_levels("bear_call_spread", [], 5122.0, 5122.0, 18878.0)
    assert out["targetAmount"] == 2561.0 and round(out["targetPct"]) == 50
    assert round(out["stopAmount"]) == 7683 and round(out["stopPct"], 1) == 40.7

    _patch_settings(monkeypatch, pas={"credit_profit_target_pct": 25, "credit_stop_loss_pct": 100})
    out = compute_exit_levels("bear_call_spread", [], 5122.0, 5122.0, 18878.0)
    assert out["targetAmount"] == 1280.5 and out["stopAmount"] == 5122.0


def test_exit_levels_debit_and_missing_margin(monkeypatch) -> None:
    from src.options.position_analytics import compute_exit_levels

    _patch_settings(monkeypatch, pas={"debit_profit_target_pct": 60, "debit_stop_loss_pct": 50})
    out = compute_exit_levels("bull_call_spread", [], -1000.0, 1500.0, None)
    assert out["targetAmount"] == 600.0 and out["stopAmount"] == 500.0
    assert out["stopPct"] is None


def test_exit_levels_short_premium_total_of_per_leg_stops(monkeypatch) -> None:
    from src.options import short_premium
    from src.options.position_analytics import compute_exit_levels

    cfg = short_premium.parse_cfg({"sl_pct": 23, "target_pct": 50, "allow_naked": True})
    monkeypatch.setattr(short_premium, "load_cfg", lambda: cfg)
    legs = [
        {"direction": "SELL", "instrument_type": "CE", "premium": 100.0, "lots": 2, "lot_size": 75},
        {"direction": "SELL", "instrument_type": "PE", "premium": 80.0, "lots": 2, "lot_size": 75},
    ]
    out = compute_exit_levels("options_intraday_short_premium", legs, 27000.0, 27000.0, 100000.0)
    assert out["stopAmount"] == pytest.approx((100 * 0.23 + 80 * 0.23) * 150)
    assert out["targetAmount"] == 13500.0


def test_exit_levels_none_when_no_credit(monkeypatch) -> None:
    from src.options.position_analytics import compute_exit_levels

    _patch_settings(monkeypatch)
    out = compute_exit_levels("bear_call_spread", [], 0.0, 0.0, 1000.0)
    assert out["targetAmount"] is None and out["stopAmount"] is None
