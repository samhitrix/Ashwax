"""Unit tests for src/brokers/adapters/kite/adapter.py."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from src.brokers.adapters.kite.adapter import KiteAdapter, _to_instrument
from src.brokers.adapters.kite.auth import KiteAuth

_IST = ZoneInfo("Asia/Kolkata")
_TS = "2026-01-15 09:30:00"


def _make_adapter() -> KiteAdapter:
    auth = KiteAuth(api_key="test_key", api_secret="test_secret", user_id="AB1234")
    return KiteAdapter(auth)


def _order_raw(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "order_id": "ORD001",
        "tradingsymbol": "INFY",
        "transaction_type": "BUY",
        "quantity": 10,
        "filled_quantity": 10,
        "pending_quantity": 0,
        "status": "COMPLETE",
        "order_type": "MARKET",
        "product": "MIS",
        "average_price": 1500.0,
        "tag": "",
        "order_timestamp": _TS,
        "exchange_order_id": "EXC001",
        "status_message": None,
    }
    base.update(kw)
    return base


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
    await adapter.subscribe_ticks(["256265", "408065"])
    assert "256265" in adapter._subscribed_tokens
    assert "408065" in adapter._subscribed_tokens


@pytest.mark.asyncio  # type: ignore[misc]
async def test_unsubscribe_ticks_removes_tokens() -> None:
    adapter = _make_adapter()
    await adapter.subscribe_ticks(["256265", "408065"])
    await adapter.unsubscribe_ticks(["256265"])
    assert "256265" not in adapter._subscribed_tokens
    assert "408065" in adapter._subscribed_tokens


# ── get_ltp ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ltp() -> None:
    adapter = _make_adapter()
    with patch.object(
        adapter._kite,
        "ltp",
        return_value={"INFY": {"last_price": 1500.0, "instrument_token": 408065}},
    ):
        result = await adapter.get_ltp(["INFY"])
    assert result == {"INFY": 1500.0}


def _kite_ltp_realistic(syms: list[str]) -> dict[str, dict[str, float]]:
    """Mimic Kite: only exchange-prefixed identifiers ("NSE:INFY") resolve;
    a bare symbol returns nothing — the exact cause of the paper-fill failures."""
    return {s: {"last_price": 1357.1} for s in syms if ":" in s}


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ltp_prefixes_bare_symbol_for_kite() -> None:
    """Regression (2026-06-16): bare symbols were sent to Kite, which returned {}
    → LTP=0 → every paper MARKET fill failed. They must be sent as NSE:SYMBOL."""
    adapter = _make_adapter()
    captured: dict[str, Any] = {}

    def fake_ltp(syms: list[str]) -> dict[str, dict[str, float]]:
        captured["syms"] = syms
        return _kite_ltp_realistic(syms)

    with patch.object(adapter._kite, "ltp", side_effect=fake_ltp):
        result = await adapter.get_ltp(["AXISBANK"])

    assert captured["syms"] == ["NSE:AXISBANK"]  # request was prefixed
    assert result == {"AXISBANK": 1357.1}  # result keyed back to caller input, non-zero


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ltp_preserves_already_prefixed_symbol() -> None:
    adapter = _make_adapter()
    with patch.object(adapter._kite, "ltp", side_effect=_kite_ltp_realistic):
        result = await adapter.get_ltp(["NSE:INDIA VIX"])
    assert result == {"NSE:INDIA VIX": 1357.1}  # not double-prefixed


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ltp_is_time_bounded_and_surfaces_a_hang_as_timeout() -> None:
    """get_ltp sits on the intraday scan loop's first stage; an unbounded Kite call
    there froze every intraday strategy silently. A hang must raise, not block."""
    import src.brokers.adapters.kite.adapter as kite_mod

    adapter = _make_adapter()
    seen: dict[str, Any] = {}
    real_wait_for = kite_mod.asyncio.wait_for

    async def spy_wait_for(aw: Any, timeout: float | None = None) -> Any:
        seen["timeout"] = timeout
        return await real_wait_for(aw, timeout=0.05)  # shrink so the test is fast

    def hang(_syms: list[str]) -> dict[str, Any]:
        import time

        time.sleep(0.5)
        return {}

    with (
        patch.object(kite_mod.asyncio, "wait_for", spy_wait_for),
        patch.object(adapter._kite, "ltp", side_effect=hang),
        pytest.raises(TimeoutError),
    ):
        await adapter.get_ltp(["INFY"])

    assert seen["timeout"] is not None and seen["timeout"] > 0


# ── get_ohlcv ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_returns_dataframe() -> None:
    adapter = _make_adapter()
    fake_records = [
        {
            "date": datetime(2026, 1, 14),
            "open": 1490.0,
            "high": 1510.0,
            "low": 1485.0,
            "close": 1505.0,
            "volume": 100000,
        },
        {
            "date": datetime(2026, 1, 15),
            "open": 1505.0,
            "high": 1520.0,
            "low": 1500.0,
            "close": 1515.0,
            "volume": 120000,
        },
    ]
    with (
        patch.object(adapter._kite, "quote", return_value={"INFY": {"instrument_token": 408065}}),
        patch.object(adapter._kite, "historical_data", return_value=fake_records),
    ):
        df = await adapter.get_ohlcv("INFY", "day", 5)
    assert len(df) == 2
    assert "close" in df.columns


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_falls_back_when_quote_key_does_not_match_exactly() -> None:
    """Regression 2026-09-08: quote()'s response key doesn't always match the
    requested key exactly — get_ohlcv used to index quote[symbol] directly and
    raise a bare, undiagnosable KeyError. It should recover the same way
    get_ltp already does, using whichever single entry Kite actually returned."""
    adapter = _make_adapter()
    with (
        patch.object(adapter._kite, "quote", return_value={"NSE:HEG": {"instrument_token": 12345}}),
        patch.object(adapter._kite, "historical_data", return_value=[]),
    ):
        df = await adapter.get_ohlcv("HEG", "day", 5)
    assert df is not None


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_rate_limits_quote_and_historical_separately() -> None:
    """Regression 2026-09-08: get_ohlcv makes two Kite API calls (quote,
    historical_data) with two different documented rate caps (1/sec, 3/sec)
    but only acquired one shared bucket — the quote() sub-call ran unthrottled
    against its real limit and got silently starved under concurrent load.
    Both buckets must be acquired."""
    adapter = _make_adapter()
    acquired: list[str] = []
    orig_acquire = adapter._limiter.acquire

    async def _tracking_acquire(category: str = "general", tokens: float = 1.0) -> float:
        acquired.append(category)
        return await orig_acquire(category, tokens)

    with (
        patch.object(adapter._limiter, "acquire", side_effect=_tracking_acquire),
        patch.object(adapter._kite, "quote", return_value={"INFY": {"instrument_token": 408065}}),
        patch.object(adapter._kite, "historical_data", return_value=[]),
    ):
        await adapter.get_ohlcv("INFY", "day", 5)

    assert acquired == ["quote", "historical"]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_raises_clear_error_when_quote_empty() -> None:
    """When Kite genuinely returns no quote data for a symbol, the error must
    say so plainly instead of surfacing a bare KeyError('NSE:SYM')."""
    adapter = _make_adapter()
    with (
        patch.object(adapter._kite, "quote", return_value={}),
        pytest.raises(ValueError, match="no quote data returned"),
    ):
        await adapter.get_ohlcv("HEG", "day", 5)


# ── instrument token cache (2026-09-15: ~500-symbol intraday scan cycles were
# taking ~34 min because get_ohlcv() re-resolved each symbol's (static)
# instrument_token via a live quote() call, serially, against Kite's 1 req/sec
# quote cap) ─────────────────────────────────────────────────────────────────


def _instrument_row(symbol: str, token: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tradingsymbol": symbol,
        "instrument_token": token,
        "exchange": "NSE",
        "segment": "NSE",
    }
    base.update(kw)
    return base


@pytest.mark.asyncio  # type: ignore[misc]
async def test_load_instrument_tokens_populates_cache_and_is_idempotent() -> None:
    adapter = _make_adapter()
    dump = [_instrument_row("INFY", 408065), _instrument_row("RELIANCE", 738561)]
    with patch.object(adapter._kite, "instruments", return_value=dump) as mock_instruments:
        n_first = await adapter.load_instrument_tokens()
        n_second = await adapter.load_instrument_tokens()  # already cached — no reload
    assert n_first == 2
    assert n_second == 2
    assert adapter._token_cache == {"INFY": 408065, "RELIANCE": 738561}
    mock_instruments.assert_called_once_with("NSE")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_load_instrument_tokens_failure_is_non_fatal() -> None:
    """A broken/timed-out dump fetch must degrade to an empty cache (get_ohlcv's
    existing per-symbol quote() fallback still works), never raise."""
    adapter = _make_adapter()
    with patch.object(adapter._kite, "instruments", side_effect=RuntimeError("network down")):
        n = await adapter.load_instrument_tokens()
    assert n == 0
    assert adapter._token_cache == {}


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_skips_quote_when_token_cached() -> None:
    adapter = _make_adapter()
    adapter._token_cache["INFY"] = 408065
    with (
        patch.object(adapter._kite, "quote") as mock_quote,
        patch.object(adapter._kite, "historical_data", return_value=[]),
    ):
        df = await adapter.get_ohlcv("INFY", "day", 5)
    mock_quote.assert_not_called()
    assert df is not None


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_cache_lookup_normalizes_exchange_prefix() -> None:
    """The orchestrator always calls get_ohlcv with an "NSE:"-prefixed symbol
    (f"NSE:{sym}"); the cache is keyed by the bare tradingsymbol from Kite's
    instrument dump, so the prefix must be stripped before the cache lookup."""
    adapter = _make_adapter()
    adapter._token_cache["INFY"] = 408065
    with (
        patch.object(adapter._kite, "quote") as mock_quote,
        patch.object(adapter._kite, "historical_data", return_value=[]),
    ):
        await adapter.get_ohlcv("NSE:INFY", "day", 5)
    mock_quote.assert_not_called()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_only_acquires_historical_bucket_when_token_cached() -> None:
    adapter = _make_adapter()
    adapter._token_cache["INFY"] = 408065
    acquired: list[str] = []
    orig_acquire = adapter._limiter.acquire

    async def _tracking_acquire(category: str = "general", tokens: float = 1.0) -> float:
        acquired.append(category)
        return await orig_acquire(category, tokens)

    with (
        patch.object(adapter._limiter, "acquire", side_effect=_tracking_acquire),
        patch.object(adapter._kite, "historical_data", return_value=[]),
    ):
        await adapter.get_ohlcv("INFY", "day", 5)
    assert acquired == ["historical"]  # no "quote" bucket touched at all


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_falls_back_to_quote_for_uncached_symbol_and_caches_result() -> None:
    """A symbol the bulk dump doesn't have (or before any dump was loaded) must
    still resolve via the existing per-symbol quote() path — and, once resolved
    that way, must be cached so a second call for the same symbol skips quote()."""
    adapter = _make_adapter()
    with (
        patch.object(
            adapter._kite, "quote", return_value={"INFY": {"instrument_token": 408065}}
        ) as mock_quote,
        patch.object(adapter._kite, "historical_data", return_value=[]),
    ):
        await adapter.get_ohlcv("INFY", "day", 5)
        await adapter.get_ohlcv("INFY", "day", 5)
    assert mock_quote.call_count == 1  # second call used the now-cached token
    assert adapter._token_cache["INFY"] == 408065


# ── place_order ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_order_translates_product() -> None:
    adapter = _make_adapter()
    from src.brokers.base import OrderRequest

    req = OrderRequest(
        symbol="INFY",
        exchange="NSE",
        direction="BUY",
        quantity=10,
        order_type="MARKET",
        product="INTRADAY",
        validity="DAY",
        agent_trade_id="AT001",
        signal_id="SIG001",
    )
    with patch.object(adapter._kite, "place_order", return_value="ORD001") as mock_place:
        order_id = await adapter.place_order(req)

    assert order_id == "ORD001"
    call_kwargs = mock_place.call_args.kwargs
    assert call_kwargs["product"] == "MIS"
    assert call_kwargs["tradingsymbol"] == "INFY"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_order_delivery_product() -> None:
    adapter = _make_adapter()
    from src.brokers.base import OrderRequest

    req = OrderRequest(
        symbol="TCS",
        exchange="NSE",
        direction="BUY",
        quantity=1,
        order_type="LIMIT",
        product="DELIVERY",
        validity="DAY",
        agent_trade_id="AT002",
        signal_id="SIG002",
        price=3500.0,
    )
    with patch.object(adapter._kite, "place_order", return_value="ORD002") as mock_place:
        await adapter.place_order(req)

    assert mock_place.call_args.kwargs["product"] == "CNC"


# ── order variety (regular vs AMO) ───────────────────────────────────────────
# Regression: place_order/modify_order/cancel_order hardcoded VARIETY_REGULAR,
# which the exchange rejects outside 09:15-15:30 IST — e.g. the 15:35 EOD
# swing scan's CNC orders would always be rejected in live mode.


def _swing_req() -> Any:
    from src.brokers.base import OrderRequest

    return OrderRequest(
        symbol="RELIANCE",
        exchange="NSE",
        direction="BUY",
        quantity=1,
        order_type="MARKET",
        product="DELIVERY",
        validity="DAY",
        agent_trade_id="AT003",
        signal_id="SWING-RELIANCE-20260729",
    )


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_order_uses_regular_during_market_hours() -> None:
    adapter = _make_adapter()
    fixed = datetime(2026, 7, 29, 11, 0, tzinfo=_IST)  # Wed 11:00 IST
    with patch("src.brokers.adapters.kite.adapter.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        with patch.object(adapter._kite, "place_order", return_value="ORD003") as mock_place:
            await adapter.place_order(_swing_req())
    assert mock_place.call_args.kwargs["variety"] == "regular"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_order_uses_amo_after_market_close() -> None:
    adapter = _make_adapter()
    fixed = datetime(2026, 7, 29, 16, 5, tzinfo=_IST)  # Wed 16:05 IST — AMO window open
    with patch("src.brokers.adapters.kite.adapter.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        with patch.object(adapter._kite, "place_order", return_value="ORD004") as mock_place:
            await adapter.place_order(_swing_req())
    assert mock_place.call_args.kwargs["variety"] == "amo"


@pytest.mark.asyncio  # type: ignore[misc]
@pytest.mark.parametrize("hh,mm", [(15, 35), (15, 47), (15, 59), (8, 57), (9, 5)])
async def test_place_order_refused_in_closed_gap(hh: int, mm: int) -> None:
    """15:30-16:00 and 08:57-09:15: no order window is open — nothing may be sent
    (2026-09-21: a 15:47 order was submitted and stranded)."""
    from src.brokers.base import OrderWindowClosed

    adapter = _make_adapter()
    fixed = datetime(2026, 7, 29, hh, mm, tzinfo=_IST)
    with (
        patch("src.brokers.adapters.kite.adapter.datetime") as mock_dt,
        patch.object(adapter._kite, "place_order") as mock_place,
    ):
        mock_dt.now.return_value = fixed
        with pytest.raises(OrderWindowClosed):
            await adapter.place_order(_swing_req())
    mock_place.assert_not_called()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_order_uses_amo_before_market_open() -> None:
    adapter = _make_adapter()
    fixed = datetime(2026, 7, 29, 8, 0, tzinfo=_IST)  # Wed 08:00 IST — pre-open
    with patch("src.brokers.adapters.kite.adapter.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        with patch.object(adapter._kite, "place_order", return_value="ORD005") as mock_place:
            await adapter.place_order(_swing_req())
    assert mock_place.call_args.kwargs["variety"] == "amo"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_order_uses_amo_on_weekend() -> None:
    adapter = _make_adapter()
    fixed = datetime(2026, 8, 1, 11, 0, tzinfo=_IST)  # Saturday
    with patch("src.brokers.adapters.kite.adapter.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        with patch.object(adapter._kite, "place_order", return_value="ORD006") as mock_place:
            await adapter.place_order(_swing_req())
    assert mock_place.call_args.kwargs["variety"] == "amo"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cancel_order_uses_matching_variety_after_close() -> None:
    adapter = _make_adapter()
    fixed = datetime(2026, 7, 29, 20, 0, tzinfo=_IST)
    with patch("src.brokers.adapters.kite.adapter.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        with patch.object(adapter._kite, "cancel_order", return_value=None) as mock_cancel:
            await adapter.cancel_order("ORD007")
    assert mock_cancel.call_args.kwargs["variety"] == "amo"


# ── cancel_order ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cancel_order_returns_true() -> None:
    adapter = _make_adapter()
    with patch.object(adapter._kite, "cancel_order"):
        result = await adapter.cancel_order("ORD001")
    assert result is True


# ── get_order_status ───────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_order_status_found() -> None:
    adapter = _make_adapter()
    with patch.object(adapter._kite, "orders", return_value=[_order_raw()]):
        order = await adapter.get_order_status("ORD001")
    assert order.broker_order_id == "ORD001"
    assert order.status == "FILLED"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_order_status_not_found_raises() -> None:
    adapter = _make_adapter()
    with (
        patch.object(adapter._kite, "orders", return_value=[]),
        pytest.raises(KeyError, match="ORD999"),
    ):
        await adapter.get_order_status("ORD999")


# ── get_positions ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_positions() -> None:
    adapter = _make_adapter()
    raw = {
        "net": [
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "quantity": 10,
                "average_price": 2500.0,
                "last_price": 2550.0,
                "pnl": 500.0,
                "product": "MIS",
            }
        ],
        "day": [],
    }
    with patch.object(adapter._kite, "positions", return_value=raw):
        positions = await adapter.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "RELIANCE"


# ── get_margins ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_margins() -> None:
    adapter = _make_adapter()
    raw = {
        "equity": {
            "net": 200000.0,
            "available": {"cash": 150000.0, "collateral": 10000.0},
            "utilised": {"debits": 50000.0},
        }
    }
    with patch.object(adapter._kite, "margins", return_value=raw):
        margin = await adapter.get_margins()
    assert margin.equity == 200000.0
    assert margin.available_cash == 150000.0
    assert margin.used_margin == 50000.0
    assert margin.collateral == 10000.0


# ── get_holdings ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_holdings() -> None:
    adapter = _make_adapter()
    raw = [
        {
            "tradingsymbol": "TCS",
            "exchange": "NSE",
            "quantity": 5,
            "average_price": 3000.0,
            "last_price": 3200.0,
            "pnl": 1000.0,
        }
    ]
    with patch.object(adapter._kite, "holdings", return_value=raw):
        holdings = await adapter.get_holdings()
    assert len(holdings) == 1
    assert holdings[0].symbol == "TCS"
    assert holdings[0].pnl == 1000.0


# ── GTT ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cancel_gtt_returns_true() -> None:
    adapter = _make_adapter()
    with patch.object(adapter._kite, "delete_gtt"):
        result = await adapter.cancel_gtt("12345")
    assert result is True


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_all_gtts() -> None:
    adapter = _make_adapter()
    raw_gtt = {
        "id": 99,
        "status": "active",
        "type": "two-leg",
        "condition": {
            "tradingsymbol": "HDFC",
            "trigger_values": [1400.0, 1600.0],
        },
    }
    with patch.object(adapter._kite, "get_gtts", return_value=[raw_gtt]):
        gtts = await adapter.get_all_gtts()
    assert len(gtts) == 1
    assert gtts[0].gtt_id == "99"
    assert gtts[0].status == "ACTIVE"
    assert gtts[0].gtt_type == "TWO_LEG"
    assert gtts[0].target_trigger == 1600.0


# ── capability flags ───────────────────────────────────────────────────────────


def test_supports_gtt() -> None:
    assert _make_adapter().supports_gtt() is True


def test_supports_bracket_order() -> None:
    assert _make_adapter().supports_bracket_order() is True


# ── _to_instrument helper ──────────────────────────────────────────────────────


def test_to_instrument_basic() -> None:
    raw: dict[str, Any] = {
        "tradingsymbol": "NIFTY25JAN17500CE",
        "exchange": "NFO",
        "instrument_token": 12345,
        "instrument_type": "CE",
        "lot_size": 50,
        "tick_size": 0.05,
        "strike": 17500.0,
        "expiry": "2025-01-30",
    }
    inst = _to_instrument(raw)
    assert inst.symbol == "NIFTY25JAN17500CE"
    assert inst.lot_size == 50
    assert inst.expiry is not None


def test_to_instrument_no_expiry() -> None:
    raw: dict[str, Any] = {
        "tradingsymbol": "INFY",
        "exchange": "NSE",
        "instrument_token": 408065,
        "instrument_type": "EQ",
        "lot_size": 1,
        "tick_size": 0.05,
        "strike": None,
        "expiry": None,
    }
    inst = _to_instrument(raw)
    assert inst.expiry is None
    assert inst.strike is None


# ── normalize passthrough ──────────────────────────────────────────────────────


def test_normalize_order_passthrough() -> None:
    order = _make_adapter().normalize_order(_order_raw())
    assert order.status == "FILLED"


def test_normalize_error_passthrough() -> None:
    err = _make_adapter().normalize_error(
        {"exception_type": "TokenException", "message": "expired", "code": "403"}
    )
    assert err.code == "TOKEN"


# ── RateLimiter wiring ─────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_order_acquires_order_rate_limit() -> None:
    from src.brokers.base import OrderRequest
    from src.brokers.rate_limiter import RateLimiter

    mock_limiter = MagicMock(spec=RateLimiter)
    mock_limiter.acquire = AsyncMock(return_value=0.0)
    adapter = KiteAdapter(
        KiteAuth(api_key="k", api_secret="s", user_id="U1"),
        rate_limiter=mock_limiter,
    )
    req = OrderRequest(
        symbol="INFY",
        exchange="NSE",
        direction="BUY",
        quantity=10,
        order_type="MARKET",
        product="INTRADAY",
        validity="DAY",
        agent_trade_id="AT001",
        signal_id="SIG001",
    )
    with patch.object(adapter._kite, "place_order", return_value="ORD001"):
        await adapter.place_order(req)

    mock_limiter.acquire.assert_awaited_once_with("order")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ltp_acquires_quote_rate_limit() -> None:
    from src.brokers.rate_limiter import RateLimiter

    mock_limiter = MagicMock(spec=RateLimiter)
    mock_limiter.acquire = AsyncMock(return_value=0.0)
    adapter = KiteAdapter(
        KiteAuth(api_key="k", api_secret="s", user_id="U1"),
        rate_limiter=mock_limiter,
    )
    with patch.object(adapter._kite, "ltp", return_value={"INFY": {"last_price": 1500.0}}):
        await adapter.get_ltp(["INFY"])

    mock_limiter.acquire.assert_awaited_once_with("quote")


# ── Cash mapping (2026-08-27) ──────────────────────────────────────────────────


def test_margins_uses_live_balance_when_cash_field_is_zero() -> None:
    """Zerodha reports available.cash = 0 while the real free cash sits in
    live_balance (observed live: cash 0, live_balance 29785.30, collateral 557857.99,
    net 587643.29). Mapping available_cash to `cash` made it read 0 everywhere, and
    `net` is wrong for CNC because it is mostly pledged collateral, which cannot buy
    delivery stock."""
    from src.brokers.adapters.kite.adapter import _margin_from_kite

    m = _margin_from_kite(
        {
            "equity": {
                "net": 587643.29,
                "available": {"cash": 0, "live_balance": 29785.30, "collateral": 557857.99},
                "utilised": {"debits": 0},
            }
        }
    )

    assert m.available_cash == pytest.approx(29785.30)
    assert m.collateral == pytest.approx(557857.99)
    assert m.equity == pytest.approx(587643.29)


def test_margins_prefers_explicit_cash_when_present() -> None:
    from src.brokers.adapters.kite.adapter import _margin_from_kite

    m = _margin_from_kite(
        {
            "equity": {
                "net": 1000.0,
                "available": {"cash": 750.0, "live_balance": 0, "collateral": 0},
                "utilised": {"debits": 0},
            }
        }
    )

    assert m.available_cash == pytest.approx(750.0)


def test_fno_margin_counts_collateral_but_cash_stays_cash_only() -> None:
    """Found live 2026-10-29: the SENSEX straddle needed 258513 and was refused against
    'have 31024' -- the cash-only figure -- with lakhs of pledged collateral the broker
    accepts for F&O. Kite's `net` (cash + collateral - utilised) is the F&O figure;
    available_cash stays cash-only for CNC."""
    from src.brokers.adapters.kite.adapter import _margin_from_kite

    m = _margin_from_kite(
        {
            "equity": {
                "net": 531024.0,
                "available": {"cash": 31024.0, "live_balance": 31024.0, "collateral": 500000.0},
                "utilised": {"debits": 0},
            }
        }
    )

    assert m.available_cash == pytest.approx(31024.0)
    assert m.fno_usable == pytest.approx(531024.0)
    assert m.fno_usable > 258513  # today's SENSEX straddle fits


def test_fno_usable_falls_back_to_cash_when_broker_reports_no_fno_figure() -> None:
    from src.brokers.base import Margin

    assert Margin(equity=100.0, available_cash=40.0, used_margin=0.0).fno_usable == 40.0


# ── GTT modify: full-spec rebuild (2026-08-28) ────────────────────────────────


def _gtt_raw() -> dict[str, Any]:
    """A live two-leg (OCO) GTT as Kite's get_gtts actually returns it."""
    return {
        "id": 771122,
        "type": "two-leg",
        "status": "active",
        "condition": {
            "exchange": "NSE",
            "tradingsymbol": "IFCI",
            "trigger_values": [80.0, 95.0],
            "last_price": 89.9,
        },
        "orders": [
            {
                "transaction_type": "SELL",
                "quantity": 145,
                "order_type": "MARKET",
                "product": "CNC",
                "price": 80.0,
            },
            {
                "transaction_type": "SELL",
                "quantity": 145,
                "order_type": "LIMIT",
                "product": "CNC",
                "price": 95.0,
            },
        ],
    }


