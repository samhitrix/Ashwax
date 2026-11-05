"""Dashboard API without Kite (spec 3.2): market data via the router, funds via each
broker's get_margins(), Kite-only endpoints return their empty shape, per-broker health
cards. Fakes only -- no broker or DB is contacted."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from src.api import server
from src.api.broker_access import BrokerAccess, BrokerNotConfiguredError
from src.brokers.base import Margin, SessionStatus
from src.market_data.router import NoMarketDataProviderError

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
_ANGEL_ENV = {"ANGEL_ONE_API_KEY": "k", "ANGEL_ONE_CLIENT_CODE": "c"}


class _FakeRouter:
    def __init__(self, prices: dict[str, float], quotes: dict[str, Any] | None = None) -> None:
        self.prices = prices
        self.quotes = quotes
        self.tokens: list[tuple[str, str]] = []
        self.ltp_calls: list[list[str]] = []

    async def get_ltp(self, keys: list[str]) -> dict[str, float]:
        self.ltp_calls.append(list(keys))
        return {k: self.prices[k] for k in keys if k in self.prices}

    async def get_quote(self, keys: list[str]) -> dict[str, Any]:
        if self.quotes is None:
            raise NoMarketDataProviderError("no provider has get_quote")
        return {k: self.quotes[k] for k in keys if k in self.quotes}

    def set_provider_token(self, name: str, token: str) -> None:
        self.tokens.append((name, token))


class _FakeAccount:
    def __init__(self, margin: Margin | Exception, valid: bool = True) -> None:
        self.margin = margin
        self.valid = valid
        self.tokens: list[str] = []
        self.margin_calls = 0

    async def get_margins(self) -> Margin:
        self.margin_calls += 1
        if isinstance(self.margin, Exception):
            raise self.margin
        return self.margin

    async def get_session_status(self) -> SessionStatus:
        return SessionStatus(is_valid=self.valid, user_id="X", message="" if self.valid else "bad")

    def set_access_token(self, token: str) -> None:
        self.tokens.append(token)


class _NoKite:
    """Any Kite SDK use in a Kite-less run is a bug."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"Kite SDK used without Kite configured: {name}")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for var in _CRED_VARS:
        monkeypatch.delenv(var, raising=False)
    for k, v in _ANGEL_ENV.items():
        monkeypatch.setenv(k, v)
    import os

    return os.environ  # type: ignore[return-value]


def _no_kite_server(
    monkeypatch: pytest.MonkeyPatch,
    router: Any,
    accounts: dict[str, Any],
    env: Any,
) -> BrokerAccess:
    def _router_factory() -> Any:
        if isinstance(router, Exception):
            raise router
        return router

    access = BrokerAccess(
        Path("unused.env"),
        router_factory=_router_factory,
        broker_factory=lambda name: accounts[name],
        env=env,
        env_reader=lambda _p: {},
    )
    monkeypatch.setattr(server, "_kite_configured", lambda: False)
    monkeypatch.setattr(server, "_access", lambda: access)
    monkeypatch.setattr(server, "_kite", lambda: _NoKite())
    monkeypatch.setattr(server, "_db_pool", None)
    return access


_ANGEL_MARGIN = Margin(equity=500_000.0, available_cash=120_000.0, used_margin=30_000.0)


# ── endpoints with no Kite ───────────────────────────────────────────────────


async def test_ltp_served_by_router_in_kite_shape(monkeypatch, env) -> None:  # type: ignore[no-untyped-def]
    _no_kite_server(monkeypatch, _FakeRouter({"NSE:NIFTY 50": 25000.5}), {}, env)
    out = await server.get_ltp("NSE:NIFTY 50")
    # Frontend reads last_price; without a quoting provider there is no ohlc (change % unknown)
    assert out == {"NSE:NIFTY 50": {"last_price": 25000.5}}


async def test_ltp_passes_rich_quotes_through_when_a_provider_has_them(monkeypatch, env) -> None:  # type: ignore[no-untyped-def]
    q = {"NSE:NIFTY 50": {"last_price": 101.0, "ohlc": {"close": 100.0}}}
    _no_kite_server(monkeypatch, _FakeRouter({}, quotes=q), {}, env)
    assert await server.get_ltp("NSE:NIFTY 50") == q


async def test_margins_from_the_configured_broker(monkeypatch, env) -> None:  # type: ignore[no-untyped-def]
    _no_kite_server(monkeypatch, _FakeRouter({}), {"angel_one": _FakeAccount(_ANGEL_MARGIN)}, env)
    out = await server.get_margins()
    assert out["broker"] == "angel_one"
    assert out["ashwax"] == {
        "available_cash": 120_000.0,
        "used_margin": 30_000.0,
        "collateral": 0.0,
        "net": 500_000.0,
    }
    assert set(out["brokers"]) == {"angel_one"}


