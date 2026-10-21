"""Dashboard API: trade/position endpoints expose `exec_broker` (migration 089: the broker that
HOLDS a row). NULL on a live row means legacy "kite"; paper rows carry None (nothing was placed).

The live DB may not have migration 089 until the owner deploys it, so every SELECT must degrade
to the pre-089 shape (undefined-column error, SQLSTATE 42703) instead of blanking the endpoint."""

from __future__ import annotations

import json
from typing import Any

import pytest
from src.api import server


class _UndefinedColumn(Exception):
    sqlstate = "42703"


def _names_real_column(query: str) -> bool:
    return "exec_broker" in query.replace("NULL::text AS exec_broker", "")


class _Pool:
    """Answers canned rows; `has_column=False` models a DB without migration 089: any query
    naming exec_broker as a real column fails, the NULL::text fallback does not."""

    def __init__(self, rows_by_table: dict[str, list[dict[str, Any]]], has_column: bool) -> None:
        self.rows_by_table = rows_by_table
        self.has_column = has_column
        self.queries: list[str] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append(query)
        if _names_real_column(query) and not self.has_column:
            raise _UndefinedColumn("column exec_broker does not exist")
        if "UNION ALL" in query:
            rows = self.rows_by_table.get("union", [])
            if not self.has_column:
                rows = [{k: v for k, v in r.items() if k != "exec_broker"} for r in rows]
            return rows
        for table in ("paper_trades", "options_positions", "trades"):
            if f"FROM {table}" in query:
                rows = self.rows_by_table.get(table, [])
                if not self.has_column:
                    rows = [{k: v for k, v in r.items() if k != "exec_broker"} for r in rows]
                return rows
        return []


@pytest.fixture(autouse=True)
def _reset_missing_flag() -> None:
    server._exec_broker_missing_until.clear()


def _swing_row(symbol: str, *, is_paper: bool, exec_broker: str | None) -> dict[str, Any]:
    return {
        "agent_trade_id": f"TRD-{symbol}",
        "symbol": symbol,
        "strategy_name": "s",
        "direction": "long",
        "quantity": 1,
        "fill_price": 100.0,
        "exit_price": None,
        "current_sl": 90.0,
        "current_target": 120.0,
        "partial_pnl_booked": 0,
        "entry_timestamp": None,
        "exit_timestamp": None,
        "is_paper": is_paper,
        "exec_broker": exec_broker,
    }


def _closed_row(symbol: str, exec_broker: str | None) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "direction": "long",
        "quantity": 1,
        "partial_qty_booked": 0,
        "fill_price": 100.0,
        "exit_price": 101.0,
        "pnl_realized": 1.0,
        "strategy_name": "s",
        "entry_timestamp": "2026-10-16T10:00:00",
        "exit_timestamp": "2026-10-16T11:00:00",
        "exec_broker": exec_broker,
    }


def _opt_row(exec_broker: str | None, is_paper: bool = False) -> dict[str, Any]:
    return {
        "position_id": "P1",
        "underlying": "NIFTY",
        "strategy_type": "short_strangle",
        "status": "CLOSED",
        "legs": json.dumps({"protection": [], "income": []}),
        "expiry": None,
        "dte_at_entry": 3,
        "lot_size_at_entry": 75,
        "net_credit": 100.0,
        "max_loss": None,
        "max_profit": 100.0,
        "entry_margin": None,
        "entry_delta": None,
        "entry_gamma": None,
        "entry_theta": None,
        "entry_vega": None,
        "realized_pnl": 10.0,
        "exit_reason": "",
        "opened_at": None,
        "closed_at": None,
        "is_paper": is_paper,
        "exec_broker": exec_broker,
    }


class _Kite:
    def quote(self, keys: list[str]) -> dict[str, Any]:
        return {}

    def ltp(self, keys: list[str]) -> dict[str, Any]:
        return {}