@pytest.mark.asyncio  # type: ignore[misc]
async def test_modify_gtt_sends_the_complete_spec_kite_requires() -> None:
    """kite.modify_gtt takes trigger_type/tradingsymbol/exchange/trigger_values/
    last_price/orders as REQUIRED arguments. Forwarding only the changed field (what
    this adapter did until 2026-08-28) raised TypeError before reaching the API, so
    every swing SL trail failed silently into update_sl's except branch."""
    adapter = _make_adapter()
    with (
        patch.object(adapter._kite, "get_gtts", return_value=[_gtt_raw()]),
        patch.object(adapter._kite, "modify_gtt", return_value={"trigger_id": 771122}) as mod,
        patch.object(adapter, "get_ltp", AsyncMock(return_value={"IFCI": 90.5})),
    ):
        out = await adapter.modify_gtt("771122", {"sl_trigger": 86.0})

    assert out == "771122"
    kw = mod.call_args.kwargs
    assert kw["trigger_id"] == 771122
    assert kw["trigger_type"] == "two-leg"
    assert kw["tradingsymbol"] == "IFCI"
    assert kw["exchange"] == "NSE"
    assert kw["last_price"] == 90.5
    # Only the stop leg moved; the target leg is preserved, and OCO still has 2 values.
    assert kw["trigger_values"] == [86.0, 95.0]
    assert kw["orders"][0]["price"] == 86.0  # limit price tracks its own trigger
    assert kw["orders"][1]["price"] == 95.0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_modify_gtt_can_move_the_target_leg() -> None:
    adapter = _make_adapter()
    with (
        patch.object(adapter._kite, "get_gtts", return_value=[_gtt_raw()]),
        patch.object(adapter._kite, "modify_gtt", return_value={"trigger_id": 771122}) as mod,
        patch.object(adapter, "get_ltp", AsyncMock(return_value={"IFCI": 90.5})),
    ):
        await adapter.modify_gtt("771122", {"target_trigger": 101.0})

    assert mod.call_args.kwargs["trigger_values"] == [80.0, 101.0]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_modify_gtt_raises_when_the_gtt_is_gone() -> None:
    """A vanished GTT must surface, not be silently rebuilt from stale local state."""
    adapter = _make_adapter()
    with (
        patch.object(adapter._kite, "get_gtts", return_value=[]),
        patch.object(adapter, "get_ltp", AsyncMock(return_value={})),
        pytest.raises(KeyError),
    ):
        await adapter.modify_gtt("771122", {"sl_trigger": 86.0})


