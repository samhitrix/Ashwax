"""Dashboard API: paper/live `mode` filtering on positions/closed/pnl endpoints.

Part of the paper-vs-live dashboard separation effort — these endpoints already
tagged rows with `paper`/`is_paper`, but had no server-side way to filter by it,
so paper and live rows were always interleaved together in the response.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from src.api import server


class _FakeSwingPool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return self._rows


def _swing_row(symbol: str, *, is_paper: bool) -> dict[str, Any]:
    return {
        "agent_trade_id": f"TRD-{symbol}",
        "symbol": symbol,
        "strategy_name": "minervini_vcp",
        "direction": "long",
        "quantity": 10,
        "fill_price": 100.0,
        "exit_price": None,
        "current_sl": 90.0,
        "current_target": 120.0,
        "partial_pnl_booked": 0,
        "entry_timestamp": None,
        "exit_timestamp": None,
        "is_paper": is_paper,
    }


@pytest.mark.asyncio
async def test_swing_positions_mode_all_returns_both(monkeypatch):
    monkeypatch.setattr(
        server,
        "_db_pool",
        _FakeSwingPool([_swing_row("LIVE1", is_paper=False), _swing_row("PAPER1", is_paper=True)]),
    )
    monkeypatch.setattr(server, "_kite", lambda: type("K", (), {"ltp": lambda self, s: {}})())

    out = await server.get_swing_positions(mode="all")

    assert {r["symbol"] for r in out} == {"LIVE1", "PAPER1"}


@pytest.mark.asyncio
async def test_swing_positions_mode_paper_excludes_live(monkeypatch):
    monkeypatch.setattr(
        server,
        "_db_pool",
        _FakeSwingPool([_swing_row("LIVE1", is_paper=False), _swing_row("PAPER1", is_paper=True)]),
    )
    monkeypatch.setattr(server, "_kite", lambda: type("K", (), {"ltp": lambda self, s: {}})())

    out = await server.get_swing_positions(mode="paper")

    assert [r["symbol"] for r in out] == ["PAPER1"]


@pytest.mark.asyncio
async def test_swing_positions_mode_live_excludes_paper(monkeypatch):
    monkeypatch.setattr(
        server,
        "_db_pool",
        _FakeSwingPool([_swing_row("LIVE1", is_paper=False), _swing_row("PAPER1", is_paper=True)]),
    )
    monkeypatch.setattr(server, "_kite", lambda: type("K", (), {"ltp": lambda self, s: {}})())

    out = await server.get_swing_positions(mode="live")

    assert [r["symbol"] for r in out] == ["LIVE1"]


class _FakePositionsPool:
    """agent_symbols lookup (first fetch) + open paper positions (second fetch)."""

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "DISTINCT ON (symbol)" in query:
            return [
                {
                    "symbol": "LIVE1",
                    "strategy_name": "",
                    "initial_sl": None,
                    "current_sl": None,
                    "initial_target": None,
                    "current_target": None,
                    "entry_timestamp": None,
                }
            ]
        return [
            {
                "symbol": "PAPER1",
                "exchange": "NSE",
                "direction": "long",
                "quantity": 20,
                "fill_price": 200.0,
                "entry_timestamp": None,
                "strategy_name": "",
                "initial_sl": None,
                "current_sl": None,
                "initial_target": None,
                "current_target": None,
            }
        ]


class _FakePositionsKite:
    def positions(self) -> dict[str, Any]:
        return {
            "net": [
                {
                    "tradingsymbol": "LIVE1",
                    "exchange": "NSE",
                    "quantity": 10,
                    "average_price": 100.0,
                    "last_price": 105.0,
                    "pnl": 50.0,
                    "product": "MIS",
                }
            ],
            "day": [
                {
                    "tradingsymbol": "LIVEDAY",
                    "exchange": "NSE",
                    "quantity": 5,
                    "average_price": 50.0,
                    "last_price": 55.0,
                    "pnl": 25.0,
                    "product": "MIS",
                }
            ],
        }

    def ltp(self, symbols: list[str]) -> dict[str, Any]:
        return {}


@pytest.mark.asyncio
async def test_positions_mode_all_includes_live_and_paper(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _FakePositionsPool())
    monkeypatch.setattr(server, "_kite", lambda: _FakePositionsKite())

    out = await server.get_positions(mode="all")

    assert {p["tradingsymbol"] for p in out["net"]} == {"LIVE1", "PAPER1"}
    assert {p["tradingsymbol"] for p in out["day"]} == {"LIVEDAY"}


@pytest.mark.asyncio
async def test_positions_mode_live_excludes_paper_net_row(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _FakePositionsPool())
    monkeypatch.setattr(server, "_kite", lambda: _FakePositionsKite())

    out = await server.get_positions(mode="live")

    assert [p["tradingsymbol"] for p in out["net"]] == ["LIVE1"]
    assert [p["tradingsymbol"] for p in out["day"]] == ["LIVEDAY"]  # day rows are always live


@pytest.mark.asyncio
async def test_positions_mode_paper_excludes_live_rows_including_day(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _FakePositionsPool())
    monkeypatch.setattr(server, "_kite", lambda: _FakePositionsKite())

    out = await server.get_positions(mode="paper")

    assert [p["tradingsymbol"] for p in out["net"]] == ["PAPER1"]
    assert out["day"] == []


class _FakeClosedPool:
    """Tracks which table each query targeted; only `trades` OR `paper_trades`
    is ever fetched depending on the caller's mode filter (verifies the
    skip-the-unwanted-table optimization, not just post-filtering)."""

    def __init__(self) -> None:
        self.tables_queried: list[str] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        table = "paper_trades" if "FROM paper_trades" in query else "trades"
        self.tables_queried.append(table)
        is_paper = table == "paper_trades"
        return [
            {
                "symbol": "PAPERX" if is_paper else "LIVEX",
                "direction": "long",
                "quantity": 5,
                "fill_price": 100.0,
                "exit_price": 110.0,
                "pnl_realized": 50.0,
                "strategy_name": "orb_intraday_smart",
                "entry_timestamp": None,
                "exit_timestamp": "2026-09-03T10:00:00",
            }
        ]


@pytest.mark.asyncio
async def test_closed_positions_mode_live_skips_paper_table_entirely(monkeypatch):
    pool = _FakeClosedPool()
    monkeypatch.setattr(server, "_db_pool", pool)

    out = await server.get_closed_positions(mode="live")

    assert pool.tables_queried == ["trades"]
    assert [r["symbol"] for r in out] == ["LIVEX"]
    assert all(r["paper"] is False for r in out)


@pytest.mark.asyncio
async def test_closed_positions_mode_paper_skips_live_table_entirely(monkeypatch):
    pool = _FakeClosedPool()
    monkeypatch.setattr(server, "_db_pool", pool)

    out = await server.get_closed_positions(mode="paper")

    assert pool.tables_queried == ["paper_trades"]
    assert [r["symbol"] for r in out] == ["PAPERX"]
    assert all(r["paper"] is True for r in out)


@pytest.mark.asyncio
async def test_closed_positions_mode_all_queries_both_tables(monkeypatch):
    pool = _FakeClosedPool()
    monkeypatch.setattr(server, "_db_pool", pool)

    out = await server.get_closed_positions(mode="all")

    assert set(pool.tables_queried) == {"trades", "paper_trades"}
    assert {r["symbol"] for r in out} == {"LIVEX", "PAPERX"}


class _FakePnlPool:
    def __init__(self) -> None:
        self.options_is_paper_args: list[bool] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        _ts = datetime(2026, 9, 3)
        if "options_positions" in query:
            is_paper = bool(args[0])
            self.options_is_paper_args.append(is_paper)
            return [{"realized_pnl": 999.0 if is_paper else 111.0, "closed_at": _ts}]
        table = "paper_trades" if "FROM paper_trades" in query else "trades"
        pnl = 200.0 if table == "paper_trades" else 100.0
        return [{"product": "MIS", "pnl_realized": pnl, "exit_timestamp": _ts}]


@pytest.mark.asyncio
async def test_pnl_breakdown_default_mode_is_live_only(monkeypatch):
    pool = _FakePnlPool()
    monkeypatch.setattr(server, "_db_pool", pool)

    out = await server.get_pnl_breakdown()

    assert out["ALL"]["net_pnl"] == pytest.approx(100.0 + 111.0)
    assert pool.options_is_paper_args == [False]


@pytest.mark.asyncio
async def test_pnl_breakdown_mode_paper_never_touches_live_table(monkeypatch):
    pool = _FakePnlPool()
    monkeypatch.setattr(server, "_db_pool", pool)

    out = await server.get_pnl_breakdown(mode="paper")

    assert out["ALL"]["net_pnl"] == pytest.approx(200.0 + 999.0)
    assert pool.options_is_paper_args == [True]


@pytest.mark.asyncio
async def test_pnl_breakdown_mode_all_returns_separated_not_merged(monkeypatch):
    pool = _FakePnlPool()
    monkeypatch.setattr(server, "_db_pool", pool)

    out = await server.get_pnl_breakdown(mode="all")

    assert set(out.keys()) == {"live", "paper"}
    assert out["live"]["ALL"]["net_pnl"] == pytest.approx(100.0 + 111.0)
    assert out["paper"]["ALL"]["net_pnl"] == pytest.approx(200.0 + 999.0)


@pytest.mark.asyncio
async def test_pnl_breakdown_no_pool_mode_all_returns_empty_separated_shape(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", None)

    out = await server.get_pnl_breakdown(mode="all")

    assert set(out.keys()) == {"live", "paper"}
    assert out["live"]["ALL"]["net_pnl"] == 0.0
    assert out["paper"]["ALL"]["net_pnl"] == 0.0
