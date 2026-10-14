"""Dashboard API: /api/tax_report -- now DB-sourced (trades + paper_trades),
not the logs/closed_trades.json cache, which never invalidated on a deleted
trade (e.g. a duplicate-entry cleanup) and kept surfacing it forever."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from src.api import server


class _FakePool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return self._rows


def _row(**overrides: Any) -> dict[str, Any]:
    base = {
        "symbol": "RELIANCE",
        "product": "MIS",
        "entry_timestamp": datetime(2026, 9, 1, 9, 30),
        "exit_timestamp": datetime(2026, 9, 1, 15, 0),
        "pnl_realized": 3573.0,
        "quantity": 10,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_tax_report_reflects_current_db_rows(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _FakePool([_row(symbol="HINDCOPPER")]))

    out = await server.get_tax_report()

    assert [t["symbol"] for t in out["trades"]] == ["HINDCOPPER"]


@pytest.mark.asyncio
async def test_tax_report_reads_only_the_requested_mode_table(monkeypatch):
    queries: list[str] = []

    class _Recording(_FakePool):
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            queries.append(query)
            return []

    monkeypatch.setattr(server, "_db_pool", _Recording([]))

    await server.get_tax_report(mode="paper")
    await server.get_tax_report()  # default = live

    assert "FROM paper_trades" in queries[0] and "FROM trades" not in queries[0]
    assert "FROM trades" in queries[1] and "paper_trades" not in queries[1]
    with pytest.raises(server.HTTPException):
        await server.get_tax_report(mode="all")


@pytest.mark.asyncio
async def test_tax_report_omits_a_deleted_trade(monkeypatch):
    # The DB is the only source of truth now -- a row absent from `trades`/
    # `paper_trades` (deleted) simply isn't in the query result, unlike the old
    # JSON-cache path where a deleted trade lingered forever.
    monkeypatch.setattr(server, "_db_pool", _FakePool([]))

    out = await server.get_tax_report()

    assert out["trades"] == []


@pytest.mark.asyncio
async def test_tax_report_no_pool_returns_empty_report(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", None)

    out = await server.get_tax_report()

    assert out["trades"] == []
