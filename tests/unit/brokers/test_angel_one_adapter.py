"""Unit tests for src/brokers/adapters/angel_one/adapter.py."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from src.brokers.adapters.angel_one.adapter import AngelOneAdapter
from src.brokers.adapters.angel_one.auth import AngelOneAuth
from src.brokers.base import GTTRequest, OrderRequest, OrderWindowClosed

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

    assert n_first == 2
    assert n_second == 2
    fake_session_cls.assert_called_once()  # only one real download
    assert adapter._token_cache["SBIN-EQ"] == ("3045", "NSE")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_load_instrument_tokens_failure_is_non_fatal() -> None:
    adapter = _make_adapter()
    with patch(
        "src.brokers.adapters.angel_one.adapter.aiohttp.ClientSession",
        side_effect=RuntimeError("network down"),
    ):
        n = await adapter.load_instrument_tokens("NSE")
    assert n == 0
    assert adapter._token_cache == {}


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


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ltp_resolves_token_and_calls_ltp_data() -> None:
    adapter = _make_adapter()
    adapter._token_cache["SBIN-EQ"] = ("3045", "NSE")

    with patch.object(
        adapter._sc,
        "ltpData",
        return_value={"status": True, "data": {"ltp": "191"}},
    ) as mock_ltp:
        result = await adapter.get_ltp(["SBIN-EQ"])

    assert result == {"SBIN-EQ": 191.0}
    mock_ltp.assert_called_once_with("NSE", "SBIN-EQ", "3045")


# ── place_order ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_order_market_translates_product_and_type() -> None:
    adapter = _make_adapter()
    adapter._token_cache["SBIN-EQ"] = ("3045", "NSE")
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
async def test_place_order_amo_after_market_close() -> None:
    adapter = _make_adapter()
    adapter._token_cache["SBIN-EQ"] = ("3045", "NSE")
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
    adapter._token_cache["SBIN-EQ"] = ("3045", "NSE")
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
    adapter._token_cache["SBIN-EQ"] = ("3045", "NSE")
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
    adapter._token_cache["SBIN-EQ"] = ("3045", "NSE")
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


# ── place_gtt (single-trigger only) ────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_gtt_two_leg_is_rejected() -> None:
    """Angel One's GTT rule has one price/triggerprice pair only — a two-leg
    SL+target request (this system's only real usage today, via
    order_lifecycle.py) has no broker-side equivalent and must fail loudly."""
    adapter = _make_adapter()
    adapter._token_cache["SBIN-EQ"] = ("3045", "NSE")

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
    adapter._token_cache["SBIN-EQ"] = ("3045", "NSE")

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
    adapter._token_cache["RELIANCE-EQ"] = ("2885", "NSE")

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
