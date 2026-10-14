"""Tests for src.orchestrator._resolve_is_paper — the per-signal paper/live
resolution shared by _execute_intraday_signal and _execute_swing_signal.

Precedence (most to least authoritative):
  1. shared["paper_mode_locked"]  (operator.json paper_mode_locked, fail-safe)
  2. shared["strategy_execution_mode"][strategy_name]  (registry override)
  3. crm_ctx.segment_paper_mode[segment]  (segment default)
"""

from __future__ import annotations

from types import SimpleNamespace

import src.orchestrator as orch_mod


def _crm_ctx(segment_paper_mode: dict) -> SimpleNamespace:
    return SimpleNamespace(segment_paper_mode=segment_paper_mode)


def test_segment_default_used_when_no_override():
    shared = {"strategy_execution_mode": {}}
    ctx = _crm_ctx({"intraday": False})
    assert orch_mod._resolve_is_paper(shared, ctx, "intraday", "orb_intraday_smart", True) is False


def test_strategy_override_forces_paper_in_live_segment():
    shared = {"strategy_execution_mode": {"orb_intraday_smart": "paper"}}
    ctx = _crm_ctx({"intraday": False})  # segment is LIVE
    assert orch_mod._resolve_is_paper(shared, ctx, "intraday", "orb_intraday_smart", True) is True


def test_strategy_override_forces_live_in_paper_segment():
    shared = {"strategy_execution_mode": {"orb_intraday_smart": "live"}}
    ctx = _crm_ctx({"intraday": True})  # segment is PAPER
    assert orch_mod._resolve_is_paper(shared, ctx, "intraday", "orb_intraday_smart", True) is False


def test_override_only_applies_to_named_strategy():
    shared = {"strategy_execution_mode": {"orb_intraday_smart": "live"}}
    ctx = _crm_ctx({"intraday": True})
    # A different strategy in the same segment is unaffected by the override.
    assert orch_mod._resolve_is_paper(shared, ctx, "intraday", "some_other_strategy", True) is True


def test_paper_mode_locked_overrides_a_live_strategy_override():
    shared = {"paper_mode_locked": True, "strategy_execution_mode": {"orb_intraday_smart": "live"}}
    ctx = _crm_ctx({"intraday": False})
    assert orch_mod._resolve_is_paper(shared, ctx, "intraday", "orb_intraday_smart", True) is True


def test_paper_mode_locked_overrides_segment_default_too():
    shared = {"paper_mode_locked": True, "strategy_execution_mode": {}}
    ctx = _crm_ctx({"intraday": False})
    assert orch_mod._resolve_is_paper(shared, ctx, "intraday", "orb_intraday_smart", True) is True


def test_missing_strategy_execution_mode_key_defaults_to_inherit():
    shared: dict = {}  # no "strategy_execution_mode" key at all
    ctx = _crm_ctx({"swing": True})
    assert orch_mod._resolve_is_paper(shared, ctx, "swing", "minervini_vcp", False) is True


def test_falls_back_to_boot_flag_when_segment_missing_from_crm_ctx():
    shared = {"strategy_execution_mode": {}}
    ctx = _crm_ctx({})  # segment not present in segment_paper_mode
    assert orch_mod._resolve_is_paper(shared, ctx, "options", "seller_desk_v2", False) is False


def test_crm_mode_of_honors_strategy_override():
    from src.risk.crm import CRMSignal, mode_of

    ctx = SimpleNamespace(segment_paper_mode={}, strategy_execution_mode={})
    ctx.segment_paper_mode = {"swing": False}
    ctx.strategy_execution_mode = {"strat_a": "paper"}
    sig = lambda name: CRMSignal(  # noqa: E731
        signal_id="1",
        symbol="X",
        direction="BUY",
        strategy_name=name,
        intended_quantity=1,
        stop_loss_pct=0.02,
        target_pct=0.04,
        risk_rupees=100.0,
    )
    assert mode_of(sig("strat_a"), ctx) == "paper"  # override beats live segment
    assert mode_of(sig("strat_b"), ctx) == "live"  # no override -> segment
    ctx.strategy_execution_mode = {"strat_a": "live"}
    ctx.segment_paper_mode = {"swing": True}
    assert mode_of(sig("strat_a"), ctx) == "live"
