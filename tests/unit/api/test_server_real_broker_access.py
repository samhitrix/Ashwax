"""The REAL BrokerAccess -> MarketDataRouter path in the API process (review I1, I2, I4).

Opted out of conftest's Kite-backed fake (`real_broker_access`): the router is built by
broker_access's own factory from a `market_data` block, with fake broker adapters (the Kite
one is the real KiteAdapter over a fake SDK). No broker or DB is contacted.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from src.api import broker_access as ba
from src.api import server
from src.brokers import factory
from src.brokers.adapters.kite.adapter import KiteAdapter
from src.brokers.base import Margin, SessionStatus

pytestmark = pytest.mark.real_broker_access

_CRED_VARS = (
    "KITE_API_KEY",
    "KITE_ACCESS_TOKEN",
    "ANGEL_ONE_API_KEY",
    "ANGEL_ONE_CLIENT_CODE",
    "ANGEL_ONE_ACCESS_TOKEN",
    "FLATTRADE_API_KEY",
    "FLATTRADE_USER_ID",
    "FLATTRADE_ACCESS_TOKEN",
)
_KITE_ENV = {"KITE_API_KEY": "k"}
_FT_ENV = {"FLATTRADE_API_KEY": "k", "FLATTRADE_USER_ID": "u"}
_ANGEL_ENV = {"ANGEL_ONE_API_KEY": "k", "ANGEL_ONE_CLIENT_CODE": "c"}
LIVE_ORDER = {"market_data": {"providers": ["flattrade", "kite", "angel_one"]}}


# ── fakes ────────────────────────────────────────────────────────────────────


def _row(last: float, prev: float) -> dict[str, Any]:
    return {"last_price": last, "ohlc": {"close": prev}}


class _FakeKiteSDK:
    """kiteconnect.KiteConnect stand-in: quote/ltp/margins/profile from fixed data."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        for i, s in enumerate(server._NIFTY50_SYMBOLS):
            self.rows[s] = _row(100.0 + (i % 3) - 1, 100.0)  # mix of up/down/flat
        for i, s in enumerate(server._SECTOR_INDICES.values()):
            self.rows[s] = _row(1000.0 + 10 * i, 1000.0)
        self.rows["NSE:NIFTY 50"] = _row(25010.0, 25000.0)
        self.rows["NSE:INFY"] = _row(1500.5, 1490.0)
        self.quote_calls: list[list[str]] = []

    def quote(self, keys: list[str]) -> dict[str, Any]:
        self.quote_calls.append(list(keys))
        return {k: self.rows[k] for k in keys if k in self.rows}

    def ltp(self, keys: list[str]) -> dict[str, Any]:
        return {k: {"last_price": self.rows[k]["last_price"]} for k in keys if k in self.rows}

    def margins(self) -> dict[str, Any]:
        return {
            "equity": {
                "net": 250_000.0,
                "available": {"cash": 200_000.0, "live_balance": 180_000.0, "collateral": 0.0},
                "utilised": {"debits": 20_000.0},
            }
        }

    def profile(self) -> dict[str, Any]:
        return {"user_id": "AB1", "user_name": "A", "email": "a@x"}


class _NoLimit:
    async def acquire(self, *_a: Any) -> None:
        return None


class _Auth:
    def __init__(self, sdk: _FakeKiteSDK) -> None:
        self.kite = sdk

    def set_access_token(self, _t: str) -> None:
        return None


class _FakeFlattrade:
    """LTP-only REST provider; records every call (each is one GetQuotes per symbol)."""

    def __init__(self, price: float = 77.0) -> None:
        self.price = price
        self.ltp_calls: list[list[str]] = []
        self.option_calls = 0

    async def get_ltp(self, keys: list[str]) -> dict[str, float]:
        self.ltp_calls.append(list(keys))
        return dict.fromkeys(keys, self.price)

    async def get_option_quotes(self, keys: list[str]) -> Any:
        self.option_calls += 1
        raise AssertionError("option chain must not be fetched from a rate-capped provider")

    async def get_instruments(self, exchange: str) -> Any:
        self.option_calls += 1
        raise AssertionError("option chain must not be fetched from a rate-capped provider")

    def set_access_token(self, _t: str) -> None:
        return None


class _Account:
    def __init__(self, margin: Margin | None = None, hang: bool = False) -> None:
        self.margin = margin or Margin(equity=1.0, available_cash=1.0, used_margin=0.0)
        self.hang = hang
        self.session_calls = 0
        self.margin_calls = 0

    async def get_session_status(self) -> SessionStatus:
        self.session_calls += 1
        if self.hang:
            await asyncio.sleep(30)
        return SessionStatus(is_valid=True, user_id="X", message="")

    async def get_margins(self) -> Margin:
        self.margin_calls += 1
        if self.hang:
            await asyncio.sleep(30)
        return self.margin

    def set_access_token(self, _t: str) -> None:
        return None


