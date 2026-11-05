"""Dashboard-facing analytics derived purely from an already-persisted position's
legs + net_credit -- no new per-position storage needed for these two. Shared by
the /api/options_positions route and the daily P&L snapshot job so unrealized P&L
is computed exactly once, the same way, everywhere it's used.

Breakeven and POP are computed only for short-premium (credit) structures --
verticals, condors, butterflies -- which is everything this desk actually trades
(bull_put_spread, bear_call_spread, iron_condor, iron_butterfly,
options_expiry_day, options_intraday_short_premium{,2}). A structure with no SELL leg (a pure debit buy, not currently
traded by this desk) returns no breakeven/POP rather than guessing with a different,
unvalidated formula -- see project rule against inventing unvalidated figures.
"""

from __future__ import annotations

from typing import Any

from src.options.strategy_selector import calibrated_pop


def compute_leg_pnl(
    legs: list[dict[str, Any]], ltp: dict[str, Any]
) -> tuple[float | None, list[float | None]]:
    """(total_pnl, per_leg_pnl). total_pnl is None if any open leg's LTP is
    unavailable -- mirrors SellerDesk._unrealized_pnl's sign convention exactly
    (BUY gains as price rises, SELL gains as price falls)."""
    total = 0.0
    known = True
    per_leg: list[float | None] = []
    for leg in legs:
        premium = float(leg.get("premium") or 0)
        qty = int(leg.get("lots") or 0) * int(leg.get("lot_size") or 0)
        tick = ltp.get(leg.get("tradingsymbol") or "")
        current = float(tick["last_price"]) if tick else None
        if current is None:
            known = False
            per_leg.append(None)
            continue
        sign = 1.0 if leg.get("direction") == "BUY" else -1.0
        leg_pnl = sign * (current - premium) * qty
        total += leg_pnl
        per_leg.append(leg_pnl)
    return (total if known else None), per_leg


def compute_breakevens(
    legs: list[dict[str, Any]], net_credit: float, spot: float | None = None
) -> list[dict[str, float | None]]:
    """Breakeven price(s) at expiry, one per side that has a SELL leg, each as
    {"strike": price, "pctFromSpot": signed % vs current spot, or None if spot
    is unavailable}.

    Standard retail-platform formula: breakeven = short strike per side, offset by
    the structure's net credit per unit, split evenly across however many short
    sides exist (one side for a vertical, two for a condor/butterfly). Empty list
    for a structure with no SELL leg at all (pure debit buy -- not computed, see
    module docstring)."""
    shorts_by_side: dict[str, float] = {}
    qty = 0
    for leg in legs:
        leg_qty = int(leg.get("lots") or 0) * int(leg.get("lot_size") or 0)
        qty = max(qty, leg_qty)
        if leg.get("direction") != "SELL":
            continue
        side = str(leg.get("instrument_type", "")).upper()
        strike = float(leg.get("strike") or 0)
        if side not in shorts_by_side:
            shorts_by_side[side] = strike
    if not shorts_by_side or qty <= 0:
        return []
    credit_per_unit = net_credit / qty
    per_side_credit = credit_per_unit / len(shorts_by_side)
    strikes: list[float] = []
    for side, strike in shorts_by_side.items():
        if side == "PE":
            strikes.append(round(strike - per_side_credit, 2))
        elif side == "CE":
            strikes.append(round(strike + per_side_credit, 2))
    strikes.sort()
    return [
        {
            "strike": s,
            "pctFromSpot": round((s - spot) / spot * 100, 1) if spot else None,
        }
        for s in strikes
    ]


