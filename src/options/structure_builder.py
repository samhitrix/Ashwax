"""Seller-desk structure builders — turns a ``StrategyRecommendation`` (WHAT
to trade, from strategy_selector.py) into actual option legs (WHICH strikes),
using the delta-based strike_selector.py primitives. This is the missing
link between strategy selection and execution: options_leg_executor.py
consumes ``(protection_legs, income_legs)`` tuples, which is exactly what
these builders produce.

Target deltas: short=0.25 / long=0.10 for credit verticals is the spec's own
number (§4, bull_put_spread row). Iron Condor/Butterfly extend that
convention to both sides. Straddle/strangle/debit-spread target deltas
(ATM=~0.50, strangle OTM=~0.30) are NOT spec-given exact numbers — documented
as reasonable, standard choices, consistent with this build's practice of
never presenting an invented number as if the spec stated it.

Every builder returns ``(protection_legs, income_legs)`` — protection legs
are the ones executed FIRST by options_leg_executor.py, so a credit
structure's long (protective) leg is always ``protection_legs`` and its
short (premium) leg is always ``income_legs``, regardless of whether the
structure is net-credit or net-debit overall.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.options import seller_settings as _seller_settings
from src.options.strike_selector import find_strike_by_delta, row_is_liquid
from src.options.structure_selector import OptionLeg

if TYPE_CHECKING:
    from src.options.nfo_chain import ChainStrike, OptionChain

# Defaults match options_spec.md §4 (credit_short/credit_long) and this
# module's own documented reasonable choices (atm/strangle_otm) -- config
# (options_settings.json strike_deltas) overrides when present. Read fresh
# on every call, not cached at import time, so a live config change (or
# seller_settings.reload()) takes effect without a process restart.
_DEFAULT_DELTAS = {"credit_short": 0.25, "credit_long": 0.10, "atm": 0.50, "strangle_otm": 0.30}


def _low_ivr_profile(ivr: float | None) -> dict[str, float] | None:
    """``low_ivr_mode`` strike_deltas override, or None if disabled / ivr
    doesn't clear the configured threshold (off by default -- see the
    block's own _note in options_settings.json for the backtest evidence
    behind this feature)."""
    if ivr is None:
        return None
    cfg = _seller_settings.low_ivr_mode()
    if not cfg.get("enabled", False):
        return None
    if not cfg.get("override_deltas", True):
        # 2026-09-08: the backtest that justified this tighter profile also
        # showed it has a REAL LOWER win rate (~71.6% vs the default 0.25
        # delta's ~77.5%, both measured), not just more premium -- under the
        # EV gate's pop_calibration recalibration this tighter delta reads as
        # net-negative EV and blocks every low_ivr_mode candidacy-floor trade
        # from ever passing. Live config sets override_deltas:false so DTE
        # shortening/candidacy floor/size reduction (none of which showed a
        # win-rate penalty) still apply without dragging in the delta change
        # that does. See options_settings.json's pop_calibration._note.
        return None
    threshold = float(cfg.get("ivr_threshold", 25.0))
    if ivr >= threshold:
        return None
    deltas = cfg.get("strike_deltas")
    return deltas if isinstance(deltas, dict) else None


def short_delta(ivr: float | None = None) -> float:
    low_iv = _low_ivr_profile(ivr)
    if low_iv is not None and "credit_short" in low_iv:
        return float(low_iv["credit_short"])
    return float(
        _seller_settings.strike_deltas().get("credit_short", _DEFAULT_DELTAS["credit_short"])
    )


def _long_delta(ivr: float | None = None) -> float:
    low_iv = _low_ivr_profile(ivr)
    if low_iv is not None and "credit_long" in low_iv:
        return float(low_iv["credit_long"])
    return float(
        _seller_settings.strike_deltas().get("credit_long", _DEFAULT_DELTAS["credit_long"])
    )


def _atm_delta() -> float:
    return float(_seller_settings.strike_deltas().get("atm", _DEFAULT_DELTAS["atm"]))


def _strangle_otm_delta() -> float:
    return float(
        _seller_settings.strike_deltas().get("strangle_otm", _DEFAULT_DELTAS["strangle_otm"])
    )


def leg_from_chain_row(
    row: ChainStrike, kind: str, direction: str, lots: int, lot_size: int
) -> OptionLeg | None:
    quote = row.call if kind == "CE" else row.put
    delta = row.call_delta if kind == "CE" else row.put_delta
    symbol = row.call_symbol if kind == "CE" else row.put_symbol
    if quote is None:
        return None
    return OptionLeg(
        tradingsymbol=symbol,
        instrument_type=kind,
        strike=row.strike,
        direction=direction,
        lots=lots,
        lot_size=lot_size,
        premium=quote.mid,
        delta=delta or 0.0,
    )


def _pick_capped_long_row(
    chain: OptionChain,
    kind: str,
    short_row: ChainStrike,
    delta_long_row: ChainStrike,
    max_loss_per_lot: float,
) -> ChainStrike | None:
    """Widest liquid protective strike between the short strike and the delta-derived wing
    whose 1-lot max loss ``(width - credit) * lot_size`` fits ``max_loss_per_lot``. The
    delta wing itself is tried first, so a structure that already fits is unchanged."""
    lo, hi = sorted((short_row.strike, delta_long_row.strike))
    between = [r for r in chain.strikes if lo <= r.strike <= hi and r.strike != short_row.strike]
    # widest (farthest from the short strike) first
    between.sort(key=lambda r: abs(r.strike - short_row.strike), reverse=True)
    short_q = short_row.call if kind == "CE" else short_row.put
    if short_q is None:
        return None
    for row in between:
        if not row_is_liquid(row, kind):
            continue
        long_q = row.call if kind == "CE" else row.put
        if long_q is None:
            continue
        credit = short_q.mid - long_q.mid
        loss = (abs(row.strike - short_row.strike) - credit) * chain.lot_size
        if loss <= max_loss_per_lot:
            return row
    return None


def _vertical(
    chain: OptionChain,
    kind: str,
    short_delta: float,
    long_delta: float,
    lots: int,
    *,
    max_loss_per_lot: float | None = None,
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Shared 2-leg vertical builder for bull_put/bear_call/bull_call/bear_put.

    ``max_loss_per_lot`` (credit verticals only): cap the spread's width so one lot's max
    loss fits the risk budget. The short strike stays at its target delta (that is what
    earns the premium); only the protective wing moves in. Without it the wing delta alone
    sets the width -- ~1,800 points on BANKNIFTY, ~₹47k a lot against a ₹20k limit."""
    short_row = find_strike_by_delta(chain, short_delta, kind)
    long_row = find_strike_by_delta(chain, long_delta, kind)
    if short_row is None or long_row is None or short_row.strike == long_row.strike:
        return None
    if max_loss_per_lot is not None:
        long_row = _pick_capped_long_row(chain, kind, short_row, long_row, max_loss_per_lot)
        if long_row is None:
            return None
    long_leg = leg_from_chain_row(long_row, kind, "BUY", lots, chain.lot_size)
    short_leg = leg_from_chain_row(short_row, kind, "SELL", lots, chain.lot_size)
    if long_leg is None or short_leg is None:
        return None
    return [long_leg], [short_leg]


