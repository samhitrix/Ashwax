"""
Gap Risk Manager — Friday / pre-holiday position review.

Runs at ~14:30 IST on Fridays (or the day before a market holiday) to flag
positions that will be exposed to a weekend / holiday gap.

Decisions:
  HOLD        — gap risk is within operator tolerance
  REDUCE      — reduce quantity to bring risk within tolerance
  CLOSE       — close the position before market close today
  MONITOR     — exposure is elevated but under soft threshold; watch only

Input:  list of OpenPosition objects (passed by caller)
Output: list of GapRiskDecision objects (one per position)

The supervisor / CRM caller is responsible for actually placing orders.
This module only evaluates and recommends.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

# Thresholds (operator-configurable in practice; hardcoded here as safe defaults)
_SOFT_GAP_RISK_PCT = 0.02  # 2% gap risk → MONITOR
_REDUCE_GAP_RISK_PCT = 0.03  # 3% gap risk → REDUCE (spec: MEDIUM threshold)
_HARD_GAP_RISK_PCT = 0.06  # 6% gap risk → CLOSE  (spec: HIGH threshold; was 0.04 — fixed)
_MAX_OVERNIGHT_EXPOSURE = 0.10  # portfolio exposure beyond which any Friday pos → REDUCE
_LONG_WEEKEND_GAP_DAYS = 3  # gap days threshold for mandatory MEDIUM floor


class GapAction(str, Enum):  # noqa: UP042
    HOLD = "HOLD"
    MONITOR = "MONITOR"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


# Severity order + the gap_risk.long_weekend_min string → action floor mapping.
_ACTION_ORDER = {
    GapAction.HOLD: 0,
    GapAction.MONITOR: 1,
    GapAction.REDUCE: 2,
    GapAction.CLOSE: 3,
}
_LONG_WEEKEND_FLOOR = {
    "HOLD": GapAction.HOLD,
    "MONITOR": GapAction.MONITOR,
    "LOW": GapAction.MONITOR,
    "MEDIUM": GapAction.REDUCE,
    "REDUCE": GapAction.REDUCE,
    "HIGH": GapAction.CLOSE,
    "CLOSE": GapAction.CLOSE,
}


@dataclass
class OpenPosition:
    symbol: str
    quantity: int
    avg_price: float  # average entry price
    current_ltp: float  # last traded price
    product: str  # "CNC" or "MIS"
    stop_loss_price: float  # current SL price
    earnings_within_days: int | None = None  # trading days until next earnings; None = unknown
    max_historical_gap_pct: float | None = (
        None  # max overnight gap from 2yr OHLCV; None = use 4% default
    )


@dataclass
class GapRiskDecision:
    symbol: str
    action: GapAction
    reason: str
    suggested_quantity: int  # 0 = close fully; same as current = hold/monitor
    gap_risk_pct: float  # estimated max gap as fraction of position value


def _is_friday(dt: datetime) -> bool:
    return dt.weekday() == 4  # Monday=0 … Friday=4


def _is_pre_holiday(dt: datetime, holidays: set[date]) -> bool:
    """True if tomorrow is a market holiday."""
    nxt = dt.date() + timedelta(days=1)
    return nxt in holidays


def _count_gap_days(dt: datetime, holidays: set[date]) -> int:
    """
    Count how many calendar days the market will be closed starting from dt.

    Friday alone = 2 (Sat + Sun).
    Friday before a Monday holiday = 3.
    Pre-holiday (not Friday) = 1 + any adjacent weekend/holidays.
    Used to detect long weekends where all positions must be treated as MEDIUM minimum.
    """
    check = dt.date()
    gap = 0
    cursor = check + timedelta(days=1)
    # Walk forward counting weekend days and holidays (up to 7 days)
    for _ in range(7):
        if cursor.weekday() >= 5 or cursor in holidays:  # Sat=5, Sun=6
            gap += 1
            cursor += timedelta(days=1)
        else:
            break
    return gap


def _position_value(pos: OpenPosition) -> float:
    return pos.current_ltp * pos.quantity


def _sl_gap_risk_pct(pos: OpenPosition) -> float:
    """
    Conservative gap risk estimate: how far a 2-day gap could move past the SL.

    For delivery (CNC) positions the SL GTT won't trigger on gap opens — the
    fill price equals the gap-open price.  We use ATR-proxy: ±4% weekend gap
    assumption, compared against current SL distance.
    """
    if pos.avg_price <= 0:
        return 0.0
    sl_distance_pct = abs(pos.current_ltp - pos.stop_loss_price) / pos.current_ltp
    # Use historical max gap when available; fall back to 4% Indian market empirical default
    weekend_gap_estimate = (
        pos.max_historical_gap_pct if pos.max_historical_gap_pct is not None else 0.04
    )
    return max(0.0, weekend_gap_estimate - sl_distance_pct)


def evaluate_gap_risk(
    positions: list[OpenPosition],
    total_capital: float,
    check_time: datetime | None = None,
    holidays: set[date] | None = None,
    gap_config: dict[str, Any] | None = None,
) -> list[GapRiskDecision]:
    """
    Evaluate each open CNC position for weekend / holiday gap risk.

    MIS positions are skipped (closed by EOD auto-square-off).

    gap_config keys (from risk.json gap_risk.*):
      enabled: bool        — if False, skip entirely
      low_max_pct: int     — gap % → REDUCE (default 3 = 3%)
      medium_max_pct: int  — gap % → CLOSE  (default 6 = 6%)
    """
    cfg = gap_config or {}
    if not cfg.get("enabled", True):
        return []
    _reduce_pct = float(cfg.get("low_max_pct", _REDUCE_GAP_RISK_PCT * 100)) / 100.0
    _close_pct = float(cfg.get("medium_max_pct", _HARD_GAP_RISK_PCT * 100)) / 100.0
    # risk.json gap_risk.* — config-driven actions:
    #   medium_reduce_pct: % of a position to trim in the medium gap band
    #   high_action:       EXIT_FULLY (close) vs REDUCE for the high gap band
    #   long_weekend_min:  minimum action floored onto every CNC pos on a 3+ day gap
    _medium_reduce = float(cfg.get("medium_reduce_pct", 50)) / 100.0
    _high_action = str(cfg.get("high_action", "EXIT_FULLY")).upper()
    _long_weekend_min = str(cfg.get("long_weekend_min", "MONITOR")).upper()

    now = check_time or datetime.now(_IST)
    holidays = holidays or set()

    is_gap_day = _is_friday(now) or _is_pre_holiday(now, holidays)
    gap_days = _count_gap_days(now, holidays) if is_gap_day else 0
    is_long_weekend = gap_days >= _LONG_WEEKEND_GAP_DAYS

    results: list[GapRiskDecision] = []

    total_cnc_exposure = sum(_position_value(p) for p in positions if p.product == "CNC")
    portfolio_exposure_pct = total_cnc_exposure / total_capital if total_capital else 0.0

    for pos in positions:
        if pos.product != "CNC":
            continue  # MIS handled by EOD auto-square-off

        gap_risk = _sl_gap_risk_pct(pos)
        pos_value = _position_value(pos)

        if not is_gap_day:
            results.append(
                GapRiskDecision(
                    symbol=pos.symbol,
                    action=GapAction.HOLD,
                    reason="Not a Friday or pre-holiday",
                    suggested_quantity=pos.quantity,
                    gap_risk_pct=round(gap_risk, 4),
                )
            )
            continue

        # Earnings within 3 days → binary risk → EXIT FULLY regardless of gap pct
        if pos.earnings_within_days is not None and pos.earnings_within_days <= 3:
            results.append(
                GapRiskDecision(
                    symbol=pos.symbol,
                    action=GapAction.CLOSE,
                    reason=f"Earnings in {pos.earnings_within_days} trading day(s) — binary risk, exit fully",
                    suggested_quantity=0,
                    gap_risk_pct=round(gap_risk, 4),
                )
            )
            continue

        # Decide action
        if gap_risk >= _close_pct:
            if _high_action == "EXIT_FULLY":
                action = GapAction.CLOSE
                suggested_qty = 0
                reason = f"Gap risk {gap_risk:.1%} >= close threshold {_close_pct:.1%} → EXIT_FULLY"
            else:
                action = GapAction.REDUCE
                suggested_qty = max(1, round(pos.quantity * (1.0 - _medium_reduce)))
                reason = (
                    f"Gap risk {gap_risk:.1%} >= close threshold {_close_pct:.1%}"
                    f" → {_high_action} (trim {_medium_reduce:.0%})"
                )

        elif gap_risk >= _reduce_pct:
            action = GapAction.REDUCE
            suggested_qty = max(1, round(pos.quantity * (1.0 - _medium_reduce)))
            reason = (
                f"Gap risk {gap_risk:.1%} — trim {_medium_reduce:.0%} to {suggested_qty} shares"
            )

        elif portfolio_exposure_pct > _MAX_OVERNIGHT_EXPOSURE and pos_value > 0:
            action = GapAction.REDUCE
            target_cnc = total_capital * _MAX_OVERNIGHT_EXPOSURE
            trim_ratio = target_cnc / total_cnc_exposure if total_cnc_exposure else 1.0
            suggested_qty = max(1, int(pos.quantity * trim_ratio))
            reason = (
                f"Portfolio overnight exposure {portfolio_exposure_pct:.1%}"
                f" > {_MAX_OVERNIGHT_EXPOSURE:.0%} — trim"
            )

        elif gap_risk >= _SOFT_GAP_RISK_PCT:
            action = GapAction.MONITOR
            reason = f"Gap risk {gap_risk:.1%} — within soft threshold, monitor"
            suggested_qty = pos.quantity

        else:
            action = GapAction.HOLD
            reason = f"Gap risk {gap_risk:.1%} — acceptable"
            suggested_qty = pos.quantity

        # Long weekend (3+ days closed): floor every CNC position to the configured
        # minimum action (risk.json gap_risk.long_weekend_min).
        if is_long_weekend:
            _floor = _LONG_WEEKEND_FLOOR.get(_long_weekend_min, GapAction.MONITOR)
            if _ACTION_ORDER[action] < _ACTION_ORDER[_floor]:
                action = _floor
                if action == GapAction.REDUCE and suggested_qty >= pos.quantity:
                    suggested_qty = max(1, round(pos.quantity * (1.0 - _medium_reduce)))
                elif action == GapAction.CLOSE:
                    suggested_qty = 0
                reason = (
                    f"Long weekend ({gap_days} days): floored to {action.value}"
                    f" (long_weekend_min={_long_weekend_min})"
                )

        results.append(
            GapRiskDecision(
                symbol=pos.symbol,
                action=action,
                reason=reason,
                suggested_quantity=suggested_qty,
                gap_risk_pct=round(gap_risk, 4),
            )
        )

    return results
