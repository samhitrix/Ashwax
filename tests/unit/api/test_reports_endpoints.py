"""Dashboard API: /api/reports/* -- closed-trade history for the Reports tab,
strictly one mode at a time and never including today's closes."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import pytest
from fastapi import HTTPException
from src.api import server


class _Pool:
    """Routes by table name and records the query + args it received."""

    def __init__(self, equity: list[dict[str, Any]], options: list[dict[str, Any]]) -> None:
        self.equity, self.options = equity, options
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((query, args))
        return self.options if "options_positions" in query else self.equity


def _eq(**o: Any) -> dict[str, Any]:
    base = {
        "symbol": "TCS",
        "direction": "BUY",
        "quantity": 10,
        "fill_price": 100.0,
        "exit_price": 110.0,
        "pnl_realized": 100.0,
        "strategy_name": "s",
        "product": "MIS",
        "entry_timestamp": datetime(2026, 9, 10, 9, 30),
        "exit_timestamp": datetime(2026, 9, 10, 15, 0),
        "charges_taxes": 20.0,
        "charges_breakdown": json.dumps({"brokerage": 15.0, "stt": 5.0}),  # exact; total 20
        "broker": "kite",
    }
    return {**base, **o}


def _opt(**o: Any) -> dict[str, Any]:
    base = {
        "underlying": "NSE:NIFTY",
        "strategy_type": "bull_put_spread",
        "legs": json.dumps(
            {
                "income": [
                    {"tradingsymbol": "NFO:X", "direction": "SELL", "lots": 1, "premium": 45.0}
                ]
            }
        ),
        "expiry": date(2026, 9, 22),
        "net_credit": 1250.0,
        "realized_pnl": -300.0,
        "exit_reason": "stop",
        "opened_at": datetime(2026, 9, 11, 9, 30),
        "closed_at": datetime(2026, 9, 11, 14, 0),
        "charges_breakdown": json.dumps({"brokerage": 80.0, "stt": 20.0}),  # exact; total 100
        "broker": "kite",
    }
    return {**base, **o}


@pytest.mark.asyncio
async def test_tradebook_merges_equity_and_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        server, "_db_pool", _Pool([_eq(), _eq(product="CNC", symbol="INFY")], [_opt()])
    )
    rows = await server.get_reports_tradebook(mode="live")
    assert {r["segment"] for r in rows} == {"INTRADAY", "SWING", "OPTIONS"}
    opt = next(r for r in rows if r["segment"] == "OPTIONS")
    # options realized_pnl is stored GROSS: pnl is now net of the recorded charges
    assert opt["symbol"] == "NIFTY" and opt["gross"] == -300.0 and opt["pnl"] == -400.0
    assert opt["chargesTotal"] == 100.0 and opt["estimated"] is False and len(opt["legs"]) == 1
    eq = next(r for r in rows if r["segment"] == "INTRADAY")
    # equity pnl_realized is stored NET: gross adds the recorded charges back
    assert eq["pnl"] == 100.0 and eq["gross"] == 120.0 and eq["charges"]["stt"] == 5.0


@pytest.mark.asyncio
async def test_mode_selects_paper_table_and_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _Pool([], [])
    monkeypatch.setattr(server, "_db_pool", pool)
    await server.get_reports_tradebook(mode="paper")
    assert "FROM paper_trades" in pool.calls[0][0]
    assert pool.calls[1][1][2] is True  # options is_paper
    await server.get_reports_tradebook(mode="live")
    assert "FROM trades" in pool.calls[2][0] and pool.calls[3][1][2] is False


@pytest.mark.asyncio
async def test_end_date_is_capped_before_today(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _Pool([], [])
    monkeypatch.setattr(server, "_db_pool", pool)
    await server.get_reports_tradebook(mode="live", date_to="2999-01-01")
    _, end = pool.calls[0][1]
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    assert end == datetime.now(ZoneInfo("Asia/Kolkata")).date() - timedelta(days=1)


@pytest.mark.asyncio
async def test_invalid_mode_and_date_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_db_pool", _Pool([], []))
    with pytest.raises(HTTPException) as e1:
        await server.get_reports_tradebook(mode="all")
    assert e1.value.status_code == 400
    with pytest.raises(HTTPException) as e2:
        await server.get_reports_tradebook(mode="live", date_from="garbage")
    assert e2.value.status_code == 400


def test_report_leg_pnl_uses_persisted_exit_and_sign() -> None:
    base = {"tradingsymbol": "NFO:X", "lots": 1, "lot_size": 50, "premium": 78.22}
    short = server._report_leg({**base, "direction": "SELL", "exit_premium": 100.0})
    assert short["pnl"] == pytest.approx(-(100.0 - 78.22) * 50)  # short loses as price rises
    long_ = server._report_leg({**base, "direction": "BUY", "premium": 33.17, "exit_premium": 20.0})
    assert long_["pnl"] == pytest.approx((20.0 - 33.17) * 50)
    legacy = server._report_leg({**base, "direction": "SELL"})  # closed before exits persisted
    assert legacy["exitPremium"] is None and legacy["pnl"] is None


@pytest.mark.asyncio
async def test_pnl_rollup(monkeypatch: pytest.MonkeyPatch) -> None:
    eq = [
        _eq(pnl_realized=100.0),
        _eq(pnl_realized=-40.0, exit_timestamp=datetime(2026, 9, 9, 15, 0)),
    ]
    monkeypatch.setattr(server, "_db_pool", _Pool(eq, [_opt(realized_pnl=-300.0)]))
    r = await server.get_reports_pnl(mode="live")
    # net: 100 - 40 + (-300 - 100 options charges); gross adds back 20 per equity trade
    assert r["totalPnl"] == -340.0 and r["trades"] == 3 and r["wins"] == 1
    assert r["grossPnl"] == pytest.approx(120.0 - 20.0 - 300.0)
    assert r["totalCharges"] == pytest.approx(140.0) and r["estimatedTrades"] == 0
    assert r["chargesByType"]["brokerage"] == pytest.approx(15 + 15 + 80)
    assert {x["strategy"] for x in r["byStrategy"]} == {"s", "bull_put_spread"}
    assert [d["date"] for d in r["byDay"]] == ["2026-09-11", "2026-09-10", "2026-09-09"]
    assert r["bySymbol"][0]["symbol"] == "NIFTY"  # worst first


@pytest.mark.asyncio
async def test_rows_without_breakdown_are_estimated_and_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _eq(charges_taxes=None, charges_breakdown=None, pnl_realized=100.0)  # gross, no costs
    monkeypatch.setattr(server, "_db_pool", _Pool([legacy], []))
    r = await server.get_reports_pnl(mode="live")
    row = (await server.get_reports_tradebook(mode="live"))[0]
    assert row["estimated"] is True and row["gross"] == 100.0
    assert 0 < row["chargesTotal"] < 100.0 and row["pnl"] == pytest.approx(
        100.0 - row["chargesTotal"], abs=0.01
    )
    assert r["estimatedTrades"] == 1