def build_bull_put_spread(
    chain: OptionChain,
    lots: int,
    *,
    ivr: float | None = None,
    max_loss_per_lot: float | None = None,
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Sell 25-delta put, buy 10-delta put for protection (§4). ``ivr``, when
    low_ivr_mode is enabled and cleared, swaps in a tighter/more-ATM profile
    (see _low_ivr_profile) -- this is the only builder pair (with
    build_bear_call_spread) low_ivr_mode applies to: debit spreads have the
    opposite IV-favorability, unaffected."""
    return _vertical(
        chain, "PE", short_delta(ivr), _long_delta(ivr), lots, max_loss_per_lot=max_loss_per_lot
    )


def build_bear_call_spread(
    chain: OptionChain,
    lots: int,
    *,
    ivr: float | None = None,
    max_loss_per_lot: float | None = None,
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Sell 25-delta call, buy 10-delta call for protection (§4). See
    build_bull_put_spread's ``ivr`` docstring."""
    return _vertical(
        chain, "CE", short_delta(ivr), _long_delta(ivr), lots, max_loss_per_lot=max_loss_per_lot
    )


def build_bull_call_spread(
    chain: OptionChain, lots: int, *, ivr: float | None = None
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Buy near-ATM call (protection), sell OTM call (reduces cost). ``ivr``
    accepted for dispatch-signature uniformity with the credit builders,
    not used -- low_ivr_mode doesn't apply to debit spreads."""
    return _vertical(chain, "CE", short_delta(), _atm_delta(), lots)


def build_bear_put_spread(
    chain: OptionChain, lots: int, *, ivr: float | None = None
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Buy near-ATM put (protection), sell OTM put (reduces cost). ``ivr``
    accepted for dispatch-signature uniformity, not used (see
    build_bull_call_spread)."""
    return _vertical(chain, "PE", short_delta(), _atm_delta(), lots)


def build_iron_condor(
    chain: OptionChain, lots: int, *, ivr: float | None = None
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Bull put spread + bear call spread combined (§4). ``ivr`` threaded
    through for consistency, though iron_condor's own regime gate
    (IVR>=55, strategy_selector._menu) never overlaps low_ivr_mode's
    threshold (default 25) in practice."""
    put_side = build_bull_put_spread(chain, lots, ivr=ivr)
    call_side = build_bear_call_spread(chain, lots, ivr=ivr)
    if put_side is None or call_side is None:
        return None
    protection = put_side[0] + call_side[0]
    income = put_side[1] + call_side[1]
    return protection, income


def build_iron_butterfly(
    chain: OptionChain, lots: int, *, ivr: float | None = None
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Sell ATM straddle, buy wings for protection (§4). ``ivr`` accepted for
    dispatch-signature uniformity, not used -- low_ivr_mode is scoped to
    bull_put_spread/bear_call_spread only."""
    atm = chain.atm()
    wing_call = find_strike_by_delta(chain, _long_delta(), "CE")
    wing_put = find_strike_by_delta(chain, _long_delta(), "PE")
    if atm is None or wing_call is None or wing_put is None:
        return None
    income_call = leg_from_chain_row(atm, "CE", "SELL", lots, chain.lot_size)
    income_put = leg_from_chain_row(atm, "PE", "SELL", lots, chain.lot_size)
    protect_call = leg_from_chain_row(wing_call, "CE", "BUY", lots, chain.lot_size)
    protect_put = leg_from_chain_row(wing_put, "PE", "BUY", lots, chain.lot_size)
    if None in (income_call, income_put, protect_call, protect_put):
        return None
    return [protect_call, protect_put], [income_call, income_put]  # type: ignore[list-item]


def build_long_straddle(
    chain: OptionChain, lots: int, *, ivr: float | None = None
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Buy ATM call + ATM put. No income legs — both are the position itself,
    treated as protection_legs so the executor's fill-first step covers them.
    ``ivr`` accepted for dispatch-signature uniformity, not used."""
    atm = chain.atm()
    if atm is None:
        return None
    call_leg = leg_from_chain_row(atm, "CE", "BUY", lots, chain.lot_size)
    put_leg = leg_from_chain_row(atm, "PE", "BUY", lots, chain.lot_size)
    if call_leg is None or put_leg is None:
        return None
    return [call_leg, put_leg], []


def build_long_strangle(
    chain: OptionChain, lots: int, *, ivr: float | None = None
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Buy OTM call + OTM put (both long — no income legs, see build_long_straddle).
    ``ivr`` accepted for dispatch-signature uniformity, not used."""
    call_row = find_strike_by_delta(chain, _strangle_otm_delta(), "CE")
    put_row = find_strike_by_delta(chain, _strangle_otm_delta(), "PE")
    if call_row is None or put_row is None:
        return None
    call_leg = leg_from_chain_row(call_row, "CE", "BUY", lots, chain.lot_size)
    put_leg = leg_from_chain_row(put_row, "PE", "BUY", lots, chain.lot_size)
    if call_leg is None or put_leg is None:
        return None
    return [call_leg, put_leg], []


def build_calendar_spread(
    near_chain: OptionChain, far_chain: OptionChain, lots: int
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Sell near-term ATM straddle, buy far-term ATM straddle for protection
    (§4: 'sell nearest weekly, buy nearest monthly'). Neutral double calendar
    on both CE+PE — the spec doesn't specify call-only/put-only for this
    non-directional play; documented as a reasonable standard choice (see
    module docstring convention)."""
    near_atm = near_chain.atm()
    far_atm = far_chain.atm()
    if near_atm is None or far_atm is None:
        return None
    near_call = leg_from_chain_row(near_atm, "CE", "SELL", lots, near_chain.lot_size)
    near_put = leg_from_chain_row(near_atm, "PE", "SELL", lots, near_chain.lot_size)
    far_call = leg_from_chain_row(far_atm, "CE", "BUY", lots, far_chain.lot_size)
    far_put = leg_from_chain_row(far_atm, "PE", "BUY", lots, far_chain.lot_size)
    if None in (near_call, near_put, far_call, far_put):
        return None
    return [far_call, far_put], [near_call, near_put]  # type: ignore[list-item]


def build_diagonal_spread(
    near_chain: OptionChain, far_chain: OptionChain, lots: int, directional_bias: float
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Sell near-term ATM option, buy far-term option offset toward the bias
    direction (§4: calendar conditions + mild directional bias). Bullish bias
    (>0) trades the call side, bearish (<0) the put side; far leg at
    the configured long-protection delta on that side (existing
    protective-leg convention)."""
    kind = "CE" if directional_bias > 0 else "PE"
    near_atm = near_chain.atm()
    far_row = find_strike_by_delta(far_chain, _long_delta(), kind)
    if near_atm is None or far_row is None:
        return None
    near_leg = leg_from_chain_row(near_atm, kind, "SELL", lots, near_chain.lot_size)
    far_leg = leg_from_chain_row(far_row, kind, "BUY", lots, far_chain.lot_size)
    if near_leg is None or far_leg is None:
        return None
    return [far_leg], [near_leg]


_BUILDERS: dict[str, Any] = {  # builders take different keyword sets (ivr / max_loss_per_lot)
    "bull_put_spread": build_bull_put_spread,
    "bear_call_spread": build_bear_call_spread,
    "bull_call_spread": build_bull_call_spread,
    "bear_put_spread": build_bear_put_spread,
    "iron_condor": build_iron_condor,
    "iron_butterfly": build_iron_butterfly,
    "long_straddle": build_long_straddle,
    "long_strangle": build_long_strangle,
}

_TWO_EXPIRY_BUILDERS = {
    "calendar_spread": build_calendar_spread,
    "diagonal_spread": build_diagonal_spread,
}


def implemented_strategy_types() -> frozenset[str]:
    """Strategy types with a real leg-construction builder. protective_put,
    collar are not implemented — even if marked enabled in
    options_settings.json, they will never be built."""
    return frozenset(_BUILDERS) | frozenset(_TWO_EXPIRY_BUILDERS)


def build_two_expiry_structure(
    strategy_type: str,
    near_chain: OptionChain,
    far_chain: OptionChain,
    lots: int,
    directional_bias: float | None = None,
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Dispatch for the two-expiry (calendar/diagonal) builders — kept
    separate from build_structure() since those take a single chain."""
    if strategy_type == "calendar_spread":
        return build_calendar_spread(near_chain, far_chain, lots)
    if strategy_type == "diagonal_spread":
        if directional_bias is None:
            return None
        return build_diagonal_spread(near_chain, far_chain, lots, directional_bias)
    return None


_WIDTH_CAPPED_TYPES = frozenset({"bull_put_spread", "bear_call_spread"})


def build_structure(
    strategy_type: str,
    chain: OptionChain,
    lots: int,
    *,
    ivr: float | None = None,
    max_loss_per_lot: float | None = None,
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Dispatch to the right builder. Returns ``None`` if the type is unknown
    or the chain lacks liquid strikes at the required deltas. ``ivr``, when
    low_ivr_mode is enabled and cleared, swaps bull_put_spread/bear_call_spread
    (and iron_condor's sub-legs) onto a tighter delta profile -- every other
    builder accepts and ignores it (see each builder's own docstring)."""
    builder = _BUILDERS.get(strategy_type)
    if builder is None:
        return None
    built: tuple[list[OptionLeg], list[OptionLeg]] | None
    if max_loss_per_lot is not None and strategy_type in _WIDTH_CAPPED_TYPES:
        built = builder(chain, lots, ivr=ivr, max_loss_per_lot=max_loss_per_lot)
    else:
        built = builder(chain, lots, ivr=ivr)
    return built
