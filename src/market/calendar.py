"""
src/market/calendar.py — NSE trading-calendar utility (single source of truth).

Loads the official holiday list from ``config/holidays/nse_holidays.json`` and
answers the only questions the engine cares about:

  * ``is_holiday(d)``      — is this date a full market closure?
  * ``is_trading_day(d)``  — Mon–Fri AND not a holiday.
  * ``is_market_open(now)``— a trading day AND within the regular session
    (config/market_hours.json).

Every "should I trade now" gate in the orchestrator must route through here so
that weekday holidays (e.g. Muharram) actually stop trading, instead of the old
bare ``now.weekday() < 5`` check that ignored the calendar entirely.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from src.market import sessions

_BASE = Path(__file__).parent.parent.parent
_HOLIDAYS_PATH = _BASE / "config" / "holidays" / "nse_holidays.json"

_cache: set[date] | None = None


def load_holidays(path: Path | None = None) -> set[date]:
    """Parse the holiday JSON into a set of ``date`` objects (cached).

    The on-disk schema is ``{"holidays": [{"date": "YYYY-MM-DD", "name": ...}]}``.
    A missing file yields an empty set; a malformed/empty ``holidays`` list raises
    so the misconfiguration is visible rather than silently disabling the gate.
    """
    global _cache
    if path is None and _cache is not None:
        return _cache

    target = path or _HOLIDAYS_PATH
    if not target.exists():
        result: set[date] = set()
    else:
        data = json.loads(target.read_text(encoding="utf-8"))
        result = {date.fromisoformat(o["date"]) for o in data.get("holidays", [])}

    if path is None:
        _cache = result
    return result


def reload() -> set[date]:
    """Clear the cache and re-read the default holiday file."""
    global _cache
    _cache = None
    return load_holidays()


def is_holiday(d: date) -> bool:
    """True if ``d`` is a full NSE market closure."""
    return d in load_holidays()


def holiday_name(d: date) -> str:
    """Official name of the closure on ``d`` ('' when ``d`` is not a listed holiday)."""
    if not _HOLIDAYS_PATH.exists():
        return ""
    data = json.loads(_HOLIDAYS_PATH.read_text(encoding="utf-8"))
    for o in data.get("holidays", []):
        if o["date"] == d.isoformat():
            return str(o.get("name", ""))
    return ""


def is_trading_day(d: date) -> bool:
    """True if ``d`` is a weekday (Mon–Fri) and not a market holiday."""
    return d.weekday() < 5 and not is_holiday(d)


def is_market_open(now: datetime) -> bool:
    """True if ``now`` falls on a trading day within the regular session (config/market_hours.json)."""
    if not is_trading_day(now.date()):
        return False
    mins = now.hour * 60 + now.minute
    return (
        sessions.minutes(sessions.regular_open())
        <= mins
        <= sessions.minutes(sessions.regular_close())
    )