@pytest.mark.parametrize("has_column", [True, False])
async def test_swing_positions_exec_broker(
    monkeypatch: pytest.MonkeyPatch, has_column: bool
) -> None:
    pool = _Pool(
        {
            "union": [
                _swing_row("AAA", is_paper=False, exec_broker="angel_one"),
                _swing_row("BBB", is_paper=False, exec_broker=None),
                _swing_row("CCC", is_paper=True, exec_broker=None),
            ]
        },
        has_column,
    )
    monkeypatch.setattr(server, "_db_pool", pool)
    monkeypatch.setattr(server, "_kite", lambda: _Kite())

    out = {r["symbol"]: r for r in await server.get_swing_positions()}

    assert out["AAA"]["exec_broker"] == ("angel_one" if has_column else "kite")
    assert out["BBB"]["exec_broker"] == "kite"  # NULL on a live row = legacy kite
    assert out["CCC"]["exec_broker"] is None  # paper rows never carry a broker
    assert set(out) == {"AAA", "BBB", "CCC"}  # degraded SELECT must not blank the endpoint


@pytest.mark.parametrize("has_column", [True, False])
async def test_closed_positions_exec_broker(
    monkeypatch: pytest.MonkeyPatch, has_column: bool
) -> None:
    pool = _Pool(
        {
            "trades": [_closed_row("AAA", "angel_one"), _closed_row("BBB", None)],
            "paper_trades": [_closed_row("CCC", None)],
        },
        has_column,
    )
    monkeypatch.setattr(server, "_db_pool", pool)

    out = {r["symbol"]: r for r in await server.get_closed_positions()}

    assert out["AAA"]["exec_broker"] == ("angel_one" if has_column else "kite")
    assert out["BBB"]["exec_broker"] == "kite"
    assert out["CCC"]["exec_broker"] is None
    assert set(out) == {"AAA", "BBB", "CCC"}


@pytest.mark.parametrize("has_column", [True, False])
async def test_options_positions_exec_broker(
    monkeypatch: pytest.MonkeyPatch, has_column: bool
) -> None:
    pool = _Pool(
        {
            "options_positions": [
                _opt_row("angel_one"),
                {**_opt_row(None), "position_id": "P2"},
                {**_opt_row(None, is_paper=True), "position_id": "P3"},
            ]
        },
        has_column,
    )
    monkeypatch.setattr(server, "_db_pool", pool)
    monkeypatch.setattr(server, "_kite", lambda: _Kite())

    out = {r["positionId"]: r for r in await server.get_options_positions()}

    assert out["P1"]["exec_broker"] == ("angel_one" if has_column else "kite")
    assert out["P2"]["exec_broker"] == "kite"
    assert out["P3"]["exec_broker"] is None
    assert len(out) == 3


async def test_non_column_db_errors_are_not_mistaken_for_a_missing_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection error must keep the existing behaviour (empty list), and must NOT be
    cached as "column missing" (which would silently relabel angel rows as kite)."""

    class _Down:
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            raise ConnectionError("db down")

    monkeypatch.setattr(server, "_db_pool", _Down())
    assert await server.get_closed_positions() == []
    assert not server._exec_broker_missing_until


async def test_missing_column_is_remembered_then_rechecked(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _Pool({"trades": [_closed_row("AAA", None)]}, has_column=False)
    monkeypatch.setattr(server, "_db_pool", pool)

    await server.get_closed_positions()
    first = len(pool.queries)
    await server.get_closed_positions()
    # Within the TTL the second call goes straight to the degraded SELECT: no failed first try.
    assert not any(_names_real_column(q) for q in pool.queries[first:])

    pool.has_column = True  # migration deployed
    server._exec_broker_missing_until["trades"] = 0.0  # TTL elapsed
    pool.rows_by_table["trades"] = [_closed_row("AAA", "angel_one")]
    out = await server.get_closed_positions()
    assert next(r for r in out if r["symbol"] == "AAA")["exec_broker"] == "angel_one"
