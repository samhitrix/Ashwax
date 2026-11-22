"""GET /api/execution_quality: must include paper_trades, not just trades — with
paper_mode on, live fills land in trades (empty) so a live-only query always
returned []. The query now UNION ALLs both tables, matching /api/closed_positions
and /api/swing_positions."""

from __future__ import annotations

from src.api import server


class _FakePool:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = ""

    async def fetch(self, query, *_args):
        self.last_query = query
        return self._rows


def _row(symbol, n, avg_slip):
    return {"symbol": symbol, "n": n, "avg_slip": avg_slip}


async def test_query_includes_both_tables(monkeypatch):
    pool = _FakePool([_row("RELIANCE", 3, 0.12)])
    monkeypatch.setattr(server, "_db_pool", pool)

    out = await server.get_execution_quality()

    assert "FROM trades" in pool.last_query
    assert "FROM paper_trades" in pool.last_query
    assert out[0]["symbol"] == "RELIANCE"
    assert out[0]["totalTrades"] == 3


async def test_empty_when_no_pool(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", None)
    assert await server.get_execution_quality() == []
