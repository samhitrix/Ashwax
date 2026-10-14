"""Unit tests for an options seller-desk signal (built via
src/options/seller_crm_adapter.build_seller_crm_signal()) running through the
CRM G0-G10 pipeline (src/risk/crm.py) -- the retrofit that replaced the
seller desk's previous complete bypass of this pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from src.options.seller_crm_adapter import build_seller_crm_signal
from src.risk.crm import CentralRiskManager, CRMContext

_IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _pin_clock_in_trading_window():
    """See tests/unit/risk/test_crm.py's identical fixture -- G3 reads the
    real wall clock, so it must be pinned to a valid entry window."""
    fixed = datetime(2026, 1, 15, 10, 30, 0, tzinfo=_IST)
    with patch("src.risk.crm.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        yield


def _ctx(**kw: object) -> CRMContext:
    base: dict[str, Any] = dict(
        total_capital=1_000_000.0,
        max_risk_per_trade=50_000.0,
        max_daily_loss=100_000.0,
        max_open_positions=10,
        max_new_trades_per_day=20,
        min_risk_reward_ratio=1.8,
        max_stop_loss_pct=0.08,
        current_equity=1_000_000.0,
        daily_loss_so_far=0.0,
        open_position_count=0,
        new_trades_today=0,
        consecutive_losses=0,
        master_switch_on=True,
        session_open_hour=0,
        session_close_hour=24,
    )
    base.update(kw)
    return CRMContext(**base)


def _signal(**kw: object):  # type: ignore[no-untyped-def]
    base: dict[str, Any] = dict(
        signal_id="SELLER-1",
        underlying="NSE:NIFTY",
        strategy_type="iron_condor",
        lots=2,
        max_loss=8_000.0,
        max_profit=3_000.0,
        regime="HIGH_VOL_RANGE_BOUND",
        vix=18.0,
    )
    base.update(kw)
    return build_seller_crm_signal(**base)


crm = CentralRiskManager()


def test_valid_credit_spread_signal_is_approved() -> None:
    decision = crm.evaluate(_signal(), _ctx())
    assert decision.approved is True
    assert decision.final_quantity == 2


def test_exhausted_open_position_cap_blocks_at_g7() -> None:
    decision = crm.evaluate(_signal(), _ctx(open_position_count=10, max_open_positions=10))
    assert decision.approved is False
    assert decision.blocking_gate == "G7"


def test_circuit_breaker_consecutive_losses_blocks_at_g2() -> None:
    decision = crm.evaluate(_signal(), _ctx(consecutive_losses=5, max_consecutive_losses=3))
    assert decision.approved is False
    assert decision.blocking_gate == "G2"


def test_llm_reject_verdict_blocks_at_g9() -> None:
    sig = _signal()
    sig.meta["llm_evaluated"] = True
    sig.meta["llm_flag"] = "REJECT"
    decision = crm.evaluate(sig, _ctx())
    assert decision.approved is False
    assert decision.blocking_gate == "G9"


def test_master_switch_off_for_options_segment_blocks_at_g0() -> None:
    decision = crm.evaluate(_signal(), _ctx(segment_master_switch={"options": False}))
    assert decision.approved is False
    assert decision.blocking_gate == "G0"


def test_daily_loss_cap_exhausted_blocks_at_g1() -> None:
    # Options use their OWN daily pot (separate book from equity, 2026-10-06).
    decision = crm.evaluate(
        _signal(), _ctx(options_daily_loss_so_far=10_000.0, max_options_daily_loss=10_000.0)
    )
    assert decision.approved is False
    assert decision.blocking_gate == "G1"


def test_equity_daily_loss_does_not_block_options() -> None:
    decision = crm.evaluate(_signal(), _ctx(daily_loss_so_far=100_000.0, max_daily_loss=100_000.0))
    assert decision.approved is True


def test_g9_required_by_registry_blocks_desk_signal_without_verdict() -> None:
    # Root cause 2026-10-05: desk signals are named "options_seller_desk::<type>" but the
    # registry key is options_seller_desk_v3, so llm_gate_required=true never applied and
    # every options trade logged "[no-verdict] LLM gate not required".
    decision = crm.evaluate(_signal(), _ctx(llm_gate_required={"options_seller_desk_v3": True}))
    assert decision.approved is False
    assert decision.blocking_gate == "G9"


def test_g9_not_required_still_passes_desk_signal_without_verdict() -> None:
    decision = crm.evaluate(_signal(), _ctx(llm_gate_required={"options_seller_desk_v3": False}))
    assert decision.approved is True


def test_g8_uses_max_options_structure_loss_per_trade_not_the_flat_equity_cap() -> None:
    # 2026-10-06/07: options previously used effective_risk_cap's flat ~Rs 2,000 equity
    # figure, so any real multi-lot structure (1,700-19,000/lot) was silently shrunk to
    # 1 lot. G8 must now use ctx.max_options_structure_loss_per_trade when it's set.
    sig = _signal(lots=3, max_loss=9_000.0)  # Rs 3,000/lot
    tight = crm.evaluate(sig, _ctx(max_options_structure_loss_per_trade=2_000.0))
    assert tight.approved is True
    # G8 floors a sizing-driven cap at 1 lot rather than rejecting outright (existing
    # convention: a structure under-budget for its full size is rescued to 1 lot).
    assert tight.final_quantity == 1
    roomy = crm.evaluate(sig, _ctx(max_options_structure_loss_per_trade=15_000.0))
    assert roomy.approved is True
    assert roomy.final_quantity == 3  # the tight-cap floor never applies once it fits


def test_g8_falls_back_to_shared_equity_cap_when_structure_cap_not_configured() -> None:
    sig = _signal(lots=1, max_loss=1_500.0)
    res = crm.evaluate(sig, _ctx(max_options_structure_loss_per_trade=0.0))
    assert (
        res.approved is True
    )  # unconfigured (0) falls back to effective_risk_cap, not a hard block
