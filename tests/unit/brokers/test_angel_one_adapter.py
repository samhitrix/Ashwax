"""Unit tests for src/brokers/adapters/angel_one/adapter.py."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from src.brokers.adapters.angel_one import adapter as adapter_module
from src.brokers.adapters.angel_one.adapter import (
    AngelOneAdapter,
    AngelOneMarginError,
    _to_instrument,
)
from src.brokers.adapters.angel_one.auth import AngelOneAuth
from src.brokers.base import GTTRequest, OrderRequest, OrderWindowClosed
from src.brokers.rate_limiter import ANGEL_ONE_RATE_LIMITS

_IST = ZoneInfo("Asia/Kolkata")


def _make_adapter() -> AngelOneAdapter:
    auth = AngelOneAuth(api_key="test_key", client_code="C123456")
    return AngelOneAdapter(auth)


def _order_req(**kw: Any) -> OrderRequest:
    base: dict[str, Any] = {
        "symbol": "SBIN-EQ",
        "exchange": "NSE",
        "direction": "BUY",
        "quantity": 10,
        "order_type": "MARKET",
        "product": "INTRADAY",
        "validity": "DAY",
        "agent_trade_id": "AT001",
        "signal_id": "SIG001",
    }
    base.update(kw)
    return OrderRequest(**base)


class _FakeAsyncCtx:
    """Minimal async context manager stand-in for aiohttp responses/sessions."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _fake_aiohttp_session(dump: list[dict[str, Any]]) -> MagicMock:
    """Build a mock replacing aiohttp.ClientSession() covering the
    `async with aiohttp.ClientSession() as http: async with http.get(...) as resp:` chain."""
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = AsyncMock(return_value=dump)

    fake_http = MagicMock()
    fake_http.get = MagicMock(return_value=_FakeAsyncCtx(fake_resp))

    return MagicMock(return_value=_FakeAsyncCtx(fake_http))


_MASTER_DUMP = [
    {
        "token": "3045",
        "symbol": "SBIN-EQ",
        "name": "SBIN",
        "exch_seg": "NSE",
        "instrumenttype": "",
        "lotsize": "1",
        "tick_size": "5.00",
        "expiry": "",
        "strike": "-1",
    },
    {
        "token": "2885",
        "symbol": "RELIANCE-EQ",
        "name": "RELIANCE",
        "exch_seg": "NSE",
        "instrumenttype": "",
        "lotsize": "1",
        "tick_size": "5.00",
        "expiry": "",
        "strike": "-1",
    },
]


# ── Auth delegation ────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_login_delegates_to_auth() -> None:
    adapter = _make_adapter()
    fake_info = MagicMock()
    with patch.object(adapter._auth, "login", new_callable=AsyncMock, return_value=fake_info):
        result = await adapter.login()
    assert result is fake_info


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_session_status_delegates() -> None:
    adapter = _make_adapter()
    fake_status = MagicMock()
    with patch.object(
        adapter._auth, "get_session_status", new_callable=AsyncMock, return_value=fake_status
    ):
        result = await adapter.get_session_status()
    assert result is fake_status


# ── subscribe_ticks / unsubscribe_ticks ────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_subscribe_ticks_records_tokens() -> None:
    adapter = _make_adapter()
    await adapter.subscribe_ticks(["3045", "2885"])
    assert "3045" in adapter._subscribed_tokens
    assert "2885" in adapter._subscribed_tokens


@pytest.mark.asyncio  # type: ignore[misc]
async def test_unsubscribe_ticks_removes_tokens() -> None:
    adapter = _make_adapter()
    await adapter.subscribe_ticks(["3045", "2885"])
    await adapter.unsubscribe_ticks(["3045"])
    assert "3045" not in adapter._subscribed_tokens
    assert "2885" in adapter._subscribed_tokens


# ── load_instrument_tokens / _resolve_token ────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_load_instrument_tokens_populates_cache_and_is_idempotent() -> None:
    adapter = _make_adapter()
    fake_session_cls = _fake_aiohttp_session(_MASTER_DUMP)

    with patch("src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession", fake_session_cls):
        n_first = await adapter.load_instrument_tokens("NSE")
        n_second = await adapter.load_instrument_tokens("NSE")  # already cached

    assert n_first == 4  # 2 raw keys + 2 bare -EQ aliases
    assert n_second == 4
    fake_session_cls.assert_called_once()  # only one real download
    assert adapter._token_cache["NSE"]["SBIN-EQ"] == ("3045", "NSE")


_REAL_ROWS = [
    {
        "token": "2885",
        "symbol": "RELIANCE-EQ",
        "name": "RELIANCE",
        "expiry": "",
        "strike": "-1.000000",
        "lotsize": "1",
        "instrumenttype": "",
        "exch_seg": "NSE",
        "tick_size": "10.000000",
    },
    {
        "token": "1594",
        "symbol": "INFY-EQ",
        "name": "INFY",
        "expiry": "",
        "strike": "-1.000000",
        "lotsize": "1",
        "instrumenttype": "",
        "exch_seg": "NSE",
        "tick_size": "5.000000",
    },
    {
        "token": "26000",
        "symbol": "NIFTY",
        "name": "NIFTY",
        "expiry": "",
        "strike": "-1.000000",
        "lotsize": "1",
        "instrumenttype": "",
        "exch_seg": "NSE",
        "tick_size": "0.000000",
    },
    {
        "token": "99926000",
        "symbol": "Nifty 50",
        "name": "NIFTY",
        "expiry": "",
        "strike": "-1.000000",
        "lotsize": "1",
        "instrumenttype": "AMXIDX",
        "exch_seg": "NSE",
        "tick_size": "0.000000",
    },
    {
        "token": "777",
        "symbol": "XYZ-BE",
        "name": "XYZ",
        "expiry": "",
        "strike": "-1.000000",
        "lotsize": "1",
        "instrumenttype": "",
        "exch_seg": "NSE",
        "tick_size": "5.000000",
    },
    {
        "token": "500",
        "symbol": "RELIANCE-EQ",
        "name": "RELIANCE",
        "expiry": "",
        "strike": "-1.000000",
        "lotsize": "1",
        "instrumenttype": "",
        "exch_seg": "BSE",
        "tick_size": "5.000000",
    },
]


