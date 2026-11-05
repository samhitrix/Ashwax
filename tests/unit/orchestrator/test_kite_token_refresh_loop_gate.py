"""The Kite token refresh loop must not run on a machine with no Kite credentials."""

from __future__ import annotations

import asyncio

import src.brokers.kite_auth as kite_auth_mod
import src.orchestrator as orch_mod


class _FakeBroker:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    def set_access_token(self, token: str) -> None:
        self.tokens.append(token)


async def test_loop_returns_promptly_without_kite_api_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.setattr(kite_auth_mod, "is_token_valid", lambda *_a, **_k: False)
    monkeypatch.setattr(kite_auth_mod, "auto_refresh", lambda *_a, **_k: "NEW")
    broker = _FakeBroker()

    await asyncio.wait_for(
        orch_mod._kite_token_refresh_loop({"kite_brokers": [broker]}, asyncio.Event()), 2.0
    )

    assert broker.tokens == []


async def test_loop_runs_when_kite_api_key_set(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("KITE_API_KEY", "k")
    checks: list[str] = []
    monkeypatch.setattr(
        kite_auth_mod, "is_token_valid", lambda *_a, **_k: checks.append("x") or True
    )
    stop = asyncio.Event()
    task = asyncio.create_task(
        orch_mod._kite_token_refresh_loop({"kite_brokers": [_FakeBroker()]}, stop)
    )
    await asyncio.sleep(0.05)
    assert not task.done()  # inside the while-loop, not early-returned
    stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert checks == ["x"]
