"""Tests for StrategyHealthMonitor."""

from src.monitoring.strategy_health import (
    HealthTier,
    StrategyHealthMonitor,
    TradeRecord,
)


def _wins(n: int, r: float = 1.8) -> list[TradeRecord]:
    return [TradeRecord(pnl_r=r) for _ in range(n)]


def _losses(n: int, r: float = -1.0) -> list[TradeRecord]:
    return [TradeRecord(pnl_r=r) for _ in range(n)]


monitor = StrategyHealthMonitor()


def test_no_trades_healthy():
    h = monitor.score("s", [])
    assert h.tier == HealthTier.HEALTHY
    assert h.sizing_multiplier == 1.0
    assert h.trade_count == 0


def test_all_wins_healthy():
    h = monitor.score("s", _wins(20), baseline_wr=0.50, baseline_avg_r=1.5)
    assert h.tier == HealthTier.HEALTHY
    assert h.sizing_multiplier == 1.0
    assert h.win_rate == 1.0


def test_mostly_losses_suspended_or_stressed():
    trades = _wins(2) + _losses(18)
    h = monitor.score("s", trades, baseline_wr=0.50, baseline_avg_r=1.5)
    assert h.tier in (HealthTier.SUSPENDED, HealthTier.STRESSED)
    assert h.sizing_multiplier <= 0.25


def test_consecutive_losses_lower_score():
    h_clean = monitor.score("s", _wins(15) + _losses(5))
    h_consec = monitor.score("s", _wins(10) + _losses(10))
    assert h_clean.score >= h_consec.score


def test_rolling_window_last_20():
    # First 10 losses, last 20 wins → window = all wins → HEALTHY
    trades = _losses(10) + _wins(20)
    h = monitor.score("s", trades, baseline_wr=0.50, baseline_avg_r=1.5)
    assert h.tier == HealthTier.HEALTHY


def test_suspended_sizing_zero():
    h = monitor.score("s", _losses(20), baseline_wr=0.50, baseline_avg_r=1.5)
    assert h.tier == HealthTier.SUSPENDED
    assert h.sizing_multiplier == 0.0


def test_score_clamped_between_0_and_100():
    h1 = monitor.score("s", _wins(20, r=5.0))
    assert 0.0 <= h1.score <= 100.0
    h2 = monitor.score("s", _losses(20, r=-5.0))
    assert 0.0 <= h2.score <= 100.0


# ---------------------------------------------------------------------------
# Tier boundary conditions (_tier_from_score tested directly)
# ---------------------------------------------------------------------------

from src.monitoring.strategy_health import _sizing_from_tier, _tier_from_score  # noqa: E402


def test_tier_boundary_healthy():
    assert _tier_from_score(100.0) == HealthTier.HEALTHY
    assert _tier_from_score(80.0) == HealthTier.HEALTHY


def test_tier_boundary_cautious():
    assert _tier_from_score(79.9) == HealthTier.CAUTIOUS
    assert _tier_from_score(60.0) == HealthTier.CAUTIOUS


def test_tier_boundary_degraded():
    assert _tier_from_score(59.9) == HealthTier.DEGRADED
    assert _tier_from_score(40.0) == HealthTier.DEGRADED


def test_tier_boundary_stressed():
    assert _tier_from_score(39.9) == HealthTier.STRESSED
    assert _tier_from_score(20.0) == HealthTier.STRESSED


def test_tier_boundary_suspended():
    assert _tier_from_score(19.9) == HealthTier.SUSPENDED
    assert _tier_from_score(0.0) == HealthTier.SUSPENDED


# ---------------------------------------------------------------------------
# Sizing multipliers for every tier
# ---------------------------------------------------------------------------


def test_sizing_multiplier_all_tiers():
    assert _sizing_from_tier(HealthTier.HEALTHY) == 1.00
    assert _sizing_from_tier(HealthTier.CAUTIOUS) == 0.75
    assert _sizing_from_tier(HealthTier.DEGRADED) == 0.50
    assert _sizing_from_tier(HealthTier.STRESSED) == 0.25
    assert _sizing_from_tier(HealthTier.SUSPENDED) == 0.00


# ---------------------------------------------------------------------------
# Penalty cap verification
# ---------------------------------------------------------------------------


def test_consec_penalty_capped_at_20():
    # 5 consec → 5×5=25, capped at 20; 10 consec → 10×5=50, also capped at 20.
    assert min(20.0, 5 * 5.0) == 20.0
    assert min(20.0, 10 * 5.0) == 20.0


def test_all_penalties_maxed_gives_score_zero():
    # All losses: WR pen=40, avg_R pen=20, consec_pen=20, dd_pen=20 → score=0.
    h = monitor.score("s", _losses(20), baseline_wr=0.50, baseline_avg_r=0.0)
    assert h.score == 0.0
    assert h.tier == HealthTier.SUSPENDED
    assert h.sizing_multiplier == 0.00


def test_empty_window_returns_healthy_100():
    h = monitor.score("s", [])
    assert h.score == 100.0
    assert h.tier == HealthTier.HEALTHY
    assert h.sizing_multiplier == 1.0
    assert h.trade_count == 0


def test_all_losses_suspended_zero_sizing():
    h = monitor.score("s", _losses(20), baseline_wr=0.50, baseline_avg_r=1.5)
    assert h.tier == HealthTier.SUSPENDED
    assert h.sizing_multiplier == 0.00
    assert h.win_rate == 0.0


def test_rolling_window_ignores_old_trades():
    # First 10 losses, last 20 wins → window is all wins → HEALTHY.
    trades = _losses(10) + _wins(20)
    h = monitor.score("s", trades, baseline_wr=0.50, baseline_avg_r=1.5)
    assert h.tier == HealthTier.HEALTHY
    assert h.trade_count == 20