async def test_margins_failure_gives_no_funds_block(monkeypatch, env) -> None:  # type: ignore[no-untyped-def]
    acct = _FakeAccount(RuntimeError("session expired"))
    _no_kite_server(monkeypatch, _FakeRouter({}), {"angel_one": acct}, env)
    out = await server.get_margins()
    assert "ashwax" not in out and out["error"] == "session expired"


async def test_options_funds_live_side_uses_configured_broker(monkeypatch, env) -> None:  # type: ignore[no-untyped-def]
    from src.options import seller_settings as _ss

    monkeypatch.setattr(_ss, "load_settings", lambda: {"options_capital_rupees": 100_000.0})
    _no_kite_server(monkeypatch, _FakeRouter({}), {"angel_one": _FakeAccount(_ANGEL_MARGIN)}, env)
    out = await server.get_options_funds_margins()
    assert out["live"]["fundAvailable"] == 120_000.0
    assert out["live"]["marginAvailable"] == 500_000.0


async def test_kite_only_endpoints_return_empty_shapes_without_touching_kite(
    monkeypatch, env
) -> None:  # type: ignore[no-untyped-def]
    _no_kite_server(monkeypatch, _FakeRouter({}), {}, env)
    assert server.get_orders() == []
    assert server.get_holdings() == []
    assert server.get_gtt() == []
    assert await server.get_positions() == {"net": [], "day": []}


async def test_breadth_and_sectors_are_empty_without_previous_close(monkeypatch, env) -> None:  # type: ignore[no-untyped-def]
    prices = {s: 100.0 for s in [*server._NIFTY50_SYMBOLS, *server._SECTOR_INDICES.values()]}
    router = _FakeRouter(prices)
    _no_kite_server(monkeypatch, router, {}, env)
    assert (await server.get_market_breadth())["source"] == "unavailable"
    assert await server.get_sector_performance() == []
    # useless without a previous close: no LTP calls spent (REST-only providers are rate-capped)
    assert router.ltp_calls == []


async def test_ltp_reuses_fresh_prices_across_endpoints(monkeypatch, env) -> None:  # type: ignore[no-untyped-def]
    router = _FakeRouter({"NSE:A": 1.0, "NSE:B": 2.0})
    access = _no_kite_server(monkeypatch, router, {}, env)
    await access.ltp(["NSE:A"])
    out = await access.ltp(["NSE:A", "NSE:B"])
    assert out == {"NSE:A": {"last_price": 1.0}, "NSE:B": {"last_price": 2.0}}
    assert router.ltp_calls == [["NSE:A"], ["NSE:B"]]  # A served from the 3s cache


async def test_no_provider_at_all_is_an_error_not_mock(monkeypatch, env) -> None:  # type: ignore[no-untyped-def]
    for k in _ANGEL_ENV:
        monkeypatch.delenv(k)
    _no_kite_server(monkeypatch, NoMarketDataProviderError("nothing eligible"), {}, env)
    assert "error" in await server.get_ltp("NSE:NIFTY 50")
    out = await server.get_margins()
    assert out == {"error": "no broker configured", "brokers": {}}


async def test_session_without_kite_reports_per_broker_cards(monkeypatch, env) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ANGEL_ONE_ACCESS_TOKEN", "jwt")
    _no_kite_server(monkeypatch, _FakeRouter({}), {"angel_one": _FakeAccount(_ANGEL_MARGIN)}, env)
    out = await server.session_status()
    assert out["ok"] is False and out["configured"] is False
    cards = {c["broker"]: c for c in out["brokers"]}
    assert list(cards) == ["kite", "angel_one", "flattrade"]
    assert cards["kite"]["configured"] is False and cards["kite"]["token_valid"] is None
    assert cards["angel_one"] == {
        "broker": "angel_one",
        "label": "Angel One",
        "configured": True,
        "token_present": True,
        "token_valid": True,
        "rest_connected": True,
        "error": None,
    }
    assert cards["flattrade"]["configured"] is False


async def test_session_card_reports_invalid_token(monkeypatch, env) -> None:  # type: ignore[no-untyped-def]
    acct = _FakeAccount(_ANGEL_MARGIN, valid=False)
    _no_kite_server(monkeypatch, _FakeRouter({}), {"angel_one": acct}, env)
    cards = {c["broker"]: c for c in (await server.session_status())["brokers"]}
    assert cards["angel_one"]["token_valid"] is False
    assert cards["angel_one"]["error"] == "bad"


# ── backward compatibility with Kite configured (legacy conftest world) ──────


