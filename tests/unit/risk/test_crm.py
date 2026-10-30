"""Unit tests for src/risk/crm.py — 10-gate CRM pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from src.risk.crm import (
    CentralRiskManager,
    CRMContext,
    CRMSignal,
    GateDecision,
    effective_risk_cap,
    is_shadow,
    mode_of,
    segment_of,
)

_IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _pin_clock_in_trading_window():
    """G3 reads the real wall clock (datetime.now); without pinning, the whole
    suite fails outside trading hours because G3 short-circuits every signal.
    Pin to 10:30 IST (a valid entry window) so non-time tests exercise later
    gates deterministically. Time-specific tests patch crm.datetime themselves
    inside their own `with`, which takes precedence over this fixture."""
    fixed = datetime(2026, 1, 15, 10, 30, 0, tzinfo=_IST)
    with patch("src.risk.crm.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        yield


def _ctx(**kw: object) -> CRMContext:
    base: dict[str, Any] = dict(
        total_capital=200_000.0,
        max_risk_per_trade=1_000.0,
        max_daily_loss=2_000.0,
        max_open_positions=5,
        max_new_trades_per_day=6,
        min_risk_reward_ratio=1.8,
        max_stop_loss_pct=0.08,
        current_equity=200_000.0,
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


def _sig(**kw: object) -> CRMSignal:
    base: dict[str, Any] = dict(
        signal_id="SIG001",
        symbol="INFY",
        direction="BUY",
        strategy_name="ORB",
        intended_quantity=10,
        stop_loss_pct=0.02,
        target_pct=0.04,
        risk_rupees=500.0,
        regime="BULL",
        strategy_health="HEALTHY",
        news_halt=False,
        earnings_proximity_days=999,
        meta={"autonomy_mode": "autonomous"},
    )
    base.update(kw)
    return CRMSignal(**base)


crm = CentralRiskManager()


# ── G7 exposure cap (operator.json capital.max_total_exposure_rupees) ───────────


def test_g7_blocks_when_exposure_cap_exceeded() -> None:
    ctx = _ctx(max_total_exposure_rupees=100_000.0, open_exposure_rupees=95_000.0)
    res = crm._g7_portfolio_margin(_sig(entry_price=1000.0), ctx, qty=10)  # +10_000 → 105k
    assert res.decision is GateDecision.BLOCK
    assert "Exposure" in res.reason


def test_g7_passes_when_exposure_within_cap() -> None:
    ctx = _ctx(max_total_exposure_rupees=100_000.0, open_exposure_rupees=50_000.0)
    res = crm._g7_portfolio_margin(_sig(entry_price=1000.0), ctx, qty=10)  # +10_000 → 60k
    assert res.decision is GateDecision.PASS


def test_g7_exposure_cap_disabled_when_zero() -> None:
    ctx = _ctx(max_total_exposure_rupees=0.0, open_exposure_rupees=9_999_999.0)
    res = crm._g7_portfolio_margin(_sig(entry_price=1000.0), ctx, qty=10)
    assert res.decision is GateDecision.PASS


# ── G2 VIX size multipliers (risk.json volatility_adjustment) ──────────────────


def test_g2_vix_reduce_uses_configured_multiplier() -> None:
    ctx = _ctx(vix_reduce_threshold=25.0, vix_halt_threshold=35.0, vix_reduce_multiplier=0.25)
    res = crm._g2_circuit_breakers(_sig(vix=26.0), ctx, qty=100)
    assert res.decision is GateDecision.MODIFY
    assert res.modified_quantity == 25  # 100 * 0.25


def test_g2_vix_reduce_multiplier_zero_blocks() -> None:
    ctx = _ctx(vix_reduce_threshold=25.0, vix_halt_threshold=35.0, vix_reduce_multiplier=0.0)
    res = crm._g2_circuit_breakers(_sig(vix=26.0), ctx, qty=100)
    assert res.decision is GateDecision.BLOCK


def test_g2_vix_halt_multiplier_zero_blocks() -> None:
    ctx = _ctx(vix_halt_threshold=35.0, vix_halt_multiplier=0.0)
    res = crm._g2_circuit_breakers(_sig(vix=36.0), ctx, qty=100)
    assert res.decision is GateDecision.BLOCK


def test_g2_vix_halt_multiplier_nonzero_reduces() -> None:
    ctx = _ctx(vix_halt_threshold=35.0, vix_halt_multiplier=0.1)
    res = crm._g2_circuit_breakers(_sig(vix=36.0), ctx, qty=100)
    assert res.decision is GateDecision.MODIFY
    assert res.modified_quantity == 10  # 100 * 0.1


def test_apply_risk_json_wires_vix_multipliers() -> None:
    ctx = _ctx()
    CRMContext.apply_risk_json(
        ctx,
        {"volatility_adjustment": {"vix_above_25_multiplier": 0.3, "vix_above_35_multiplier": 0.0}},
    )
    assert ctx.vix_reduce_multiplier == 0.3
    assert ctx.vix_halt_multiplier == 0.0


# ── Happy path ─────────────────────────────────────────────────────────────────


def test_all_gates_pass_returns_approved() -> None:
    decision = crm.evaluate(_sig(), _ctx())
    assert decision.approved is True
    assert decision.final_quantity == 10
    assert decision.blocking_gate == ""
    assert len(decision.gate_results) == 11  # G0-G10


# ── G0 master switch ───────────────────────────────────────────────────────────


def test_g0_blocks_when_master_switch_off() -> None:
    decision = crm.evaluate(_sig(), _ctx(master_switch_on=False))
    assert decision.approved is False
    assert decision.blocking_gate == "G0"


def test_g0_blocks_when_segment_master_switch_off() -> None:
    # _sig() defaults to engine="swing" (no asset_class="options") → segment "swing".
    sig = _sig(engine="swing")
    decision = crm.evaluate(sig, _ctx(segment_master_switch={"swing": False}))
    assert decision.approved is False
    assert decision.blocking_gate == "G0"


def test_g0_segment_master_switch_off_does_not_block_other_segments() -> None:
    sig = _sig(engine="intraday")
    decision = crm.evaluate(sig, _ctx(segment_master_switch={"swing": False}))
    assert decision.approved is True


def test_g0_segment_master_switch_defaults_to_pass_when_unconfigured() -> None:
    decision = crm.evaluate(_sig(), _ctx())
    assert decision.approved is True


def test_segment_of_options_regardless_of_engine() -> None:
    sig = _sig(asset_class="options", engine="swing")
    assert segment_of(sig) == "options"


def test_segment_of_equity_uses_engine() -> None:
    assert segment_of(_sig(engine="intraday")) == "intraday"
    assert segment_of(_sig(engine="swing")) == "swing"


# ── G1 hard caps ──────────────────────────────────────────────────────────────


def test_g1_blocks_on_25pct_floor_breach() -> None:
    decision = crm.evaluate(_sig(), _ctx(current_equity=49_000.0))
    assert decision.approved is False
    assert decision.blocking_gate == "G1"
    assert "25%" in decision.blocking_reason


def test_g1_passes_at_exactly_25pct_floor() -> None:
    decision = crm.evaluate(_sig(), _ctx(current_equity=50_000.0))
    assert decision.approved is True


def test_g1_blocks_when_daily_loss_cap_hit() -> None:
    # 2026-10-07: swing (the _sig() default engine) is now EXEMPT from this generic
    # daily-loss check (no daily loss limit, by design); this legacy/fallback branch
    # still exists for any signal tagged with an engine other than swing/intraday/
    # options, so exercise it with one such engine.
    decision = crm.evaluate(_sig(engine="other"), _ctx(daily_loss_so_far=2_000.0))
    assert decision.approved is False
    assert decision.blocking_gate == "G1"


def test_g1_swing_is_exempt_from_the_daily_loss_cap() -> None:
    # Operator decision 2026-10-06: swing lives by its own stop-loss and target only.
    decision = crm.evaluate(_sig(engine="swing"), _ctx(daily_loss_so_far=1_000_000.0))
    assert decision.approved is True


def test_g1_daily_loss_cap_is_independent_per_mode() -> None:
    # engine="other" (not swing/intraday/options) to exercise the legacy/fallback
    # branch -- swing itself is exempt from this check since 2026-10-06.
    ctx = _ctx(mode_daily_loss_so_far={"paper": 2_000.0, "live": 0.0})
    paper_sig = _sig(engine="other")
    decision = crm.evaluate(paper_sig, ctx)
    assert decision.approved is False
    assert decision.blocking_gate == "G1"

    live_sig = _sig(engine="other")
    ctx2 = _ctx(
        mode_daily_loss_so_far={"paper": 2_000.0, "live": 0.0},
        segment_paper_mode={"other": False},
    )
    decision2 = crm.evaluate(live_sig, ctx2)
    assert decision2.approved is True


def test_g1_daily_loss_falls_back_to_legacy_field_when_mode_dict_unset() -> None:
    decision = crm.evaluate(_sig(engine="other"), _ctx(daily_loss_so_far=2_000.0))
    assert decision.approved is False
    assert decision.blocking_gate == "G1"


def test_g1_oversized_signal_sized_down_by_g8() -> None:
    # risk_rupees=1500 with qty=10 → risk_per_unit=150; G8 sizes to int(1000/150)=6
    decision = crm.evaluate(_sig(risk_rupees=1_500.0), _ctx())
    assert decision.approved is True
    g8 = next(r for r in decision.gate_results if r.gate_id == "G8")
    assert g8.decision == GateDecision.MODIFY
    assert decision.final_quantity == 6


def test_g1_blocks_on_low_rr() -> None:
    decision = crm.evaluate(_sig(target_pct=0.02, stop_loss_pct=0.02), _ctx())
    assert decision.approved is False
    assert decision.blocking_gate == "G1"


def test_g1_rr_a_tick_under_the_minimum_passes_at_two_decimals() -> None:
    # Live KARURVYSYA 2026-10-21: LLM levels on 0.01 ticks gave R:R 10.38/5.78 = 1.7958, logged
    # "R:R 1.80 < minimum 1.80" and the idea expired. 1.7958 IS 1.80 at the precision the floor
    # is quoted and logged at (same convention as the stop-floor check in the intraday pipeline).
    entry = 384.95
    decision = crm.evaluate(_sig(stop_loss_pct=5.78 / entry, target_pct=10.38 / entry), _ctx())
    assert decision.blocking_gate != "G1"


def test_g1_rr_genuinely_under_minimum_blocks_with_three_decimal_message() -> None:
    decision = crm.evaluate(_sig(stop_loss_pct=0.05, target_pct=0.0895), _ctx())  # R:R 1.790
    assert decision.blocking_gate == "G1"
    g1 = next(r for r in decision.gate_results if r.gate_id == "G1")
    assert "R:R 1.790 < minimum 1.800" in g1.reason


def test_g1_blocks_on_sl_exceeding_max() -> None:
    decision = crm.evaluate(_sig(stop_loss_pct=0.10), _ctx())
    assert decision.approved is False
    assert decision.blocking_gate == "G1"


def test_g1_sl_clamped_exactly_to_max_is_not_blocked_by_float_noise() -> None:
    # Live FINCABLES case: compiler clamped SL to entry*(1-8%) → 1304.56, whose
    # pct computes as 0.08000000000000004 and was blocked "8.00% > max 8.00%".
    entry = 1418.0
    sl_pct = abs(entry - entry * (1 - 8.0 / 100)) / entry
    assert sl_pct > 0.08  # precondition: the float noise really exists
    decision = crm.evaluate(_sig(stop_loss_pct=sl_pct, target_pct=sl_pct * 2.5), _ctx())
    assert decision.blocking_gate != "G1"


def test_g1_still_blocks_sl_genuinely_over_max() -> None:
    decision = crm.evaluate(_sig(stop_loss_pct=0.0801, target_pct=0.0801 * 2.5), _ctx())
    assert decision.approved is False
    assert decision.blocking_gate == "G1"


# ── G2 circuit breakers ────────────────────────────────────────────────────────


def test_g2_blocks_on_consecutive_loss_limit() -> None:
    decision = crm.evaluate(_sig(), _ctx(consecutive_losses=3))
    assert decision.approved is False
    assert decision.blocking_gate == "G2"


def test_g2_passes_below_limit() -> None:
    assert crm.evaluate(_sig(), _ctx(consecutive_losses=2)).approved is True


def test_g2_blocks_when_risk_inputs_unverified() -> None:
    # A failed counter/open-risk refresh must not let the caps judge 0 / stale figures.
    decision = crm.evaluate(_sig(), _ctx(risk_inputs_unverified="daily_counters: db down"))
    assert decision.approved is False
    assert decision.blocking_gate == "G2"
    assert "db down" in decision.blocking_reason


def test_g2_risk_inputs_unverified_never_blocks_options() -> None:
    # The unverified inputs are equity-only; the daily expiry/premium trades must still go.
    ctx = _ctx(risk_inputs_unverified="daily_counters: db down")
    decision = crm.evaluate(_sig(asset_class="options"), ctx)
    assert decision.blocking_gate != "G2"


def test_g2_passes_when_risk_inputs_verified() -> None:
    assert crm.evaluate(_sig(), _ctx(risk_inputs_unverified="")).approved is True


def test_g2_consecutive_losses_independent_per_mode() -> None:
    ctx = _ctx(mode_consecutive_losses={"paper": 3, "live": 0})
    assert crm.evaluate(_sig(engine="swing"), ctx).approved is False
    live_ctx = _ctx(
        mode_consecutive_losses={"paper": 3, "live": 0}, segment_paper_mode={"swing": False}
    )
    assert crm.evaluate(_sig(engine="swing"), live_ctx).approved is True


# ── risk.json authoritative wiring ──────────────────────────────────────────────


def test_apply_risk_json_overrides_operator_limits() -> None:
    """risk.json is the authoritative override layer: its hard_limits + circuit_breakers
    win over the operator.json base values on the CRMContext."""
    ctx = _ctx(
        max_new_trades_per_day=6,
        max_open_positions=8,
        max_daily_loss=4_000.0,
        max_consecutive_losses=3,
    )
    CRMContext.apply_risk_json(
        ctx,
        {
            "hard_limits": {"max_new_trades_per_day": 4, "max_open_positions": 3},
            "circuit_breakers": {"consecutive_loss_count": 4, "daily_loss_limit_rupees": 2500},
        },
    )
    assert ctx.max_new_trades_per_day == 4
    assert ctx.max_open_positions == 3
    assert ctx.max_consecutive_losses == 4
    assert ctx.max_daily_loss == 2500.0


async def test_four_consecutive_paper_losses_halt_new_entries() -> None:
    """End-to-end: 4 closed paper losses flow through the real _refresh_daily_counters
    into the CRMContext, and G2 then halts the next entry at the risk.json threshold
    (consecutive_loss_count=4). 3 losses still pass (boundary)."""
    from src.orchestrator import _refresh_daily_counters

    class _PaperEngine:
        def __init__(self, streak: int) -> None:
            self._streak = streak

        async def daily_risk_counters(self, table: str = "paper_trades") -> dict[str, float]:
            return {
                "new_trades_today": 4,
                "open_position_count": 0,
                "daily_loss_so_far": 1500.0,  # < _ctx max_daily_loss (2000) so G1 passes
                "consecutive_losses": self._streak,
            }

    ctx3 = _ctx(max_consecutive_losses=4)
    await _refresh_daily_counters({"paper_ledger": _PaperEngine(3)}, ctx3)
    assert crm.evaluate(_sig(), ctx3).approved is True  # 3 < 4 → still allowed

    ctx4 = _ctx(max_consecutive_losses=4)
    await _refresh_daily_counters({"paper_ledger": _PaperEngine(4)}, ctx4)
    assert ctx4.consecutive_losses == 4
    decision = crm.evaluate(_sig(), ctx4)
    assert decision.approved is False
    assert decision.blocking_gate == "G2"


# ── G3 time of day ─────────────────────────────────────────────────────────────


def test_g3_allows_midday_entries_no_lunch_restriction() -> None:
    """Intraday entries follow the trading window only: 12:30 is an ordinary trading time."""
    for hh, mm in ((10, 45), (12, 30), (13, 0)):
        fake_now = datetime(2026, 1, 15, hh, mm, 0, tzinfo=_IST)
        with patch("src.risk.crm.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            decision = crm.evaluate(_sig(), _ctx())
        assert decision.blocking_gate != "G3", f"G3 blocked at {hh}:{mm:02d}"


def test_g3_blocks_after_the_session_in_exit_only_window() -> None:
    # Exit-only applies to INTRADAY; swing (daily candles) is exempt since 2026-10-06
    # (tests/unit/risk/test_g3_swing_daily.py).
    fake_now = datetime(2026, 1, 15, 15, 20, 0, tzinfo=_IST)
    sig = _sig()
    sig.engine = "intraday"
    with patch("src.risk.crm.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        decision = crm.evaluate(sig, _ctx())
    assert decision.approved is False
    assert decision.blocking_gate == "G3"


# ── G4 regime ─────────────────────────────────────────────────────────────────


def test_g4_blocks_on_unknown_regime() -> None:
    decision = crm.evaluate(_sig(regime="UNKNOWN"), _ctx())
    assert decision.approved is False
    assert decision.blocking_gate == "G4"


def test_g4_blocks_when_regime_not_in_allowed_list() -> None:
    ctx = _ctx(allowed_regimes={"ORB": ["BULL", "STRONG_BULL"]})
    decision = crm.evaluate(_sig(regime="BEAR"), ctx)
    assert decision.approved is False
    assert decision.blocking_gate == "G4"


def test_g4_passes_when_no_allowed_list() -> None:
    assert crm.evaluate(_sig(regime="BEAR"), _ctx()).approved is True


# ── G5 news ────────────────────────────────────────────────────────────────────


def test_g5_blocks_on_news_halt() -> None:
    decision = crm.evaluate(_sig(news_halt=True), _ctx())
    assert decision.approved is False
    assert decision.blocking_gate == "G5"


def test_g5_blocks_within_5_days_of_earnings() -> None:
    # doc §8.6: earnings within 5 trading days → reject swing entries
    for days in (1, 2, 3, 4, 5):
        decision = crm.evaluate(_sig(earnings_proximity_days=days), _ctx())
        assert decision.approved is False, f"expected block at {days} days"
        assert decision.blocking_gate == "G5"


def test_g5_passes_6_days_before_earnings() -> None:
    assert crm.evaluate(_sig(earnings_proximity_days=6), _ctx()).approved is True


# ── G6 strategy health ─────────────────────────────────────────────────────────


def test_g6_blocks_suspended_strategy() -> None:
    decision = crm.evaluate(_sig(strategy_health="SUSPENDED"), _ctx())
    assert decision.approved is False
    assert decision.blocking_gate == "G6"


def test_g6_blocks_unknown_health() -> None:
    decision = crm.evaluate(_sig(strategy_health="UNKNOWN"), _ctx())
    assert decision.approved is False
    assert decision.blocking_gate == "G6"


def test_g6_healthy_passes_full_size() -> None:
    decision = crm.evaluate(_sig(strategy_health="HEALTHY", intended_quantity=10), _ctx())
    assert decision.approved is True
    assert decision.final_quantity == 10


def test_g6_cautious_reduces_size_to_75pct() -> None:
    decision = crm.evaluate(_sig(strategy_health="CAUTIOUS", intended_quantity=8), _ctx())
    assert decision.approved is True
    assert decision.final_quantity == 6  # int(8 * 0.75)


def test_g6_degraded_reduces_size_to_50pct() -> None:
    decision = crm.evaluate(_sig(strategy_health="DEGRADED", intended_quantity=8), _ctx())
    assert decision.approved is True
    assert decision.final_quantity == 4  # int(8 * 0.50)


def test_g6_stressed_reduces_size_to_25pct() -> None:
    decision = crm.evaluate(_sig(strategy_health="STRESSED", intended_quantity=8), _ctx())
    assert decision.approved is True
    assert decision.final_quantity == 2  # int(8 * 0.25)


# ── G6 backtest is informational only ──────────────────────────────────────────
# A strategy whose periodic off-market backtest failed is never blocked at G6 —
# backtest is a confidence signal, not a gate. A valid live signal is sized down
# as a caution and proceeds to G9 (LLM confirmation) to decide.


def test_g6_backtest_failed_low_quality_sizes_down_not_blocked() -> None:
    sig = _sig(
        strategy_health="HEALTHY",
        intended_quantity=10,
        meta={"autonomy_mode": "autonomous", "backtest_passed": False, "setup_quality_score": 50},
    )
    decision = crm.evaluate(sig, _ctx())
    assert decision.approved is True
    assert decision.final_quantity == 5  # int(10 * 0.5 size mult)


def test_g6_backtest_failed_high_quality_sizes_down() -> None:
    sig = _sig(
        strategy_health="HEALTHY",
        intended_quantity=10,
        meta={"autonomy_mode": "autonomous", "backtest_passed": False, "setup_quality_score": 90},
    )
    decision = crm.evaluate(sig, _ctx())
    assert decision.approved is True
    assert decision.final_quantity == 5  # int(10 * 0.5 size mult)


def test_g6_backtest_passed_true_unaffected() -> None:
    sig = _sig(
        strategy_health="HEALTHY",
        intended_quantity=10,
        meta={"autonomy_mode": "autonomous", "backtest_passed": True, "setup_quality_score": 10},
    )
    decision = crm.evaluate(sig, _ctx())
    assert decision.approved is True
    assert decision.final_quantity == 10


# ── G7 portfolio margin ────────────────────────────────────────────────────────


def test_g7_blocks_when_max_positions_reached() -> None:
    decision = crm.evaluate(_sig(), _ctx(open_position_count=5))
    assert decision.approved is False
    assert decision.blocking_gate == "G7"


def test_g7_blocks_when_max_daily_trades_reached() -> None:
    decision = crm.evaluate(_sig(), _ctx(new_trades_today=6))
    assert decision.approved is False
    assert decision.blocking_gate == "G7"


def test_g7_open_position_cap_independent_per_mode() -> None:
    ctx = _ctx(mode_open_position_count={"paper": 5, "live": 0}, max_open_positions=5)
    assert crm.evaluate(_sig(engine="swing"), ctx).approved is False
    live_ctx = _ctx(
        mode_open_position_count={"paper": 5, "live": 0},
        max_open_positions=5,
        segment_paper_mode={"swing": False},
    )
    assert crm.evaluate(_sig(engine="swing"), live_ctx).approved is True


def test_g7_new_trades_cap_independent_per_mode() -> None:
    ctx = _ctx(mode_new_trades_today={"paper": 6, "live": 0})
    assert crm.evaluate(_sig(engine="swing"), ctx).approved is False
    live_ctx = _ctx(
        mode_new_trades_today={"paper": 6, "live": 0}, segment_paper_mode={"swing": False}
    )
    assert crm.evaluate(_sig(engine="swing"), live_ctx).approved is True


def test_g7_engine_open_cap_independent_per_mode() -> None:
    ctx = _ctx(
        max_swing_open_positions=3,
        mode_engine_open_positions={"paper:swing": 3, "live:swing": 0},
    )
    assert crm.evaluate(_sig(engine="swing"), ctx).approved is False
    live_ctx = _ctx(
        max_swing_open_positions=3,
        mode_engine_open_positions={"paper:swing": 3, "live:swing": 0},
        segment_paper_mode={"swing": False},
    )
    assert crm.evaluate(_sig(engine="swing"), live_ctx).approved is True


# operator.json hard_limits.swing.max_open_positions — concurrently open swing
# positions, decoupled from the shared global max_open_positions (a swing position
# can sit open across many days, which the global cap alone did not police per-engine).


def test_g7_blocks_swing_when_swing_open_cap_reached() -> None:
    ctx = _ctx(max_swing_open_positions=3, swing_open_positions=3)
    decision = crm.evaluate(_sig(engine="swing"), ctx)
    assert decision.approved is False
    assert decision.blocking_gate == "G7"


def test_g7_passes_swing_below_swing_open_cap() -> None:
    ctx = _ctx(max_swing_open_positions=3, swing_open_positions=2)
    decision = crm.evaluate(_sig(engine="swing"), ctx)
    assert decision.approved is True


def test_g7_swing_open_cap_disabled_when_zero() -> None:
    ctx = _ctx(max_swing_open_positions=0, swing_open_positions=99)
    decision = crm.evaluate(_sig(engine="swing"), ctx)
    assert decision.approved is True


def test_g7_swing_open_cap_does_not_apply_to_intraday() -> None:
    # An intraday signal must never be blocked by the swing-only open-count cap.
    ctx = _ctx(max_swing_open_positions=3, swing_open_positions=3)
    decision = crm.evaluate(_sig(engine="intraday"), ctx)
    assert decision.approved is True


# operator.json hard_limits.intraday.max_open_positions — mirrors the swing cap
# above but for intraday, so neither engine can starve the other's budget.


def test_g7_blocks_intraday_when_intraday_open_cap_reached() -> None:
    ctx = _ctx(max_intraday_open_positions=3, intraday_open_positions=3)
    decision = crm.evaluate(_sig(engine="intraday"), ctx)
    assert decision.approved is False
    assert decision.blocking_gate == "G7"


def test_g7_passes_intraday_below_intraday_open_cap() -> None:
    ctx = _ctx(max_intraday_open_positions=3, intraday_open_positions=2)
    decision = crm.evaluate(_sig(engine="intraday"), ctx)
    assert decision.approved is True


def test_g7_intraday_open_cap_disabled_falls_back_to_global_cap() -> None:
    # max_intraday_open_positions=0 (not configured) → the shared global cap is
    # the backstop for intraday too.
    ctx = _ctx(max_intraday_open_positions=0, open_position_count=5, max_open_positions=5)
    decision = crm.evaluate(_sig(engine="intraday"), ctx)
    assert decision.approved is False


# options_settings.json position_limits.max_open_options_positions +
# segment_of()-based engine resolution (2026-09-10) -- found live: an options
# signal's asset_class="options" but signal.engine stays at CRMSignal's
# unrelated "swing" default, so G7 used to resolve it as a swing trade and
# block it against swing's OWN daily-trade cap the moment swing filled it,
# with zero options positions open. segment_of(signal) (asset_class-driven)
# is the fix; these tests use asset_class="options" with engine left at its
# default to prove the real signal, not the stale field, now drives G7.


def test_g7_options_signal_not_blocked_by_unrelated_swing_daily_cap() -> None:
    # The exact live scenario: swing has hit its own 2/day cap, options has
    # placed zero trades and holds zero positions -- must NOT be blocked.
    ctx = _ctx(max_swing_trades_per_day=2, swing_trades_today=2)
    decision = crm.evaluate(_sig(asset_class="options"), ctx)
    assert decision.approved is True


def test_g7_options_signal_not_blocked_by_unrelated_swing_open_cap() -> None:
    ctx = _ctx(max_swing_open_positions=2, swing_open_positions=2)
    decision = crm.evaluate(_sig(asset_class="options"), ctx)
    assert decision.approved is True


def test_g7_blocks_options_when_options_open_cap_reached() -> None:
    ctx = _ctx(max_options_open_positions=4, options_open_positions=4)
    decision = crm.evaluate(_sig(asset_class="options"), ctx)
    assert decision.approved is False
    assert decision.blocking_gate == "G7"


def test_g7_passes_options_below_options_open_cap() -> None:
    ctx = _ctx(max_options_open_positions=4, options_open_positions=3)
    decision = crm.evaluate(_sig(asset_class="options"), ctx)
    assert decision.approved is True


def test_g7_options_open_cap_disabled_falls_back_to_global_cap() -> None:
    ctx = _ctx(max_options_open_positions=0, open_position_count=5, max_open_positions=5)
    decision = crm.evaluate(_sig(asset_class="options"), ctx)
    assert decision.approved is False


def test_g7_hosted_strategy_bypasses_options_open_cap_even_when_desk_is_maxed() -> None:
    # 2026-10-07: desk hit its own max_options_open_positions cap from its OWN 3
    # structures alone (found live). The expiry-day butterfly and the short-premium
    # strategies each already guarantee their own concurrency elsewhere (own once-a-day /
    # one-open checks) -- that IS their slot, so the desk filling its own cap must never
    # block them.
    ctx = _ctx(max_options_open_positions=3, options_open_positions=3)
    for name in ("options_intraday_short_premium2", "options_expiry_day"):
        decision = crm.evaluate(_sig(asset_class="options", strategy_name=name), ctx)
        assert decision.approved is True, name


def test_g7_hosted_strategy_still_blocked_by_other_real_gates() -> None:
    # The exemption is specifically the open-position COUNT -- not a blanket bypass.
    ctx = _ctx(max_total_exposure_rupees=100.0)
    decision = crm.evaluate(
        _sig(
            asset_class="options",
            strategy_name="options_expiry_day",
            entry_price=1000.0,
        ),
        ctx,
    )
    assert decision.approved is False


def test_g7_options_intraday_short_premium_is_uncapped_but_other_hosted_strategies_are_not() -> (
    None
):
    # 2026-10-12: options_intraday_short_premium must run every day regardless of open positions, trades
    # already taken today or deployed exposure. The expiry-day fly keeps the trade-count + exposure
    # caps (it only ever skips the open-position count).
    ctx = _ctx(
        max_options_open_positions=3,
        options_open_positions=3,
        max_new_trades_per_day=2,
        new_trades_today=2,
        max_total_exposure_rupees=100.0,
    )
    ok = crm.evaluate(
        _sig(
            asset_class="options",
            strategy_name="options_intraday_short_premium",
            entry_price=1000.0,
        ),
        ctx,
    )
    assert ok.approved is True
    ok2 = crm.evaluate(
        _sig(
            asset_class="options",
            strategy_name="options_intraday_short_premium2",
            entry_price=1000.0,
        ),
        ctx,
    )
    assert ok2.approved is True  # the second instance is uncapped the same way
    blocked = crm.evaluate(
        _sig(asset_class="options", strategy_name="options_expiry_day", entry_price=1000.0), ctx
    )
    assert blocked.approved is False and blocked.blocking_gate == "G7"


def test_options_intraday_short_premium_still_faces_the_real_breakers() -> None:
    # The exemption is G7's count/exposure caps only -- the options daily-loss cap (G1) and the
    # consecutive-loss breaker (G2) still stop it.
    ctx = _ctx(options_daily_loss_so_far=10_000.0, max_options_daily_loss=10_000.0)
    d = crm.evaluate(
        _sig(asset_class="options", strategy_name="options_intraday_short_premium"), ctx
    )
    assert d.approved is False and d.blocking_gate == "G1"
    d = crm.evaluate(
        _sig(asset_class="options", strategy_name="options_intraday_short_premium"),
        _ctx(consecutive_losses=5, max_consecutive_losses=3),
    )
    assert d.approved is False and d.blocking_gate == "G2"


def test_g7_swing_signal_unaffected_by_options_resolution_change() -> None:
    # Regression: a genuine swing signal (no asset_class="options") must
    # still resolve as "swing" and respect swing's own caps as before.
    ctx = _ctx(max_swing_open_positions=3, swing_open_positions=3)
    decision = crm.evaluate(_sig(engine="swing"), ctx)
    assert decision.approved is False
    assert decision.blocking_gate == "G7"
    assert decision.blocking_gate == "G7"


def test_g7_intraday_not_starved_by_exhausted_swing_book() -> None:
    # Regression (2026-08-10): 14 open swing positions (over both the swing cap
    # and the shared global cap) blocked an intraday signal that had ZERO open
    # positions of its own, because both engines shared one global count. With
    # per-engine caps configured, intraday must be judged on its own count only.
    ctx = _ctx(
        open_position_count=14,
        max_open_positions=10,
        max_swing_open_positions=10,
        swing_open_positions=14,
        max_intraday_open_positions=6,
        intraday_open_positions=0,
    )
    decision = crm.evaluate(_sig(engine="intraday"), ctx)
    assert decision.approved is True


# ── G8 sl/sizing ──────────────────────────────────────────────────────────────


def test_g8_blocks_when_no_stop_loss() -> None:
    decision = crm.evaluate(_sig(stop_loss_pct=0.0), _ctx())
    assert decision.approved is False
    assert decision.blocking_gate == "G8"


def test_g8_sizes_down_when_daily_budget_tight() -> None:
    # risk_per_unit=50, remaining=200 → budget_qty=4, intended=10 → MODIFY to 4
    # engine="other": swing/intraday/options are exempt from this shared-pot
    # rationing since 2026-10-07 (each has its own per-trade rupee cap instead).
    decision = crm.evaluate(
        _sig(risk_rupees=500.0, engine="other"), _ctx(daily_loss_so_far=1_800.0)
    )
    assert decision.approved is True
    g8 = next(r for r in decision.gate_results if r.gate_id == "G8")
    assert g8.decision == GateDecision.MODIFY
    assert decision.final_quantity == 4


def test_g8_blocks_when_one_unit_exceeds_daily_budget() -> None:
    # risk_per_unit=50, remaining=40 → even 1 unit cannot fit → BLOCK
    decision = crm.evaluate(
        _sig(risk_rupees=500.0, engine="other"), _ctx(daily_loss_so_far=1_960.0)
    )
    assert decision.approved is False
    assert decision.blocking_gate == "G8"


def test_g8_modifies_when_signal_oversized() -> None:
    # entry=1000, sl=980 (2%), cap=1000 → cap_qty=50; intended=100 → MODIFY to 50
    # Use max_single_position_pct_of_capital=100 so G1 doesn't reduce qty before G8 runs
    sig = _sig(entry_price=1000.0, stop_loss_pct=0.02, intended_quantity=100, risk_rupees=2000.0)
    decision = crm.evaluate(sig, _ctx(max_single_position_pct_of_capital=100.0))
    g8 = next(r for r in decision.gate_results if r.gate_id == "G8")
    assert g8.decision == GateDecision.MODIFY
    assert decision.final_quantity == 50


def test_g8_passes_when_within_cap() -> None:
    # entry=1000, sl=980 (2%), 10 shares, risk=200 < cap 1000 → PASS
    sig = _sig(entry_price=1000.0, stop_loss_pct=0.02, intended_quantity=10, risk_rupees=200.0)
    decision = crm.evaluate(sig, _ctx())
    g8 = next(r for r in decision.gate_results if r.gate_id == "G8")
    assert g8.decision == GateDecision.PASS
    assert decision.final_quantity == 10


# ── G9 llm (BLOCKING) ─────────────────────────────────────────────────────────


def _evaluate_g9(meta: dict[str, Any], **ctx_kw: object) -> Any:
    """Run the full pipeline with the time-of-day filter (G3) forced open, so G9
    is exercised deterministically regardless of the day/time the suite runs."""
    with patch("src.risk.crm._TIME_FILTER.check", return_value=(True, "")):
        return crm.evaluate(_sig(meta=meta), _ctx(**ctx_kw))


def test_g9_reject_with_real_verdict_blocks() -> None:
    decision = _evaluate_g9(
        {
            "autonomy_mode": "autonomous",
            "llm_flag": "REJECT",
            "llm_reason": "risky",
            "llm_evaluated": True,
        }
    )
    assert decision.approved is False
    assert decision.blocking_gate == "G9"


def test_g9_low_confidence_below_floor_blocks() -> None:
    decision = _evaluate_g9(
        {
            "autonomy_mode": "autonomous",
            "llm_flag": "CONFIRM",
            "llm_confidence": 0.20,
            "llm_evaluated": True,
        }
    )
    assert decision.approved is False
    g9 = next(r for r in decision.gate_results if r.gate_id == "G9")
    assert g9.decision == GateDecision.BLOCK
    assert "below floor" in g9.reason


def test_g9_confidence_above_floor_passes() -> None:
    decision = _evaluate_g9(
        {
            "autonomy_mode": "autonomous",
            "llm_flag": "CONFIRM",
            "llm_confidence": 0.80,
            "llm_evaluated": True,
        }
    )
    assert decision.approved is True


def test_g9_fails_open_without_verdict() -> None:
    # No llm_evaluated → LLM unavailable; gate must PASS so trading continues DSL-only,
    # even when a stale REJECT flag is present.
    decision = _evaluate_g9({"autonomy_mode": "autonomous", "llm_flag": "REJECT"})
    assert decision.approved is True
    g9 = next(r for r in decision.gate_results if r.gate_id == "G9")
    assert g9.decision == GateDecision.PASS
    assert "no-verdict" in g9.reason


def test_g9_bypass_passes() -> None:
    decision = _evaluate_g9(
        {"autonomy_mode": "autonomous", "llm_flag": "REJECT", "llm_evaluated": True},
        llm_bypass=True,
    )
    assert decision.approved is True


def _required() -> dict[str, bool]:
    return {_sig().strategy_name: True}


def test_g9_required_blocks_without_verdict() -> None:
    """llm_gate_required: no genuine verdict -> no trade (paper or live)."""
    decision = _evaluate_g9({"autonomy_mode": "autonomous"}, llm_gate_required=_required())
    assert decision.approved is False
    assert decision.blocking_gate == "G9"
    assert "gate required" in decision.blocking_reason


def test_g9_required_ignores_watchdog_bypass() -> None:
    decision = _evaluate_g9(
        {"autonomy_mode": "autonomous"}, llm_gate_required=_required(), llm_bypass=True
    )
    assert decision.approved is False
    assert decision.blocking_gate == "G9"


def test_g9_required_passes_with_real_confirm() -> None:
    decision = _evaluate_g9(
        {"autonomy_mode": "autonomous", "llm_flag": "CONFIRM", "llm_evaluated": True},
        llm_gate_required=_required(),
    )
    assert decision.approved is True


def test_g9_not_required_still_fails_open() -> None:
    decision = _evaluate_g9(
        {"autonomy_mode": "autonomous"}, llm_gate_required={_sig().strategy_name: False}
    )
    assert decision.approved is True


# ── G10 autonomy mode ─────────────────────────────────────────────────────────


def test_g10_blocks_in_manual_mode() -> None:
    sig = _sig(meta={"autonomy_mode": "manual"})
    decision = crm.evaluate(sig, _ctx())
    assert decision.approved is False
    assert decision.blocking_gate == "G10"


def test_g10_passes_in_autonomous_mode() -> None:
    assert crm.evaluate(_sig(meta={"autonomy_mode": "autonomous"}), _ctx()).approved is True


def test_g10_segment_autonomy_mode_blocks_only_that_segment() -> None:
    sig = _sig(engine="swing", meta={"autonomy_mode": "autonomous"})
    decision = crm.evaluate(sig, _ctx(segment_autonomy_mode={"swing": "manual"}))
    assert decision.approved is False
    assert decision.blocking_gate == "G10"

    other = _sig(engine="intraday", meta={"autonomy_mode": "autonomous"})
    decision2 = crm.evaluate(other, _ctx(segment_autonomy_mode={"swing": "manual"}))
    assert decision2.approved is True


def test_g10_falls_back_to_meta_when_segment_unconfigured() -> None:
    # Regression guard: existing meta-only callers/tests must be unaffected.
    sig = _sig(meta={"autonomy_mode": "manual"})
    assert crm.evaluate(sig, _ctx()).approved is False
    assert crm.evaluate(_sig(meta={"autonomy_mode": "autonomous"}), _ctx()).approved is True


# ── mode_of (paper/live resolution — not a gate) ────────────────────────────────


def test_mode_of_defaults_to_paper_when_unconfigured() -> None:
    assert mode_of(_sig(), _ctx()) == "paper"


def test_mode_of_resolves_per_segment() -> None:
    ctx = _ctx(segment_paper_mode={"swing": False})
    assert mode_of(_sig(engine="swing"), ctx) == "live"
    assert mode_of(_sig(engine="intraday"), ctx) == "paper"


def test_mode_of_options_segment_independent_of_engine() -> None:
    ctx = _ctx(segment_paper_mode={"options": False, "swing": True})
    sig = _sig(asset_class="options", engine="swing")
    assert mode_of(sig, ctx) == "live"


# ── shadow_mode (not a gate — checked by the caller after approval) ────────────


def test_is_shadow_false_by_default() -> None:
    assert is_shadow(_sig(), _ctx()) is False


def test_is_shadow_true_for_configured_segment() -> None:
    sig = _sig(engine="swing")
    assert is_shadow(sig, _ctx(segment_shadow_mode={"swing": True})) is True


def test_is_shadow_scoped_to_segment() -> None:
    sig = _sig(engine="intraday")
    assert is_shadow(sig, _ctx(segment_shadow_mode={"swing": True})) is False


def test_is_shadow_signal_still_passes_gates() -> None:
    # Shadow signals PASS the full pipeline — the caller decides to skip
    # placement afterward. A shadow signal must not show up as a G0/G10 block.
    sig = _sig(engine="swing")
    ctx = _ctx(segment_shadow_mode={"swing": True})
    decision = crm.evaluate(sig, ctx)
    assert decision.approved is True
    assert is_shadow(sig, ctx) is True


# ── Pipeline short-circuits on first block ─────────────────────────────────────


def test_blocking_gate_stops_pipeline_early() -> None:
    decision = crm.evaluate(_sig(), _ctx(master_switch_on=False))
    assert len(decision.gate_results) == 1
    assert decision.gate_results[0].gate_id == "G0"


# ── Regime Pullback Swing additions: G4 attempt limiter, G7 open-risk/sector
#    caps, G8 risk_pct override (spec §5.2, §6.3, §6.4, §8.2) ──────────────────


def test_g4_regime_phase_attempts_blocks_at_max() -> None:
    ctx = _ctx()
    res = crm._g4_regime(_sig(regime_phase_attempts=3, max_regime_phase_attempts=3), ctx, qty=10)
    assert res.decision is GateDecision.BLOCK
    assert "attempts" in res.reason


def test_g4_regime_phase_attempts_passes_below_max() -> None:
    ctx = _ctx()
    res = crm._g4_regime(_sig(regime_phase_attempts=2, max_regime_phase_attempts=3), ctx, qty=10)
    assert res.decision is GateDecision.PASS


def test_g4_regime_phase_attempts_disabled_when_max_zero() -> None:
    ctx = _ctx()
    res = crm._g4_regime(_sig(regime_phase_attempts=99, max_regime_phase_attempts=0), ctx, qty=10)
    assert res.decision is GateDecision.PASS


def test_g7_portfolio_open_risk_cap_blocks() -> None:
    ctx = _ctx(max_portfolio_risk_pct=1.0, open_risk_rupees=1_900.0)  # cap = 2_000
    res = crm._g7_portfolio_margin(_sig(risk_rupees=500.0), ctx, qty=10)
    assert res.decision is GateDecision.BLOCK
    assert "open risk" in res.reason


def test_g7_portfolio_open_risk_cap_passes_within_budget() -> None:
    ctx = _ctx(max_portfolio_risk_pct=1.0, open_risk_rupees=500.0)  # cap = 2_000
    res = crm._g7_portfolio_margin(_sig(risk_rupees=500.0), ctx, qty=10)
    assert res.decision is GateDecision.PASS


def test_g7_portfolio_open_risk_cap_disabled_when_zero() -> None:
    ctx = _ctx(max_portfolio_risk_pct=0.0, open_risk_rupees=9_999_999.0)
    res = crm._g7_portfolio_margin(_sig(risk_rupees=500.0), ctx, qty=10)
    assert res.decision is GateDecision.PASS


def test_g7_portfolio_open_risk_uses_own_mode_only() -> None:
    # 1_900 of LIVE risk must not consume a PAPER trade's budget (signal defaults to paper).
    ctx = _ctx(
        max_portfolio_risk_pct=1.0,
        open_risk_rupees=1_900.0,
        mode_open_risk_rupees={"live": 1_900.0, "paper": 0.0},
    )
    res = crm._g7_portfolio_margin(_sig(risk_rupees=500.0), ctx, qty=10)
    assert res.decision is GateDecision.PASS


def test_g7_intraday_open_risk_cap_blocks_intraday_only() -> None:
    # cap = 0.40% of 1_000_000 = 4_000; 2_100 open + 2_000 new = 4_100 > cap.
    ctx = _ctx(
        total_capital=1_000_000.0,
        max_intraday_open_risk_pct=0.40,
        mode_open_risk_rupees={"paper:intraday": 2_100.0},
    )
    res = crm._g7_portfolio_margin(_sig(risk_rupees=2_000.0, engine="intraday"), ctx, qty=10)
    assert res.decision is GateDecision.BLOCK
    assert "Intraday open risk" in res.reason
    swing = crm._g7_portfolio_margin(_sig(risk_rupees=2_000.0, engine="swing"), ctx, qty=10)
    assert swing.decision is GateDecision.PASS


def test_g7_sector_exposure_cap_blocks() -> None:
    ctx = _ctx(
        max_sector_exposure_pct=5.0,  # cap = 10_000 on 200_000 capital
        sector_open_exposure_rupees={"IT": 9_500.0},
    )
    res = crm._g7_portfolio_margin(
        _sig(entry_price=1000.0, sector="IT"), ctx, qty=10
    )  # +10_000 → 19_500 > 10_000
    assert res.decision is GateDecision.BLOCK
    assert "IT" in res.reason


def test_g7_sector_exposure_cap_passes_within_budget() -> None:
    ctx = _ctx(max_sector_exposure_pct=5.0, sector_open_exposure_rupees={"IT": 1_000.0})
    res = crm._g7_portfolio_margin(_sig(entry_price=1000.0, sector="IT"), ctx, qty=1)
    assert res.decision is GateDecision.PASS


def test_g7_sector_exposure_cap_disabled_when_zero() -> None:
    ctx = _ctx(max_sector_exposure_pct=0.0, sector_open_exposure_rupees={"IT": 9_999_999.0})
    res = crm._g7_portfolio_margin(_sig(entry_price=1000.0, sector="IT"), ctx, qty=10)
    assert res.decision is GateDecision.PASS


# options_settings.json position_limits.max_daily_loss_rupees (2026-09-10) --
# found live: operator.json's shared max_daily_loss_rupees=4000 pot (sized for
# equity) can never afford a single real index-options spread's 15,000-20,000+
# risk, on ANY day. options signals now check ctx.max_options_daily_loss
# instead when it's configured (> 0).


def test_g8_options_signal_uses_own_daily_budget_not_shared_equity_pot() -> None:
    # Shared equity pot is already exhausted (0 remaining); options' own
    # budget (50,000) easily covers this 16,572 trade.
    ctx = _ctx(max_daily_loss=4_000.0, daily_loss_so_far=4_000.0, max_options_daily_loss=50_000.0)
    res = crm._g8_sl_sizing(
        _sig(asset_class="options", entry_price=0.0, risk_rupees=16_572.0, stop_loss_pct=0.02),
        ctx,
        qty=1,
    )
    assert res.decision is not GateDecision.BLOCK


def test_options_daily_stop_is_a_g1_threshold_on_todays_loss_not_trade_size() -> None:
    # 2026-10-06: the options daily limit is "stop for the day once today's realized
    # options loss reaches it" (G1), never "is this one trade bigger than the limit" (G8).
    sig = _sig(asset_class="options", entry_price=0.0, risk_rupees=16_572.0, stop_loss_pct=0.02)
    fresh = _ctx(max_options_daily_loss=10_000.0, options_daily_loss_so_far=0.0)
    assert crm._g8_sl_sizing(sig, fresh, qty=1).decision is not GateDecision.BLOCK
    assert crm._g1_hard_caps(sig, fresh, qty=1).decision is not GateDecision.BLOCK
    spent = _ctx(max_options_daily_loss=10_000.0, options_daily_loss_so_far=10_000.0)
    res = crm._g1_hard_caps(sig, spent, qty=1)
    assert res.decision is GateDecision.BLOCK
    assert "Daily options loss cap" in res.reason


def test_options_never_draw_on_the_shared_equity_daily_pot() -> None:
    # Equity's pot exhausted and no options daily limit configured: options are not
    # blocked by equity losses (separate books).
    ctx = _ctx(max_daily_loss=4_000.0, daily_loss_so_far=4_000.0, max_options_daily_loss=0.0)
    sig = _sig(asset_class="options", entry_price=0.0, risk_rupees=16_572.0, stop_loss_pct=0.02)
    assert crm._g8_sl_sizing(sig, ctx, qty=1).decision is not GateDecision.BLOCK
    assert crm._g1_hard_caps(sig, ctx, qty=1).decision is not GateDecision.BLOCK


def test_g8_equity_signal_unaffected_by_options_budget_change() -> None:
    # Regression: a non-options/swing/intraday signal must still use the shared pot
    # even when max_options_daily_loss is configured.
    ctx = _ctx(max_daily_loss=4_000.0, daily_loss_so_far=4_000.0, max_options_daily_loss=50_000.0)
    res = crm._g8_sl_sizing(_sig(entry_price=100.0, stop_loss_pct=0.02, engine="other"), ctx, qty=1)
    assert res.decision is GateDecision.BLOCK


def test_g8_swing_and_intraday_also_exempt_from_the_shared_daily_pot() -> None:
    ctx = _ctx(max_daily_loss=4_000.0, daily_loss_so_far=4_000.0)
    for engine in ("swing", "intraday"):
        res = crm._g8_sl_sizing(
            _sig(entry_price=100.0, stop_loss_pct=0.02, engine=engine), ctx, qty=1
        )
        assert res.decision is not GateDecision.BLOCK, engine


def test_g8_risk_pct_override_tightens_sizing() -> None:
    # risk_pct=0.1% of 200_000 capital = 200, below the global cap (2_000) — the
    # tighter of the two must bind. risk/unit = 100*0.02 = 2 → cap_qty = 100.
    ctx = _ctx(max_risk_per_trade=2_000.0, total_capital=200_000.0)
    res = crm._g8_sl_sizing(
        _sig(entry_price=100.0, stop_loss_pct=0.02, risk_pct=0.1), ctx, qty=1000
    )
    assert res.decision is GateDecision.MODIFY
    assert res.modified_quantity == 100


def test_g8_risk_pct_scales_past_flat_rupee_cap() -> None:
    # risk_pct is the primary driver, NOT bounded by the flat rupee cap — that flat
    # cap only applies when no percentage is configured. 1% of 200_000 equity = 2_000,
    # well above max_risk_per_trade=1_000, so risk/unit=2 → cap_qty=1_000 ≥ qty → PASS.
    ctx = _ctx(max_risk_per_trade=1_000.0, current_equity=200_000.0)
    res = crm._g8_sl_sizing(
        _sig(entry_price=100.0, stop_loss_pct=0.02, risk_pct=1.0), ctx, qty=1000
    )
    assert res.decision is GateDecision.PASS


def test_g8_risk_pct_sizes_off_live_equity_not_static_capital() -> None:
    # The whole point of the change: capital is the static operator baseline, equity is
    # live. Set them far apart — sizing must follow equity (1% of 400_000 = 4_000 →
    # cap_qty = 4_000/2 = 2_000), not capital (1% of 200_000 would cap at 1_000).
    # max_daily_loss raised so the daily-budget cap can't bind before the risk cap.
    ctx = _ctx(
        max_risk_per_trade=1_000.0,
        total_capital=200_000.0,
        current_equity=400_000.0,
        max_daily_loss=100_000.0,
    )
    res = crm._g8_sl_sizing(
        _sig(entry_price=100.0, stop_loss_pct=0.02, risk_pct=1.0), ctx, qty=1500
    )
    assert res.decision is GateDecision.PASS  # 1500 <= cap_qty 2000
    assert effective_risk_cap(1.0, ctx) == 4_000.0  # equity-derived, not 2_000 off capital


def test_g8_risk_cap_scales_linearly_with_equity() -> None:
    # Same risk_pct, growing account → proportionally larger position. This is the
    # behavior that was missing: a flat rupee cap held size constant as capital grew.
    caps = [
        effective_risk_cap(0.5, _ctx(current_equity=eq))
        for eq in (500_000.0, 1_000_000.0, 2_000_000.0)
    ]
    assert caps == [2_500.0, 5_000.0, 10_000.0]


def test_g8_risk_pct_clamped_by_ceiling() -> None:
    # A misconfigured strategy asking for 10% is clamped to the operator ceiling (2%).
    ctx = _ctx(current_equity=1_000_000.0, max_risk_per_trade_pct_ceiling=2.0)
    assert effective_risk_cap(10.0, ctx) == 20_000.0


def test_g8_zero_ceiling_means_unconfigured_not_zero_risk() -> None:
    # 0.0 must read as "no ceiling set", never as "clamp risk to nothing" — otherwise
    # every trade sizes to zero the moment the key is absent from operator.json.
    ctx = _ctx(current_equity=1_000_000.0, max_risk_per_trade_pct_ceiling=0.0)
    assert effective_risk_cap(0.5, ctx) == 5_000.0


def test_g8_operator_default_risk_pct_applies_without_strategy_override() -> None:
    ctx = _ctx(current_equity=1_000_000.0, default_risk_pct=0.5)
    assert effective_risk_cap(None, ctx) == 5_000.0


def test_g8_no_risk_pct_uses_global_cap_unchanged() -> None:
    ctx = _ctx(max_risk_per_trade=1_000.0, total_capital=200_000.0)
    res = crm._g8_sl_sizing(_sig(entry_price=100.0, stop_loss_pct=0.02), ctx, qty=1000)
    assert res.decision is GateDecision.MODIFY
    assert res.modified_quantity == 500  # unchanged from pre-existing behavior


# ── G9 line names the LLM that answered (2026-09-24) ────────────────────────────────────


def test_g9_pass_reason_names_the_llm_verdict_and_provider() -> None:
    sig = _sig()
    sig.meta.update(
        {
            "llm_evaluated": True,
            "llm_flag": "CONFIRM",
            "llm_confidence": 0.75,
            "llm_provider": "gemini",
            "llm_latency_ms": 8394,
            "llm_reason": "clean breakdown",
        }
    )
    result = crm._g9_llm_confirmation(sig, _ctx(), 10)
    assert result.decision.value == "PASS"
    assert "CONFIRM" in result.reason and "conf=0.75" in result.reason
    assert "by gemini" in result.reason and "8394ms" in result.reason


def test_g9_reject_reason_names_the_llm_too() -> None:
    sig = _sig()
    sig.meta.update(
        {"llm_evaluated": True, "llm_flag": "REJECT", "llm_provider": "groq", "llm_reason": "weak"}
    )
    result = crm._g9_llm_confirmation(sig, _ctx(), 10)
    assert result.decision.value == "BLOCK" and "rejected by groq" in result.reason


def test_g4_blocks_a_strategy_in_its_blocked_regime_but_not_others() -> None:
    """llm_swing_agent may not open positions in STRONG_BEAR (config llm_swing.block_regimes)."""
    ctx = _ctx()
    ctx.blocked_regimes = {"llm_swing_agent": ["STRONG_BEAR"]}
    crm = CentralRiskManager()

    blocked = crm._g4_regime(_sig(strategy_name="llm_swing_agent", regime="STRONG_BEAR"), ctx, 10)
    assert blocked.decision == GateDecision.BLOCK and "STRONG_BEAR" in blocked.reason
    assert "exits unaffected" in blocked.reason

    # a different regime, or a different strategy, is untouched
    ok_regime = crm._g4_regime(_sig(strategy_name="llm_swing_agent", regime="BULL"), ctx, 10)
    assert ok_regime.decision == GateDecision.PASS
    other = crm._g4_regime(_sig(strategy_name="minervini_vcp", regime="STRONG_BEAR"), ctx, 10)
    assert other.decision == GateDecision.PASS


# ── Swing/intraday config/capital.json wiring (2026-10-07) ─────────────────────────


def test_g8_swing_uses_its_own_per_trade_rupee_cap_when_set() -> None:
    # entry_price=0 -> G8's risk_rupees/qty branch: risk_per_unit = 9,000/10 = 900.
    sig = _sig(engine="swing", entry_price=0.0, risk_rupees=9_000.0, stop_loss_pct=0.02)
    tight = crm.evaluate(sig, _ctx(max_swing_loss_per_trade=900.0))
    assert tight.approved is True
    assert tight.final_quantity == 1  # 900/900 = 1
    roomy = crm.evaluate(sig, _ctx(max_swing_loss_per_trade=90_000.0))
    assert roomy.final_quantity == 10  # full requested qty fits


def test_g8_intraday_uses_its_own_per_trade_rupee_cap_when_set() -> None:
    sig = _sig(engine="intraday", entry_price=0.0, risk_rupees=9_000.0, stop_loss_pct=0.02)
    tight = crm.evaluate(sig, _ctx(max_intraday_loss_per_trade=900.0))
    assert tight.final_quantity == 1
    roomy = crm.evaluate(sig, _ctx(max_intraday_loss_per_trade=90_000.0))
    assert roomy.final_quantity == 10


def test_g7_swing_open_risk_ceiling_blocks_when_breached() -> None:
    sig = _sig(engine="swing", risk_rupees=15_000.0)
    ctx = _ctx(max_swing_shared_risk=20_000.0, mode_open_risk_rupees={"paper:swing": 10_000.0})
    res = crm._g7_portfolio_margin(sig, ctx, qty=sig.intended_quantity)
    assert res.decision == GateDecision.BLOCK
    assert "Swing open risk" in res.reason


def test_g7_swing_open_risk_ceiling_passes_within_budget() -> None:
    sig = _sig(engine="swing", risk_rupees=5_000.0)
    ctx = _ctx(max_swing_shared_risk=20_000.0, mode_open_risk_rupees={"paper:swing": 10_000.0})
    res = crm._g7_portfolio_margin(sig, ctx, qty=sig.intended_quantity)
    assert res.decision == GateDecision.PASS


def test_g7_intraday_new_rupee_field_takes_priority_over_legacy_pct() -> None:
    sig = _sig(engine="intraday", risk_rupees=5_000.0)
    # Legacy pct x total_capital would allow this (huge); the new rupee field is tiny
    # and must win.
    ctx = _ctx(
        max_intraday_open_risk_pct=50.0,
        max_intraday_shared_risk=4_000.0,
        mode_open_risk_rupees={"paper:intraday": 2_000.0},
    )
    res = crm._g7_portfolio_margin(sig, ctx, qty=sig.intended_quantity)
    assert res.decision == GateDecision.BLOCK


def test_g1_intraday_daily_cap_blocks_per_mode_swing_unaffected() -> None:
    ctx = _ctx(
        max_intraday_daily_loss=5_000.0,
        mode_intraday_daily_loss_so_far={"paper": 5_000.0, "live": 0.0},
    )
    blocked = crm.evaluate(_sig(engine="intraday"), ctx)
    assert blocked.approved is False and blocked.blocking_gate == "G1"
    # Live mode's own counter is separate and still under cap.
    live_sig = _sig(engine="intraday")
    ctx2 = _ctx(
        max_intraday_daily_loss=5_000.0,
        mode_intraday_daily_loss_so_far={"paper": 5_000.0, "live": 0.0},
        segment_paper_mode={"intraday": False},
    )
    live_decision = crm.evaluate(live_sig, ctx2)
    assert live_decision.approved is True
    # Swing is NEVER affected by intraday's own daily loss, no matter how large.
    swing_decision = crm.evaluate(
        _sig(engine="swing"),
        _ctx(max_intraday_daily_loss=5_000.0, mode_intraday_daily_loss_so_far={"paper": 999_999.0}),
    )
    assert swing_decision.approved is True
