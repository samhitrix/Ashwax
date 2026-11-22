"""Tests for TimeFilter."""

from datetime import datetime
from zoneinfo import ZoneInfo

from src.risk.time_filter import TimeFilter

_IST = ZoneInfo("Asia/Kolkata")


def _ist(h: int, m: int) -> datetime:
    return datetime(2026, 5, 30, h, m, 0, tzinfo=_IST)


def test_morning_window_allowed():
    ok, reason = TimeFilter().check(_ist(9, 45))
    assert ok
    assert "OK" in reason


def test_midday_is_an_ordinary_trading_time():
    for hh, mm in ((10, 45), (12, 0), (12, 30), (13, 29)):
        ok, reason = TimeFilter().check(_ist(hh, mm))
        assert ok, f"blocked at {hh}:{mm:02d}: {reason}"


def test_pre_close_exit_only():
    ok, reason = TimeFilter().check(_ist(15, 20))
    assert not ok
    assert "exit" in reason.lower() or "pre-close" in reason.lower()


def test_outside_hours_blocked():
    """08:57-09:15 is a structural dead zone (Zerodha accepts neither AMO nor
    REGULAR here — see KiteAdapter._current_order_variety()), genuinely
    uncovered by any window. 08:00 itself now falls in the 00:00-08:57 AMO
    SWING window (see test_early_morning_amo_window_allowed), so it no longer
    represents "outside hours" for this test's purpose."""
    ok, reason = TimeFilter().check(_ist(9, 0))
    assert not ok
    assert "outside" in reason


def test_pre_close_swing_low_confidence_blocked():
    ok, reason = TimeFilter().check(_ist(14, 45), strategy_type="SWING", llm_confidence=0.70)
    assert not ok
    assert "confidence" in reason


def test_pre_close_swing_high_confidence_allowed():
    ok, _ = TimeFilter().check(_ist(14, 45), strategy_type="SWING", llm_confidence=0.90)
    assert ok


def test_opening_minutes_follow_the_trading_window_no_orb_only_carve_out():
    """09:15 is an ordinary trading time now: no ORB-only block for other strategies."""
    for st in ("SWING", "MIS", "ORB", "ALL"):
        ok, reason = TimeFilter().check(_ist(9, 20), strategy_type=st)
        assert ok, f"{st} blocked at 09:20: {reason}"


def test_afternoon_window_allowed():
    ok, _ = TimeFilter().check(_ist(14, 0))
    assert ok


def test_mid_morning_has_no_swing_only_restriction_intraday_may_enter():
    ok, reason = TimeFilter().check(_ist(11, 0), strategy_type="MIS")
    assert ok, reason


def test_post_close_eod_swing_scan_allowed():
    """The swing scan runs once the AMO window opens (16:00 IST, config/
    market_hours.json) to evaluate the completed daily candle; the post-close
    window must accept SWING entries."""
    ok, _ = TimeFilter().check(_ist(16, 0), strategy_type="SWING")
    assert ok


def test_post_close_window_blocks_non_swing():
    ok, reason = TimeFilter().check(_ist(16, 0), strategy_type="MIS")
    assert not ok
    assert "swing" in reason.lower()


def test_dead_zone_before_amo_window_blocked():
    """15:30-16:00: regular session has closed but the AMO window hasn't
    opened yet — no order variety is accepted here, so G3 must block."""
    for hh, mm in ((15, 35), (15, 47), (15, 59)):
        ok, reason = TimeFilter().check(_ist(hh, mm), strategy_type="SWING")
        assert not ok
        assert "outside" in reason


def test_evening_amo_window_allowed():
    """Regression: G3's post-close SWING window used to end at 16:00, while
    KiteAdapter._current_order_variety() (the broker layer) accepts AMO orders
    all the way to 08:57 IST the next session. That mismatch silently blocked
    every swing signal generated after 16:00 (e.g. after a launcher restart
    later in the evening) even though the broker would have accepted it."""
    ok, _ = TimeFilter().check(_ist(22, 48), strategy_type="SWING")
    assert ok


def test_early_morning_amo_window_allowed():
    ok, _ = TimeFilter().check(_ist(8, 0), strategy_type="SWING")
    assert ok


def test_windows_come_from_market_hours_json() -> None:
    from src.market import sessions

    wins = TimeFilter()._windows
    assert wins[0][1] == sessions.amo_close()  # derived pre-open SWING window
    assert wins[-1][0] == sessions.amo_open()  # derived post-close SWING window
