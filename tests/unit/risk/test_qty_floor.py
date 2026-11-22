"""Tests for src/risk/qty_floor.py — min-quantity floor with conviction scaling."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from src.risk import qty_floor
from src.risk.crm import CentralRiskManager, CRMContext, CRMSignal, GateDecision
from src.risk.qty_floor import (
    QtyFloorConfig,
    apply_qty_floor,
    conviction_score,
    floored_quantity,
    load_qty_floor,
)

_CFG = QtyFloorConfig(min_qty=100, max_loss_per_trade_pct=0.5)


# ── floored_quantity ────────────────────────────────────────────────────────
# entry 500, sl 490 -> ₹10/share; ceiling ₹3,000 -> 300 shares; cash plenty.
def test_conviction_zero_gives_exactly_min_qty() -> None:
    assert floored_quantity(500, 490, 100, 3_000, 1e9, 0.0) == (100, "scaled")


def test_conviction_one_gives_max_qty() -> None:
    assert floored_quantity(500, 490, 100, 3_000, 1e9, 1.0) == (300, "scaled")


def test_conviction_half_lands_in_the_middle() -> None:
    assert floored_quantity(500, 490, 100, 3_000, 1e9, 0.5) == (200, "scaled")


def test_ceiling_below_min_reduces_never_skips() -> None:
    # ₹50/share stop, ₹3,000 ceiling -> 60 shares fit: trade 60, not 0.
    assert floored_quantity(500, 450, 100, 3_000, 1e9, 1.0) == (60, "reduced_ceiling")


def test_cash_below_min_reduces_to_affordable() -> None:
    assert floored_quantity(500, 490, 100, 3_000, 20_000, 1.0) == (40, "reduced_cash")


def test_liquidity_cap_binds() -> None:
    assert floored_quantity(500, 490, 100, 3_000, 1e9, 1.0, extra_cap_qty=150) == (150, "scaled")
    assert floored_quantity(500, 490, 100, 3_000, 1e9, 1.0, extra_cap_qty=70) == (
        70,
        "reduced_liquidity",
    )


def test_zero_only_when_not_one_share_fits() -> None:
    qty, _ = floored_quantity(500, 490, 100, 3_000, 100, 1.0)  # cash < 1 share
    assert qty == 0


def test_invalid_stop_returns_zero() -> None:
    assert floored_quantity(500, 500, 100, 3_000, 1e9, 1.0)[0] == 0


# ── conviction_score ────────────────────────────────────────────────────────
def test_missing_inputs_are_neutral() -> None:
    assert conviction_score(None, None, None, _CFG) == 1.0


def test_conviction_is_product_of_bands() -> None:
    # counter-trend 0.5 x setup 40-70 0.75 x llm<0.6 0.75
    assert conviction_score(0.5, 55.0, 0.5, _CFG) == pytest.approx(0.28125)
    assert conviction_score(1.0, 80.0, 0.8, _CFG) == 1.0
    assert conviction_score(1.0, 20.0, None, _CFG) == 0.5


# ── load_qty_floor ──────────────────────────────────────────────────────────
def _write(tmp_path: Path, blk: dict[str, Any]) -> Path:
    p = tmp_path / "capital.json"
    p.write_text(json.dumps({"swing": {"min_qty_floor": blk}}), encoding="utf-8")
    return p


def test_switch_per_mode(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "enabled_paper": True,
            "enabled_live": False,
            "min_qty": 100,
            "max_loss_per_trade": "0.5%",
        },
    )
    assert load_qty_floor("swing", True, p) is not None
    assert load_qty_floor("swing", False, p) is None
    assert load_qty_floor("intraday", True, p) is None  # no block = off


def test_malformed_block_is_off(tmp_path: Path) -> None:
    p = _write(tmp_path, {"enabled_paper": True, "min_qty": 100, "max_loss_per_trade": 0.5})
    assert load_qty_floor("swing", True, p) is None


# ── apply_qty_floor + G8 ────────────────────────────────────────────────────
def _sig(**kw: object) -> CRMSignal:
    base: dict[str, Any] = dict(
        signal_id="SIG-T",
        symbol="TESTSYM",
        direction="BUY",
        strategy_name="test_strategy",
        intended_quantity=12,
        stop_loss_pct=0.02,
        target_pct=0.04,
        risk_rupees=120.0,
        entry_price=500.0,
        regime="BULL",
        strategy_health="HEALTHY",
        news_halt=False,
        earnings_proximity_days=999,
        engine="swing",
        meta={"autonomy_mode": "autonomous"},
    )
    base.update(kw)
    return CRMSignal(**base)


def _ctx(**kw: object) -> CRMContext:
    base: dict[str, Any] = dict(
        total_capital=1_000_000.0,
        max_risk_per_trade=1_000.0,
        max_daily_loss=2_000.0,
        max_open_positions=5,
        max_new_trades_per_day=6,
        min_risk_reward_ratio=1.8,
        max_stop_loss_pct=0.08,
        current_equity=1_000_000.0,
        max_swing_loss_per_trade=2_000.0,
        session_open_hour=0,
        session_close_hour=24,
    )
    base.update(kw)
    return CRMContext(**base)


@pytest.fixture(autouse=True)
def _pin_clock():
    with patch("src.risk.crm.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 1, 15, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
        yield


def test_disabled_leaves_signal_untouched() -> None:
    sig = _sig()
    with patch.object(qty_floor, "load_qty_floor", return_value=None):
        assert not apply_qty_floor(
            sig,
            segment="swing",
            is_paper=False,
            entry=500,
            sl=490,
            equity=1e6,
            available_funds=1e9,
            dma_mult=None,
            setup_q=None,
        )
    assert sig.intended_quantity == 12
    assert "qty_floor_cap_rupees" not in sig.meta


def test_apply_resizes_and_g8_keeps_floored_qty() -> None:
    # equity 10L x 0.5% = ₹5,000 ceiling, ₹10/share -> max 500; neutral conviction -> 500.
    sig = _sig()
    with patch.object(qty_floor, "load_qty_floor", return_value=_CFG):
        assert apply_qty_floor(
            sig,
            segment="swing",
            is_paper=True,
            entry=500,
            sl=490,
            equity=1_000_000,
            available_funds=1e9,
            dma_mult=None,
            setup_q=None,
        )
    assert sig.intended_quantity == 500
    assert sig.risk_rupees == 5_000
    assert sig.meta["max_risk_rupees"] == 5_000
    # Without the floor cap G8 would cut 500 -> 200 (₹2,000 swing cap / ₹10).
    res = CentralRiskManager()._g8_sl_sizing(sig, _ctx(), qty=sig.intended_quantity)
    assert res.decision is GateDecision.PASS


def test_g8_without_floor_meta_unchanged() -> None:
    res = CentralRiskManager()._g8_sl_sizing(_sig(), _ctx(), qty=500)
    assert res.decision is GateDecision.MODIFY
    assert res.modified_quantity == 200
