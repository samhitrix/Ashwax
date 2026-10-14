"""/api/positions: Kite positions() serves a sticky last_price (measured frozen
for 18s while ltp() moved), so live rows must be overlaid with a fresh ltp()."""

from __future__ import annotations

from typing import Any

import pytest
from src.api import server


class _Kite:
    def __init__(self, quotes: dict[str, Any] | Exception) -> None:
        self._quotes = quotes
        self.asked: list[str] = []

    def ltp(self, keys: list[str]) -> dict[str, Any]:
        self.asked = keys
        if isinstance(self._quotes, Exception):
            raise self._quotes
        return self._quotes


def _data() -> dict[str, Any]:
    row = {
        "tradingsymbol": "LAURUSLABS",
        "exchange": "NSE",
        "quantity": 14,
        "last_price": 2019.4,
        "pnl": 9.8,
    }
    return {"net": [dict(row)], "day": [dict(row)]}


@pytest.mark.asyncio
async def test_long_row_gets_fresh_price_and_shifted_pnl(monkeypatch: pytest.MonkeyPatch) -> None:
    kite = _Kite({"NSE:LAURUSLABS": {"last_price": 2020.4}})
    monkeypatch.setattr(server, "_kite", lambda: kite)
    data = _data()
    await server._overlay_live_ltp(data)
    assert kite.asked == ["NSE:LAURUSLABS"]
    for p in data["net"] + data["day"]:
        assert p["last_price"] == 2020.4
        assert p["pnl"] == pytest.approx(9.8 + (2020.4 - 2019.4) * 14)


@pytest.mark.asyncio
async def test_short_row_pnl_moves_against_price_rise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_kite", lambda: _Kite({"NSE:X": {"last_price": 101.0}}))
    data = {
        "net": [
            {
                "tradingsymbol": "X",
                "exchange": "NSE",
                "quantity": -10,
                "last_price": 100.0,
                "pnl": 0.0,
            }
        ]
    }
    await server._overlay_live_ltp(data)
    assert data["net"][0]["pnl"] == pytest.approx(-10.0)


@pytest.mark.asyncio
async def test_quote_failure_keeps_kite_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_kite", lambda: _Kite(RuntimeError("boom")))
    data = _data()
    await server._overlay_live_ltp(data)
    assert data["net"][0]["last_price"] == 2019.4 and data["net"][0]["pnl"] == 9.8


@pytest.mark.asyncio
async def test_missing_quote_keeps_that_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_kite", lambda: _Kite({}))
    data = _data()
    await server._overlay_live_ltp(data)
    assert data["net"][0]["last_price"] == 2019.4
