"""Tests for CRMContext.from_operator_json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.risk.crm import CRMContext


@pytest.fixture()
def operator_json(tmp_path: Path) -> Path:
    data = {
        "capital": {
            "total_capital_rupees": 200000,
            "max_risk_per_trade_rupees": 1000,
            "max_daily_loss_rupees": 2000,
        },
        "hard_limits": {
            "max_open_positions": 5,
            "max_new_trades_per_day": 6,
            "min_risk_reward_ratio": 1.8,
            "max_stop_loss_pct": 8.0,
        },
    }
    p = tmp_path / "operator.json"
    p.write_text(json.dumps(data))
    return p


def test_loads_capital_fields(operator_json: Path) -> None:
    ctx = CRMContext.from_operator_json(operator_json)
    assert ctx.total_capital == 200_000.0
    assert ctx.max_risk_per_trade == 1_000.0
    assert ctx.max_daily_loss == 2_000.0


def test_loads_hard_limits(operator_json: Path) -> None:
    ctx = CRMContext.from_operator_json(operator_json)
    assert ctx.max_open_positions == 5
    assert ctx.max_new_trades_per_day == 6
    assert ctx.min_risk_reward_ratio == 1.8


def test_stop_loss_pct_converted_to_decimal(operator_json: Path) -> None:
    # operator.json stores 8.0 (percent); CRMContext needs 0.08 (decimal)
    ctx = CRMContext.from_operator_json(operator_json)
    assert ctx.max_stop_loss_pct == pytest.approx(0.08)


def test_runtime_fields_default_to_zero(operator_json: Path) -> None:
    ctx = CRMContext.from_operator_json(operator_json)
    assert ctx.daily_loss_so_far == 0.0
    assert ctx.open_position_count == 0
    assert ctx.consecutive_losses == 0


def test_loads_real_operator_json() -> None:
    ctx = CRMContext.from_operator_json("config/operator.json")
    assert ctx.total_capital > 0
    assert 0 < ctx.max_stop_loss_pct < 1.0
