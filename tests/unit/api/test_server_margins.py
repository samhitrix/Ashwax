"""Dashboard API: /api/margins normalization.

Zerodha reports equity.available.cash = 0 for accounts whose free cash sits in
available.live_balance (observed live 2026-08-27: cash 0, live_balance 29785.30,
collateral 557857.99, net 587643.29). The dashboard read the raw field and showed
₹0 Available Cash all day, so the endpoint now also returns an `ashwax` block
carrying the same figures the engine trades on.

Since Kite became optional (spec 3.2) the figures come from the broker's get_margins()
(KiteAdapter -> _margin_from_kite), not a raw KiteConnect payload; the conftest serves that
seam from the faked `_kite()` used here.
"""

from __future__ import annotations

from typing import Any

import pytest
from src.api import server

_LIVE_PAYLOAD: dict[str, Any] = {
    "equity": {
        "net": 587643.29,
        "available": {"cash": 0, "live_balance": 29785.30, "collateral": 557857.99},
        "utilised": {"debits": 1200.0},
    }
}


class _FakeKite:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def margins(self) -> dict[str, Any]:
        return self._payload


async def test_margins_normalizes_zero_cash_to_live_balance(monkeypatch):
    monkeypatch.setattr(server, "_kite", lambda: _FakeKite(_LIVE_PAYLOAD))

    out = await server.get_margins()

    assert out["ashwax"]["available_cash"] == pytest.approx(29785.30)
    assert out["ashwax"]["collateral"] == pytest.approx(557857.99)
    assert out["ashwax"]["used_margin"] == pytest.approx(1200.0)
    assert out["ashwax"]["net"] == pytest.approx(587643.29)


async def test_margins_names_the_broker_and_lists_every_configured_one(monkeypatch):
    """The dashboard reads only `ashwax`; `broker`/`brokers` are additive."""
    monkeypatch.setattr(server, "_kite", lambda: _FakeKite(_LIVE_PAYLOAD))

    out = await server.get_margins()

    assert out["broker"] == "kite"
    assert out["brokers"]["kite"] == out["ashwax"]


async def test_margins_prefers_explicit_cash(monkeypatch):
    payload = {
        "equity": {
            "net": 1000.0,
            "available": {"cash": 750.0, "live_balance": 0, "collateral": 0},
            "utilised": {"debits": 0},
        }
    }
    monkeypatch.setattr(server, "_kite", lambda: _FakeKite(payload))

    assert (await server.get_margins())["ashwax"]["available_cash"] == pytest.approx(750.0)


async def test_margins_broker_error_has_no_ashwax_block(monkeypatch):
    """On a broker failure the dashboard must show the empty state, not ₹0 funds."""

    def _boom():
        raise RuntimeError("token expired")

    monkeypatch.setattr(server, "_kite", _boom)

    out = await server.get_margins()

    assert out["error"] == "token expired"
    assert "ashwax" not in out
