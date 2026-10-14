"""Tests for src.market.calendar — the NSE trading-calendar gate."""

from __future__ import annotations

from datetime import date, datetime

from src.market import calendar as cal


def test_real_holiday_is_loaded_and_blocks_trading() -> None:
    # Regression for the old dict-key-iteration bug that always returned an empty set.
    holidays = cal.reload()
    assert holidays, "holiday set must not be empty"
    assert date(2026, 6, 26) in holidays  # Muharram

    assert cal.is_holiday(date(2026, 6, 26)) is True
    assert cal.is_trading_day(date(2026, 6, 26)) is False  # the live-trading bug


def test_normal_weekday_is_a_trading_day() -> None:
    assert cal.is_trading_day(date(2026, 6, 25)) is True  # Thursday, not a holiday


def test_weekend_is_not_a_trading_day() -> None:
    assert cal.is_trading_day(date(2026, 6, 27)) is False  # Saturday


def test_market_open_window() -> None:
    open_dt = datetime(2026, 6, 25, 10, 0)  # trading day, in session
    early_dt = datetime(2026, 6, 25, 8, 0)  # trading day, before open
    holiday_dt = datetime(2026, 6, 26, 10, 0)  # holiday, would be in-session

    assert cal.is_market_open(open_dt) is True
    assert cal.is_market_open(early_dt) is False
    assert cal.is_market_open(holiday_dt) is False


def test_missing_file_yields_empty_set(tmp_path) -> None:
    missing = tmp_path / "nope.json"
    assert cal.load_holidays(missing) == set()


def test_holiday_name_returns_official_name_or_empty() -> None:
    assert cal.holiday_name(date(2026, 10, 2)) == "Mahatma Gandhi Jayanti"
    assert cal.holiday_name(date(2026, 10, 1)) == ""
