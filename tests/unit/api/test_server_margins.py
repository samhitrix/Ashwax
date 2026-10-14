"""Dashboard API: /api/margins normalization.

Zerodha reports equity.available.cash = 0 for accounts whose free cash sits in
available.live_balance (observed live 2026-08-27: cash 0, live_balance 29785.30,
collateral 557857.99, net 587643.29). The dashboard read the raw field and showed
₹0 Available Cash all day, so the endpoint now also returns an `ashwax` block
carrying the same figures the engine trades on.
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


def test_margins_normalizes_zero_cash_to_live_balance(monkeypatch):
    monkeypatch.setattr(server, "_kite", lambda: _FakeKite(_LIVE_PAYLOAD))

    out = server.get_margins()

    assert out["ashwax"]["available_cash"] == pytest.approx(29785.30)
    assert out["ashwax"]["collateral"] == pytest.approx(557857.99)
    assert out["ashwax"]["used_margin"] == pytest.approx(1200.0)
    assert out["ashwax"]["net"] == pytest.approx(587643.29)


def test_margins_keeps_raw_payload_intact(monkeypatch):
    """Raw Kite fields stay untouched so the quirk remains visible when debugging."""
    monkeypatch.setattr(server, "_kite", lambda: _FakeKite(_LIVE_PAYLOAD))

    out = server.get_margins()

    assert out["equity"]["available"]["cash"] == 0
    assert out["equity"]["net"] == pytest.approx(587643.29)


def test_margins_prefers_explicit_cash(monkeypatch):
    payload = {
        "equity": {
            "net": 1000.0,
            "available": {"cash": 750.0, "live_balance": 0, "collateral": 0},
            "utilised": {"debits": 0},
        }
    }
    monkeypatch.setattr(server, "_kite", lambda: _FakeKite(payload))

    assert server.get_margins()["ashwax"]["available_cash"] == pytest.approx(750.0)


def test_margins_broker_error_has_no_ashwax_block(monkeypatch):
    """On a broker failure the dashboard must show the empty state, not ₹0 funds."""

    def _boom():
        raise RuntimeError("token expired")

    monkeypatch.setattr(server, "_kite", _boom)

    out = server.get_margins()

    assert out == {"error": "token expired"}
    assert "ashwax" not in out
