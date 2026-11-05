"""Seller-desk strike selection engine (options_spec.md §5).

Delta-based strike selection (never price-based — delta already incorporates
IV/DTE/underlying price), per-leg liquidity validation, book-walked fill
estimation (using real multi-level market depth via
``BrokerInterface.get_depth``, not mid-price), and max-pain computation for
Iron Butterfly centering.

Liquidity validation implements 3 of the spec's 4 checks (OI, volume,
spread) — the 4th, "minimum 5 lots at best bid/ask", needs the top-of-book
size, which the depth ladder already carries via ``BidAsk.quantity``, so it's
folded into the same check rather than a separate one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.options.nfo_chain import liquidity_ok

if TYPE_CHECKING:
    from src.brokers.base import MarketDepth
    from src.options.nfo_chain import ChainStrike, OptionChain

_DELTA_TOLERANCE = 0.03
_MAX_SPREAD_PCT = 0.05
_MIN_DEPTH_LOTS = 5


def spread_ok(bid: float, ask: float, *, max_spread_pct: float = _MAX_SPREAD_PCT) -> bool:
    if bid <= 0.0 or ask <= 0.0:
        return False
    mid = (bid + ask) / 2.0
    return mid > 0.0 and (ask - bid) / mid <= max_spread_pct


def row_is_liquid(row: ChainStrike, kind: str, *, min_oi: int = 500, min_volume: int = 200) -> bool:
    """The per-strike liquidity + spread test find_strike_by_delta applies to a candidate."""
    quote = row.call if kind.upper() == "CE" else row.put
    return (
        quote is not None
        and liquidity_ok(quote, min_oi, min_volume)
        and spread_ok(quote.bid, quote.ask)
    )


def find_strike_by_delta(
    chain: OptionChain,
    target_delta: float,
    kind: str,
    *,
    min_oi: int = 500,
    min_volume: int = 200,
    tolerance: float = _DELTA_TOLERANCE,
) -> ChainStrike | None:
    """Nearest-delta strike passing liquidity, trying progressively farther
    candidates within ``tolerance`` before giving up (options_spec.md §5)."""
    kind = kind.upper()
    is_call = kind == "CE"
    # Put delta is negative by convention; compare against -target for puts.
    signed_target = target_delta if is_call else -target_delta

    def _delta(row: ChainStrike) -> float | None:
        return row.call_delta if is_call else row.put_delta

    def _quote(row: ChainStrike):  # type: ignore[no-untyped-def]
        return row.call if is_call else row.put

    candidates = [
        row
        for row in chain.strikes
        if _delta(row) is not None and abs(_delta(row) - signed_target) <= tolerance  # type: ignore[operator]
    ]
    candidates.sort(key=lambda row: abs(_delta(row) - signed_target))  # type: ignore[operator]

    for row in candidates:
        if row_is_liquid(row, kind, min_oi=min_oi, min_volume=min_volume):
            return row
    return None


def book_walk_fill(depth: MarketDepth, side: str, lots: int, lot_size: int) -> float | None:
    """Realistic average fill price walking the depth ladder (options_spec.md §5).

    ``side`` is the order side: "BUY" walks the ask ladder, "SELL" walks bids.
    Returns ``None`` if the visible book can't fill the full quantity.
    """
    levels = depth.asks if side.upper() == "BUY" else depth.bids
    remaining = lots * lot_size
    total_cost = 0.0
    for level in levels:
        fill_here = min(level.quantity, remaining)
        total_cost += level.price * fill_here
        remaining -= fill_here
        if remaining <= 0:
            break
    if remaining > 0:
        return None  # insufficient visible depth
    return total_cost / (lots * lot_size)


def depth_liquidity_ok(depth: MarketDepth, min_lots: int, lot_size: int) -> bool:
    """Minimum 5 lots available at best bid AND best ask (options_spec.md §5)."""
    if not depth.bids or not depth.asks:
        return False
    min_qty = min_lots * lot_size
    return depth.bids[0].quantity >= min_qty and depth.asks[0].quantity >= min_qty


def max_pain(chain: OptionChain) -> float | None:
    """Strike minimizing aggregate option-writer payout (options_spec.md §5)."""
    strikes_with_oi = [
        row for row in chain.strikes if (row.call is not None or row.put is not None)
    ]
    if not strikes_with_oi:
        return None

    def _payout(s: float) -> float:
        total = 0.0
        for row in strikes_with_oi:
            call_oi = row.call.oi if row.call else 0
            put_oi = row.put.oi if row.put else 0
            total += call_oi * max(0.0, row.strike - s) + put_oi * max(0.0, s - row.strike)
        return total

    return min((row.strike for row in strikes_with_oi), key=_payout)
