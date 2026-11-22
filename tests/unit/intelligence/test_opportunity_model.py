"""Tests for OpportunityValueModel — Phase 4 Item 5."""

from __future__ import annotations

from src.intelligence.opportunity_model import OpportunityValueModel


def _eval(regime: str, health: str = "HEALTHY", move: float = 0.0) -> bool:
    model = OpportunityValueModel()
    base = 80.0 if health == "HEALTHY" else 70.0 if health == "DEGRADED" else 55.0
    result = model.evaluate(
        signal_id="SIG1",
        symbol="RELIANCE",
        base_score=base,
        context={"regime": regime, "price_move_since_signal": move},
    )
    return result.should_take


def test_bull_regime_healthy_strategy_passes() -> None:
    assert _eval("BULL") is True


def test_bear_regime_blocks_even_healthy_strategy() -> None:
    # 80 * 0.5 = 40 < 65 → should_take=False
    assert _eval("BEAR") is False


def test_unknown_regime_blocks() -> None:
    # 80 * 0.3 = 24 < 65 → should_take=False
    assert _eval("UNKNOWN") is False


def test_sideways_regime_healthy_just_below_threshold() -> None:
    # 80 * 0.8 = 64 < 65 → should_take=False
    assert _eval("SIDEWAYS") is False


def test_degraded_strategy_bull_regime_passes() -> None:
    # 70 * 1.0 = 70 >= 65 → True
    assert _eval("BULL", health="DEGRADED") is True


def test_suspended_strategy_bull_regime_blocked() -> None:
    # 55 * 1.0 = 55 < 65 → False
    assert _eval("BULL", health="SUSPENDED") is False


def test_fomo_penalty_small_move_no_block() -> None:
    # move=0.01 (1%) → penalty = min(20, 0.01*500) = 5 → 80-5=75 >= 65
    assert _eval("BULL", move=0.01) is True


def test_fomo_penalty_large_move_blocks() -> None:
    # move=0.05 (5%) > 0.02 → penalty = min(20, 0.05*500) = 20 → 80-20=60 < 65
    assert _eval("BULL", move=0.05) is False


def test_score_fields_populated() -> None:
    model = OpportunityValueModel()
    result = model.evaluate("S1", "SYM", 80.0, {"regime": "BULL", "price_move_since_signal": 0.0})
    assert result.signal_id == "S1"
    assert result.symbol == "SYM"
    assert result.base_score == 80.0
    assert result.fomo_penalty == 0.0
    assert result.adjusted_score == 80.0
    assert result.should_take is True
