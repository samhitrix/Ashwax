"""Swing sizing must use the SAME per-trade rupee cap CRM G8 applies.

Found 2026-10-09: config/capital.json swing.max_loss_per_trade (0.2%, Rs 4,000 at Rs 20L)
was wired into G8 on 2026-10-07, but the swing sizer still used the legacy
effective_risk_cap (~Rs 2,000). G8 can only size DOWN, so every swing trade was sized
at about half the configured risk.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from src.orchestrator import _execute_swing_signal
from src.risk.crm import effective_risk_cap, swing_risk_cap

from tests.unit.execution.test_swing_signal import _NOW, _SIG, _capturing_crm, _shared


def _ctx(**kw: float) -> SimpleNamespace:
    base = dict(
        current_equity=2_000_000.0,
        max_risk_per_trade=2000.0,
        default_risk_pct=0.0,
        max_risk_per_trade_pct_ceiling=0.0,
        max_swing_loss_per_trade=4000.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_swing_cap_is_the_capital_json_per_trade_figure() -> None:
    assert swing_risk_cap(None, _ctx()) == 4000.0


def test_swing_cap_falls_back_to_legacy_when_unset() -> None:
    ctx = _ctx(max_swing_loss_per_trade=0.0)
    assert swing_risk_cap(0.5, ctx) == effective_risk_cap(0.5, ctx)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_executor_sizes_swing_off_the_capital_json_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Legacy cap Rs 2,000, capital.json per-trade cap Rs 3,000: entry 100 / SL 95 -> 600 qty.
    # The ctx starts WITHOUT the field: the budget refresh must run before sizing.
    import src.orchestrator as orch

    def _refresh(ctx):  # type: ignore[no-untyped-def]
        ctx.max_swing_loss_per_trade = 3000.0

    monkeypatch.setattr(orch, "_refresh_options_risk_budget", _refresh)
    crm, captured = _capturing_crm()
    shared = _shared(crm=crm)
    await _execute_swing_signal(shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW)
    assert captured["sig"].intended_quantity == 600
    assert captured["sig"].risk_rupees == pytest.approx(3000.0)
