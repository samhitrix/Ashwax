import json
from pathlib import Path

import pytest
from src.risk.crm import (
    CentralRiskManager,
    CRMContext,
    CRMSignal,
    GateDecision,
    engine_of,
    resolve_engine_counts,
)


@pytest.mark.parametrize(
    "tf,expected",
    [
        ("5MIN", "intraday"),
        ("15min", "intraday"),
        ("5M", "intraday"),
        ("15m", "intraday"),
        ("DAILY", "swing"),
        ("WEEKLY", "swing"),
        ("", "swing"),
        (None, "swing"),
    ],
)
def test_engine_of_classifies_timeframe(tf, expected):
    assert engine_of(tf) == expected


def _write_operator(tmp_path: Path, hard_limits: dict) -> Path:
    data = {
        "capital": {
            "total_capital_rupees": 1000000,
            "max_risk_per_trade_rupees": 5000,
            "max_daily_loss_rupees": 20000,
        },
        "hard_limits": hard_limits,
        "agent_permissions": {},
    }
    p = tmp_path / "operator.json"
    p.write_text(json.dumps(data))
    return p


def test_from_operator_json_parses_engine_blocks(tmp_path):
    p = _write_operator(
        tmp_path,
        {
            "max_open_positions": 8,
            "max_new_trades_per_day": 10,
            "min_risk_reward_ratio": 1.8,
            "max_stop_loss_pct": 8.0,
            "intraday": {
                "max_new_trades_per_day": 6,
                "max_new_trades_per_day_per_strategy": 2,
                "per_strategy_overrides": {"orb_intraday_smart": 3},
            },
            "swing": {
                "max_new_trades_per_day": 4,
                "max_new_trades_per_day_per_strategy": 2,
                "per_strategy_overrides": {},
                "max_open_positions": 5,
            },
        },
    )
    ctx = CRMContext.from_operator_json(p)
    assert ctx.max_intraday_trades_per_day == 6
    assert ctx.max_intraday_per_strategy == 2
    assert ctx.intraday_per_strategy_overrides == {"orb_intraday_smart": 3}
    assert ctx.max_swing_trades_per_day == 4
    assert ctx.max_swing_per_strategy == 2
    assert ctx.swing_per_strategy_overrides == {}
    assert ctx.max_swing_open_positions == 5


def test_from_operator_json_engine_blocks_absent_defaults_zero(tmp_path):
    p = _write_operator(
        tmp_path,
        {
            "max_open_positions": 8,
            "max_new_trades_per_day": 6,
            "min_risk_reward_ratio": 1.8,
            "max_stop_loss_pct": 8.0,
        },
    )
    ctx = CRMContext.from_operator_json(p)
    assert ctx.max_intraday_trades_per_day == 0
    assert ctx.max_swing_trades_per_day == 0
    assert ctx.intraday_per_strategy_overrides == {}
    assert ctx.per_strategy_trades_today == {}
    assert ctx.max_swing_open_positions == 0


def _ctx_with_budgets() -> CRMContext:
    return CRMContext(
        total_capital=1_000_000,
        max_risk_per_trade=5_000,
        max_daily_loss=20_000,
        max_open_positions=8,
        max_new_trades_per_day=10,
        min_risk_reward_ratio=1.8,
        max_stop_loss_pct=0.08,
        max_intraday_trades_per_day=6,
        max_intraday_per_strategy=2,
        intraday_per_strategy_overrides={},
        max_swing_trades_per_day=4,
        max_swing_per_strategy=2,
        swing_per_strategy_overrides={},
    )


def _sig(engine: str, strategy: str) -> CRMSignal:
    return CRMSignal(
        signal_id="t",
        symbol="NSE:RELIANCE",
        direction="BUY",
        strategy_name=strategy,
        intended_quantity=1,
        stop_loss_pct=0.02,
        target_pct=0.04,
        risk_rupees=100.0,
        entry_price=100.0,
        engine=engine,
    )


def test_g7_blocks_at_per_strategy_cap():
    crm = CentralRiskManager()
    ctx = _ctx_with_budgets()
    ctx.per_strategy_trades_today = {"orb_intraday_smart": 2}
    ctx.intraday_trades_today = 2
    res = crm._g7_portfolio_margin(_sig("intraday", "orb_intraday_smart"), ctx, qty=1)
    assert res.decision == GateDecision.BLOCK


def test_g7_per_strategy_override_beats_default():
    crm = CentralRiskManager()
    ctx = _ctx_with_budgets()
    ctx.intraday_per_strategy_overrides = {"orb_intraday_smart": 3}
    ctx.per_strategy_trades_today = {"orb_intraday_smart": 2}
    ctx.intraday_trades_today = 2
    # default would block at 2, but override raises the cap to 3 → PASS
    res = crm._g7_portfolio_margin(_sig("intraday", "orb_intraday_smart"), ctx, qty=1)
    assert res.decision == GateDecision.PASS


def test_g7_engines_are_decoupled():
    """Intraday at its full daily cap must NOT block a swing signal."""
    crm = CentralRiskManager()
    ctx = _ctx_with_budgets()
    ctx.intraday_trades_today = 6  # intraday exhausted
    ctx.per_strategy_trades_today = {"orb_intraday_smart": 2}
    res = crm._g7_portfolio_margin(_sig("swing", "minervini_vcp"), ctx, qty=1)
    assert res.decision == GateDecision.PASS


def test_g7_blocks_at_engine_total_cap():
    crm = CentralRiskManager()
    ctx = _ctx_with_budgets()
    ctx.swing_trades_today = 4
    res = crm._g7_portfolio_margin(_sig("swing", "minervini_vcp"), ctx, qty=1)
    assert res.decision == GateDecision.BLOCK


def test_resolve_engine_counts_splits_by_engine():
    per_strategy = {"orb_intraday_smart": 2, "minervini_vcp": 1, "ema_pullback": 1}
    name_to_engine = {
        "orb_intraday_smart": "intraday",
        "minervini_vcp": "swing",
        "ema_pullback": "swing",
    }
    intra, swing = resolve_engine_counts(per_strategy, name_to_engine)
    assert intra == 2
    assert swing == 2


def test_resolve_engine_counts_unknown_strategy_is_swing():
    intra, swing = resolve_engine_counts({"mystery": 3}, {})
    assert intra == 0
    assert swing == 3