def compute_exit_levels(
    strategy_type: str,
    legs: list[dict[str, Any]],
    net_credit: float,
    max_profit: float,
    entry_margin: float | None,
) -> dict[str, float | None]:
    """Rupee P&L at which the desk auto-exits (target) and stops out (stop, a positive
    loss magnitude), for the dashboard. Reads the SAME config accessors and applies the SAME
    formulas as the exit code (position_manager.check_profit_target/check_stop_loss,
    SellerDesk._manage_expiry_day, short_premium._check_target and its
    per-leg stops), so a config edit shows here exactly when the exit logic picks it up.

    ``targetPct`` = target as % of max profit, ``stopPct`` = stop as % of margin; each None when
    its base is unknown. Any amount is None when the strategy has no such exit."""
    from src.options import position_manager as pm
    from src.options import seller_settings
    from src.options.short_premium import (
        EXPIRY_STRANGLE_TYPE,
        SHORT_PREMIUM2_TYPE,
        SHORT_PREMIUM_TYPE,
        load_cfg,
        load_cfg2,
        load_expiry_cfg,
    )

    credit = abs(net_credit)
    target: float | None = None
    stop: float | None = None

    # The expiry-day strategy is a per-leg-stop strangle (short_premium engine) when its config says so,
    # else the legacy butterfly with a credit-multiple stop (branch below).
    is_strangle = strategy_type == EXPIRY_STRANGLE_TYPE and load_expiry_cfg() is not None
    if strategy_type in (SHORT_PREMIUM_TYPE, SHORT_PREMIUM2_TYPE) or is_strangle:
        cfg = {SHORT_PREMIUM_TYPE: load_cfg, SHORT_PREMIUM2_TYPE: load_cfg2}.get(
            strategy_type, load_expiry_cfg
        )()
        if cfg is not None:
            # Same rule as short_premium._check_target: measured on the legs still open (all of them,
            # until one is stopped out -- then the target shrinks to what the remaining legs can earn).
            open_credit = sum(
                (1.0 if leg.get("direction") == "SELL" else -1.0)
                * float(leg.get("premium") or 0)
                * int(leg.get("lots") or 0)
                * int(leg.get("lot_size") or 0)
                for leg in legs
            )
            target = cfg.target_pct / 100.0 * open_credit if cfg.target_pct > 0 else None
            # Every short leg stopped exactly at its trigger (per-leg SL, see sl_defined_max_loss).
            stop = sum(
                float(leg.get("premium") or 0)
                * (cfg.sl_pct_ce if leg.get("instrument_type") == "CE" else cfg.sl_pct_pe)
                / 100.0
                * int(leg.get("lots") or 0)
                * int(leg.get("lot_size") or 0)
                for leg in legs
                if leg.get("direction") == "SELL"
            )
    elif strategy_type == "options_expiry_day":
        block = seller_settings.expiry_day()
        target = float(block.get("profit_target_pct", 50)) / 100.0 * credit
        stop = float(block.get("stop_loss_credit_multiple", 2.0)) * credit
    elif credit > 0:
        direction = "credit" if net_credit > 0 else "debit"
        if direction == "credit":
            target = (
                pm._pct("credit_profit_target_pct", pm._CREDIT_PROFIT_TARGET_PCT) / 100.0 * credit
            )
            stop = pm._pct("credit_stop_loss_pct", pm._CREDIT_STOP_LOSS_PCT) / 100.0 * credit
        else:
            target = (
                pm._pct("debit_profit_target_pct", pm._DEBIT_PROFIT_TARGET_PCT) / 100.0 * credit
            )
            stop = pm._pct("debit_stop_loss_pct", pm._DEBIT_STOP_LOSS_PCT) / 100.0 * credit

    target = target if target and target > 0 else None
    stop = stop if stop and stop > 0 else None
    return {
        "targetAmount": target,
        "targetPct": (target / max_profit * 100.0) if (target and max_profit > 0) else None,
        "stopAmount": stop,
        "stopPct": (stop / entry_margin * 100.0)
        if (stop and entry_margin and entry_margin > 0)
        else None,
    }


def compute_pop(strategy_type: str, legs: list[dict[str, Any]]) -> float | None:
    """Probability of profit for every structure type with a SELL leg (operator
    request 2026-10-07: show it for all strategies, not just the two backtested
    ones). calibrated_pop() itself decides the model per type: the real
    backtested anchors for bull_put_spread/bear_call_spread (the only two
    scripts/backtest_options_seller_regime.py has actually measured), falling
    back to the crude 1-sum(|short_delta|) touch-probability proxy for every
    other type (iron_condor, iron_butterfly,
    options_expiry_day, ...) -- a standard, honest approximation, just a less
    precise one than the calibrated model. None only when there is no SELL leg
    at all (a pure debit buy -- no short-delta-based formula applies)."""
    short_deltas = [
        abs(float(leg["delta"]))
        for leg in legs
        if leg.get("direction") == "SELL" and leg.get("delta") is not None
    ]
    if not short_deltas:
        return None
    return calibrated_pop(strategy_type, short_deltas)
