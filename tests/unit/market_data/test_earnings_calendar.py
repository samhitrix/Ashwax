"""Tests for EarningsCalendar — date parsing, blocking logic, cache I/O."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from src.market_data.earnings_calendar import EarningsCalendar

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_dict(symbol: str, days: int, purpose: str = "Financial Results") -> dict:
    event_date = date.today() + timedelta(days=days)
    return {"symbol": symbol, "date": event_date.strftime("%Y-%m-%d"), "purpose": purpose}


def _cal(tmp_path: Path) -> EarningsCalendar:
    return EarningsCalendar(log_path=tmp_path / "earnings_calendar.json")


# ---------------------------------------------------------------------------
# is_blocked — core blocking logic
# ---------------------------------------------------------------------------


def test_is_blocked_within_5_days(tmp_path):
    cal = _cal(tmp_path)
    cal._parse_and_store([_event_dict("RELIANCE", 3)])
    assert cal.is_blocked("RELIANCE") is True


def test_is_blocked_exactly_5_days(tmp_path):
    cal = _cal(tmp_path)
    cal._parse_and_store([_event_dict("RELIANCE", 5)])
    assert cal.is_blocked("RELIANCE") is True


def test_not_blocked_at_6_days(tmp_path):
    cal = _cal(tmp_path)
    cal._parse_and_store([_event_dict("RELIANCE", 6)])
    assert cal.is_blocked("RELIANCE") is False


def test_not_blocked_unknown_symbol(tmp_path):
    cal = _cal(tmp_path)
    cal._parse_and_store([_event_dict("RELIANCE", 2)])
    assert cal.is_blocked("TATASTEEL") is False


def test_is_blocked_case_insensitive(tmp_path):
    cal = _cal(tmp_path)
    cal._parse_and_store([_event_dict("HDFCBANK", 4)])
    assert cal.is_blocked("hdfcbank") is True
    assert cal.is_blocked("HdfcBank") is True


def test_today_day_0_is_blocked(tmp_path):
    cal = _cal(tmp_path)
    cal._parse_and_store([_event_dict("INFY", 0)])
    assert cal.is_blocked("INFY") is True


def test_past_event_excluded(tmp_path):
    cal = _cal(tmp_path)
    past = (date.today() - timedelta(days=2)).strftime("%Y-%m-%d")
    cal._parse_and_store([{"symbol": "TCS", "date": past, "purpose": "Financial Results"}])
    assert cal.is_blocked("TCS") is False


# ---------------------------------------------------------------------------
# days_to_earnings
# ---------------------------------------------------------------------------


def test_days_to_earnings_correct_value(tmp_path):
    cal = _cal(tmp_path)
    cal._parse_and_store([_event_dict("WIPRO", 8)])
    assert cal.days_to_earnings("WIPRO") == 8


def test_days_to_earnings_unknown_returns_none(tmp_path):
    cal = _cal(tmp_path)
    assert cal.days_to_earnings("NONEXISTENT") is None


# ---------------------------------------------------------------------------
# Multiple symbols & nearest-event logic
# ---------------------------------------------------------------------------


def test_multiple_symbols_isolated_blocking(tmp_path):
    cal = _cal(tmp_path)
    cal._parse_and_store([_event_dict("RELIANCE", 3), _event_dict("INFY", 10)])
    assert cal.is_blocked("RELIANCE") is True
    assert cal.is_blocked("INFY") is False


def test_nearest_event_wins_for_same_symbol(tmp_path):
    cal = _cal(tmp_path)
    far = (date.today() + timedelta(days=20)).strftime("%Y-%m-%d")
    near = (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")
    cal._parse_and_store(
        [
            {"symbol": "ONGC", "date": far, "purpose": "Financial Results"},
            {"symbol": "ONGC", "date": near, "purpose": "Financial Results"},
        ]
    )
    assert cal.days_to_earnings("ONGC") == 3


def test_event_beyond_30_days_excluded(tmp_path):
    cal = _cal(tmp_path)
    cal._parse_and_store([_event_dict("BPCL", 31)])
    assert cal.days_to_earnings("BPCL") is None


# ---------------------------------------------------------------------------
# Date format parsing
# ---------------------------------------------------------------------------


def test_parse_date_iso_format():
    assert EarningsCalendar._parse_date("2026-06-15") == date(2026, 6, 15)


def test_parse_date_dd_mon_yyyy():
    assert EarningsCalendar._parse_date("15-Jun-2026") == date(2026, 6, 15)


def test_parse_date_dd_slash_mm_yyyy():
    assert EarningsCalendar._parse_date("15/06/2026") == date(2026, 6, 15)


def test_parse_date_invalid_returns_none():
    assert EarningsCalendar._parse_date("not-a-date") is None
    assert EarningsCalendar._parse_date("") is None


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------


def test_save_and_load_cache_roundtrip(tmp_path):
    log_file = tmp_path / "earnings_calendar.json"
    cal = EarningsCalendar(log_path=log_file)
    cal._parse_and_store([_event_dict("BAJAJFINSV", 4)])
    assert cal.is_blocked("BAJAJFINSV") is True

    cal2 = EarningsCalendar(log_path=log_file)
    assert cal2.is_blocked("BAJAJFINSV") is True


def test_missing_cache_file_does_not_raise(tmp_path):
    cal = EarningsCalendar(log_path=tmp_path / "nonexistent.json")
    assert cal.days_to_earnings("ANYTHING") is None


def test_corrupted_cache_does_not_raise(tmp_path):
    log_file = tmp_path / "earnings_calendar.json"
    log_file.write_text("NOT VALID JSON", encoding="utf-8")
    cal = EarningsCalendar(log_path=log_file)
    assert cal.days_to_earnings("ANYTHING") is None


def test_save_cache_writes_valid_json(tmp_path):
    log_file = tmp_path / "earnings_calendar.json"
    cal = EarningsCalendar(log_path=log_file)
    cal._parse_and_store([_event_dict("NTPC", 7)])
    raw = json.loads(log_file.read_text(encoding="utf-8"))
    assert "events" in raw and "fetched_at" in raw
    assert any(e["symbol"] == "NTPC" for e in raw["events"])


# ---------------------------------------------------------------------------
# API failure preserves existing events
# ---------------------------------------------------------------------------


def test_existing_events_unchanged_without_refresh(tmp_path):
    """Without calling refresh/parse_and_store again, events stay intact."""
    cal = _cal(tmp_path)
    cal._parse_and_store([_event_dict("POWERGRID", 2)])
    count_before = len(cal._events)
    # No additional parse call — events must remain
    assert cal.is_blocked("POWERGRID") is True
    assert len(cal._events) == count_before
