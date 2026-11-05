"""Unit tests for src/market_data/router.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from src.market_data.provider_config import ProviderConfig
from src.market_data.router import MarketDataRouter

CFG = ProviderConfig(("a", "b"), max_consecutive_failures=2, cooldown_seconds=100.0, per_use={})


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _prov(**methods: object) -> MagicMock:
    p = MagicMock()
    for name, val in methods.items():
        setattr(p, name, val)
    return p


async def test_first_provider_serves() -> None:
    a = _prov(get_ltp=AsyncMock(return_value={"X": 1.0}))
    b = _prov(get_ltp=AsyncMock(return_value={"X": 2.0}))
    r = MarketDataRouter({"a": a, "b": b}, CFG)
    assert await r.get_ltp(["X"]) == {"X": 1.0}
    b.get_ltp.assert_not_called()


async def test_fails_over_and_reports_source() -> None:
    df = pd.DataFrame({"close": [1.0]})
    a = _prov(get_ohlcv=AsyncMock(side_effect=RuntimeError("boom")))
    b = _prov(get_ohlcv=AsyncMock(return_value=df))
    r = MarketDataRouter({"a": a, "b": b}, CFG)
    out, src = await r.get_ohlcv_with_source("NSE:X", "day", 5)
    assert src == "b" and out is df


async def test_cooldown_skips_failing_provider_then_recovers() -> None:
    clock = Clock()
    a = _prov(get_ltp=AsyncMock(side_effect=RuntimeError("down")))
    b = _prov(get_ltp=AsyncMock(return_value={"X": 2.0}))
    r = MarketDataRouter({"a": a, "b": b}, CFG, clock=clock)
    await r.get_ltp(["X"])  # a fails (1), b serves
    await r.get_ltp(["X"])  # a fails (2) -> cooling, b serves
    calls = a.get_ltp.await_count
    await r.get_ltp(["X"])  # a skipped while cooling
    assert a.get_ltp.await_count == calls
    clock.t += 101.0
    await r.get_ltp(["X"])  # cooldown over -> a retried
    assert a.get_ltp.await_count == calls + 1


async def test_single_provider_never_blacked_out() -> None:
    cfg = ProviderConfig(("a",), 1, 1000.0, {})
    a = _prov(get_ltp=AsyncMock(side_effect=[RuntimeError("x"), {"X": 9.0}]))
    r = MarketDataRouter({"a": a}, cfg)
    with pytest.raises(RuntimeError):
        await r.get_ltp(["X"])
    assert await r.get_ltp(["X"]) == {"X": 9.0}  # retried despite cooldown


async def test_all_fail_reraises_last_original_exception() -> None:
    a = _prov(get_ltp=AsyncMock(side_effect=TimeoutError("a")))
    b = _prov(get_ltp=AsyncMock(side_effect=TimeoutError("b")))
    r = MarketDataRouter({"a": a, "b": b}, CFG)
    with pytest.raises(TimeoutError, match="b"):
        await r.get_ltp(["X"])


async def test_not_implemented_is_not_a_health_failure() -> None:
    a = _prov(get_order_margin=AsyncMock(side_effect=NotImplementedError("no")))
    b = _prov(get_order_margin=AsyncMock(return_value="margin"))
    r = MarketDataRouter({"a": a, "b": b}, CFG)
    for _ in range(5):
        assert await r.get_order_margin([]) == "margin"
    assert a.get_order_margin.await_count == 5  # never put on cooldown


async def test_unverified_angel_margin_gap_never_cools_it_and_next_provider_serves() -> None:
    """The real Angel adapter ships get_order_margin gated off (NotImplementedError); the
    router must treat that as a capability gap, keep Angel healthy for LTP/OHLCV, and let the
    next provider serve the margin."""
    from src.brokers.adapters.angel_one.adapter import AngelOneAdapter
    from src.brokers.adapters.angel_one.auth import AngelOneAuth

    angel = AngelOneAdapter(AngelOneAuth(api_key="k", client_code="C1"))
    angel.get_ltp = AsyncMock(return_value={"X": 1.0})  # type: ignore[method-assign]
    kite = _prov(get_order_margin=AsyncMock(return_value="margin"), get_ltp=AsyncMock())
    r = MarketDataRouter({"a": angel, "b": kite}, CFG)
    for _ in range(5):  # > max_consecutive_failures (2)
        assert await r.get_order_margin([]) == "margin"
    assert await r.get_ltp(["X"]) == {"X": 1.0}  # still first choice for LTP: never cooled
    kite.get_ltp.assert_not_called()


async def test_per_use_override_changes_order() -> None:
    cfg = ProviderConfig(("a", "b"), 2, 100.0, {"options_chain": ("b", "a")})
    a = _prov(get_option_quotes=AsyncMock(return_value={"o": "a"}))
    b = _prov(get_option_quotes=AsyncMock(return_value={"o": "b"}))
    r = MarketDataRouter({"a": a, "b": b}, cfg)
    assert await r.get_option_quotes(["o"]) == {"o": "b"}


async def test_priority_ltp_falls_back_to_get_ltp() -> None:
    a = MagicMock(spec=["get_ltp"])
    a.get_ltp = AsyncMock(return_value={"X": 3.0})
    r = MarketDataRouter({"a": a}, CFG)
    assert await r.priority_ltp(["X"]) == {"X": 3.0}


def test_no_set_access_token_on_router() -> None:
    r = MarketDataRouter({"a": MagicMock()}, CFG)
    assert not hasattr(r, "set_access_token")


def test_empty_providers_rejected() -> None:
    with pytest.raises(ValueError):
        MarketDataRouter({}, CFG)


def test_set_provider_token_reaches_only_the_named_provider() -> None:
    a, b = _prov(), _prov()
    r = MarketDataRouter({"a": a, "b": b}, CFG)
    r.set_provider_token("a", "T")
    a.set_access_token.assert_called_once_with("T")
    b.set_access_token.assert_not_called()


def test_set_provider_token_is_silent_noop_for_unknown_or_tokenless_provider() -> None:
    tokenless = MagicMock(spec=[])
    r = MarketDataRouter({"a": tokenless}, CFG)
    r.set_provider_token("a", "T")
    r.set_provider_token("missing", "T")


async def test_key_error_falls_through_and_never_cools_the_provider() -> None:
    # KeyError = "this provider cannot resolve this symbol": symbol-specific, not ill health.
    a = _prov(get_ohlcv=AsyncMock(side_effect=KeyError("NSE:ODD")))
    df = pd.DataFrame({"close": [1.0]})
    b = _prov(get_ohlcv=AsyncMock(return_value=df))
    r = MarketDataRouter({"a": a, "b": b}, CFG)
    for _ in range(10):
        out, src = await r.get_ohlcv_with_source("NSE:ODD", "day", 5)
        assert src == "b" and out is df
    assert a.get_ohlcv.await_count == 10  # still tried every time, never put on cooldown


async def test_cooling_provider_is_last_resort_when_others_only_lack_the_capability() -> None:
    clock = Clock()
    a = _prov(get_order_margin=AsyncMock(side_effect=NotImplementedError("no")))
    b = _prov(get_order_margin=AsyncMock(side_effect=[RuntimeError("1"), RuntimeError("2"), "m"]))
    r = MarketDataRouter({"a": a, "b": b}, CFG, clock=clock)
    for _ in range(2):  # b fails twice -> cooling
        with pytest.raises(RuntimeError):
            await r.get_order_margin([])
    # a is healthy but cannot serve; b is cooling yet is the only one that can.
    assert await r.get_order_margin([]) == "m"


async def test_cooling_provider_not_retried_when_a_healthy_one_really_failed() -> None:
    clock = Clock()
    a = _prov(get_ltp=AsyncMock(side_effect=RuntimeError("a down")))
    b = _prov(get_ltp=AsyncMock(side_effect=RuntimeError("b down")))
    cfg = ProviderConfig(("a", "b"), 1, 100.0, {})
    r = MarketDataRouter({"a": a, "b": b}, cfg, clock=clock)
    with pytest.raises(RuntimeError):
        await r.get_ltp(["X"])  # both fail once -> both cooling
    with pytest.raises(RuntimeError, match="b down"):
        await r.get_ltp(["X"])  # all cooling -> try all, re-raise the last original error


async def test_all_key_error_reraises_key_error() -> None:
    a = _prov(get_ohlcv=AsyncMock(side_effect=KeyError("a")))
    b = _prov(get_ohlcv=AsyncMock(side_effect=KeyError("b")))
    r = MarketDataRouter({"a": a, "b": b}, CFG)
    with pytest.raises(KeyError, match="b"):
        await r.get_ohlcv("NSE:ODD", "day", 5)


async def test_all_capability_gaps_without_key_error_raise_no_provider_error() -> None:
    from src.market_data.router import NoMarketDataProviderError

    a = _prov(get_order_margin=AsyncMock(side_effect=NotImplementedError("no")))
    b = MagicMock(spec=[])  # no such method at all
    r = MarketDataRouter({"a": a, "b": b}, CFG)
    with pytest.raises(NoMarketDataProviderError):
        await r.get_order_margin([])


async def test_failover_warning_only_on_first_failure_and_cooldown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    cfg = ProviderConfig(("a", "b"), 3, 100.0, {})
    a = _prov(get_ltp=AsyncMock(side_effect=RuntimeError("down")))
    b = _prov(get_ltp=AsyncMock(return_value={"X": 1.0}))
    r = MarketDataRouter({"a": a, "b": b}, cfg, clock=Clock())
    with caplog.at_level(logging.DEBUG, logger="src.market_data.router"):
        for _ in range(3):
            await r.get_ltp(["X"])
    levels = [x.levelno for x in caplog.records if "market_data_failover" in x.getMessage()]
    assert levels == [logging.WARNING, logging.DEBUG, logging.WARNING]  # 1st, 2nd, 3rd=cooldown