@pytest.mark.asyncio  # type: ignore[misc]
async def test_modify_gtt_resizes_every_leg_after_a_partial_book() -> None:
    """A GTT left at the original quantity tries to sell stock no longer held when it
    triggers — the exchange rejects the oversell and the whole stop goes with it."""
    adapter = _make_adapter()
    with (
        patch.object(adapter._kite, "get_gtts", return_value=[_gtt_raw()]),
        patch.object(adapter._kite, "modify_gtt", return_value={"trigger_id": 771122}) as mod,
        patch.object(adapter, "get_ltp", AsyncMock(return_value={"IFCI": 90.5})),
    ):
        await adapter.modify_gtt("771122", {"quantity": 72})

    kw = mod.call_args.kwargs
    assert [o["quantity"] for o in kw["orders"]] == [72, 72]
    assert kw["trigger_values"] == [80.0, 95.0]  # levels untouched by a resize


def test_modify_gtt_payload_is_accepted_by_kiteconnect() -> None:
    """Guard against the original bug class: build the real payload with the real
    library so a signature/shape mismatch fails here, not silently in production."""
    from kiteconnect import KiteConnect

    raw = _gtt_raw()
    condition, orders = KiteConnect(api_key="k")._get_gtt_payload(
        trigger_type=raw["type"],
        tradingsymbol=raw["condition"]["tradingsymbol"],
        exchange=raw["condition"]["exchange"],
        trigger_values=[86.0, 95.0],
        last_price=90.5,
        orders=raw["orders"],
    )
    assert condition["trigger_values"] == [86.0, 95.0]
    assert len(orders) == 2


