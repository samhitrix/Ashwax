"""Shared fixtures for the dashboard API tests.

Most endpoint tests here predate Kite becoming optional: they fake Kite's SDK by patching
`server._kite()` and assume Kite is configured. The autouse fixture keeps that world for
them -- Kite counts as configured and the BrokerAccess seam (market data + funds) is served
from whatever `server._kite()` returns at call time. Tests of the Kite-less paths opt out by
patching `_kite_configured` / `_access` themselves (a later monkeypatch wins), or opt out
entirely with `@pytest.mark.real_broker_access` (tests that must exercise the real
BrokerAccess -> MarketDataRouter path, e.g. to prove "unchanged with Kite").
"""

from __future__ import annotations

import sys
from typing import Any

import pytest


class KiteBackedAccess:
    """BrokerAccess stand-in whose market data and Kite funds come from server._kite()."""

    def __init__(self, server: Any) -> None:
        self._server = server

    def configured(self) -> list[str]:
        return ["kite"]

    def primary_account(self, legacy: str | None) -> str | None:
        return "kite"

    def token_present(self, name: str) -> bool:
        return name == "kite"

    async def ltp(self, keys: list[str]) -> dict[str, Any]:
        return dict(self._server._kite().ltp(keys))

    async def quote(self, keys: list[str], *, ltp_fallback: bool = True) -> dict[str, Any]:
        return dict(self._server._kite().quote(keys))

    async def margins(self, name: str, **_bounds: Any) -> Any:
        from src.brokers.adapters.kite.adapter import _margin_from_kite

        return _margin_from_kite(self._server._kite().margins())

    async def session_status(self, name: str) -> Any:
        raise RuntimeError(f"{name} not configured in this test")

    async def cached(self, key: str, ttl_s: float, fetch: Any) -> Any:
        return await fetch()

    def clear_cache(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _legacy_kite_world(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("real_broker_access") is not None:
        return
    server = sys.modules.get("src.api.server")
    if server is None:  # this test module does not use the API server
        return
    monkeypatch.setattr(server, "_kite_configured", lambda: True)
    access = KiteBackedAccess(server)
    monkeypatch.setattr(server, "_access", lambda: access)
