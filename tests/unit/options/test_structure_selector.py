"""Tests for src/options/structure_selector.py — §11 structure selection."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.brokers.base import OptionQuote
from src.options.greeks import bs_price, days_to_years, greeks
from src.options.nfo_chain import ChainStrike, OptionChain
from src.options.structure_selector import (
    Opportunity,
    PortfolioState,
    StructureSelector,
)

_IST = ZoneInfo("Asia/Kolkata")
_NOW = datetime(2026, 6, 14, 10, 0, tzinfo=_IST)
_SPOT = 2750.0
_IV = 0.22
_R = 0.065
_LOT = 50

_CFG = {
    "enabled": True,
    "preferred_strategies": ["bull_call_spread", "protective_put", "long_call"],
    "max_iv_percentile_for_buying": 70,
    "max_premium_at_risk_rupees": 5000,
}

# IV history with ~20 samples straddling 0.22 → ATM IV sits mid-range (pct < 70).
_IV_HISTORY = [0.15 + 0.0075 * i for i in range(20)]  # 0.15 .. 0.2925


def _build_chain(spot: float = _SPOT, dte: int = 30) -> OptionChain:
    strikes = [2650.0, 2700.0, 2750.0, 2800.0, 2850.0, 2900.0, 2950.0, 3000.0]
    t = days_to_years(dte)
    rows: list[ChainStrike] = []
    for k in strikes:
        ce = bs_price(spot, k, t, _R, _IV, "CE")
        pe = bs_price(spot, k, t, _R, _IV, "PE")
        rows.append(
            ChainStrike(
                strike=k,
                call=OptionQuote(
                    symbol=f"NFO:RELIANCE{int(k)}CE",
                    ltp=ce,
                    bid=ce - 0.5,
                    ask=ce + 0.5,
                    oi=5000,
                    volume=2000,
                ),
                put=OptionQuote(
                    symbol=f"NFO:RELIANCE{int(k)}PE",
                    ltp=pe,
                    bid=pe - 0.5,
                    ask=pe + 0.5,
                    oi=5000,
                    volume=2000,
                ),
                call_iv=_IV,
                put_iv=_IV,
                call_delta=greeks(spot, k, t, _R, _IV, "CE")["delta"],
                put_delta=greeks(spot, k, t, _R, _IV, "PE")["delta"],
                call_symbol=f"NFO:RELIANCE{int(k)}CE",
                put_symbol=f"NFO:RELIANCE{int(k)}PE",
            )
        )
    return OptionChain(
        underlying="RELIANCE",
        spot=spot,
        expiry=_NOW + timedelta(days=dte),
        days_to_expiry=dte,
        lot_size=_LOT,
        strikes=rows,
    )


def _opp(**over: object) -> Opportunity:
    base = dict(
        symbol="NSE:RELIANCE",
        direction="BUY",
        conviction=0.6,
        entry_price=2750.0,
        stop_loss_pct=0.025,
        target_pct=0.073,
        risk_rupees=3000.0,
        time_horizon="swing",
        earnings_proximity_days=999,
    )
    base.update(over)
    return Opportunity(**base)  # type: ignore[arg-type]


_PORT = PortfolioState(open_position_count=1, max_open_positions=5)


# ── hard rules that force equity ─────────────────────────────────────────────


def test_options_disabled_returns_equity() -> None:
    sel = StructureSelector({"enabled": False})
    choice = sel.select(_opp(), _PORT, _build_chain(), _IV_HISTORY)
    assert choice.structure_type == "equity"


def test_no_chain_returns_equity() -> None:
    sel = StructureSelector(_CFG)
    choice = sel.select(_opp(), _PORT, None, _IV_HISTORY)
    assert choice.structure_type == "equity"


def test_short_signal_returns_equity() -> None:
    sel = StructureSelector(_CFG)
    choice = sel.select(_opp(direction="SELL"), _PORT, _build_chain(), _IV_HISTORY)
    assert choice.structure_type == "equity"


def test_intraday_returns_equity_mis() -> None:
    sel = StructureSelector(_CFG)
    choice = sel.select(_opp(time_horizon="intraday"), _PORT, _build_chain(), _IV_HISTORY)
    assert choice.structure_type == "equity"
    assert choice.product == "INTRADAY"


def test_iv_percentile_unavailable_fails_safe_to_equity() -> None:
    sel = StructureSelector(_CFG)
    # Empty history → percentile None → fail-safe equity.
    choice = sel.select(_opp(), _PORT, _build_chain(), iv_history=[])
    assert choice.structure_type == "equity"
    assert "IV percentile unavailable" in choice.reasoning


def test_high_iv_blocks_buying_returns_equity() -> None:
    sel = StructureSelector(_CFG)
    # History entirely below ATM IV → percentile ~100 > 70 → equity.
    low_history = [0.05 + 0.001 * i for i in range(20)]
    choice = sel.select(_opp(), _PORT, _build_chain(), low_history)
    assert choice.structure_type == "equity"


# ── options selection (the §11 capital-efficiency comparison) ────────────────


def test_moderate_conviction_swing_picks_capital_efficient_options() -> None:
    sel = StructureSelector(_CFG)
    choice = sel.select(_opp(conviction=0.6), _PORT, _build_chain(), _IV_HISTORY)
    assert choice.structure_type in ("bull_call_spread", "long_call")
    assert choice.legs  # has option legs
    # §11 factor 4: options win on capital efficiency (less capital deployed).
    equity = sel._equity_choice(_opp(conviction=0.6), "equity")
    assert choice.capital_required < equity.capital_required
    roc_opt = choice.expected_value / choice.capital_required
    roc_eq = equity.expected_value / equity.capital_required
    assert roc_opt > roc_eq


def test_high_conviction_tight_setup_picks_equity() -> None:
    sel = StructureSelector(_CFG)
    # §11 factor 1: high conviction + tight setup (no event) → equity participation.
    choice = sel.select(_opp(conviction=0.9), _PORT, _build_chain(), _IV_HISTORY)
    assert choice.structure_type == "equity"
    assert "high conviction" in choice.reasoning


def test_high_conviction_with_earnings_event_picks_options() -> None:
    sel = StructureSelector(_CFG)
    # Defined event overrides the equity-participation shortcut → options structure.
    choice = sel.select(
        _opp(conviction=0.9, earnings_proximity_days=3), _PORT, _build_chain(), _IV_HISTORY
    )
    assert choice.structure_type != "equity"


def test_chosen_structure_is_defined_risk() -> None:
    sel = StructureSelector(_CFG)
    choice = sel.select(_opp(conviction=0.6), _PORT, _build_chain(), _IV_HISTORY)
    # Defined risk: bounded, positive max loss within the premium-at-risk budget.
    assert 0.0 < choice.max_loss <= _CFG["max_premium_at_risk_rupees"] * 1.05
    # No naked short: net long premium (debit) — any short leg is covered by a long.
    buys = [leg for leg in choice.legs if leg.direction == "BUY"]
    assert buys, "structure must contain at least one long leg"


def test_portfolio_full_drops_equity_prefers_options() -> None:
    sel = StructureSelector(_CFG)
    full = PortfolioState(open_position_count=5, max_open_positions=5)
    choice = sel.select(_opp(), full, _build_chain(), _IV_HISTORY)
    assert choice.structure_type != "equity"


def test_net_delta_within_unhedged_bounds() -> None:
    sel = StructureSelector(_CFG)
    choice = sel.select(_opp(), _PORT, _build_chain(), _IV_HISTORY)
    # Long-biased bullish structure → positive net delta per share.
    if choice.legs:
        assert choice.net_delta > 0.0


# ── _p_win calibration (Options learning-engine PR4/PR6) ───────────────────


def test_p_win_defaults_to_conviction_clamp_when_no_structure_type() -> None:
    from src.options.structure_selector import _p_win

    assert _p_win(0.55) == 0.55
    assert _p_win(0.10) == 0.45  # floor
    assert _p_win(0.99) == 0.70  # ceiling


def test_p_win_uses_calibrated_win_rate_when_ev_calibration_live(monkeypatch) -> None:
    from src.options import ev_calibration_gate as gate
    from src.options.structure_selector import _p_win

    monkeypatch.setattr(gate, "calibrated_win_rate", lambda st: 0.62)
    monkeypatch.setattr(gate, "ev_calibration_mode", lambda: "live")
    assert _p_win(0.10, "bull_put_spread") == 0.62  # conviction ignored, calibrated used


def test_p_win_ignores_calibration_when_ev_calibration_mode_not_live(monkeypatch) -> None:
    from src.options import ev_calibration_gate as gate
    from src.options.structure_selector import _p_win

    monkeypatch.setattr(gate, "calibrated_win_rate", lambda st: 0.62)
    monkeypatch.setattr(gate, "ev_calibration_mode", lambda: "shadow")
    assert _p_win(0.55, "bull_put_spread") == 0.55  # calibration computed but not used


def test_p_win_uses_injected_bounds_over_hardcoded_clamp() -> None:
    from src.options.structure_selector import _p_win

    assert _p_win(0.99, bounds=(0.30, 0.80)) == 0.80
    assert _p_win(0.01, bounds=(0.30, 0.80)) == 0.30


def test_structure_selector_bounds_for_returns_none_when_conviction_calibration_off(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    sel = StructureSelector(_CFG, conviction_bounds={"long_call": (0.30, 0.80)})
    assert sel._bounds_for("long_call") is None  # no config/learning.json -> off


def test_structure_selector_bounds_for_returns_pair_when_live(tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "learning.json").write_text(
        json.dumps({"options_conviction_calibration": {"mode": "live"}})
    )
    sel = StructureSelector(_CFG, conviction_bounds={"long_call": (0.30, 0.80)})
    assert sel._bounds_for("long_call") == (0.30, 0.80)
    assert sel._bounds_for("protective_put") is None  # no entry for this type