# Real rows from findings B5 (2026-10-15 scrip master inspection).
_INDEX_NFO_ROWS = [
    *_REAL_ROWS,
    {
        "token": "99926009",
        "symbol": "Nifty Bank",
        "name": "BANKNIFTY",
        "expiry": "",
        "strike": "-1.000000",
        "lotsize": "1",
        "instrumenttype": "AMXIDX",
        "exch_seg": "NSE",
        "tick_size": "0.000000",
    },
    {
        "token": "99926017",
        "symbol": "India VIX",
        "name": "INDIAVIX",
        "expiry": "",
        "strike": "-1.000000",
        "lotsize": "1",
        "instrumenttype": "AMXIDX",
        "exch_seg": "NSE",
        "tick_size": "0.000000",
    },
    {
        "token": "26009",
        "symbol": "BANKNIFTY",
        "name": "BANKNIFTY",
        "expiry": "",
        "strike": "-1.000000",
        "lotsize": "1",
        "instrumenttype": "",
        "exch_seg": "NSE",
        "tick_size": "0.000000",
    },
    {
        "token": "39756",
        "symbol": "NIFTY09NOV2620300CE",
        "name": "NIFTY",
        "expiry": "09NOV2026",
        "strike": "2030000.000000",
        "lotsize": "65",
        "instrumenttype": "OPTIDX",
        "exch_seg": "NFO",
        "tick_size": "5.000000",
    },
    {
        "token": "48704",
        "symbol": "NIFTY27OCT26FUT",
        "name": "NIFTY",
        "expiry": "27OCT2026",
        "strike": "-1.000000",
        "lotsize": "65",
        "instrumenttype": "FUTIDX",
        "exch_seg": "NFO",
        "tick_size": "10.000000",
    },
]


@pytest.mark.asyncio  # type: ignore[misc]
@pytest.mark.parametrize(
    ("symbol", "token"),
    [
        ("NSE:NIFTY 50", "99926000"),
        ("NIFTY 50", "99926000"),
        ("Nifty 50", "99926000"),
        ("nifty 50", "99926000"),
        ("NSE:NIFTY", "99926000"),
        ("NIFTY", "99926000"),
        ("NSE:NIFTY BANK", "99926009"),
        ("Nifty Bank", "99926009"),
        ("NSE:BANKNIFTY", "99926009"),
        ("NSE:INDIA VIX", "99926017"),
        ("India VIX", "99926017"),
    ],
)
async def test_index_names_resolve_to_amxidx_token(symbol: str, token: str) -> None:
    adapter = _make_adapter()
    with patch(
        "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
        _fake_aiohttp_session(_INDEX_NFO_ROWS),
    ):
        assert await adapter._resolve_token(symbol) == (token, "NSE")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_index_alias_does_not_disturb_equity_aliases() -> None:
    adapter = _make_adapter()
    with patch(
        "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
        _fake_aiohttp_session(_INDEX_NFO_ROWS),
    ):
        assert await adapter._resolve_token("RELIANCE") == ("2885", "NSE")
        # raw master symbols that are not aliased keep resolving as before
        assert await adapter._resolve_token("XYZ-BE") == ("777", "NSE")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_nfo_loads_after_nse_and_each_exchange_downloads_once() -> None:
    adapter = _make_adapter()
    fake_session_cls = _fake_aiohttp_session(_INDEX_NFO_ROWS)
    with patch("src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession", fake_session_cls):
        n_nse = await adapter.load_instrument_tokens("NSE")
        assert n_nse > 0
        n_nfo = await adapter.load_instrument_tokens("NFO")
        assert n_nfo == 2  # the option row and the future row
        assert await adapter.load_instrument_tokens("NFO") == 2
        assert await adapter.load_instrument_tokens("NSE") == n_nse
    assert fake_session_cls.call_count == 2  # one download per exchange, none repeated
    assert adapter._token_cache["NFO"]["NIFTY09NOV2620300CE"] == ("39756", "NFO")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_nfo_symbol_resolves_after_nse_loaded() -> None:
    adapter = _make_adapter()
    with patch(
        "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
        _fake_aiohttp_session(_INDEX_NFO_ROWS),
    ):
        assert await adapter._resolve_token("RELIANCE") == ("2885", "NSE")  # NSE loads first
        assert await adapter._resolve_token("NFO:NIFTY09NOV2620300CE") == ("39756", "NFO")
        assert await adapter._resolve_token("NIFTY27OCT26FUT", exchange="NFO") == ("48704", "NFO")
        # an NFO symbol is not an NSE symbol
        with pytest.raises(KeyError, match="no Angel One symboltoken"):
            await adapter._resolve_token("NIFTY09NOV2620300CE")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_same_symbol_on_two_exchanges_keeps_each_token() -> None:
    adapter = _make_adapter()
    with patch(
        "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
        _fake_aiohttp_session(_INDEX_NFO_ROWS),
    ):
        assert await adapter._resolve_token("NSE:RELIANCE-EQ") == ("2885", "NSE")
        assert await adapter._resolve_token("BSE:RELIANCE-EQ") == ("500", "BSE")
        assert await adapter._resolve_token("NSE:RELIANCE-EQ") == ("2885", "NSE")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_index_name_alias_never_clobbers_an_equity_and_warns_once() -> None:
    rows = [
        {
            "token": "555",
            "symbol": "FINNIFTY-EQ",
            "name": "FINNIFTY",
            "expiry": "",
            "strike": "-1.000000",
            "lotsize": "1",
            "instrumenttype": "",
            "exch_seg": "NSE",
            "tick_size": "5.000000",
        },
        {
            "token": "99926037",
            "symbol": "Nifty Fin Service",
            "name": "FINNIFTY",
            "expiry": "",
            "strike": "-1.000000",
            "lotsize": "1",
            "instrumenttype": "AMXIDX",
            "exch_seg": "NSE",
            "tick_size": "0.000000",
        },
    ]
    adapter = _make_adapter()
    with (
        patch(
            "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
            _fake_aiohttp_session(rows),
        ),
        patch.object(adapter_module, "_log") as mock_log,
    ):
        await adapter.load_instrument_tokens("NSE")

    nse = adapter._token_cache["NSE"]
    assert nse["FINNIFTY"] == ("555", "NSE")  # the equity alias survives
    assert nse["Nifty Fin Service"] == ("99926037", "NSE")  # index symbol forms still resolve
    assert nse["NIFTY FIN SERVICE"] == ("99926037", "NSE")
    assert mock_log.warning.call_count == 1


