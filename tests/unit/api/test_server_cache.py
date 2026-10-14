"""Dashboard API: cache-freshness guard + engine heartbeat normalization.

Covers the source-of-truth rule — JSON files under logs/ are CACHE ONLY and a
stale cache must be treated as cleared, and engine status is derived from real
heartbeat age (DB truth), never fabricated.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from src.api import server

# ── _read_cache: proper cache clearing ────────────────────────────────────────


def test_read_cache_missing_returns_default(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_BASE", tmp_path)
    (tmp_path / "logs").mkdir()
    assert server._read_cache("nope.json", 60, {"x": 1}) == {"x": 1}


def test_read_cache_fresh_returns_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_BASE", tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "f.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert server._read_cache("f.json", 60, []) == [1, 2, 3]


def test_read_cache_stale_returns_default(tmp_path, monkeypatch):
    """A cache older than max_age_s is treated as cleared → default, not stale data."""
    monkeypatch.setattr(server, "_BASE", tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    p = logs / "old.json"
    p.write_text(json.dumps({"v": 9}), encoding="utf-8")
    old = time.time() - 3600
    os.utime(p, (old, old))
    assert server._read_cache("old.json", 60, {}) == {}


# ── _engine_heartbeats: status from real heartbeat age ────────────────────────


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, _query, *_args):
        return self._rows


async def test_engine_heartbeats_status_from_age(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "engine_name": "orchestrator",
            "pid": 100,
            "status": "ALIVE",
            "metadata": json.dumps({"cpu": 12.5, "rss_mb": 64.0, "gc": 5}),
            "last_beat": now,
        },
        {
            "engine_name": "OldEngine",
            "pid": 100,
            "status": "ALIVE",
            "metadata": None,
            "last_beat": now - timedelta(seconds=300),
        },
    ]
    monkeypatch.setattr(server, "_db_pool", _FakePool(rows))
    out = await server._engine_heartbeats()
    by = {o["name"]: o for o in out}
    assert by["orchestrator"]["status"] == "RUNNING"
    assert by["orchestrator"]["cpu"] == 12.5
    assert by["orchestrator"]["memory"] == 64.0
    assert by["OldEngine"]["status"] == "STOPPED"


async def test_engine_heartbeats_503_when_no_pool(monkeypatch):
    """No pool is an outage, not "0 engines" — the grid keeps its last-good rows."""
    monkeypatch.setattr(server, "_db_pool", None)
    with pytest.raises(HTTPException) as ei:
        await server._engine_heartbeats()
    assert ei.value.status_code == 503


# ── /api/positions: external (unmanaged) tagging ──────────────────────────────


class _FakeKite:
    def __init__(self, net):
        self._net = net

    def positions(self):
        return {"net": self._net, "day": []}


async def test_positions_external_flag(monkeypatch):
    """A symbol absent from `trades` is tagged external; an agent-held one is not."""
    monkeypatch.setattr(
        server,
        "_kite",
        lambda: _FakeKite(
            [
                {"tradingsymbol": "RELIANCE", "quantity": 10},
                {"tradingsymbol": "NIFTY2661623150CE", "quantity": 65},
            ]
        ),
    )
    monkeypatch.setattr(
        server,
        "_db_pool",
        _FakePool(
            [
                {
                    "symbol": "RELIANCE",
                    "strategy_name": "",
                    "initial_sl": None,
                    "current_sl": None,
                    "initial_target": None,
                    "current_target": None,
                    "entry_timestamp": None,
                }
            ]
        ),
    )
    out = await server.get_positions()
    by = {p["tradingsymbol"]: p for p in out["net"]}
    assert by["RELIANCE"]["external"] is False
    assert by["NIFTY2661623150CE"]["external"] is True


class _FakePositionsPoolPaperOnly:
    """agent_symbols lookup (empty -- no `trades` row) + one open paper position,
    mirroring /api/positions' two distinct queries (see test_server_mode_filter.py's
    _FakePositionsPool for the shape)."""

    async def fetch(self, query, *_args):
        if "DISTINCT ON (symbol)" in query:
            return []
        return [
            {
                "symbol": "PAPERSYM",
                "exchange": "NSE",
                "direction": "short",
                "quantity": 10,
                "fill_price": 100.0,
                "entry_timestamp": None,
                "strategy_name": "llm_intraday_agent",
                "initial_sl": 110.0,
                "current_sl": 110.0,
                "initial_target": 90.0,
                "current_target": 90.0,
            }
        ]


class _FakeKiteNoBrokerPosition:
    """The broker itself holds nothing in PAPERSYM -- it only exists in paper_trades."""

    def positions(self):
        return {"net": [], "day": []}

    def ltp(self, symbols):
        return {}


async def test_positions_shared_cache_not_corrupted_by_second_poller(monkeypatch):
    """Two same-tick pollers (tradingStore.refreshData + advancedStore.refreshAdvancedData
    both hit /api/positions) must not duplicate or mis-tag a paper row.

    Regression for 2026-09-22: _broker_cached returns the same object to every
    caller within its 3s TTL, and get_positions() used to mutate that shared
    object in place (tagging rows, appending paper rows) -- so a second caller
    landing in the same cache window re-tagged the first caller's just-appended
    paper row as `external` and appended a second copy, doubling the row on the
    dashboard with one copy wrongly marked EXTERNAL."""
    monkeypatch.setattr(server, "_db_pool", _FakePositionsPoolPaperOnly())
    monkeypatch.setattr(server, "_kite", lambda: _FakeKiteNoBrokerPosition())
    server._clear_broker_cache()

    first = await server.get_positions()
    second = await server.get_positions()  # same 3s cache window, no clear in between

    for out, label in ((first, "first"), (second, "second")):
        rows = [p for p in out["net"] if p["tradingsymbol"] == "PAPERSYM"]
        assert len(rows) == 1, f"{label} call: expected 1 PAPERSYM row, got {len(rows)}"
        assert rows[0]["paper"] is True
        assert rows[0]["external"] is False


async def test_positions_not_external_when_db_down(monkeypatch):
    """DB unavailable → do not mislabel; everything stays external=False."""
    monkeypatch.setattr(
        server,
        "_kite",
        lambda: _FakeKite(
            [
                {"tradingsymbol": "RELIANCE", "quantity": 10},
            ]
        ),
    )
    monkeypatch.setattr(server, "_db_pool", None)
    out = await server.get_positions()
    assert out["net"][0]["external"] is False


# ── _kite_refreshed: throttled auto_refresh ───────────────────────────────────


def test_kite_refreshed_skips_auto_refresh_within_interval(monkeypatch):
    """auto_refresh() makes a live Kite profile() call every invocation — a
    request within _AUTH_CHECK_INTERVAL_S of the last check must not repeat it,
    or the dashboard's 2s poll loop would hammer Kite's API for no reason."""
    calls: list[int] = []
    monkeypatch.setattr(server, "auto_refresh", lambda *_a, **_k: calls.append(1) or "tok")
    monkeypatch.setattr(server, "_kite", lambda: "kite-client")
    monkeypatch.setattr(server, "_last_auth_check", time.monotonic())

    result = server._kite_refreshed()

    assert result == "kite-client"
    assert calls == []  # within the throttle window — auto_refresh not called


def test_kite_refreshed_calls_auto_refresh_after_interval(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(server, "auto_refresh", lambda *_a, **_k: calls.append(1) or "tok")
    monkeypatch.setattr(server, "_kite", lambda: "kite-client")
    monkeypatch.setattr(
        server, "_last_auth_check", time.monotonic() - server._AUTH_CHECK_INTERVAL_S - 1
    )

    server._kite_refreshed()

    assert calls == [1]  # outside the throttle window — auto_refresh runs once
