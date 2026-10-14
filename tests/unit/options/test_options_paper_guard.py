"""Paper mode must route through the SAME options structure selection as live
(src/orchestrator.py) — safety comes from the injected broker (PaperAdapter
simulates fills against real market data and never places a live order), not
from a paper_mode special-case in the routing function itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.orchestrator import _try_options_structure


def _crm_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        max_risk_per_trade=3000.0,
        can_trade_options=True,
        open_position_count=1,
        max_open_positions=5,
    )


def _strategy() -> SimpleNamespace:
    meta = SimpleNamespace(product="DELIVERY", name="vcp")
    return SimpleNamespace(spec=SimpleNamespace(meta=meta, name="vcp"))


async def test_options_structure_not_skipped_in_paper_mode() -> None:
    provider = AsyncMock()
    provider.get_chain.return_value = None  # no chain available -> falls back to equity anyway
    shared = {
        "options": {
            "enabled": True,
            "provider": provider,
            "selector": None,
            "gate": None,
            "executor": None,
            "repo": None,
            "cfg": {"min_days_to_expiry": 14},
        }
    }

    placed = await _try_options_structure(
        shared=shared,
        crm_ctx=_crm_ctx(),
        strategy=_strategy(),
        symbol="NSE:INFY",
        direction="BUY",
        entry=100.0,
        sl_pct=0.02,
        tgt_pct=0.04,
        conviction=0.7,
        earnings_days=999,
        signal_id="SIG1",
        paper_mode=True,
        paper_engine=None,
    )

    assert placed is False  # no chain -> equity path (unrelated to paper_mode)
    provider.get_chain.assert_awaited_once()  # paper mode DOES fetch the chain