@pytest.mark.asyncio  # type: ignore[misc]
async def test_index_bare_name_alias_limited_to_well_known_index_names() -> None:
    rows = [
        {
            "token": "99926033",
            "symbol": "Nifty IT",
            "name": "NIFTYIT",
            "expiry": "",
            "strike": "-1.000000",
            "lotsize": "1",
            "instrumenttype": "AMXIDX",
            "exch_seg": "NSE",
            "tick_size": "0.000000",
        },
    ]
    adapter = _make_adapter()
    with patch(
        "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
        _fake_aiohttp_session(rows),
    ):
        await adapter.load_instrument_tokens("NSE")
    nse = adapter._token_cache["NSE"]
    assert nse["Nifty IT"] == nse["NIFTY IT"] == ("99926033", "NSE")
    assert "NIFTYIT" not in nse  # not a name the codebase passes bare


def test_to_instrument_strike_parse_is_numeric() -> None:
    rows = {r["symbol"]: r for r in _INDEX_NFO_ROWS}
    assert _to_instrument(rows["NIFTY09NOV2620300CE"]).strike == 20300.0
    assert _to_instrument(rows["NIFTY27OCT26FUT"]).strike is None  # '-1.000000'
    assert _to_instrument(rows["RELIANCE-EQ"]).strike is None
    assert _to_instrument({**rows["RELIANCE-EQ"], "strike": ""}).strike is None
    assert _to_instrument({**rows["RELIANCE-EQ"], "strike": "-5.5"}).strike is None  # any negative


@pytest.mark.asyncio  # type: ignore[misc]
async def test_bare_and_prefixed_equity_symbols_resolve_to_eq_token() -> None:
    adapter = _make_adapter()
    with patch(
        "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
        _fake_aiohttp_session(_REAL_ROWS),
    ):
        assert await adapter._resolve_token("RELIANCE") == ("2885", "NSE")
        assert await adapter._resolve_token("NSE:RELIANCE") == ("2885", "NSE")
        assert await adapter._resolve_token("RELIANCE-EQ") == ("2885", "NSE")
        assert await adapter._resolve_token("INFY") == ("1594", "NSE")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_non_eq_series_creates_no_bare_alias() -> None:
    adapter = _make_adapter()
    with patch(
        "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
        _fake_aiohttp_session(_REAL_ROWS),
    ):
        await adapter.load_instrument_tokens("NSE")
    assert "XYZ-BE" in adapter._token_cache["NSE"]
    assert "XYZ" not in adapter._token_cache["NSE"]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_equity_alias_never_overwrites_index_raw_key() -> None:
    adapter = _make_adapter()
    with patch(
        "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
        _fake_aiohttp_session(_REAL_ROWS),
    ):
        await adapter.load_instrument_tokens("NSE")
    # A1: the bare index name now resolves to the AMXIDX row (99926000), the one
    # Angel's docs use for candles/quotes, not the raw token-26000 "NIFTY" row.
    assert adapter._token_cache["NSE"]["NIFTY"] == ("99926000", "NSE")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_no_bare_alias_for_non_nse_exchange() -> None:
    adapter = _make_adapter()
    with patch(
        "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
        _fake_aiohttp_session(_REAL_ROWS),
    ):
        await adapter.load_instrument_tokens("BSE")
    assert adapter._token_cache == {"BSE": {"RELIANCE-EQ": ("500", "BSE")}}


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_instruments_nse_returns_bare_equity_symbols() -> None:
    adapter = _make_adapter()
    with patch(
        "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
        _fake_aiohttp_session(_REAL_ROWS),
    ):
        instruments = await adapter.get_instruments("NSE")

    by_symbol = {i.symbol: i for i in instruments}
    assert "RELIANCE-EQ" not in by_symbol
    assert "INFY-EQ" not in by_symbol
    rel = by_symbol["RELIANCE"]
    assert rel.instrument_type == "EQ"
    assert rel.instrument_token == "2885"
    assert rel.exchange == "NSE"
    # same predicate instrument_master.valid_nse_symbols applies
    valid = {i.symbol for i in instruments if i.instrument_type == "EQ" and i.exchange == "NSE"}
    assert {"RELIANCE", "INFY"} <= valid
    # non-EQ-series rows are untouched by the post-processing
    assert by_symbol["XYZ-BE"].instrument_token == "777"
    assert by_symbol["Nifty 50"].instrument_type == "AMXIDX"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_instruments_non_nse_unchanged() -> None:
    adapter = _make_adapter()
    with patch(
        "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
        _fake_aiohttp_session(_REAL_ROWS),
    ):
        instruments = await adapter.get_instruments("BSE")
    assert [i.symbol for i in instruments] == ["RELIANCE-EQ"]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_load_instrument_tokens_failure_is_non_fatal() -> None:
    adapter = _make_adapter()
    with patch(
        "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
        side_effect=RuntimeError("network down"),
    ):
        n = await adapter.load_instrument_tokens("NSE")
    assert n == 0
    assert not adapter._token_cache.get("NSE")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_resolve_token_raises_clear_error_when_unresolvable() -> None:
    adapter = _make_adapter()
    with (
        patch(
            "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
            side_effect=RuntimeError("network down"),
        ),
        pytest.raises(KeyError, match="no Angel One symboltoken"),
    ):
        await adapter._resolve_token("UNKNOWN-EQ")