# ── GTT tick-size rounding (2026-09-07 — CGCL sat unprotected 12h+ because every
# arm/re-arm attempt sent an un-rounded sl/target and Kite rejected it identically
# every time: "Stoploss trigger price should be a multiple of tick size 0.05.") ──


def test_round_to_tick_snaps_to_nearest_multiple() -> None:
    from src.brokers.adapters.kite.adapter import _round_to_tick

    assert _round_to_tick(279.9166666) == 279.9
    assert _round_to_tick(286.033) == 286.05
    assert _round_to_tick(85.0) == 85.0  # already aligned — unchanged


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_gtt_rounds_sl_and_target_to_tick_size() -> None:
    """CGCL's real failure: sl/target computed off raw ATR math (e.g. 279.9166666)
    never lands on a clean 0.05 multiple. place_gtt must snap both trigger and
    price before the payload reaches Kite, or the broker rejects it outright."""
    from src.brokers.base import GTTRequest

    adapter = _make_adapter()
    with (
        patch.object(adapter._kite, "place_gtt", return_value={"trigger_id": 990011}) as pg,
        patch.object(adapter, "get_ltp", AsyncMock(return_value={"CGCL": 286.4})),
    ):
        out = await adapter.place_gtt(
            GTTRequest(
                symbol="CGCL",
                product="DELIVERY",
                gtt_type="TWO_LEG",
                quantity=45,
                agent_trade_id="TRD-DF487287C262",
                sl_trigger=279.9166666,
                sl_order_type="LIMIT",
                sl_price=279.9166666,
                target_trigger=306.0333333,
                target_price=306.0333333,
            )
        )

    assert out == "990011"
    kw = pg.call_args.kwargs
    assert kw["trigger_values"] == [279.9, 306.05]
    assert kw["orders"][0]["price"] == 279.9
    assert kw["orders"][1]["price"] == 306.05


@pytest.mark.asyncio  # type: ignore[misc]
async def test_modify_gtt_rounds_new_trigger_to_tick_size() -> None:
    adapter = _make_adapter()
    with (
        patch.object(adapter._kite, "get_gtts", return_value=[_gtt_raw()]),
        patch.object(adapter._kite, "modify_gtt", return_value={"trigger_id": 771122}) as mod,
        patch.object(adapter, "get_ltp", AsyncMock(return_value={"IFCI": 90.5})),
    ):
        await adapter.modify_gtt("771122", {"sl_trigger": 86.033})

    kw = mod.call_args.kwargs
    assert kw["trigger_values"] == [86.05, 95.0]
    assert kw["orders"][0]["price"] == 86.05