async def test_session_with_kite_keeps_legacy_fields_and_adds_cards(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class _K:
        def profile(self) -> dict[str, Any]:
            return {"user_id": "AB1", "user_name": "A", "email": "a@x"}

    server._clear_broker_cache()
    monkeypatch.setenv("KITE_API_KEY", "k")
    monkeypatch.setattr(server, "_kite", lambda: _K())
    out = await server.session_status()
    assert {k: out[k] for k in ("ok", "user_id", "user_name", "email")} == {
        "ok": True,
        "user_id": "AB1",
        "user_name": "A",
        "email": "a@x",
    }
    kite_card = next(c for c in out["brokers"] if c["broker"] == "kite")
    assert kite_card["token_valid"] is True and kite_card["rest_connected"] is True
    server._clear_broker_cache()


# ── BrokerAccess ─────────────────────────────────────────────────────────────


def test_account_refuses_an_unconfigured_broker(env) -> None:  # type: ignore[no-untyped-def]
    access = BrokerAccess(Path("x"), broker_factory=lambda n: _FakeAccount(_ANGEL_MARGIN), env=env)
    assert access.configured() == ["angel_one"]
    with pytest.raises(BrokerNotConfiguredError):
        access.account("kite")


def test_primary_account_prefers_legacy_broker_when_configured(env) -> None:  # type: ignore[no-untyped-def]
    access = BrokerAccess(Path("x"), env={**env, "KITE_API_KEY": "k"})
    assert access.primary_account("kite") == "kite"
    assert access.primary_account("flattrade") == "kite"  # not configured -> first configured
    assert BrokerAccess(Path("x"), env={}).primary_account("kite") is None


def test_router_failure_is_retried_after_interval() -> None:
    now = {"t": 0.0}
    calls: list[int] = []

    def _factory() -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise NoMarketDataProviderError("none")
        return _FakeRouter({})

    access = BrokerAccess(Path("x"), router_factory=_factory, clock=lambda: now["t"])
    assert access.router() is None
    assert access.router() is None and len(calls) == 1  # inside the retry window
    now["t"] = 61.0
    assert access.router() is not None and len(calls) == 2


def test_tokens_from_env_file_are_pushed_by_name_once_per_change() -> None:
    router = _FakeRouter({})
    acct = _FakeAccount(_ANGEL_MARGIN)
    file_env = {"FLATTRADE_ACCESS_TOKEN": "ft1"}
    access = BrokerAccess(
        Path("x"),
        router_factory=lambda: router,
        broker_factory=lambda n: acct,
        env={"FLATTRADE_API_KEY": "k", "FLATTRADE_USER_ID": "u"},
        env_reader=lambda _p: file_env,
        clock=lambda: 0.0,
    )
    access.router()
    access.account("flattrade")
    access.sync_tokens(force=True)
    access.sync_tokens(force=True)  # unchanged token: not pushed again
    file_env["FLATTRADE_ACCESS_TOKEN"] = "ft2"  # manual login wrote a new one
    access.sync_tokens(force=True)
    assert router.tokens == [("flattrade", "ft1"), ("flattrade", "ft2")]
    assert acct.tokens == ["ft1", "ft2"]


async def test_margins_are_cached_briefly(env) -> None:  # type: ignore[no-untyped-def]
    acct = _FakeAccount(_ANGEL_MARGIN)
    access = BrokerAccess(
        Path("x"), broker_factory=lambda n: acct, env=env, env_reader=lambda _p: {}
    )
    await access.margins("angel_one")
    await access.margins("angel_one")
    assert acct.margin_calls == 1


async def test_failed_read_retries_once_after_a_token_rotated_in_env() -> None:
    """A mid-day re-login within the 60s token-sync throttle must not cost a failed poll."""

    class _StaleRouter:
        def __init__(self) -> None:
            self.token = "old"
            self.calls = 0

        def set_provider_token(self, _name: str, token: str) -> None:
            self.token = token

        async def get_ltp(self, keys: list[str]) -> dict[str, float]:
            self.calls += 1
            if self.token == "old":
                raise RuntimeError("invalid token")
            return {k: 1.0 for k in keys}

    router = _StaleRouter()
    file_env = {"KITE_ACCESS_TOKEN": "old"}
    access = BrokerAccess(
        Path("x"),
        router_factory=lambda: router,
        env={},
        env_reader=lambda _p: file_env,
        clock=lambda: 0.0,
    )
    access.sync_tokens(force=True)  # throttle window now open for 60s
    file_env["KITE_ACCESS_TOKEN"] = "new"
    out = await access.ltp(["NSE:X"])
    assert out == {"NSE:X": {"last_price": 1.0}}
    assert router.calls == 2


async def test_failed_read_is_not_retried_when_no_token_changed() -> None:
    router = _FakeRouter({})
    calls: list[int] = []

    async def _boom(keys: list[str]) -> dict[str, float]:
        calls.append(1)
        raise RuntimeError("down")

    router.get_ltp = _boom  # type: ignore[method-assign]
    access = BrokerAccess(
        Path("x"), router_factory=lambda: router, env={}, env_reader=lambda _p: {}
    )
    with pytest.raises(RuntimeError, match="down"):
        await access.ltp(["NSE:X"])
    assert len(calls) == 1