# ── get_ltp ────────────────────────────────────────────────────────────────────


def _ltp_responder(calls: list[tuple[str, dict[str, list[str]]]]) -> Any:
    """Fake getMarketData: records (mode, exchangeTokens); LTP = token / 10."""

    def _fake(mode: str, exchange_tokens: dict[str, list[str]]) -> dict[str, Any]:
        calls.append((mode, exchange_tokens))
        fetched = [
            {"exchange": exch, "tradingSymbol": f"T{tok}", "symbolToken": tok, "ltp": int(tok) / 10}
            for exch, toks in exchange_tokens.items()
            for tok in toks
        ]
        return {"status": True, "data": {"fetched": fetched, "unfetched": []}}

    return _fake


def _no_wait_limiter(adapter: AngelOneAdapter) -> MagicMock:
    limiter = MagicMock()
    limiter.acquire = AsyncMock(return_value=0.0)
    adapter._limiter = limiter
    return limiter


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ltp_batches_via_market_data_ltp_mode() -> None:
    adapter = _make_adapter()
    adapter._token_cache["NSE"] = {"SBIN-EQ": ("3045", "NSE")}
    calls: list[tuple[str, dict[str, list[str]]]] = []
    with patch.object(adapter._sc, "getMarketData", side_effect=_ltp_responder(calls)):
        result = await adapter.get_ltp(["SBIN-EQ"])

    assert result == {"SBIN-EQ": 304.5}
    assert calls == [("LTP", {"NSE": ["3045"]})]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ltp_120_symbols_make_3_calls_and_map_back_to_requested_keys() -> None:
    adapter = _make_adapter()
    limiter = _no_wait_limiter(adapter)
    adapter._token_cache["NSE"] = {f"S{i}-EQ": (str(1000 + i), "NSE") for i in range(120)}
    # mix bare and NSE:-prefixed spellings: result keys must be the exact requested strings
    requested = [f"S{i}-EQ" if i % 2 else f"NSE:S{i}-EQ" for i in range(120)]
    calls: list[tuple[str, dict[str, list[str]]]] = []
    with patch.object(adapter._sc, "getMarketData", side_effect=_ltp_responder(calls)):
        result = await adapter.get_ltp(requested)

    assert len(calls) == 3  # ceil(120 / 50)
    assert all(mode == "LTP" for mode, _ in calls)
    assert [len(et["NSE"]) for _, et in calls] == [50, 50, 20]
    assert set(result) == set(requested)
    assert result["NSE:S0-EQ"] == 100.0
    assert result["S119-EQ"] == 111.9
    assert [c.args for c in limiter.acquire.await_args_list] == [("quote",)] * 3


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ltp_groups_per_exchange_and_dedups_tokens() -> None:
    adapter = _make_adapter()
    adapter._token_cache["NSE"] = {"SBIN-EQ": ("3045", "NSE")}
    adapter._token_cache["NFO"] = {"NIFTY27OCT26FUT": ("48704", "NFO")}
    calls: list[tuple[str, dict[str, list[str]]]] = []
    with patch.object(adapter._sc, "getMarketData", side_effect=_ltp_responder(calls)):
        result = await adapter.get_ltp(["SBIN-EQ", "NSE:SBIN-EQ", "NFO:NIFTY27OCT26FUT"])

    assert sorted(tuple(et) for _, et in calls) == [("NFO",), ("NSE",)]
    assert [et for _, et in calls if "NSE" in et] == [{"NSE": ["3045"]}]  # deduped
    assert result == {"SBIN-EQ": 304.5, "NSE:SBIN-EQ": 304.5, "NFO:NIFTY27OCT26FUT": 4870.4}


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ltp_symbol_missing_from_response_is_absent() -> None:
    adapter = _make_adapter()
    adapter._token_cache["NSE"] = {"A-EQ": ("1", "NSE"), "B-EQ": ("2", "NSE")}
    response = {
        "status": True,
        "data": {"fetched": [{"symbolToken": "1", "ltp": 50}], "unfetched": [{"symbolToken": "2"}]},
    }
    with patch.object(adapter._sc, "getMarketData", return_value=response):
        assert await adapter.get_ltp(["A-EQ", "B-EQ"]) == {"A-EQ": 50.0}


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ltp_failed_batch_leaves_symbols_absent() -> None:
    adapter = _make_adapter()
    adapter._token_cache["NSE"] = {"A-EQ": ("1", "NSE")}
    with patch.object(adapter._sc, "getMarketData", return_value={"status": False, "data": None}):
        assert await adapter.get_ltp(["A-EQ"]) == {}


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ltp_unresolvable_symbol_still_raises_keyerror() -> None:
    adapter = _make_adapter()
    adapter._token_cache["NSE"] = {"A-EQ": ("1", "NSE")}
    with (
        patch(
            "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
            side_effect=RuntimeError("network down"),
        ),
        patch.object(adapter._sc, "getMarketData") as mock_md,
        pytest.raises(KeyError, match="no Angel One symboltoken"),
    ):
        await adapter.get_ltp(["A-EQ", "NOPE-EQ"])
    mock_md.assert_not_called()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ltp_empty_makes_no_call() -> None:
    adapter = _make_adapter()
    with patch.object(adapter._sc, "getMarketData") as mock_md:
        assert await adapter.get_ltp([]) == {}
    mock_md.assert_not_called()


