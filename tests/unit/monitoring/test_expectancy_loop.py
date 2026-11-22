"""Tests for ExpectancyEngine — Phase 4 Item 1."""

from __future__ import annotations

from src.monitoring.expectancy_engine import ExpectancyEngine, ExpectancyStatus


def _make_engine(
    wins: int, losses: int, win_r: float = 2.0, loss_r: float = -1.0
) -> ExpectancyEngine:
    engine = ExpectancyEngine()
    for _ in range(wins):
        engine.record("VCP", win_r)
    for _ in range(losses):
        engine.record("VCP", loss_r)
    return engine


def test_healthy_with_positive_expectancy() -> None:
    engine = _make_engine(wins=15, losses=5)
    result = engine.evaluate("VCP")
    assert result.status == ExpectancyStatus.HEALTHY
    assert result.current_e > 0
    assert result.should_pause is False


def test_empty_strategy_returns_healthy() -> None:
    engine = ExpectancyEngine()
    result = engine.evaluate("UNKNOWN")
    assert result.status == ExpectancyStatus.HEALTHY
    assert result.trade_count == 0
    assert result.should_pause is False


def test_warning_when_current_e_below_half_alltime() -> None:
    engine = ExpectancyEngine()
    # Build a good historical record first
    for _ in range(40):
        engine.record("S1", 3.0)
    for _ in range(10):
        engine.record("S1", -1.0)
    # Add a bad recent window (20 trades of losses)
    for _ in range(20):
        engine.record("S1", -1.5)
    result = engine.evaluate("S1")
    assert result.status in (ExpectancyStatus.WARNING, ExpectancyStatus.PAUSED)


def test_paused_after_10_consecutive_negative_windows() -> None:
    engine = ExpectancyEngine()
    # 200 losing trades → many rolling 20-trade windows all negative
    for _ in range(200):
        engine.record("LOSER", -1.0)
    result = engine.evaluate("LOSER")
    assert result.should_pause is True
    assert result.status == ExpectancyStatus.PAUSED
    assert result.consecutive_negative >= 10


def test_record_accumulates_trades() -> None:
    engine = ExpectancyEngine()
    engine.record("A", 1.0)
    engine.record("A", -0.5)
    engine.record("A", 2.0)
    assert engine.evaluate("A").trade_count == 3


def test_seen_ids_dedup_logic() -> None:
    """Simulate the _seen_ids guard used in _expectancy_loop."""
    seen: set[str] = set()
    trades = [
        {"trade_id": "T1", "strategy_name": "VCP", "pnl_r": 1.5},
        {"trade_id": "T1", "strategy_name": "VCP", "pnl_r": 1.5},  # duplicate
        {"trade_id": "T2", "strategy_name": "VCP", "pnl_r": -0.8},
    ]
    engine = ExpectancyEngine()
    for t in trades:
        tid = str(t.get("trade_id", ""))
        if not tid or tid in seen:
            continue
        seen.add(tid)
        engine.record(str(t["strategy_name"]), float(t["pnl_r"]))

    assert engine.evaluate("VCP").trade_count == 2  # T1 once + T2


def test_multiple_strategies_tracked_independently() -> None:
    engine = ExpectancyEngine()
    engine.record("VCP", 2.0)
    engine.record("EMA", -1.0)
    assert engine.evaluate("VCP").current_e > 0
    assert engine.evaluate("EMA").current_e <= 0
    assert engine.evaluate("VCP").trade_count == 1
    assert engine.evaluate("EMA").trade_count == 1
