"""Tests for the seller-desk strike selection engine (options_spec.md §5)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.brokers.base import BidAsk, MarketDepth, OptionQuote
from src.options.nfo_chain import ChainStrike, OptionChain
from src.options.strike_selector import (
    book_walk_fill,
    depth_liquidity_ok,
    find_strike_by_delta,
    max_pain,
    spread_ok,
)

_IST = ZoneInfo("Asia/Kolkata")


def _quote(mid: float, oi: int = 1000, volume: int = 500, spread: float = 1.0) -> OptionQuote:
    return OptionQuote(
        "SYM", ltp=mid, bid=mid - spread / 2, ask=mid + spread / 2, oi=oi, volume=volume
    )


def test_spread_ok_within_threshold() -> None:
    assert spread_ok(bid=99.0, ask=101.0) is True  # 2/100 = 2%


def test_spread_ok_exceeds_threshold() -> None:
    assert spread_ok(bid=90.0, ask=110.0) is False  # 20/100 = 20%


def test_spread_ok_zero_bid_fails() -> None:
    assert spread_ok(bid=0.0, ask=10.0) is False


def _chain_for_delta_test() -> OptionChain:
    expiry = datetime.now(_IST) + timedelta(days=30)
    rows = [
        ChainStrike(
            strike=24400.0,
            call=_quote(50.0),  # liquid, close to target 0.25
            put=None,
            call_delta=0.24,
        ),
        ChainStrike(
            strike=24350.0,
            call=_quote(60.0, oi=10, volume=5),  # closer to 0.25 but illiquid
            put=None,
            call_delta=0.26,
        ),
    ]
    return OptionChain(
        underlying="NIFTY",
        spot=24000.0,
        expiry=expiry,
        days_to_expiry=30,
        lot_size=25,
        strikes=rows,
    )


def test_find_strike_by_delta_skips_illiquid_nearest() -> None:
    chain = _chain_for_delta_test()
    found = find_strike_by_delta(chain, target_delta=0.25, kind="CE")
    assert found is not None
    assert found.strike == 24400.0  # the illiquid 24350 (closer delta) is skipped


def test_find_strike_by_delta_none_when_all_illiquid() -> None:
    expiry = datetime.now(_IST) + timedelta(days=30)
    rows = [ChainStrike(strike=24400.0, call=_quote(50.0, oi=1, volume=1), call_delta=0.25)]
    chain = OptionChain(
        underlying="NIFTY",
        spot=24000.0,
        expiry=expiry,
        days_to_expiry=30,
        lot_size=25,
        strikes=rows,
    )
    assert find_strike_by_delta(chain, target_delta=0.25, kind="CE") is None


def test_find_strike_by_delta_put_uses_negative_convention() -> None:
    expiry = datetime.now(_IST) + timedelta(days=30)
    rows = [ChainStrike(strike=23600.0, put=_quote(50.0), put_delta=-0.24)]
    chain = OptionChain(
        underlying="NIFTY",
        spot=24000.0,
        expiry=expiry,
        days_to_expiry=30,
        lot_size=25,
        strikes=rows,
    )
    found = find_strike_by_delta(chain, target_delta=0.25, kind="PE")
    assert found is not None
    assert found.strike == 23600.0


def test_book_walk_fill_sufficient_depth() -> None:
    depth = MarketDepth(
        symbol="X",
        bids=[BidAsk(99.0, 50, 3), BidAsk(98.5, 100, 5)],
        asks=[BidAsk(100.0, 20, 2), BidAsk(100.5, 100, 4)],
        ltp=99.5,
        timestamp=datetime.now(_IST),
    )
    # buy 1 lot of 25 -> 20 @ 100.0 + 5 @ 100.5
    fill = book_walk_fill(depth, "BUY", lots=1, lot_size=25)
    assert fill is not None
    expected = (20 * 100.0 + 5 * 100.5) / 25
    assert abs(fill - expected) < 1e-9


def test_book_walk_fill_insufficient_depth_returns_none() -> None:
    depth = MarketDepth(
        symbol="X", bids=[], asks=[BidAsk(100.0, 5, 1)], ltp=99.5, timestamp=datetime.now(_IST)
    )
    assert book_walk_fill(depth, "BUY", lots=1, lot_size=25) is None


def test_depth_liquidity_ok_requires_5_lots_both_sides() -> None:
    depth = MarketDepth(
        symbol="X",
        bids=[BidAsk(99.0, 125, 3)],
        asks=[BidAsk(100.0, 125, 3)],
        ltp=99.5,
        timestamp=datetime.now(_IST),
    )
    assert depth_liquidity_ok(depth, min_lots=5, lot_size=25) is True  # exactly 125 = 5*25

    thin = MarketDepth(
        symbol="X",
        bids=[BidAsk(99.0, 10, 1)],
        asks=[BidAsk(100.0, 125, 3)],
        ltp=99.5,
        timestamp=datetime.now(_IST),
    )
    assert depth_liquidity_ok(thin, min_lots=5, lot_size=25) is False


def test_max_pain_symmetric_book_centers_at_spot() -> None:
    expiry = datetime.now(_IST) + timedelta(days=7)
    rows = [
        ChainStrike(strike=23800.0, call=_quote(1, oi=100), put=_quote(1, oi=100)),
        ChainStrike(strike=24000.0, call=_quote(1, oi=500), put=_quote(1, oi=500)),
        ChainStrike(strike=24200.0, call=_quote(1, oi=100), put=_quote(1, oi=100)),
    ]
    chain = OptionChain(
        underlying="NIFTY", spot=24000.0, expiry=expiry, days_to_expiry=7, lot_size=25, strikes=rows
    )
    assert max_pain(chain) == 24000.0
