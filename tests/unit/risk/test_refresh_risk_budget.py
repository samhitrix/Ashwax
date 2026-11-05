"""orchestrator._refresh_options_risk_budget: the per-cycle refresh that keeps every
config/capital.json-derived rupee cap (options AND swing/intraday, despite the name)
in step with live equity. No dedicated test existed before 2026-10-07 even though
G1/G7/G8 depend on it every cycle -- this proves every field it sets, directly."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from src.orchestrator import _refresh_options_risk_budget
from src.risk import budget as budget_module


@pytest.fixture(autouse=True)
def _reset_equity():
    yield
    budget_module._current_equity = None


def test_refresh_populates_every_segments_rupee_fields() -> None:
    budget_module.set_current_equity(2_000_000.0)
    ctx = SimpleNamespace()
    _refresh_options_risk_budget(ctx)

    assert ctx.max_options_daily_loss == pytest.approx(10_000.0)
    assert ctx.max_options_structure_loss_per_trade == pytest.approx(15_000.0)
    assert ctx.max_swing_loss_per_trade == pytest.approx(4_000.0)
    assert ctx.max_swing_shared_risk == pytest.approx(20_000.0)
    assert ctx.max_intraday_loss_per_trade == pytest.approx(1_000.0)
    assert ctx.max_intraday_shared_risk == pytest.approx(2_000.0)
    assert ctx.max_intraday_daily_loss == pytest.approx(3_000.0)


def test_refresh_doubles_every_field_when_equity_doubles() -> None:
    budget_module.set_current_equity(2_000_000.0)
    ctx = SimpleNamespace()
    _refresh_options_risk_budget(ctx)
    low = vars(ctx).copy()

    budget_module.set_current_equity(4_000_000.0)
    _refresh_options_risk_budget(ctx)
    high = vars(ctx)

    for key in low:
        assert high[key] == pytest.approx(low[key] * 2), key