# ── wiring ───────────────────────────────────────────────────────────────────


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake adapters behind the REAL factory/router; env and global.json set per test."""
    for var in _CRED_VARS:
        monkeypatch.delenv(var, raising=False)
    sdk = _FakeKiteSDK()
    kite = KiteAdapter(_Auth(sdk), rate_limiter=_NoLimit())  # type: ignore[arg-type]
    ft = _FakeFlattrade()
    w: dict[str, Any] = {"sdk": sdk, "kite": kite, "ft": ft, "cfg": LIVE_ORDER}
    monkeypatch.setattr(factory, "_load_global_config", lambda: w["cfg"])
    monkeypatch.setattr(factory, "get_adapter_class", lambda _n: object)
    monkeypatch.setattr(factory, "_create_kite", lambda _c, **_k: kite)
    monkeypatch.setattr(factory, "_create_flattrade", lambda _c, **_k: ft)

    def _no_angel(_c: Any, **_k: Any) -> Any:
        raise RuntimeError("angel not used in these tests")

    monkeypatch.setattr(factory, "_create_angel_one", _no_angel)
    monkeypatch.setattr(server, "_kite", lambda: sdk)
    monkeypatch.setattr(server, "_kite_refreshed", lambda: None)
    monkeypatch.setattr(server, "_db_pool", None)
    server._clear_broker_cache()
    yield w
    server._clear_broker_cache()


def _real_access(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    accounts: dict[str, Any] | None = None,
    clock: Any = time.monotonic,
) -> ba.BrokerAccess:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import os

    access = ba.BrokerAccess(
        Path("unused.env"),
        broker_factory=lambda n: (accounts or {})[n],
        env=os.environ,
        env_reader=lambda _p: {},
        clock=clock,
    )
    monkeypatch.setattr(server, "_access", lambda: access)
    monkeypatch.setattr(server, "_kite_configured", lambda: "KITE_API_KEY" in env)
    return access


class _KiteOnlyAccess:
    """The pre-branch behaviour: every read straight from the Kite SDK (the expected values)."""

    def __init__(self, sdk: _FakeKiteSDK) -> None:
        self.sdk = sdk

    def configured(self) -> list[str]:
        return ["kite"]

    def primary_account(self, legacy: str | None) -> str:
        return "kite"

    def token_present(self, name: str) -> bool:
        return True

    async def ltp(self, keys: list[str]) -> dict[str, Any]:
        return self.sdk.ltp(keys)

    async def quote(self, keys: list[str], *, ltp_fallback: bool = True) -> dict[str, Any]:
        return self.sdk.quote(keys)

    async def margins(self, name: str, **_k: Any) -> Any:
        from src.brokers.adapters.kite.adapter import _margin_from_kite

        return _margin_from_kite(self.sdk.margins())

    async def cached(self, key: str, ttl_s: float, fetch: Any, **_k: Any) -> Any:
        return await fetch()


async def _snapshot() -> dict[str, Any]:
    return {
        "ltp": await server.get_ltp("NSE:NIFTY 50,NSE:INFY"),
        "breadth": await server.get_market_breadth(),
        "sectors": await server.get_sector_performance(),
        "margins": await server.get_margins(),
    }


# ── I4: unchanged with Kite, through the real path ──────────────────────────


async def test_live_provider_order_with_kite_matches_kite_only_values(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_access", lambda: _KiteOnlyAccess(world["sdk"]))
    monkeypatch.setattr(server, "_kite_configured", lambda: True)
    expected = await _snapshot()
    server._clear_broker_cache()

    # Same endpoints through the REAL BrokerAccess, live order [flattrade, kite, angel_one],
    # Kite and Flattrade both credentialed.
    access = _real_access(
        monkeypatch,
        {**_KITE_ENV, **_FT_ENV},
        accounts={"kite": world["kite"], "flattrade": _Account()},
    )
    got = await _snapshot()
    assert got["ltp"] == expected["ltp"]
    assert got["ltp"]["NSE:NIFTY 50"]["ohlc"]["close"] == 25000.0
    assert got["breadth"] == expected["breadth"] and got["breadth"]["source"] == "kite"
    assert got["sectors"] == expected["sectors"] and got["sectors"]
    assert got["margins"]["ashwax"] == expected["margins"]["ashwax"]
    assert got["margins"]["broker"] == "kite"
    # Paper-row LTP (positions / paper_trades / WS) is Kite's too, never Flattrade's.
    assert await access.ltp(["NSE:INFY"]) == {"NSE:INFY": {"last_price": 1500.5}}
    assert access.router().names == ["kite"]
    assert world["ft"].ltp_calls == []


# ── I2: dashboard provider order ─────────────────────────────────────────────


def test_dashboard_cfg_drops_rate_capped_providers_when_others_exist() -> None:
    cfg = {
        "market_data": {
            "providers": ["flattrade", "kite", "angel_one"],
            "per_use": {"ltp": ["flattrade"], "options_chain": ["flattrade", "angel_one"]},
            "failover": {"cooldown_seconds": 60},
        },
        "broker": {"name": "kite"},
    }
    out = ba.dashboard_market_data_cfg(cfg)
    assert out["market_data"]["providers"] == ["kite", "angel_one"]
    assert out["market_data"]["per_use"] == {"options_chain": ["angel_one"]}
    assert out["market_data"]["failover"] == {"cooldown_seconds": 60}
    assert out["broker"] == {"name": "kite"}
    assert cfg["market_data"]["providers"] == ["flattrade", "kite", "angel_one"]  # not mutated
    only_ft = {"market_data": {"providers": ["flattrade"]}}
    assert ba.dashboard_market_data_cfg(only_ft) == only_ft
    assert ba.dashboard_market_data_cfg({}) == {}


async def test_flattrade_only_serves_with_the_longer_price_cache(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = {"t": 0.0}
    access = _real_access(monkeypatch, _FT_ENV, clock=lambda: now["t"])
    assert access.router().names == ["flattrade"] and access.rate_capped()
    assert await access.ltp(["NSE:INFY"]) == {"NSE:INFY": {"last_price": 77.0}}
    now["t"] = 5.0  # next dashboard poll: reused (a 3s TTL would have refetched)
    await access.ltp(["NSE:INFY"])
    now["t"] = ba.RATE_CAPPED_LTP_TTL_SECONDS + 0.1
    await access.ltp(["NSE:INFY"])
    assert world["ft"].ltp_calls == [["NSE:INFY"], ["NSE:INFY"]]
    assert ba.RATE_CAPPED_LTP_TTL_SECONDS >= 10.0


async def test_kite_keeps_the_short_price_cache(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = {"t": 0.0}
    access = _real_access(monkeypatch, {**_KITE_ENV, **_FT_ENV}, clock=lambda: now["t"])
    await access.ltp(["NSE:INFY"])
    now["t"] = 5.0
    await access.ltp(["NSE:INFY"])
    assert not access.rate_capped()
    assert len(world["sdk"].quote_calls) == 0  # ltp, not quote
    assert world["ft"].ltp_calls == []


async def test_option_chain_refused_when_flattrade_is_the_only_provider(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_access(monkeypatch, _FT_ENV)
    out = await server.get_option_chain("NIFTY")
    assert out["rate_limited"] is True and out["strikes"] == [] and "Flattrade" in out["error"]
    assert world["ft"].option_calls == 0


# ── I1: a slow secondary broker never holds up Kite ──────────────────────────


async def test_session_kite_fields_return_promptly_with_a_hanging_angel(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ba, "SECONDARY_TIMEOUT_SECONDS", 0.2)
    angel = _Account(hang=True)
    _real_access(monkeypatch, {**_KITE_ENV, **_ANGEL_ENV}, accounts={"angel_one": angel})
    t0 = time.monotonic()
    out = await server.session_status()
    assert time.monotonic() - t0 < 2.0
    assert out["ok"] is True and out["user_id"] == "AB1"
    cards = {c["broker"]: c for c in out["brokers"]}
    assert cards["kite"]["token_valid"] is True
    assert cards["angel_one"]["token_valid"] is False
    assert "did not answer" in cards["angel_one"]["error"]
    # The failure is reused: the next poll neither waits nor calls Angel again.
    t0 = time.monotonic()
    out = await server.session_status()
    assert time.monotonic() - t0 < 0.15
    assert angel.session_calls == 1
    assert out["ok"] is True


async def test_session_checks_run_concurrently(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ba, "SECONDARY_TIMEOUT_SECONDS", 0.3)
    _real_access(
        monkeypatch,
        {**_KITE_ENV, **_ANGEL_ENV, **_FT_ENV},
        accounts={"angel_one": _Account(hang=True), "flattrade": _Account(hang=True)},
    )
    t0 = time.monotonic()
    out = await server.session_status()
    assert time.monotonic() - t0 < 0.55  # one bound, not one per broker
    assert out["ok"] is True


async def test_failing_kite_profile_is_called_once_per_poll(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def _bad_profile() -> dict[str, Any]:
        calls.append(1)
        raise RuntimeError("Incorrect `api_key` or `access_token`.")

    monkeypatch.setattr(world["sdk"], "profile", _bad_profile)
    _real_access(monkeypatch, _KITE_ENV)
    out = await server.session_status()
    assert out["ok"] is False and "access_token" in out["error"]
    kite_card = next(c for c in out["brokers"] if c["broker"] == "kite")
    assert kite_card["token_valid"] is False
    assert calls == [1]


async def test_margins_returns_kite_funds_without_waiting_on_angel(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "SECONDARY_TIMEOUT_SECONDS", 0.2)
    angel = _Account(hang=True)
    _real_access(
        monkeypatch,
        {**_KITE_ENV, **_ANGEL_ENV},
        accounts={"kite": world["kite"], "angel_one": angel},
    )
    t0 = time.monotonic()
    out = await server.get_margins()
    assert time.monotonic() - t0 < 2.0
    assert out["broker"] == "kite" and out["ashwax"]["available_cash"] == 200_000.0
    assert "did not answer" in out["brokers"]["angel_one"]["error"]
    t0 = time.monotonic()
    await server.get_margins()
    assert time.monotonic() - t0 < 0.15 and angel.margin_calls == 1  # failure reused