def test_angel_quote_bucket_is_one_per_second() -> None:
    # findings B1: doc text says 1 req/s for market quote, table says 10/s -> use the safe 1.
    assert ANGEL_ONE_RATE_LIMITS["quote"] == 1.0
    assert ANGEL_ONE_RATE_LIMITS["historical"] == 3.0


# ── get_ohlcv (chunked history) ────────────────────────────────────────────────

_NOW = datetime(2026, 10, 15, 15, 30, tzinfo=_IST)
_FMT = "%Y-%m-%d %H:%M"
_OHLCV_COLS = ["date", "open", "high", "low", "close", "volume"]


def _iso(text: str) -> str:
    return datetime.strptime(text, _FMT).strftime("%Y-%m-%dT%H:%M:00+05:30")


def _boundary_candles(params: dict[str, Any]) -> dict[str, Any]:
    """Fake getCandleData: one candle at each window edge (newest first), so
    adjacent windows share exactly one timestamp and the order exercises the sort."""
    return {
        "status": True,
        "data": [
            [_iso(params["todate"]), 2, 3, 1, 2.5, 20],
            [_iso(params["fromdate"]), 1, 2, 0.5, 1.5, 10],
        ],
    }


async def _fetch(
    adapter: AngelOneAdapter, interval: str, days: int, fake: Any
) -> tuple[Any, list[dict[str, Any]], MagicMock]:
    calls: list[dict[str, Any]] = []

    def _recording(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params)
        return fake(params)  # type: ignore[no-any-return]

    limiter = _no_wait_limiter(adapter)
    adapter._token_cache["NSE"] = {"SBIN-EQ": ("3045", "NSE")}
    with (
        patch("src.brokers.adapters.angel_one.adapter.datetime") as mock_dt,
        patch.object(adapter._sc, "getCandleData", side_effect=_recording),
    ):
        mock_dt.now.return_value = _NOW
        df = await adapter.get_ohlcv("NSE:SBIN-EQ", interval, days)
    return df, calls, limiter


@pytest.mark.asyncio  # type: ignore[misc]
@pytest.mark.parametrize(
    ("interval", "angel_interval", "max_days", "days", "n_windows"),
    [
        ("minute", "ONE_MINUTE", 30, 10, 1),
        ("minute", "ONE_MINUTE", 30, 29, 1),
        ("minute", "ONE_MINUTE", 30, 30, 2),
        ("minute", "ONE_MINUTE", 30, 90, 4),
        ("3minute", "THREE_MINUTE", 60, 120, 3),
        ("5minute", "FIVE_MINUTE", 100, 99, 1),
        ("5minute", "FIVE_MINUTE", 100, 100, 2),
        ("10minute", "TEN_MINUTE", 100, 250, 3),
        ("15minute", "FIFTEEN_MINUTE", 200, 400, 3),
        ("30minute", "THIRTY_MINUTE", 200, 401, 3),
        ("60minute", "ONE_HOUR", 400, 800, 3),
        ("day", "ONE_DAY", 2000, 2500, 2),
    ],
)
async def test_get_ohlcv_splits_into_windows_within_interval_max(
    interval: str, angel_interval: str, max_days: int, days: int, n_windows: int
) -> None:
    adapter = _make_adapter()
    df, calls, _ = await _fetch(adapter, interval, days, _boundary_candles)

    assert len(calls) == n_windows
    assert {c["interval"] for c in calls} == {angel_interval}
    spans = [
        (datetime.strptime(c["fromdate"], _FMT), datetime.strptime(c["todate"], _FMT))
        for c in calls
    ]
    # one day under the vendor max: its day-count inclusivity is unverified, and a loud
    # rejection on every multi-window fetch is worse than one extra request
    assert all(to > frm and (to - frm) <= timedelta(days=max_days - 1) for frm, to in spans)
    # contiguous, oldest -> newest, covering exactly [now - days, now]
    assert spans[0][0] == (_NOW - timedelta(days=days)).replace(tzinfo=None)
    assert spans[-1][1] == _NOW.replace(tzinfo=None)
    assert all(spans[i][1] == spans[i + 1][0] for i in range(len(spans) - 1))
    assert not df.empty


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_merges_sorted_deduped_with_ist_timestamps() -> None:
    adapter = _make_adapter()
    df, calls, limiter = await _fetch(adapter, "minute", 90, _boundary_candles)

    assert len(calls) == 4
    assert list(df.columns) == _OHLCV_COLS
    assert len(df) == 5  # 4 windows x 2 edge candles; 3 shared boundaries collapse
    assert df["date"].is_monotonic_increasing
    assert df["date"].is_unique
    assert str(df["date"].dt.tz) == "Asia/Kolkata"
    assert str(df["date"].dtype) == "datetime64[ns, Asia/Kolkata]"  # same as the empty frame
    assert df["date"].iloc[-1] == _NOW
    assert df["open"].iloc[0] == 1  # oldest edge candle of the first window
    assert [c.args for c in limiter.acquire.await_args_list] == [("historical",)] * 4


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_all_empty_returns_empty_frame_with_columns() -> None:
    adapter = _make_adapter()
    df, calls, _ = await _fetch(adapter, "minute", 90, lambda _p: {"status": True, "data": []})
    assert len(calls) == 4
    assert df.empty
    assert list(df.columns) == _OHLCV_COLS
    # tz-aware dtype even when empty, so callers using .dt never fail
    assert str(df["date"].dtype) == "datetime64[ns, Asia/Kolkata]"
    assert str(df["date"].dt.tz) == "Asia/Kolkata"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_empty_window_is_skipped() -> None:
    adapter = _make_adapter()
    n_calls = 0

    def _fake(params: dict[str, Any]) -> dict[str, Any]:
        nonlocal n_calls
        n_calls += 1
        if n_calls == 1:
            return {"status": True, "data": []}
        return {"status": True, "data": [[_iso(params["fromdate"]), 1, 2, 0.5, 1.5, 10]]}

    df, calls, _ = await _fetch(adapter, "minute", 58, _fake)
    assert len(calls) == 2
    assert len(df) == 1


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_one_failing_window_raises_no_partial_history() -> None:
    adapter = _make_adapter()
    n_calls = 0

    def _fake(params: dict[str, Any]) -> dict[str, Any]:
        nonlocal n_calls
        n_calls += 1
        if n_calls == 2:
            return {"status": False, "message": "Access denied", "data": None}
        return {"status": True, "data": [[_iso(params["fromdate"]), 1, 2, 0.5, 1.5, 10]]}

    with pytest.raises(ValueError, match="getCandleData failed"):
        await _fetch(adapter, "minute", 90, _fake)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_unknown_interval_rejected() -> None:
    adapter = _make_adapter()
    with pytest.raises(ValueError, match="unrecognized interval"):
        await adapter.get_ohlcv("SBIN-EQ", "7minute", 5)


