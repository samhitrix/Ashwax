"""Tests for src/options/options_risk.py — one case per options_execution param."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.brokers.base import OptionQuote
from src.options.greeks import bs_price, days_to_years, greeks
from src.options.nfo_chain import ChainStrike, OptionChain
from src.options.options_risk import OptionsRiskGate
from src.options.structure_selector import (
    Opportunity,
    OptionLeg,
    PortfolioState,
    StructureChoice,
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
    "max_premium_at_risk_rupees": 5000,
    "max_delta_exposure": 0.7,
    "preferred_strategies": ["bull_call_spread", "long_call"],
    "avoid_naked_selling": True,
    "min_days_to_expiry": 14,
    "max_iv_percentile_for_buying": 70,
    "liquidity_min_oi": 1000,
    "liquidity_min_volume": 500,
}
_IV_HISTORY = [0.15 + 0.0075 * i for i in range(20)]  # mid-range → pct < 70


def _build_chain(*, dte: int = 30, oi: int = 5000, vol: int = 2000) -> OptionChain:
    strikes = [2650.0, 2700.0, 2750.0, 2800.0, 2850.0, 2900.0, 2950.0, 3000.0]
    t = days_to_years(dte)
    rows: list[ChainStrike] = []
    for k in strikes:
        ce = bs_price(_SPOT, k, t, _R, _IV, "CE")
        pe = bs_price(_SPOT, k, t, _R, _IV, "PE")
        rows.append(
            ChainStrike(
                strike=k,
                call=OptionQuote(
                    symbol=f"NFO:R{int(k)}CE", ltp=ce, bid=ce - 0.5, ask=ce + 0.5, oi=oi, volume=vol
                ),
                put=OptionQuote(
                    symbol=f"NFO:R{int(k)}PE", ltp=pe, bid=pe - 0.5, ask=pe + 0.5, oi=oi, volume=vol
                ),
                call_iv=_IV,
                put_iv=_IV,
                call_delta=greeks(_SPOT, k, t, _R, _IV, "CE")["delta"],
                put_delta=greeks(_SPOT, k, t, _R, _IV, "PE")["delta"],
                call_symbol=f"NFO:R{int(k)}CE",
                put_symbol=f"NFO:R{int(k)}PE",
            )
        )
    return OptionChain("RELIANCE", _SPOT, _NOW + timedelta(days=dte), dte, _LOT, rows)


def _valid_choice(chain: OptionChain) -> StructureChoice:
    """A real bull_call_spread / long_call from the selector (guaranteed valid)."""
    opp = Opportunity(
        symbol="NSE:RELIANCE",
        direction="BUY",
        conviction=0.6,
        entry_price=_SPOT,
        stop_loss_pct=0.025,
        target_pct=0.073,
        risk_rupees=3000.0,
        time_horizon="swing",
    )
    sel = StructureSelector(_CFG)
    choice = sel.select(opp, PortfolioState(1, 5), chain, _IV_HISTORY)
    assert choice.legs, "fixture expected an options structure"
    return choice


# ── pass-through ─────────────────────────────────────────────────────────────


def test_equity_choice_always_passes() -> None:
    gate = OptionsRiskGate(_CFG)
    equity = StructureChoice(
        symbol="NSE:X",
        structure_type="equity",
        product="DELIVERY",
        entry=100,
        stop=98,
        target=104,
        capital_required=10000,
        max_loss=200,
        max_profit=400,
        rr=2.0,
        expected_value=100,
        confidence=0.6,
        reasoning="equity",
        legs=[],
    )
    assert gate.verify(equity, None).passed is True


def test_valid_options_structure_passes() -> None:
    chain = _build_chain()
    gate = OptionsRiskGate(_CFG)
    res = gate.verify(_valid_choice(chain), chain, _IV_HISTORY)
    assert res.passed is True, res.reason


# ── one block per parameter ──────────────────────────────────────────────────


def test_disabled_blocks() -> None:
    chain = _build_chain()
    gate = OptionsRiskGate({**_CFG, "enabled": False})
    assert gate.verify(_valid_choice(chain), chain, _IV_HISTORY).passed is False


def test_max_premium_blocks() -> None:
    chain = _build_chain()
    gate = OptionsRiskGate({**_CFG, "max_premium_at_risk_rupees": 100})
    res = gate.verify(_valid_choice(chain), chain, _IV_HISTORY)
    assert res.passed is False
    assert "max_premium" in res.reason


def test_max_delta_blocks() -> None:
    chain = _build_chain()
    gate = OptionsRiskGate({**_CFG, "max_delta_exposure": 0.05})
    res = gate.verify(_valid_choice(chain), chain, _IV_HISTORY)
    assert res.passed is False
    assert "delta" in res.reason


def test_naked_short_blocks() -> None:
    chain = _build_chain()
    gate = OptionsRiskGate(_CFG)
    naked = StructureChoice(
        symbol="NSE:R",
        structure_type="short_call",
        product="MARGIN",
        entry=70,
        stop=140,
        target=0,
        capital_required=0,
        max_loss=1000,
        max_profit=3500,
        rr=3.5,
        expected_value=500,
        confidence=0.6,
        reasoning="naked",
        legs=[OptionLeg("NFO:R2750CE", "CE", 2750.0, "SELL", 1, _LOT, 70.0, -0.5)],
    )
    res = gate.verify(naked, chain, _IV_HISTORY)
    assert res.passed is False
    assert "naked" in res.reason


def test_min_dte_blocks() -> None:
    chain = _build_chain(dte=5)
    gate = OptionsRiskGate(_CFG)
    res = gate.verify(_valid_choice(chain), chain, _IV_HISTORY)
    assert res.passed is False
    assert "dte" in res.reason


def test_iv_percentile_unavailable_blocks() -> None:
    chain = _build_chain()
    gate = OptionsRiskGate(_CFG)
    res = gate.verify(_valid_choice(chain), chain, iv_history=[])
    assert res.passed is False
    assert "IV percentile" in res.reason


def test_iv_too_high_blocks() -> None:
    chain = _build_chain()
    gate = OptionsRiskGate(_CFG)
    low_history = [0.05 + 0.001 * i for i in range(20)]  # ATM IV ranks ~100 pct
    res = gate.verify(_valid_choice(chain), chain, low_history)
    assert res.passed is False
    assert "IV pct" in res.reason


def test_illiquid_legs_block() -> None:
    chain = _build_chain()  # liquid chain → builds a valid choice
    choice = _valid_choice(chain)
    illiquid = _build_chain(oi=100, vol=50)  # same strikes, thin book
    gate = OptionsRiskGate(_CFG)
    res = gate.verify(choice, illiquid, _IV_HISTORY)
    assert res.passed is False
    assert "liquidity" in res.reason


def test_premium_within_budget_but_delta_ok_passes() -> None:
    """Sanity: a slightly mutated valid choice still passes when within bounds."""
    chain = _build_chain()
    choice = dataclasses.replace(_valid_choice(chain), max_loss=4000.0)
    gate = OptionsRiskGate(_CFG)
    assert gate.verify(choice, chain, _IV_HISTORY).passed is True
