"""Pre-order entry discipline — the "don't chase a breakout that already ran" guard.

The signal computes entry/SL/target off a reference level (e.g. the ORB breakout).
If, by the time we are about to place the MARKET order, price has already run far
past that level — or is already beyond the target / through the stop — entering now
means chasing an extended move with worse expectancy. This pure helper decides,
from the current reference price, whether to skip the entry entirely (no order is
placed, so no slippage loss is booked).

Distinct from post_fill_risk.reanchor_protection: that runs AFTER a fill to make the
protection valid; this runs BEFORE the order to avoid entering at all.
"""

from __future__ import annotations

import logging

# NOT get_logger("execution") — that flat name has no handler (see logger.py
# _COMPONENTS, which maps the dotted "src.execution" hierarchy to execution.log).
# __name__ here resolves to "src.execution.entry_guard", correctly nesting under it.
_log = logging.getLogger(__name__)


def should_skip_chase(
    *,
    entry: float,
    sl: float,
    target: float,
    ltp: float,
    direction: str,
    max_chase_r: float,
) -> tuple[bool, str]:
    """Return (skip, reason). skip=True means do NOT place the entry.

    Args:
        entry/sl/target: the signal's planned levels.
        ltp: current reference price (live LTP or latest candle close).
        direction: BUY/long or SELL/short.
        max_chase_r: max allowed favourable drift past entry, in R units (e.g. 0.75).
    """
    _log.debug(
        "should_skip_chase_received entry=%.2f sl=%.2f target=%.2f ltp=%.2f direction=%s "
        "max_chase_r=%.2f",
        entry,
        sl,
        target,
        ltp,
        direction,
        max_chase_r,
    )
    risk = abs(entry - sl)
    if risk <= 0:
        _log.debug("should_skip_chase_found skip=True reason=degenerate_geometry")
        return True, "degenerate_geometry"
    is_long = direction.upper() in ("BUY", "LONG")
    if is_long:
        if ltp >= target:
            _log.debug("should_skip_chase_found skip=True reason=ltp_past_target")
            return True, "ltp_past_target"
        if ltp <= sl:
            _log.debug("should_skip_chase_found skip=True reason=ltp_through_sl")
            return True, "ltp_through_sl"
        drift_r = (ltp - entry) / risk
    else:
        if ltp <= target:
            _log.debug("should_skip_chase_found skip=True reason=ltp_past_target")
            return True, "ltp_past_target"
        if ltp >= sl:
            _log.debug("should_skip_chase_found skip=True reason=ltp_through_sl")
            return True, "ltp_through_sl"
        # For SHORT: adverse direction is price rising BACK ABOVE the breakdown level.
        # Price falling below entry is normal for a breakdown entry — do not skip it.
        drift_r = (ltp - entry) / risk  # positive only when ltp > entry (bounce above ORB low)
    if drift_r > max_chase_r:
        _log.debug(
            "should_skip_chase_found skip=True reason=drift_%.2fR_over_%.2fR", drift_r, max_chase_r
        )
        return True, f"drift_{drift_r:.2f}R_over_{max_chase_r:.2f}R"
    _log.debug("should_skip_chase_found skip=False drift_r=%.2f", drift_r)
    return False, ""


def regime_blocks_direction(regime: str, direction: str) -> bool:
    """Hard structural-regime gate: BEAR* blocks LONG, BULL* blocks SHORT.

    Operator rule: never trade against the day's structural regime, regardless
    of strategy signal quality. NEUTRAL/UNKNOWN block nothing — the intraday
    breadth gate remains as the faster-moving second layer.
    """
    r = (regime or "").upper()
    d = (direction or "").upper()
    if d in ("BUY", "LONG"):
        blocked = r in ("BEAR", "STRONG_BEAR")
        _log.debug("regime_blocks_direction_found regime=%s direction=%s blocked=%s", r, d, blocked)
        return blocked
    if d in ("SELL", "SHORT"):
        blocked = r in ("BULL", "STRONG_BULL")
        _log.debug("regime_blocks_direction_found regime=%s direction=%s blocked=%s", r, d, blocked)
        return blocked
    return False


def classify_intraday_regime(
    index_change_pct: float | None,
    bearish_pct: float | None,
    *,
    index_move_min_pct: float = 0.10,
    breadth_confirm: float = 0.50,
) -> str:
    """Day-scoped intraday regime, refreshed every scan cycle (~5 min).

    Combines the benchmark index's move since previous close with watchlist
    breadth (share of symbols below their EMA20): BEAR needs the index down at
    least `index_move_min_pct` AND at least `breadth_confirm` of the watchlist
    bearish; BULL mirrors. Anything else is NEUTRAL. Missing index data →
    UNKNOWN (caller decides the fallback). Missing breadth (first cycle) is
    treated as neutral 0.5 so the index alone can classify.
    """
    if index_change_pct is None:
        _log.debug("classify_intraday_regime_found index_change_pct=None result=UNKNOWN")
        return "UNKNOWN"
    b = 0.5 if bearish_pct is None else float(bearish_pct)
    if index_change_pct <= -index_move_min_pct and b >= breadth_confirm:
        result = "BEAR"
    elif index_change_pct >= index_move_min_pct and (1.0 - b) >= breadth_confirm:
        result = "BULL"
    else:
        result = "NEUTRAL"
    _log.debug(
        "classify_intraday_regime_found index_change_pct=%.3f bearish_pct=%.3f result=%s",
        index_change_pct,
        b,
        result,
    )
    return result
