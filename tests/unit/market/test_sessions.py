"""config/market_hours.json reader — single source of every session/AMO time."""

from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from src.market import sessions

_IST = ZoneInfo("Asia/Kolkata")


def _at(h: int, m: int, day: int = 21) -> datetime:  # 2026-09-21 is a Monday
    return datetime(2026, 9, day, h, m, tzinfo=_IST)


@pytest.mark.parametrize(
    "h,m,expected",
    [
        (9, 14, sessions.CLOSED),
        (9, 15, sessions.REGULAR),
        (10, 0, sessions.REGULAR),
        (15, 30, sessions.REGULAR),
        (15, 31, sessions.CLOSED),
        (15, 47, sessions.CLOSED),
        (15, 59, sessions.CLOSED),
        (16, 0, sessions.AMO),
        (23, 30, sessions.AMO),
        (8, 56, sessions.AMO),
        (8, 57, sessions.CLOSED),
        (9, 0, sessions.CLOSED),
    ],
)
def test_order_variety_boundaries(h: int, m: int, expected: str) -> None:
    assert sessions.order_variety_at(_at(h, m)) == expected


def test_weekend_is_amo() -> None:
    assert sessions.order_variety_at(_at(11, 0, day=26)) == sessions.AMO  # Saturday


def test_amo_open_is_1600() -> None:
    assert sessions.amo_open() == time(16, 0)


def test_derived_windows_follow_amo(tmp_path: Path) -> None:
    wins = sessions.entry_windows()
    assert wins[0] == (time(0, 0), sessions.amo_close(), "SWING", 0.0)
    assert wins[-1][0] == sessions.amo_open()


def test_no_midday_entry_restriction_intraday_follows_the_trading_window() -> None:
    """One continuous entry window from the open to last_entry_by; no NONE (no-entry) window."""
    wins = sessions.entry_windows()
    assert all(w[2] != "NONE" for w in wins)
    assert not hasattr(sessions, "lunch_lull")
    for start, end, allowed, _c in wins:
        if start <= time(12, 30) < end:
            assert allowed == "ALL"


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(sessions.MarketHoursConfigError):
        sessions._load(tmp_path / "absent.json")


def test_invalid_file_raises(tmp_path: Path) -> None:
    bad = tmp_path / "m.json"
    bad.write_text(json.dumps({"markets": {"x": {}}, "schedules": {}}))
    with pytest.raises(sessions.MarketHoursConfigError):
        sessions._load(bad)


def test_unknown_schedule_raises() -> None:
    with pytest.raises(sessions.MarketHoursConfigError):
        sessions.schedule("nope")


def test_strategy_window_outside_market_rejected() -> None:
    from pydantic import ValidationError
    from src.strategies.uss.schema import IntradaySession

    IntradaySession(entry_window="0930-1430")  # narrower: fine
    with pytest.raises(ValidationError):
        IntradaySession(entry_window="0900-1430")  # opens before the session
    with pytest.raises(ValidationError):
        IntradaySession(entry_window="0930-1520")  # past the EXIT_ONLY start
