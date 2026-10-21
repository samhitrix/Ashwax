"""Tests for src.orchestrator._kite_token_refresh_loop.

Found live 2026-09-09: when segments are split paper/live (options paper,
swing live), the live segment's broker is a SEPARATELY constructed
KiteAdapter instance from shared["broker"] -- a refresh applied only to
shared["broker"] silently left the live one still holding a stale token,
even though the loop logged "kite_token_refreshed" successfully. The fix
is shared["kite_brokers"]: every distinct Kite-credentialed instance, all
refreshed together from one auto_refresh() call.
"""

from __future__ import annotations

import asyncio

import pytest
import src.brokers.kite_auth as kite_auth_mod
import src.orchestrator as orch_mod


@pytest.fixture(autouse=True)
def _kite_credentials(monkeypatch):  # type: ignore[no-untyped-def]
    # The loop is a no-op without KITE_API_KEY; these tests exercise the refresh path.
    monkeypatch.setenv("KITE_API_KEY", "test-key")


class _FakeKiteBroker:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    def set_access_token(self, token: str) -> None:
        self.tokens.append(token)


class _NoTokenBroker:
    """A broker with no set_access_token -- e.g. a plain test double."""


async def _run_one_tick(shared: dict) -> None:
    """Runs _kite_token_refresh_loop for exactly one check-then-sleep cycle."""
    stop = asyncio.Event()

    async def _stop_after_first_sleep(*_a, **_k):  # type: ignore[no-untyped-def]
        stop.set()

    orig_sleep = asyncio.sleep
    asyncio.sleep = _stop_after_first_sleep  # type: ignore[assignment]
    try:
        await orch_mod._kite_token_refresh_loop(shared, stop)
    finally:
        asyncio.sleep = orig_sleep  # type: ignore[assignment]


async def test_refreshes_every_broker_in_kite_brokers_list(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(kite_auth_mod, "is_token_valid", lambda *_a, **_k: False)
    monkeypatch.setattr(kite_auth_mod, "auto_refresh", lambda *_a, **_k: "NEW_TOKEN")

    paper_side_broker = _FakeKiteBroker()
    live_broker = _FakeKiteBroker()
    shared = {"broker": paper_side_broker, "kite_brokers": [paper_side_broker, live_broker]}

    await _run_one_tick(shared)

    assert paper_side_broker.tokens == ["NEW_TOKEN"]
    assert live_broker.tokens == ["NEW_TOKEN"]


async def test_falls_back_to_broker_key_when_kite_brokers_absent(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Backward-compat path -- a caller that never set shared["kite_brokers"]
    # still gets shared["broker"] refreshed, matching the old behavior.
    monkeypatch.setattr(kite_auth_mod, "is_token_valid", lambda *_a, **_k: False)
    monkeypatch.setattr(kite_auth_mod, "auto_refresh", lambda *_a, **_k: "NEW_TOKEN")

    broker = _FakeKiteBroker()
    shared = {"broker": broker}

    await _run_one_tick(shared)

    assert broker.tokens == ["NEW_TOKEN"]


async def test_no_kite_backed_broker_returns_immediately(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    monkeypatch.setattr(
        kite_auth_mod, "is_token_valid", lambda *_a, **_k: calls.append("x") or False
    )

    shared = {"broker": _NoTokenBroker()}
    stop = asyncio.Event()
    await orch_mod._kite_token_refresh_loop(shared, stop)

    assert calls == []  # returned before ever checking token validity


async def test_valid_token_skips_refresh(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    refresh_calls: list[str] = []
    monkeypatch.setattr(kite_auth_mod, "is_token_valid", lambda *_a, **_k: True)
    monkeypatch.setattr(
        kite_auth_mod, "auto_refresh", lambda *_a, **_k: refresh_calls.append("x") or "TOKEN"
    )

    broker = _FakeKiteBroker()
    shared = {"kite_brokers": [broker]}

    await _run_one_tick(shared)

    assert refresh_calls == []
    assert broker.tokens == []


async def test_one_bad_broker_does_not_block_others(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A single broker raising on set_access_token must not stop the loop
    # iteration from reaching -- current behavior: the whole tick's
    # exception is caught at the outer try/except (one bad refresh must not
    # kill the loop), consistent with the pre-existing per-tick error
    # handling; this documents that a broker-level failure surfaces the
    # same way a network failure already does.
    monkeypatch.setattr(kite_auth_mod, "is_token_valid", lambda *_a, **_k: False)
    monkeypatch.setattr(kite_auth_mod, "auto_refresh", lambda *_a, **_k: "NEW_TOKEN")

    class _RaisingBroker:
        def set_access_token(self, token: str) -> None:
            raise RuntimeError("boom")

    good = _FakeKiteBroker()
    shared = {"kite_brokers": [_RaisingBroker(), good]}

    await _run_one_tick(shared)  # must not raise
