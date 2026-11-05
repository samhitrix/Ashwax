"""Intraday sizing must use the SAME per-trade rupee cap CRM G8 applies.

Found live 2026-10-09 (JSWINFRA): config/capital.json intraday.max_loss_per_trade (0.05%,
RsN) was wired into G8 on earlier but the intraday sizer still used the legacy
effective_risk_cap (~RsN). G7's open-risk check runs BEFORE G8 trims the size, so the
oversized intended trade (RsN) was blocked: open RsN + RsN > cap RsN
although a correctly-sized RsN trade (RsN) fits.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from src.risk.crm import (
    effective_risk_cap,
    intraday_risk_cap,
)
from src.risk.position_sizing import intraday_position_size

from tests.unit.execution.test_intraday_g9_scoped import _run, _shared


def _ctx(**kw: float) -> SimpleNamespace:
    base = dict(
        current_equity=2_000_000.0,
        max_risk_per_trade=2000.0,
        default_risk_pct=0.0,
        max_risk_per_trade_pct_ceiling=0.0,
        max_intraday_loss_per_trade=1000.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_intraday_cap_is_the_capital_json_per_trade_figure() -> None:
    assert intraday_risk_cap(_ctx()) == 1000.0


def test_intraday_cap_falls_back_to_legacy_when_unset() -> None:
    ctx = _ctx(max_intraday_loss_per_trade=0.0)
    assert intraday_risk_cap(ctx) == effective_risk_cap(None, ctx) == 2000.0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_executor_sizes_intraday_off_the_capital_json_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Legacy cap RsN capital.json per-trade cap RsN: entry 100 / SL 99 -> 500 qty.
    # The ctx starts WITHOUT the field: the budget refresh must run before sizing.
    import src.orchestrator as orch

    def _refresh(ctx):  # type: ignore[no-untyped-def]
        ctx.max_intraday_loss_per_trade = 500.0

    monkeypatch.setattr(orch, "_refresh_options_risk_budget", _refresh)
    shared, _, captured = _shared()
    await _run(shared, strategy="news_catalyst_momentum")
    assert captured["sig"].risk_rupees == pytest.approx(500.0)


def test_jswinfra_case_correct_size_passes_g7_open_risk() -> None:
    # The live block: open RsN shared cap RsN (N% of RsN), entry 340 / SL N.
    entry, sl = 340.0, 337.70
    qty = intraday_position_size(entry, sl, intraday_risk_cap(_ctx()), 1e9)
    assert qty * (entry - sl) <= 1000.0
    assert 847.0 + qty * (entry - sl) <= 2000.0