# ── place_order ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_order_market_translates_product_and_type() -> None:
    adapter = _make_adapter()
    adapter._token_cache["NSE"] = {"SBIN-EQ": ("3045", "NSE")}
    fixed = datetime(2026, 7, 29, 11, 0, tzinfo=_IST)  # Wed 11:00 IST — regular session

    with (
        patch("src.brokers.adapters.angel_one.adapter.datetime") as mock_dt,
        patch.object(adapter._sc, "placeOrder", return_value="ORD001") as mock_place,
    ):
        mock_dt.now.return_value = fixed
        order_id = await adapter.place_order(_order_req())

    assert order_id == "ORD001"
    params = mock_place.call_args.args[0]
    assert params["producttype"] == "INTRADAY"
    assert params["ordertype"] == "MARKET"
    assert params["variety"] == "NORMAL"
    assert params["symboltoken"] == "3045"
    assert params["tradingsymbol"] == "SBIN-EQ"


@pytest.mark.asyncio  # type: ignore[misc]
@pytest.mark.parametrize(("product", "expected"), [("MIS", "INTRADAY"), ("CNC", "DELIVERY")])
async def test_place_order_maps_kite_style_product_aliases(product: str, expected: str) -> None:
    adapter = _make_adapter()
    adapter._token_cache["NSE"] = {"SBIN-EQ": ("3045", "NSE")}
    fixed = datetime(2026, 7, 29, 11, 0, tzinfo=_IST)

    with (
        patch("src.brokers.adapters.angel_one.adapter.datetime") as mock_dt,
        patch.object(adapter._sc, "placeOrder", return_value="ORD001") as mock_place,
    ):
        mock_dt.now.return_value = fixed
        await adapter.place_order(_order_req(product=product))

    assert mock_place.call_args.args[0]["producttype"] == expected


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_order_amo_after_market_close() -> None:
    adapter = _make_adapter()
    adapter._token_cache["NSE"] = {"SBIN-EQ": ("3045", "NSE")}
    fixed = datetime(2026, 7, 29, 16, 5, tzinfo=_IST)  # AMO window

    with (
        patch("src.brokers.adapters.angel_one.adapter.datetime") as mock_dt,
        patch.object(adapter._sc, "placeOrder", return_value="ORD002") as mock_place,
    ):
        mock_dt.now.return_value = fixed
        await adapter.place_order(_order_req())

    assert mock_place.call_args.args[0]["variety"] == "AMO"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_order_refused_in_closed_gap() -> None:
    adapter = _make_adapter()
    adapter._token_cache["NSE"] = {"SBIN-EQ": ("3045", "NSE")}
    fixed = datetime(2026, 7, 29, 15, 47, tzinfo=_IST)  # no-order gap

    with (
        patch("src.brokers.adapters.angel_one.adapter.datetime") as mock_dt,
        patch.object(adapter._sc, "placeOrder") as mock_place,
    ):
        mock_dt.now.return_value = fixed
        with pytest.raises(OrderWindowClosed):
            await adapter.place_order(_order_req())
    mock_place.assert_not_called()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_order_stoploss_overrides_variety_regardless_of_session() -> None:
    """SL/SL-M orders need variety='STOPLOSS', not the session-based NORMAL/AMO
    (verified against SmartAPI forum guidance) — even inside the no-order gap
    where a NORMAL/AMO order would be refused, since STOPLOSS isn't session-gated
    the same way (no OrderWindowClosed expected here)."""
    adapter = _make_adapter()
    adapter._token_cache["NSE"] = {"SBIN-EQ": ("3045", "NSE")}
    fixed = datetime(2026, 7, 29, 11, 0, tzinfo=_IST)

    with (
        patch("src.brokers.adapters.angel_one.adapter.datetime") as mock_dt,
        patch.object(adapter._sc, "placeOrder", return_value="ORD003") as mock_place,
    ):
        mock_dt.now.return_value = fixed
        await adapter.place_order(_order_req(order_type="SL", price=500.0, trigger_price=495.0))

    params = mock_place.call_args.args[0]
    assert params["variety"] == "STOPLOSS"
    assert params["ordertype"] == "STOPLOSS_LIMIT"
    assert params["triggerprice"] == "495.0"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_order_acquires_order_rate_limit() -> None:
    adapter = _make_adapter()
    adapter._token_cache["NSE"] = {"SBIN-EQ": ("3045", "NSE")}
    mock_limiter = MagicMock()
    mock_limiter.acquire = AsyncMock(return_value=0.0)
    adapter._limiter = mock_limiter

    fixed = datetime(2026, 7, 29, 11, 0, tzinfo=_IST)
    with (
        patch("src.brokers.adapters.angel_one.adapter.datetime") as mock_dt,
        patch.object(adapter._sc, "placeOrder", return_value="ORD001"),
    ):
        mock_dt.now.return_value = fixed
        await adapter.place_order(_order_req())

    mock_limiter.acquire.assert_awaited_once_with("order")


