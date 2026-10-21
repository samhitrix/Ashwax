"""Unit tests for src/brokers/adapters/paper/adapter.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from src.brokers.adapters.paper.adapter import PaperAdapter
from src.brokers.base import BrokerInterface, GTTRequest, OrderRequest


def _make_market(ltp: float = 1500.0) -> BrokerInterface:
    market = MagicMock(spec=BrokerInterface)
    market.get_ltp = AsyncMock(return_value={"INFY": ltp})
    market.subscribe_ticks = AsyncMock()
    market.unsubscribe_ticks = AsyncMock()
    return market


def _make_adapter(ltp: float = 1500.0, capital: float = 200_000.0) -> PaperAdapter:
    return PaperAdapter(
        market_data_broker=_make_market(ltp), initial_capital=capital, slippage_bps=0.0
    )


def _order_req(**kw: object) -> OrderRequest:
    base = dict(
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
    base.update(kw)
    return OrderRequest(**base)  # type: ignore[arg-type]


# ── Auth (no-op) ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_login_returns_paper_session() -> None:
    info = await _make_adapter().login()
    assert info.user_id == "PAPER"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_session_status_always_valid() -> None:
    status = await _make_adapter().get_session_status()
    assert status.is_valid is True


# ── place_order — MARKET fills immediately ─────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_market_order_fills_immediately() -> None:
    adapter = _make_adapter(ltp=1500.0)
    order_id = await adapter.place_order(_order_req())

    order = await adapter.get_order_status(order_id)
    assert order.status == "FILLED"
    assert order.filled_quantity == 10
    assert order.average_price == 1500.0  # slippage_bps=0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_market_order_creates_position() -> None:
    adapter = _make_adapter(ltp=1500.0)
    await adapter.place_order(_order_req())

    positions = await adapter.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "INFY"
    assert positions[0].quantity == 10


# ── place_order — LIMIT does NOT fill when ltp > limit ────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_limit_order_stays_open_when_ltp_above() -> None:
    adapter = _make_adapter(ltp=1600.0)
    order_id = await adapter.place_order(_order_req(order_type="LIMIT", price=1500.0))

    order = await adapter.get_order_status(order_id)
    assert order.status == "OPEN"
    assert order.filled_quantity == 0


# ── cancel_order ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cancel_open_order() -> None:
    adapter = _make_adapter(ltp=1600.0)
    order_id = await adapter.place_order(_order_req(order_type="LIMIT", price=1500.0))

    result = await adapter.cancel_order(order_id)
    assert result is True

    order = await adapter.get_order_status(order_id)
    assert order.status == "CANCELLED"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cancel_filled_order_returns_false() -> None:
    adapter = _make_adapter(ltp=1500.0)
    order_id = await adapter.place_order(_order_req())
    result = await adapter.cancel_order(order_id)
    assert result is False


# ── get_all_orders ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_all_orders_returns_all() -> None:
    adapter = _make_adapter()
    await adapter.place_order(_order_req())
    adapter._market.get_ltp = AsyncMock(return_value={"RELIANCE": 2500.0})  # type: ignore[method-assign]
    await adapter.place_order(_order_req(symbol="RELIANCE"))
    orders = await adapter.get_all_orders()
    assert len(orders) == 2


# ── get_trades ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_trades_returns_fills() -> None:
    adapter = _make_adapter(ltp=1500.0)
    order_id = await adapter.place_order(_order_req())
    fills = await adapter.get_trades(order_id)
    assert len(fills) == 1
    assert fills[0].quantity == 10


# ── position tracking ──────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_buy_then_sell_removes_position() -> None:
    adapter = _make_adapter(ltp=1500.0)
    await adapter.place_order(_order_req(direction="BUY", quantity=10))
    await adapter.place_order(_order_req(direction="SELL", quantity=10))

    positions = await adapter.get_positions()
    assert len(positions) == 0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_multiple_buys_average_price() -> None:
    adapter = _make_adapter(ltp=1000.0)
    await adapter.place_order(_order_req(quantity=10))

    adapter._market.get_ltp = AsyncMock(return_value={"INFY": 1100.0})  # type: ignore[method-assign]
    await adapter.place_order(_order_req(quantity=10))

    positions = await adapter.get_positions()
    assert len(positions) == 1
    assert positions[0].quantity == 20
    assert positions[0].average_price == 1050.0


# ── GTT orders ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_and_get_gtt() -> None:
    adapter = _make_adapter(ltp=1500.0)
    gtt = GTTRequest(
        symbol="INFY",
        product="DELIVERY",
        gtt_type="TWO_LEG",
        quantity=5,
        agent_trade_id="AT002",
        sl_trigger=1400.0,
        sl_order_type="MARKET",
        target_trigger=1600.0,
    )
    gtt_id = await adapter.place_gtt(gtt)
    assert gtt_id.startswith("PAPER-GTT")

    gtts = await adapter.get_all_gtts()
    assert len(gtts) == 1
    assert gtts[0].status == "ACTIVE"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_gtt_auto_triggers_on_sl() -> None:
    adapter = _make_adapter(ltp=1399.0)
    gtt = GTTRequest(
        symbol="INFY",
        product="DELIVERY",
        gtt_type="SINGLE_LEG",
        quantity=5,
        agent_trade_id="AT003",
        sl_trigger=1400.0,
        sl_order_type="MARKET",
    )
    await adapter.place_gtt(gtt)
    gtts = await adapter.get_all_gtts()
    assert gtts[0].status == "TRIGGERED"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_modify_gtt_actually_moves_the_trigger() -> None:
    """The simulated trail must move the simulated stop.

    Until 2026-08-28 modify_gtt returned the id WITHOUT touching sl_trigger, so a
    trailed stop kept firing at its original level while the caller logged a
    successful trail — paper reported work it had not done, which is exactly the
    kind of false confirmation that hides a live bug.
    """
    adapter = _make_adapter(ltp=1500.0)
    gtt = GTTRequest(
        symbol="INFY",
        product="DELIVERY",
        gtt_type="TWO_LEG",
        quantity=5,
        agent_trade_id="AT005",
        sl_trigger=1400.0,
        sl_order_type="MARKET",
        target_trigger=1600.0,
    )
    gtt_id = await adapter.place_gtt(gtt)

    await adapter.modify_gtt(gtt_id, {"sl_trigger": 1480.0})

    stored = next(g for g in await adapter.get_all_gtts() if g.gtt_id == gtt_id)
    assert stored.sl_trigger == 1480.0
    assert stored.target_trigger == 1600.0  # untouched leg preserved


@pytest.mark.asyncio  # type: ignore[misc]
async def test_modified_gtt_triggers_at_the_new_level() -> None:
    """A stop trailed to 1480 must fire at 1490, which the original 1400 would not."""
    adapter = _make_adapter(ltp=1490.0)
    gtt = GTTRequest(
        symbol="INFY",
        product="DELIVERY",
        gtt_type="SINGLE_LEG",
        quantity=5,
        agent_trade_id="AT006",
        sl_trigger=1400.0,
        sl_order_type="MARKET",
    )
    gtt_id = await adapter.place_gtt(gtt)
    assert (await adapter.get_all_gtts())[0].status == "ACTIVE"

    await adapter.modify_gtt(gtt_id, {"sl_trigger": 1495.0})

    assert (await adapter.get_all_gtts())[0].status == "TRIGGERED"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cancel_gtt() -> None:
    adapter = _make_adapter(ltp=1500.0)
    gtt = GTTRequest(
        symbol="INFY",
        product="DELIVERY",
        gtt_type="SINGLE_LEG",
        quantity=5,
        agent_trade_id="AT004",
        sl_trigger=1400.0,
        sl_order_type="MARKET",
    )
    gtt_id = await adapter.place_gtt(gtt)
    result = await adapter.cancel_gtt(gtt_id)
    assert result is True

    gtts = await adapter.get_all_gtts()
    assert gtts[0].status == "CANCELLED"


# ── get_margins ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_margins_returns_initial_capital() -> None:
    margin = await _make_adapter(capital=150_000.0).get_margins()
    assert margin.equity == 150_000.0
    assert margin.available_cash == 150_000.0


# ── Resting-order re-evaluation (2026-08-25) ───────────────────────────────────


def _limit_req(tid: str, price: float, direction: str = "BUY") -> OrderRequest:
    return OrderRequest(
        symbol="INFY",
        exchange="NSE",
        direction=direction,
        quantity=10,
        order_type="LIMIT",
        product="MIS",
        validity="DAY",
        agent_trade_id=tid,
        signal_id=f"SIG-{tid}",
        price=price,
    )


@pytest.mark.asyncio
async def test_get_order_status_refills_resting_limit_when_price_returns() -> None:
    """place_order() decided the fill exactly once and get_order_status() replayed that
    frozen record forever, so a LIMIT that missed by a tick stayed OPEN for the rest of
    the session. A real broker fills it when price comes back; the simulator must too."""
    adapter = _make_adapter(ltp=1500.0)

    order_id = await adapter.place_order(_limit_req("AT-REST", 1490.0))
    assert (await adapter.get_order_status(order_id)).status == "OPEN"

    adapter._market.get_ltp = AsyncMock(return_value={"INFY": 1489.0})  # now marketable
    refreshed = await adapter.get_order_status(order_id)

    assert refreshed.status == "FILLED"
    assert refreshed.filled_quantity == 10
    assert len(await adapter.get_trades(order_id)) == 1


@pytest.mark.asyncio
async def test_get_order_status_leaves_terminal_order_alone() -> None:
    """Re-evaluation must never resurrect a terminal order."""
    adapter = _make_adapter(ltp=1500.0)

    order_id = await adapter.place_order(_limit_req("AT-CXL", 1490.0))
    await adapter.cancel_order(order_id)

    adapter._market.get_ltp = AsyncMock(return_value={"INFY": 1489.0})

    assert (await adapter.get_order_status(order_id)).status == "CANCELLED"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_load_instrument_tokens_is_delegated_to_market_broker() -> None:
    # _refresh_intraday_ohlcv pre-warms the token cache with
    # getattr(broker, "load_instrument_tokens"). In paper mode `broker` is this
    # wrapper, so without the delegate the pre-warm silently never ran and the first
    # post-restart cycle resolved ~500 tokens via Kite's 1 req/s quote() (~640s).
    market = _make_market()
    market.load_instrument_tokens = AsyncMock(return_value=2500)  # type: ignore[attr-defined]
    adapter = PaperAdapter(market_data_broker=market, initial_capital=200_000.0)

    assert await adapter.load_instrument_tokens("NSE") == 2500
    market.load_instrument_tokens.assert_awaited_once_with("NSE")  # type: ignore[attr-defined]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_load_instrument_tokens_is_zero_when_market_broker_has_none() -> None:
    market = MagicMock(spec=BrokerInterface)  # no load_instrument_tokens attribute
    adapter = PaperAdapter(market_data_broker=market, initial_capital=200_000.0)
    assert await adapter.load_instrument_tokens() == 0


@pytest.mark.asyncio  # type: ignore[misc]
@pytest.mark.parametrize("hh,mm,ok", [(15, 47, False), (10, 0, True), (16, 5, True)])
async def test_enforce_order_windows_mirrors_broker(hh: int, mm: int, ok: bool) -> None:
    """With enforce_order_windows the paper broker refuses orders in the no-order gap
    (2026-09-21: a 15:47 paper order sat SUBMITTED for 37 minutes)."""
    from datetime import datetime
    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    from src.brokers.base import OrderWindowClosed

    adapter = PaperAdapter(
        market_data_broker=_make_market(), slippage_bps=0.0, enforce_order_windows=True
    )
    fixed = datetime(2026, 9, 21, hh, mm, tzinfo=ZoneInfo("Asia/Kolkata"))
    with patch("src.brokers.adapters.paper.adapter.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        if ok:
            assert await adapter.place_order(_order_req())
        else:
            with pytest.raises(OrderWindowClosed):
                await adapter.place_order(_order_req())


# -- get_ohlcv_with_source: provider tag survives the paper wrapper ---------------------------


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_with_source_forwards_router_source() -> None:
    import pandas as pd

    df = pd.DataFrame({"close": [1.0]})
    market = _make_market()
    market.get_ohlcv_with_source = AsyncMock(return_value=(df, "angel_one"))
    adapter = PaperAdapter(market_data_broker=market, initial_capital=1000.0)
    out, source = await adapter.get_ohlcv_with_source("NSE:INFY", "day", 5)
    assert out is df and source == "angel_one"
    market.get_ohlcv_with_source.assert_awaited_once_with("NSE:INFY", "day", 5)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_ohlcv_with_source_plain_market_broker_has_no_source() -> None:
    import pandas as pd

    df = pd.DataFrame({"close": [1.0]})
    market = _make_market()
    market.get_ohlcv = AsyncMock(return_value=df)
    adapter = PaperAdapter(market_data_broker=market, initial_capital=1000.0)
    out, source = await adapter.get_ohlcv_with_source("NSE:INFY", "day", 5)
    assert out is df and source is None
