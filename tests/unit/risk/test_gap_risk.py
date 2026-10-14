"""Unit tests for src/risk/gap_risk.py"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.risk.gap_risk import GapAction, OpenPosition, evaluate_gap_risk

_IST = ZoneInfo("Asia/Kolkata")

_FRIDAY_14H = datetime(2026, 5, 29, 14, 30, tzinfo=_IST)  # 2026-05-29 is a Friday
_MONDAY_10H = datetime(2026, 6, 1, 10, 0, tzinfo=_IST)


def _pos(symbol: str, qty: int, avg: float, ltp: float, sl: float) -> OpenPosition:
    return OpenPosition(
        symbol=symbol,
        quantity=qty,
        avg_price=avg,
        current_ltp=ltp,
        product="CNC",
        stop_loss_price=sl,
    )


def test_not_friday_returns_hold():
    pos = _pos("X", 10, 100.0, 100.0, 90.0)
    results = evaluate_gap_risk([pos], 200_000.0, check_time=_MONDAY_10H)
    assert len(results) == 1
    assert results[0].action == GapAction.HOLD
    assert "Not a Friday" in results[0].reason


def test_friday_tight_sl_returns_reduce():
    # SL == ltp: no distance → gap_risk = 4% (hardcoded weekend estimate).
    # Correct close threshold is 6%, so 4% → REDUCE (not CLOSE).
    pos = _pos("TIGHT", 10, 100.0, 100.0, 100.0)
    results = evaluate_gap_risk([pos], 200_000.0, check_time=_FRIDAY_14H)
    assert results[0].action == GapAction.REDUCE


def test_earnings_proximity_triggers_close():
    # Earnings within 3 days on a gap day → CLOSE regardless of gap pct
    pos = OpenPosition(
        symbol="EARNER",
        quantity=10,
        avg_price=100.0,
        current_ltp=105.0,
        product="CNC",
        stop_loss_price=90.0,  # wide SL — would normally HOLD
        earnings_within_days=2,
    )
    results = evaluate_gap_risk([pos], 200_000.0, check_time=_FRIDAY_14H)
    assert results[0].action == GapAction.CLOSE
    assert "earnings" in results[0].reason.lower()


def test_friday_wide_sl_returns_hold():
    # SL at 90 -- 10% away, gap risk = max(0, 0.04 - 0.10) = 0 -> HOLD
    pos = _pos("WIDE", 10, 100.0, 100.0, 90.0)
    results = evaluate_gap_risk([pos], 200_000.0, check_time=_FRIDAY_14H)
    assert results[0].action == GapAction.HOLD


def test_mis_positions_skipped():
    mis_pos = OpenPosition(
        symbol="MIS_TRADE",
        quantity=10,
        avg_price=100.0,
        current_ltp=100.0,
        product="MIS",
        stop_loss_price=95.0,
    )
    results = evaluate_gap_risk([mis_pos], 200_000.0, check_time=_FRIDAY_14H)
    assert len(results) == 0


def test_pre_holiday_treated_as_gap_day():
    holidays = {date(2026, 6, 3)}
    tuesday_14h = datetime(2026, 6, 2, 14, 30, tzinfo=_IST)
    pos = _pos("TIGHT2", 10, 100.0, 100.0, 99.9)
    results = evaluate_gap_risk([pos], 200_000.0, check_time=tuesday_14h, holidays=holidays)
    assert results[0].action in (GapAction.CLOSE, GapAction.REDUCE, GapAction.MONITOR)


# ── risk.json gap_risk config keys (Phase 2 wiring) ────────────────────────────


def test_medium_reduce_pct_drives_trim():
    # 4% gap → REDUCE band; medium_reduce_pct=80 → keep 20%
    pos = _pos("TIGHT", 100, 100.0, 100.0, 100.0)
    cfg = {"enabled": True, "medium_reduce_pct": 80}
    results = evaluate_gap_risk([pos], 200_000.0, check_time=_FRIDAY_14H, gap_config=cfg)
    assert results[0].action == GapAction.REDUCE
    assert results[0].suggested_quantity == 20


def test_high_action_reduce_instead_of_close():
    # medium_max_pct=3 pushes the 4% gap into the high band; high_action=REDUCE keeps it open
    pos = _pos("HIGH", 100, 100.0, 100.0, 100.0)
    cfg = {"enabled": True, "medium_max_pct": 3, "high_action": "REDUCE", "medium_reduce_pct": 50}
    results = evaluate_gap_risk([pos], 200_000.0, check_time=_FRIDAY_14H, gap_config=cfg)
    assert results[0].action == GapAction.REDUCE
    assert results[0].suggested_quantity == 50


def test_high_action_exit_fully_closes():
    pos = _pos("HIGH", 100, 100.0, 100.0, 100.0)
    cfg = {"enabled": True, "medium_max_pct": 3, "high_action": "EXIT_FULLY"}
    results = evaluate_gap_risk([pos], 200_000.0, check_time=_FRIDAY_14H, gap_config=cfg)
    assert results[0].action == GapAction.CLOSE
    assert results[0].suggested_quantity == 0


def test_long_weekend_min_floors_to_medium():
    # Wide SL → normally HOLD; a 3-day gap with long_weekend_min=MEDIUM floors to REDUCE
    holidays = {date(2026, 6, 1)}  # Monday holiday after Fri 2026-05-29
    pos = _pos("WIDE", 100, 100.0, 100.0, 90.0)
    cfg = {"enabled": True, "long_weekend_min": "MEDIUM", "medium_reduce_pct": 50}
    results = evaluate_gap_risk(
        [pos], 200_000.0, check_time=_FRIDAY_14H, holidays=holidays, gap_config=cfg
    )
    assert results[0].action == GapAction.REDUCE
