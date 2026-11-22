"""Tests for src.orchestrator._fetch_closed_options_from_db.

Options positions live in options_positions, never in paper_trades/trades, so every
options strategy was invisible to the Intelligence/Learning tabs. Made-up values only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
import src.orchestrator as orch_mod


def _row(**kw: Any) -> dict[str, Any]:
    base = {
        "position_id": "OPT-TEST1",
        "strategy_type": "bull_put_spread",
        "realized_pnl": 500.0,
        "max_loss": 2000.0,
        "charges_taxes": 40.0,
        "opened_at": datetime(2026, 1, 5, 9, 30),
        "closed_at": datetime(2026, 1, 5, 15, 0),
        "entry_regime": "NEUTRAL",
        "is_paper": True,
    }
    base.update(kw)
    return base


class _FakePool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append(query)
        return self.rows


@pytest.mark.asyncio
async def test_maps_desk_structure_to_registry_strategy_and_r_over_max_loss() -> None:
    out = await orch_mod._fetch_closed_options_from_db(_FakePool([_row()]), mode="paper")
    assert len(out) == 1
    t = out[0]
    assert t["strategy_name"] == "options_seller_desk_v3"
    assert t["structure"] == "bull_put_spread"
    assert t["asset_class"] == "options"
    assert t["pnl_r"] == pytest.approx(0.25)
    assert t["regime_at_entry"] == "NEUTRAL"
    assert t["gross_pnl"] == pytest.approx(540.0)
    assert t["agent_trade_id"] == "OPT-TEST1"


@pytest.mark.asyncio
async def test_hosted_strategy_keeps_its_registry_name() -> None:
    rows = [_row(strategy_type="options_intraday_short_premium2", realized_pnl=-300.0)]
    out = await orch_mod._fetch_closed_options_from_db(_FakePool(rows), mode="live")
    assert out[0]["strategy_name"] == "options_intraday_short_premium2"
    assert out[0]["pnl_r"] == pytest.approx(-0.15)


@pytest.mark.parametrize(
    ("mode", "fragment"),
    [("live", "is_paper = FALSE"), ("paper", "is_paper = TRUE")],
)
@pytest.mark.asyncio
async def test_mode_filters_on_is_paper(mode: str, fragment: str) -> None:
    pool = _FakePool([])
    await orch_mod._fetch_closed_options_from_db(pool, mode=mode)
    assert fragment in pool.queries[0]


@pytest.mark.asyncio
async def test_missing_regime_and_zero_max_loss_do_not_crash() -> None:
    rows = [_row(entry_regime=None, max_loss=0, charges_taxes=None)]
    out = await orch_mod._fetch_closed_options_from_db(_FakePool(rows))
    assert out[0]["regime_at_entry"] == "UNKNOWN"
    assert out[0]["gross_pnl"] is None


@pytest.mark.asyncio
async def test_no_pool_or_db_error_returns_empty() -> None:
    class _Boom:
        async def fetch(self, *a: Any) -> list[Any]:
            raise RuntimeError("db down")

    assert await orch_mod._fetch_closed_options_from_db(None) == []
    assert await orch_mod._fetch_closed_options_from_db(_Boom()) == []
