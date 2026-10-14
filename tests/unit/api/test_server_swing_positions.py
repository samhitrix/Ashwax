"""Dashboard API: /api/swing_positions LTP resolution across both symbol forms.

`trades.symbol` is bare ("IFCI") because Kite's order API takes a bare tradingsymbol.
`paper_trades.symbol` is exchange-qualified ("NSE:IFCI") since it never reaches that
API. kite.ltp() answers qualified keys only and silently omits anything else, so the
endpoint's "already exchange-qualified" assumption left every LIVE row with no quote:
LTP, P&L, %Net Change and To Target all rendered "–" on exactly the real-money
positions (observed 2026-08-28 with IFCI and JINDALSAW).
"""

from __future__ import annotations

from typing import Any

import pytest
from src.api import server


class _FakePool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return self._rows


class _FakeKite:
    """Mirrors Kite: only exchange-qualified keys come back."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def ltp(self, symbols: list[str]) -> dict[str, Any]:
        self.asked = list(symbols)
        prices = {"NSE:IFCI": 89.96, "NSE:RADICO": 4605.0}
        return {s: {"last_price": prices[s]} for s in symbols if s in prices}


def _row(symbol: str, *, is_paper: bool, fill: float, qty: int) -> dict[str, Any]:
    return {
        "agent_trade_id": f"TRD-{symbol}",
        "symbol": symbol,
        "strategy_name": "minervini_vcp",
        "direction": "long",
        "quantity": qty,
        "fill_price": fill,
        "exit_price": None,
        "current_sl": 77.40,
        "current_target": 101.45,
        "partial_pnl_booked": 0,
        "entry_timestamp": None,
        "exit_timestamp": None,
        "is_paper": is_paper,
    }


@pytest.mark.asyncio
async def test_bare_live_symbol_still_resolves_an_ltp(monkeypatch):
    kite = _FakeKite()
    monkeypatch.setattr(
        server, "_db_pool", _FakePool([_row("IFCI", is_paper=False, fill=84.27, qty=145)])
    )
    monkeypatch.setattr(server, "_kite", lambda: kite)

    out = await server.get_swing_positions()

    assert out[0]["ltp"] == pytest.approx(89.96)
    assert out[0]["pnl"] == pytest.approx((89.96 - 84.27) * 145, rel=1e-6)
    assert kite.asked == ["NSE:IFCI"]  # queried qualified, not bare


@pytest.mark.asyncio
async def test_qualified_paper_symbol_is_not_double_prefixed(monkeypatch):
    kite = _FakeKite()
    monkeypatch.setattr(
        server, "_db_pool", _FakePool([_row("NSE:RADICO", is_paper=True, fill=4443.2, qty=2)])
    )
    monkeypatch.setattr(server, "_kite", lambda: kite)

    out = await server.get_swing_positions()

    assert kite.asked == ["NSE:RADICO"]  # NOT "NSE:NSE:RADICO"
    assert out[0]["ltp"] == pytest.approx(4605.0)


@pytest.mark.asyncio
async def test_both_symbol_forms_resolve_in_one_call(monkeypatch):
    """The real table is a UNION of both conventions — one quote call must serve both."""
    kite = _FakeKite()
    monkeypatch.setattr(
        server,
        "_db_pool",
        _FakePool(
            [
                _row("IFCI", is_paper=False, fill=84.27, qty=145),
                _row("NSE:RADICO", is_paper=True, fill=4443.2, qty=2),
            ]
        ),
    )
    monkeypatch.setattr(server, "_kite", lambda: kite)

    out = await server.get_swing_positions()

    assert {r["symbol"]: r["ltp"] for r in out} == {
        "IFCI": pytest.approx(89.96),
        "NSE:RADICO": pytest.approx(4605.0),
    }
    assert sorted(kite.asked) == ["NSE:IFCI", "NSE:RADICO"]


@pytest.mark.asyncio
async def test_unquotable_symbol_reports_none_not_a_fake_zero(monkeypatch):
    """A missing quote must render as empty, never as a 0.0 LTP or a bogus P&L."""
    monkeypatch.setattr(
        server, "_db_pool", _FakePool([_row("DELISTED", is_paper=False, fill=10.0, qty=5)])
    )
    monkeypatch.setattr(server, "_kite", lambda: _FakeKite())

    out = await server.get_swing_positions()

    assert out[0]["ltp"] is None
    assert out[0]["pnl"] is None
