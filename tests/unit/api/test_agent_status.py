"""/api/agent_status: report 'unknown' (503) on DB error, never a false STOPPED.

Root cause of the Active/Stopped badge flickering: the endpoint swallowed any DB
error and returned active:false (HTTP 200), so a momentarily-busy shared asyncpg
pool was reported as "engine stopped". active:false is reserved for a genuine
absence of heartbeats; transient errors must surface as 503 so the dashboard
keeps last-good instead of flapping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from src.api import server


class _RowPool:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, _query, *_args):
        return self._row


class _FailPool:
    async def fetchrow(self, _query, *_args):
        raise RuntimeError("pool exhausted")


async def test_active_true_for_fresh_heartbeat(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _RowPool({"last_beat": datetime.now(UTC)}))
    out = await server.agent_status()
    assert out["active"] is True


async def test_active_false_for_stale_heartbeat(monkeypatch):
    old = datetime.now(UTC) - timedelta(seconds=120)
    monkeypatch.setattr(server, "_db_pool", _RowPool({"last_beat": old}))
    out = await server.agent_status()
    assert out["active"] is False


async def test_active_false_when_no_heartbeats(monkeypatch):
    """Empty heartbeats table = engine never started → genuine STOPPED, not 503."""
    monkeypatch.setattr(server, "_db_pool", _RowPool(None))
    out = await server.agent_status()
    assert out["active"] is False


async def test_db_error_raises_503_not_false(monkeypatch):
    """The flicker bug: a transient DB error must NOT be reported as active:false."""
    monkeypatch.setattr(server, "_db_pool", _FailPool())
    with pytest.raises(HTTPException) as exc:
        await server.agent_status()
    assert exc.value.status_code == 503


async def test_no_pool_raises_503(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", None)
    with pytest.raises(HTTPException) as exc:
        await server.agent_status()
    assert exc.value.status_code == 503
