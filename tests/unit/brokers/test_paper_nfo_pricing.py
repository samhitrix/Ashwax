"""Paper broker pricing of NFO option orders. Live 2026-10-12: the order path asked Kite for
"NSE:<option symbol>" (does not exist) so every paper option order stayed OPEN/unfilled, and each price
lookup also queued ~12 s behind other loops' quote calls."""

from __future__ import annotations

from src.brokers.adapters.paper.adapter import PaperAdapter
from src.brokers.base import OrderRequest


class _Market:
    """Records what the paper broker asks the market for; only exchange-qualified NFO keys exist."""

    def __init__(self) -> None:
        self.ltp_calls: list[list[str]] = []
        self.prices = {"NFO:NIFTY26O1322350PE": 42.0, "INFY": 1500.0}

    async def get_ltp(self, symbols):  # type: ignore[no-untyped-def]
        self.ltp_calls.append(list(symbols))
        return {s: self.prices[s] for s in symbols if s in self.prices}  # bare keys are NOT found


def _order(symbol: str, exchange: str, direction: str = "SELL") -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        exchange=exchange,
        direction=direction,
        quantity=65,
        order_type="MARKET",
        product="MARGIN",
        validity="DAY",
        agent_trade_id="T1",
        signal_id="S1",
        tag="t",
    )


async def test_option_order_is_priced_on_its_own_exchange_and_fills() -> None:
    market = _Market()
    paper = PaperAdapter(market, initial_capital=1_000_000.0)
    oid = await paper.place_order(_order("NIFTY26O1322350PE", "NFO"))
    assert market.ltp_calls == [["NFO:NIFTY26O1322350PE"]]  # not "NSE:NIFTY26O..."
    assert (await paper.get_order_status(oid)).status == "FILLED"


async def test_recent_prices_are_reused_so_orders_and_stops_do_not_queue_behind_other_loops() -> (
    None
):
    market = _Market()
    paper = PaperAdapter(market, initial_capital=1_000_000.0)
    await paper.get_ltp(["NFO:NIFTY26O1322350PE"])  # e.g. the stop loop's price fetch
    market.ltp_calls.clear()
    await paper.place_order(
        _order("NIFTY26O1322350PE", "NFO", "BUY")
    )  # the close order moments later
    assert await paper.get_ltp(["NFO:NIFTY26O1322350PE"]) == {"NFO:NIFTY26O1322350PE": 42.0}
    assert market.ltp_calls == []  # served from the few-seconds-old price, no Kite round trip


async def test_stale_prices_are_not_reused(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    market = _Market()
    paper = PaperAdapter(market, initial_capital=1_000_000.0)
    await paper.get_ltp(["NFO:NIFTY26O1322350PE"])
    market.ltp_calls.clear()
    monkeypatch.setattr(PaperAdapter, "_NFO_PX_TTL_SECONDS", -1.0)
    await paper.get_ltp(["NFO:NIFTY26O1322350PE"])
    assert market.ltp_calls == [["NFO:NIFTY26O1322350PE"]]


async def test_equity_orders_are_untouched_by_the_cache() -> None:
    market = _Market()
    paper = PaperAdapter(market, initial_capital=1_000_000.0)
    await paper.place_order(_order("INFY", "NSE", "BUY"))
    await paper.place_order(_order("INFY", "NSE", "BUY"))
    assert market.ltp_calls == [["INFY"], ["INFY"]]  # bare symbol, every order hits the market


async def test_hinted_prices_fill_an_order_with_no_market_round_trip() -> None:
    """The strategy hands over the quotes it decided on; the paper fill must not wait on Kite."""
    market = _Market()
    paper = PaperAdapter(market, initial_capital=1_000_000.0)
    paper.hint_prices({"NFO:NIFTY26O1322350PE": 41.0})
    oid = await paper.place_order(_order("NIFTY26O1322350PE", "NFO"))
    assert market.ltp_calls == []
    order = await paper.get_order_status(oid)
    assert order.status == "FILLED" and abs(order.average_price - 41.0) < 0.5


async def test_priority_ltp_uses_the_market_priority_lane_when_it_has_one() -> None:
    class _M(_Market):
        def __init__(self) -> None:
            super().__init__()
            self.priority_calls: list[list[str]] = []

        async def priority_ltp(self, symbols):  # type: ignore[no-untyped-def]
            self.priority_calls.append(list(symbols))
            return {s: self.prices[s] for s in symbols if s in self.prices}

    market = _M()
    paper = PaperAdapter(market, initial_capital=1_000_000.0)
    assert await paper.priority_ltp(["NFO:NIFTY26O1322350PE"]) == {"NFO:NIFTY26O1322350PE": 42.0}
    assert market.priority_calls == [["NFO:NIFTY26O1322350PE"]] and market.ltp_calls == []
    plain = PaperAdapter(
        _Market(), initial_capital=1_000_000.0
    )  # no priority lane -> plain get_ltp
    assert await plain.priority_ltp(["NFO:NIFTY26O1322350PE"]) == {"NFO:NIFTY26O1322350PE": 42.0}


def test_kite_limiter_has_a_separate_priority_quote_lane() -> None:
    from src.brokers.rate_limiter import KITE_RATE_LIMITS

    assert "quote_priority" in KITE_RATE_LIMITS and KITE_RATE_LIMITS["quote_priority"] > 0