# ── get_order_margin (margin batch API) ────────────────────────────────────────

# Documented sample response (findings B2, AO-DOC MarginCalculator).
_MARGIN_OK = {
    "status": True,
    "message": "SUCCESS",
    "errorcode": "",
    "data": {
        "totalMarginRequired": 29612.35,
        "marginComponents": {
            "netPremium": 5060,
            "spanMargin": 0,
            "marginBenefit": 79876.5,
            "deliveryMargin": 0,
            "nonNFOMargin": 0,
            "totOptionsPremium": 10100,
        },
    },
}


def _nfo_leg(symbol: str, direction: str, **kw: Any) -> OrderRequest:
    return _order_req(
        symbol=symbol, exchange="NFO", direction=direction, quantity=65, product="MARGIN", **kw
    )


def _margin_adapter() -> AngelOneAdapter:
    adapter = _make_adapter()
    _no_wait_limiter(adapter)
    adapter._token_cache["NFO"] = {
        "NIFTY09NOV2620300CE": ("39756", "NFO"),
        "NIFTY09NOV2620400CE": ("39757", "NFO"),
    }
    return adapter


@pytest.fixture  # type: ignore[misc]
def margin_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flip the capability flag a paper-basket check would flip in production."""
    monkeypatch.setitem(adapter_module._CAPS, "margin_api_verified", True)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_order_margin_is_gated_off_by_default() -> None:
    """Shipped default is the safe value: the UNVERIFIED margin mapping must not
    serve figures (nor count as provider ill-health) until a paper-basket check."""
    assert adapter_module._CAPS["margin_api"] is True  # vendor capability statement
    assert adapter_module._CAPS["margin_api_verified"] is False
    adapter = _margin_adapter()
    with patch.object(adapter._sc, "getMarginApi") as mock_margin:
        # NotImplementedError comes BEFORE any validation or request, even for
        # input that would otherwise be a ValueError.
        for orders in ([_nfo_leg("NIFTY09NOV2620300CE", "SELL")], [], [_nfo_leg("X", "BUY")] * 51):
            with pytest.raises(NotImplementedError, match="UNVERIFIED"):
                await adapter.get_order_margin(orders)
    mock_margin.assert_not_called()
    adapter._limiter.acquire.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio  # type: ignore[misc]
@pytest.mark.usefixtures("margin_verified")
async def test_get_order_margin_maps_request_and_total() -> None:
    adapter = _margin_adapter()
    legs = [
        _nfo_leg("NIFTY09NOV2620300CE", "SELL"),
        _nfo_leg("NIFTY09NOV2620400CE", "BUY", order_type="LIMIT", price=12.5),
    ]
    with patch.object(adapter._sc, "getMarginApi", return_value=_MARGIN_OK) as mock_margin:
        result = await adapter.get_order_margin(legs)

    assert mock_margin.call_args.args[0] == {
        "positions": [
            {
                "exchange": "NFO",
                "qty": 65,  # UNITS exactly as OrderRequest.quantity (lots-vs-units UNVERIFIED)
                "price": 0,
                "productType": "MARGIN",
                "token": "39756",
                "tradeType": "SELL",
                "orderType": "MARKET",
            },
            {
                "exchange": "NFO",
                "qty": 65,
                "price": 12.5,
                "productType": "MARGIN",
                "token": "39757",
                "tradeType": "BUY",
                "orderType": "LIMIT",
            },
        ]
    }
    assert result.total_margin == 29612.35
    assert result.per_leg == []  # the API returns basket totals only
    assert result.raw == _MARGIN_OK["data"]


@pytest.mark.asyncio  # type: ignore[misc]
@pytest.mark.usefixtures("margin_verified")
async def test_get_order_margin_acquires_a_rate_limit_slot() -> None:
    adapter = _margin_adapter()
    with patch.object(adapter._sc, "getMarginApi", return_value=_MARGIN_OK):
        await adapter.get_order_margin([_nfo_leg("NIFTY09NOV2620300CE", "SELL")])
    adapter._limiter.acquire.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio  # type: ignore[misc]
@pytest.mark.usefixtures("margin_verified")
async def test_get_order_margin_more_than_50_legs_raises_and_sends_nothing() -> None:
    adapter = _margin_adapter()
    legs = [_nfo_leg("NIFTY09NOV2620300CE", "SELL")] * 51
    with (
        patch.object(adapter._sc, "getMarginApi") as mock_margin,
        pytest.raises(ValueError, match="50"),
    ):
        await adapter.get_order_margin(legs)
    mock_margin.assert_not_called()


@pytest.mark.asyncio  # type: ignore[misc]
@pytest.mark.usefixtures("margin_verified")
async def test_get_order_margin_exactly_50_legs_is_sent_whole() -> None:
    adapter = _margin_adapter()
    legs = [_nfo_leg("NIFTY09NOV2620300CE", "SELL")] * 50
    with patch.object(adapter._sc, "getMarginApi", return_value=_MARGIN_OK) as mock_margin:
        await adapter.get_order_margin(legs)
    assert len(mock_margin.call_args.args[0]["positions"]) == 50


@pytest.mark.asyncio  # type: ignore[misc]
@pytest.mark.usefixtures("margin_verified")
@pytest.mark.parametrize(
    "response",
    [
        {"status": False, "message": "Invalid Token", "errorcode": "AG8001", "data": None},
        {"status": True, "message": "SUCCESS", "errorcode": "AB1010", "data": {}},
    ],
)
async def test_get_order_margin_error_response_raises_typed_error(
    response: dict[str, Any],
) -> None:
    adapter = _margin_adapter()
    with (
        patch.object(adapter._sc, "getMarginApi", return_value=response),
        pytest.raises(AngelOneMarginError) as exc_info,
    ):
        await adapter.get_order_margin([_nfo_leg("NIFTY09NOV2620300CE", "SELL")])
    assert response["errorcode"] in str(exc_info.value)


@pytest.mark.asyncio  # type: ignore[misc]
@pytest.mark.usefixtures("margin_verified")
async def test_get_order_margin_success_without_total_raises_not_zero() -> None:
    """A margin of 0 would silently pass any capital gate — fail loudly instead."""
    adapter = _margin_adapter()
    with (
        patch.object(
            adapter._sc,
            "getMarginApi",
            return_value={"status": True, "message": "SUCCESS", "errorcode": "", "data": {}},
        ),
        pytest.raises(AngelOneMarginError, match="totalMarginRequired"),
    ):
        await adapter.get_order_margin([_nfo_leg("NIFTY09NOV2620300CE", "SELL")])


@pytest.mark.asyncio  # type: ignore[misc]
@pytest.mark.usefixtures("margin_verified")
async def test_get_order_margin_unverified_order_types_and_empty_basket_raise() -> None:
    adapter = _margin_adapter()
    with patch.object(adapter._sc, "getMarginApi") as mock_margin:
        with pytest.raises(ValueError, match="orderType"):
            await adapter.get_order_margin(
                [_nfo_leg("NIFTY09NOV2620300CE", "SELL", order_type="SL", trigger_price=5.0)]
            )
        with pytest.raises(ValueError, match="no orders"):
            await adapter.get_order_margin([])
    mock_margin.assert_not_called()


# ── place_gtt (single-trigger only) ────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_gtt_two_leg_is_rejected() -> None:
    """Angel One's GTT rule has one price/triggerprice pair only — a two-leg
    SL+target request (this system's only real usage today, via
    order_lifecycle.py) has no broker-side equivalent and must fail loudly."""
    adapter = _make_adapter()
    adapter._token_cache["NSE"] = {"SBIN-EQ": ("3045", "NSE")}

    with pytest.raises(ValueError, match="single trigger only"):
        await adapter.place_gtt(
            GTTRequest(
                symbol="SBIN-EQ",
                product="DELIVERY",
                gtt_type="TWO_LEG",
                quantity=10,
                agent_trade_id="AT001",
                sl_trigger=490.0,
                sl_order_type="LIMIT",
                sl_price=489.0,
                target_trigger=520.0,
                target_price=520.0,
            )
        )


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_gtt_single_leg_creates_rule() -> None:
    adapter = _make_adapter()
    adapter._token_cache["NSE"] = {"SBIN-EQ": ("3045", "NSE")}

    with patch.object(adapter._sc, "gttCreateRule", return_value=778899) as mock_create:
        gtt_id = await adapter.place_gtt(
            GTTRequest(
                symbol="SBIN-EQ",
                product="DELIVERY",
                gtt_type="SINGLE_LEG",
                quantity=10,
                agent_trade_id="AT001",
                sl_trigger=490.0,
                sl_order_type="LIMIT",
                sl_price=489.0,
            )
        )

    assert gtt_id == "778899"
    params = mock_create.call_args.args[0]
    assert params["symboltoken"] == "3045"
    assert params["transactiontype"] == "SELL"
    assert params["triggerprice"] == 490.0


# ── get_positions (LTP enrichment) ─────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_positions_enriches_with_ltp() -> None:
    """SmartAPI's getPosition() rows carry no live ltp/pnl — get_positions()
    must fetch it separately before normalizing."""
    adapter = _make_adapter()
    adapter._token_cache["NSE"] = {"RELIANCE-EQ": ("2885", "NSE")}

    raw_position = {
        "exchange": "NSE",
        "symboltoken": "2885",
        "producttype": "DELIVERY",
        "tradingsymbol": "RELIANCE-EQ",
        "netqty": "1",
        "avgnetprice": "2235.80",
    }
    with (
        patch.object(
            adapter._sc, "position", return_value={"status": True, "data": [raw_position]}
        ),
        patch.object(adapter, "get_ltp", AsyncMock(return_value={"RELIANCE-EQ": 2250.0})),
    ):
        positions = await adapter.get_positions()

    assert len(positions) == 1
    assert positions[0].last_price == 2250.0
    assert positions[0].pnl == pytest.approx(14.2)  # (2250 - 2235.80) * 1


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_positions_empty() -> None:
    adapter = _make_adapter()
    with patch.object(adapter._sc, "position", return_value={"status": True, "data": []}):
        positions = await adapter.get_positions()
    assert positions == []


# ── capability flags ───────────────────────────────────────────────────────────


def test_capability_flags_default_false() -> None:
    adapter = _make_adapter()
    assert adapter.supports_gtt() is False
    assert adapter.supports_bracket_order() is False
    assert adapter.supports_cover_order() is False


# ── normalization delegation ───────────────────────────────────────────────────


def test_normalize_error_delegates() -> None:
    adapter = _make_adapter()
    err = adapter.normalize_error(
        {"exception_type": "TokenException", "message": "bad token", "code": "403"}
    )
    assert err.code == "TOKEN"
