"""Unit tests for src/risk/circuit_breaker.py."""

from __future__ import annotations

from src.risk.circuit_breaker import CircuitBreakerEngine

engine = CircuitBreakerEngine()


def test_not_tripped_below_max() -> None:
    result = engine.evaluate(consecutive_losses=2, max_consecutive_losses=3)
    assert result.tripped is False
    assert result.reason == ""


def test_tripped_at_max() -> None:
    result = engine.evaluate(consecutive_losses=3, max_consecutive_losses=3)
    assert result.tripped is True
    assert "3" in result.reason


def test_tripped_above_max() -> None:
    result = engine.evaluate(consecutive_losses=5, max_consecutive_losses=3)
    assert result.tripped is True


def test_zero_losses_not_tripped() -> None:
    result = engine.evaluate(consecutive_losses=0, max_consecutive_losses=3)
    assert result.tripped is False


def test_g2_uses_engine() -> None:
    from src.risk.crm import CentralRiskManager, CRMContext, CRMSignal, GateDecision

    crm = CentralRiskManager()
    ctx = CRMContext(
        total_capital=200_000.0,
        max_risk_per_trade=1_000.0,
        max_daily_loss=2_000.0,
        max_open_positions=5,
        max_new_trades_per_day=6,
        min_risk_reward_ratio=1.8,
        max_stop_loss_pct=0.08,
        current_equity=200_000.0,
        consecutive_losses=3,
        max_consecutive_losses=3,
        session_open_hour=0,
        session_close_hour=24,
    )
    sig = CRMSignal(
        signal_id="SIG",
        symbol="INFY",
        direction="BUY",
        strategy_name="ORB",
        intended_quantity=10,
        stop_loss_pct=0.02,
        target_pct=0.04,
        risk_rupees=500.0,
        meta={"autonomy_mode": "autonomous"},
    )
    decision = crm.evaluate(sig, ctx)
    assert decision.approved is False
    assert decision.blocking_gate == "G2"
    g2 = next(r for r in decision.gate_results if r.gate_id == "G2")
    assert g2.decision == GateDecision.BLOCK
