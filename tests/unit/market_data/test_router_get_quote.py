"""MarketDataRouter.get_quote: rich quotes only from providers that have them."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from src.brokers.adapters.kite.adapter import KiteAdapter
from src.brokers.adapters.kite.auth import KiteAuth
from src.market_data.provider_config import ProviderConfig
from src.market_data.router import MarketDataRouter, NoMarketDataProviderError


class _LtpOnly:
    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return {s: 1.0 for s in symbols}


class _Quoting:
    def __init__(self) -> None:
        self.calls = 0

    async def get_quote(self, symbols: list[str]) -> dict[str, Any]:
        self.calls += 1
        return {s: {"last_price": 2.0, "ohlc": {"close": 1.5}} for s in symbols}


def _cfg(*names: str) -> ProviderConfig:
    return ProviderConfig(tuple(names), 3, 300.0, {})


async def test_skips_providers_without_get_quote() -> None:
    q = _Quoting()
    router = MarketDataRouter({"angel_one": _LtpOnly(), "kite": q}, _cfg("angel_one", "kite"))
    out = await router.get_quote(["NSE:X"])
    assert out == {"NSE:X": {"last_price": 2.0, "ohlc": {"close": 1.5}}}
    assert q.calls == 1


async def test_no_quoting_provider_raises_no_provider() -> None:
    router = MarketDataRouter({"angel_one": _LtpOnly()}, _cfg("angel_one"))
    with pytest.raises(NoMarketDataProviderError):
        await router.get_quote(["NSE:X"])


async def test_kite_adapter_get_quote_prefixes_bare_and_keeps_caller_keys() -> None:
    adapter = KiteAdapter(KiteAuth(api_key="k", api_secret="s", user_id="AB1"))
    seen: dict[str, Any] = {}

    def _quote(syms: list[str]) -> dict[str, Any]:
        seen["syms"] = syms
        return {s: {"last_price": 10.0, "ohlc": {"close": 9.0}} for s in syms}

    with patch.object(adapter._kite, "quote", side_effect=_quote):
        out = await adapter.get_quote(["INFY", "NSE:NIFTY 50"])
    assert seen["syms"] == ["NSE:INFY", "NSE:NIFTY 50"]
    assert set(out) == {"INFY", "NSE:NIFTY 50"}
    assert out["INFY"]["ohlc"]["close"] == 9.0
