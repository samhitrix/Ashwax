"""Dashboard API: /api/options_funds_margins -- Funds & Margins strip, paper
and live, recomputed independently of the orchestrator's in-process state
(API and orchestrator are separate processes)."""

from __future__ import annotations

from typing import Any

import pytest
from src.api import server


class _FakePool:
    def __init__(self, total_committed: float) -> None:
        self._total = total_committed

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        return {"total": self._total}


_KITE_MARGINS = {
    "equity": {
        "net": 950_000.0,
        "available": {"cash": 0.0, "live_balance": 210_558.0, "collateral": 739_442.0},
        "utilised": {"debits": 185_000.0},
    }
}


@pytest.mark.asyncio
async def test_paper_side_derives_from_options_capital_and_open_margin(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _FakePool(24_206.0))
    from src.options import seller_settings as _ss

    monkeypatch.setattr(_ss, "load_settings", lambda: {"options_capital_rupees": 1_000_000.0})
    monkeypatch.setattr(server, "_broker_cached", lambda *a, **k: _KITE_MARGINS)

    out = await server.get_options_funds_margins()

    assert out["paper"]["fundAvailable"] == 1_000_000.0
    assert out["paper"]["marginAvailable"] == 1_000_000.0 - 24_206.0
    assert out["paper"]["sharedRiskCeiling"] > 0


@pytest.mark.asyncio
async def test_live_side_reads_from_broker_margins(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _FakePool(0.0))
    from src.options import seller_settings as _ss

    monkeypatch.setattr(_ss, "load_settings", lambda: {"options_capital_rupees": 1_000_000.0})
    monkeypatch.setattr(server, "_broker_cached", lambda *a, **k: _KITE_MARGINS)

    out = await server.get_options_funds_margins()

    assert out["live"]["fundAvailable"] == 210_558.0  # free cash (live_balance fallback)
    assert out["live"]["marginAvailable"] == 950_000.0  # total margin capacity (equity.net)
    # one account-wide ceiling, same for both modes
    assert out["live"]["sharedRiskCeiling"] == out["paper"]["sharedRiskCeiling"]


@pytest.mark.asyncio
async def test_broker_failure_leaves_live_figures_at_zero_not_crashing(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _FakePool(0.0))
    from src.options import seller_settings as _ss

    monkeypatch.setattr(_ss, "load_settings", lambda: {"options_capital_rupees": 1_000_000.0})

    def _raise(*a: Any, **k: Any) -> Any:
        raise RuntimeError("kite down")

    monkeypatch.setattr(server, "_broker_cached", _raise)

    out = await server.get_options_funds_margins()

    assert out["live"]["fundAvailable"] == 0.0
    assert out["live"]["marginAvailable"] == 0.0
    # ceiling still computed (falls back to capital.json's configured fallback equity)
    assert out["paper"]["sharedRiskCeiling"] > 0


@pytest.mark.asyncio
async def test_no_pool_leaves_paper_margin_committed_at_zero(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", None)
    from src.options import seller_settings as _ss

    monkeypatch.setattr(_ss, "load_settings", lambda: {"options_capital_rupees": 1_000_000.0})
    monkeypatch.setattr(server, "_broker_cached", lambda *a, **k: _KITE_MARGINS)

    out = await server.get_options_funds_margins()

    assert out["paper"]["marginAvailable"] == 1_000_000.0
