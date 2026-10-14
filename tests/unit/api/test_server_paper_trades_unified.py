"""Dashboard API: /api/paper_trades unifies intraday, swing AND options paper
activity into one list with a `segment` field, instead of options being absent
entirely (previously only equity paper_trades were returned)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from src.api import server


class _FakePool:
    def __init__(
        self, equity_rows: list[dict[str, Any]], options_rows: list[dict[str, Any]]
    ) -> None:
        self._equity_rows = equity_rows
        self._options_rows = options_rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "options_positions" in query:
            return self._options_rows
        return self._equity_rows


def _equity_row(symbol: str, product: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "direction": "long",
        "quantity": 10,
        "fill_price": 100.0,
        "exit_price": None,
        "pnl_realized": 0.0,
        "partial_pnl_booked": 0.0,
        "strategy_name": "orb_intraday_smart",
        "order_state": "OPEN",
        "exit_reason": "",
        "initial_sl": 90.0,
        "current_sl": 90.0,
        "initial_target": 120.0,
        "current_target": 120.0,
        "regime_at_entry": "TRENDING",
        "setup_quality_score": 80.0,
        "llm_confidence": 0.8,
        "llm_flag": "CONFIRM",
        "llm_reason": "",
        "product": product,
        "entry_timestamp": datetime(2026, 9, 3, 9, 30),
        "exit_timestamp": None,
    }


def _options_row() -> dict[str, Any]:
    return {
        "position_id": "OPT-1",
        "underlying": "BANKNIFTY",
        "strategy_type": "iron_condor",
        "status": "OPEN",
        "net_credit": 1500.0,
        "realized_pnl": 0.0,
        "exit_reason": "",
        "opened_at": datetime(2026, 9, 3, 9, 20),
        "closed_at": None,
        "legs": {
            "protection": [
                {
                    "tradingsymbol": "NFO:BN-LONG",
                    "direction": "BUY",
                    "lots": 1,
                    "lot_size": 15,
                    "premium": 50.0,
                }
            ],
            "income": [
                {
                    "tradingsymbol": "NFO:BN-SHORT",
                    "direction": "SELL",
                    "lots": 1,
                    "lot_size": 15,
                    "premium": 120.0,
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_paper_trades_open_options_row_has_live_pnl_and_qty(monkeypatch):
    # Was hard-coded to qty 0 / unrealized +0 regardless of the market.
    quotes = {"NFO:BN-LONG": {"last_price": 60.0}, "NFO:BN-SHORT": {"last_price": 100.0}}
    monkeypatch.setattr(server, "_db_pool", _FakePool([], [_options_row()]))
    monkeypatch.setattr(server, "_kite", lambda: type("K", (), {"ltp": lambda self, s: quotes})())

    row = (await server.get_paper_trades())[0]

    assert row["quantity"] == 15
    assert row["unrealizedPnl"] == 450.0  # (60-50)*15 + (120-100)*15


@pytest.mark.asyncio
async def test_paper_trades_open_options_row_pnl_is_none_when_a_quote_is_missing(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _FakePool([], [_options_row()]))
    monkeypatch.setattr(server, "_kite", lambda: type("K", (), {"ltp": lambda self, s: {}})())

    assert (await server.get_paper_trades())[0]["unrealizedPnl"] is None


@pytest.mark.asyncio
async def test_paper_trades_includes_all_three_segments(monkeypatch):
    monkeypatch.setattr(
        server,
        "_db_pool",
        _FakePool(
            [_equity_row("ADANIPORTS", "MIS"), _equity_row("TATASTEEL", "CNC")],
            [_options_row()],
        ),
    )
    monkeypatch.setattr(server, "_kite", lambda: type("K", (), {"ltp": lambda self, s: {}})())

    out = await server.get_paper_trades()

    segments = {r["segment"] for r in out}
    assert segments == {"INTRADAY", "SWING", "OPTIONS"}


@pytest.mark.asyncio
async def test_paper_trades_options_row_shape(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _FakePool([], [_options_row()]))
    monkeypatch.setattr(server, "_kite", lambda: type("K", (), {"ltp": lambda self, s: {}})())

    out = await server.get_paper_trades()

    assert len(out) == 1
    row = out[0]
    assert row["segment"] == "OPTIONS"
    assert row["symbol"] == "BANKNIFTY iron_condor"
    assert row["strategy"] == "iron_condor"
    assert row["state"] == "OPEN"


@pytest.mark.asyncio
async def test_paper_trades_options_fetch_failure_does_not_break_equity_rows(monkeypatch):
    class _FailingOptionsPool(_FakePool):
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            if "options_positions" in query:
                raise RuntimeError("boom")
            return self._equity_rows

    monkeypatch.setattr(
        server, "_db_pool", _FailingOptionsPool([_equity_row("ADANIPORTS", "MIS")], [])
    )
    monkeypatch.setattr(server, "_kite", lambda: type("K", (), {"ltp": lambda self, s: {}})())

    out = await server.get_paper_trades()

    assert len(out) == 1
    assert out[0]["segment"] == "INTRADAY"


@pytest.mark.asyncio
async def test_paper_trades_lists_every_leg_including_the_one_already_stopped_out(monkeypatch):
    import json

    legs = json.dumps(
        {
            "protection": [],
            "income": [
                {
                    "tradingsymbol": "NFO:NIFTY26O1322600CE",
                    "instrument_type": "CE",
                    "strike": 22600.0,
                    "direction": "SELL",
                    "lots": 3,
                    "lot_size": 65,
                    "premium": 39.85,
                    "delta": 0.2,
                }
            ],
            "closed": [
                {
                    "tradingsymbol": "NFO:NIFTY26O1322300PE",
                    "instrument_type": "PE",
                    "strike": 22300.0,
                    "direction": "SELL",
                    "lots": 3,
                    "lot_size": 65,
                    "premium": 38.65,
                    "exit_premium": 47.55,
                    "exit_at": "2026-10-12T11:01:31+05:30",
                    "reason": "SHORT_PREMIUM_SL 23%",
                }
            ],
        }
    )
    row = {**_options_row(), "strategy_type": "options_intraday_short_premium", "legs": legs}

    class _Kite:
        def ltp(self, symbols):  # type: ignore[no-untyped-def]
            return {"NFO:NIFTY26O1322600CE": {"last_price": 29.15}}

    monkeypatch.setattr(server, "_db_pool", _FakePool([], [row]))
    monkeypatch.setattr(server, "_kite", lambda: _Kite())
    out = [t for t in await server.get_paper_trades() if t["segment"] == "OPTIONS"]
    assert len(out) == 1
    legs_out = {leg["contract"]: leg for leg in out[0]["legs"]}
    assert set(legs_out) == {"NFO:NIFTY26O1322600CE", "NFO:NIFTY26O1322300PE"}
    assert legs_out["NFO:NIFTY26O1322300PE"]["state"] == "CLOSED"
    assert legs_out["NFO:NIFTY26O1322300PE"]["pnl"] == pytest.approx((38.65 - 47.55) * 195)
    assert legs_out["NFO:NIFTY26O1322300PE"]["quantity"] == 195
    assert legs_out["NFO:NIFTY26O1322600CE"]["pnl"] == pytest.approx((39.85 - 29.15) * 195)
    assert out[0]["partialPnlBooked"] == pytest.approx(
        (38.65 - 47.55) * 195
    )  # booked, shown under P&L
    assert out[0]["unrealizedPnl"] == pytest.approx((39.85 - 29.15) * 195)  # live remainder
