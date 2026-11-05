"""Health tier fixes: sample-size floor, own-backtest baseline, paper-only cap, paper recovery."""

from __future__ import annotations

from src.monitoring.strategy_health import HealthTier, StrategyHealthMonitor, TradeRecord
from src.orchestrator import _effective_health


def _t(*r: float) -> list[TradeRecord]:
    return [TradeRecord(pnl_r=x) for x in r]


def _mons() -> tuple[StrategyHealthMonitor, StrategyHealthMonitor]:
    return (
        StrategyHealthMonitor(evaluation_window_trades=20, min_trades_for_suspension=10),
        StrategyHealthMonitor(evaluation_window_trades=10, min_trades_for_suspension=0),
    )


def test_three_trade_sample_cannot_suspend():
    # The real minervini_vcp live record that suspended it on 2026-09-23.
    m, _ = _mons()
    h = m.score("s", _t(0.7269, -0.7319, -1.0104))
    assert h.tier == HealthTier.DEGRADED and h.sizing_multiplier > 0  # not blocked


def test_full_sample_of_losers_still_suspends():
    m, _ = _mons()
    h = m.score("s", _t(*([-1.0] * 12)))
    assert h.tier == HealthTier.SUSPENDED and h.sizing_multiplier == 0


def test_own_backtest_baseline_is_scored_against_not_50pct_1_5r():
    m, _ = _mons()
    trades = _t(*([1.0, -1.0] * 5))  # 50% WR, 0R expectancy over 10 trades
    default = m.score("s", trades)
    own = m.score("s", trades, baseline_wr=0.50, baseline_avg_r=0.0)
    assert own.score > default.score  # the generic 1.5R baseline over-penalises a 0R strategy


def test_paper_only_strategy_is_never_blocked_and_scored_on_paper():
    m, rec = _mons()
    live_bad = _t(*([-1.0] * 12))
    paper_bad = _t(*([-1.0] * 12))
    h, note = _effective_health(m, rec, "s", live_bad, paper_bad, True, {}, 10)
    assert h.tier == HealthTier.DEGRADED and "basis=paper" in note and "capped_from" in note
    # good paper record: real tier, no cap needed
    h2, _ = _effective_health(m, rec, "s", live_bad, _t(*([1.0] * 12)), True, {}, 10)
    assert h2.tier == HealthTier.HEALTHY


def test_live_suspension_lifts_only_with_enough_healthy_paper_trades():
    m, rec = _mons()
    live_bad = _t(*([-1.0] * 12))
    h, note = _effective_health(m, rec, "s", live_bad, _t(*([1.0] * 4)), False, {}, 10)
    assert h.tier == HealthTier.SUSPENDED and "no recovery evidence" in note
    h, note = _effective_health(m, rec, "s", live_bad, _t(*([-1.0] * 10)), False, {}, 10)
    assert h.tier == HealthTier.SUSPENDED and "paper_recovery_score" in note
    h, note = _effective_health(m, rec, "s", live_bad, _t(*([1.0] * 10)), False, {}, 10)
    assert h.tier == HealthTier.CAUTIOUS and "lifted to CAUTIOUS" in note
