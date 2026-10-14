"""GET /api/closed_positions: closed positions reflect process, not mode — live
(`trades`) and paper (`paper_trades`) closes are merged into one feed, each tagged
via `paper` so the UI can badge it, sorted by exit time across both sources.

The today-only date scoping lives in the SQL WHERE clause, so it isn't exercised by
these query-shape fakes (they return canned rows regardless of the date filter) —
verified separately against the real DB."""

from __future__ import annotations

from src.api import server


class _FakeTwoTablePool:
    def __init__(self, live_rows, paper_rows):
        self._live_rows = live_rows
        self._paper_rows = paper_rows

    async def fetch(self, query, *_args):
        if "FROM paper_trades" in query:
            return self._paper_rows
        return self._live_rows


def _row(symbol, exit_ts, pnl, partial_qty=0):
    return {
        "symbol": symbol,
        "direction": "long",
        "quantity": 10,
        "partial_qty_booked": partial_qty,
        "fill_price": 100.0,
        "exit_price": 101.0,
        "pnl_realized": pnl,
        "strategy_name": "orb_intraday_top",
        "entry_timestamp": exit_ts,
        "exit_timestamp": exit_ts,
    }


async def test_merges_live_and_paper_sorted_by_exit_time(monkeypatch):
    live_rows = [_row("RELIANCE", "2026-08-07T10:00:00", 50.0)]
    paper_rows = [_row("LT", "2026-08-07T10:36:07", -158.7)]
    monkeypatch.setattr(server, "_db_pool", _FakeTwoTablePool(live_rows, paper_rows))

    out = await server.get_closed_positions()

    symbols = [r["symbol"] for r in out]
    assert symbols == ["LT", "RELIANCE"]  # LT closed later → sorts first
    by_symbol = {r["symbol"]: r for r in out}
    assert by_symbol["LT"]["paper"] is True
    assert by_symbol["RELIANCE"]["paper"] is False


async def test_empty_when_no_pool(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", None)
    assert await server.get_closed_positions() == []


async def test_quantity_is_original_size_after_a_partial(monkeypatch):
    """pnl_realized is the whole trade (last leg + partial), so quantity must be the
    original size — remaining 13 + 13 sold in the partial = 26 — not just the last leg
    (2026-09-24 LAURUSLABS showed qty 13 next to half its real P&L)."""
    paper_rows = [_row("LAURUSLABS", "2026-09-24T12:07:12", 3624.28, partial_qty=13)]
    paper_rows[0]["quantity"] = 13
    monkeypatch.setattr(server, "_db_pool", _FakeTwoTablePool([], paper_rows))

    out = await server.get_closed_positions()

    assert out[0]["quantity"] == 26
    assert out[0]["pnl"] == 3624.28


async def test_reports_tradebook_quantity_is_original_size_after_a_partial(monkeypatch):
    """Reports > Tradebook showed 13 for LAURUSLABS (26 bought, 13 sold in a partial) next to the
    whole-trade P&L of +3,624 -- its own query never read partial_qty_booked (2026-09-28)."""
    paper_rows = [_row("LAURUSLABS", "2026-09-24T12:07:12", 3624.28, partial_qty=13)]
    paper_rows[0]["quantity"] = 13
    paper_rows[0]["product"] = "CNC"
    paper_rows[0].update(charges_taxes=20.0, charges_breakdown={"brokerage": 20.0}, broker="kite")
    monkeypatch.setattr(server, "_db_pool", _FakeTwoTablePool([], paper_rows))

    out = await server._closed_history_rows("paper", "2026-09-24", "2026-09-24")

    equity = [r for r in out if r["symbol"] == "LAURUSLABS"]
    assert equity[0]["quantity"] == 26
    assert equity[0]["pnl"] == 3624.28
