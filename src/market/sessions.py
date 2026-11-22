"""
src/market/sessions.py — single reader of config/market_hours.json.

Every session time, order-window boundary and time-of-day schedule in the system
comes from here. There are deliberately NO built-in defaults: a missing or
invalid file raises, because a silently-wrong default is how a 15:45 "AMO opens"
assumption (Zerodha's real window opens 16:00) got baked into ten places.

Order-variety model per market (times IST):

  regular_session.open .. close   -> REGULAR
  amo.open .. midnight, midnight .. amo.close (next day) -> AMO
  anything else (close..amo.open, amo.close..open)      -> CLOSED (no order accepted)
  weekend                                               -> AMO
"""

from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_PATH = Path(__file__).parent.parent.parent / "config" / "market_hours.json"
_IST = ZoneInfo("Asia/Kolkata")

REGULAR = "REGULAR"
AMO = "AMO"
CLOSED = "CLOSED"

_EntryWindow = tuple[time, time, str, float]  # (start, end, allowed, min_llm_confidence)

_cache: dict[str, Any] | None = None


class MarketHoursConfigError(RuntimeError):
    """config/market_hours.json is missing or malformed."""


def hhmm(value: str) -> time:
    """'HH:MM' (or 'HH:MM:SS') -> time; raises ValueError on garbage."""
    return time.fromisoformat(str(value))


def minutes(t: time) -> int:
    """Minutes since midnight."""
    return t.hour * 60 + t.minute


def _load(path: Path | None = None) -> dict[str, Any]:
    global _cache
    if path is None and _cache is not None:
        return _cache
    target = path or _PATH
    try:
        data: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
        markets = data["markets"]
        for name, m in markets.items():
            hhmm(m["regular_session"]["open"])
            hhmm(m["regular_session"]["close"])
            hhmm(m["amo"]["open"])
            hhmm(m["amo"]["close"])
            _ = name
        data["schedules"]  # noqa: B018 — presence check
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MarketHoursConfigError(f"invalid/missing {target}: {exc!r}") from exc
    if path is None:
        _cache = data
    return data


def reload() -> None:
    """Drop the cache (tests / hot-reload)."""
    global _cache
    _cache = None


def market_config(market: str = "nse_equity") -> dict[str, Any]:
    """Raw block for one market, e.g. for expiry_guardian / iv_history_close."""
    try:
        return dict(_load()["markets"][market])
    except KeyError as exc:
        raise MarketHoursConfigError(f"unknown market {market!r}") from exc


def regular_open(market: str = "nse_equity") -> time:
    return hhmm(market_config(market)["regular_session"]["open"])


def regular_close(market: str = "nse_equity") -> time:
    return hhmm(market_config(market)["regular_session"]["close"])


def amo_open(market: str = "nse_equity") -> time:
    return hhmm(market_config(market)["amo"]["open"])


def amo_close(market: str = "nse_equity") -> time:
    return hhmm(market_config(market)["amo"]["close"])


def mis_flat_by(market: str = "nse_equity") -> time:
    return hhmm(market_config(market)["mis_flat_by"])


def last_entry_by(market: str = "nse_equity") -> time:
    return hhmm(market_config(market)["last_entry_by"])


def morning_open_block_minutes(market: str = "nse_equity") -> int:
    return int(market_config(market)["morning_open_block_minutes"])


def _explicit_windows(market: str) -> list[_EntryWindow]:
    out: list[_EntryWindow] = []
    for w in market_config(market).get("entry_windows", []):
        out.append(
            (
                hhmm(w["start"]),
                hhmm(w["end"]),
                str(w.get("allowed", "ALL")),
                float(w.get("min_llm_confidence", 0.0)),
            )
        )
    return out


def entry_windows(market: str = "nse_equity") -> list[_EntryWindow]:
    """Explicit in-session windows PLUS the derived pre-open / post-close SWING
    (AMO) windows, so the AMO time is defined in exactly one place."""
    wins = _explicit_windows(market)
    open_t, close_t = amo_open(market), amo_close(market)
    wins.insert(0, (time(0, 0), close_t, "SWING", 0.0))
    wins.append((open_t, time(23, 59, 59), "SWING", 0.0))
    return wins


def order_variety_at(now: datetime, market: str = "nse_equity") -> str:
    """REGULAR | AMO | CLOSED for an order placed at `now` (tz-aware or naive IST)."""
    now = now.astimezone(_IST) if now.tzinfo else now
    if now.weekday() >= 5:
        return AMO
    t = now.time().replace(second=0, microsecond=0)
    if regular_open(market) <= t <= regular_close(market):
        return REGULAR
    if t >= amo_open(market) or t < amo_close(market):
        return AMO
    return CLOSED


def in_regular_minutes(mins: int, market: str = "nse_equity") -> bool:
    """True when `mins` (minutes since IST midnight) is inside the regular session
    (inclusive of the close minute, matching order_variety_at)."""
    return minutes(regular_open(market)) <= mins <= minutes(regular_close(market))


def exit_only_start(market: str = "nse_equity") -> time:
    """Start of the EXIT_ONLY window — the last minute new intraday entries can be scanned."""
    for start, _end, allowed, _c in _explicit_windows(market):
        if allowed == "EXIT_ONLY":
            return start
    return regular_close(market)


def is_regular_session(now: datetime, market: str = "nse_equity") -> bool:
    return order_variety_at(now, market) == REGULAR


def schedule(name: str) -> Any:
    """Raw value of schedules.<name> (str, list or dict)."""
    try:
        return _load()["schedules"][name]
    except KeyError as exc:
        raise MarketHoursConfigError(f"unknown schedule {name!r}") from exc


def schedule_time(name: str) -> time:
    """schedules.<name> when it is a single 'HH:MM'."""
    return hhmm(schedule(name))


def schedule_window(name: str) -> tuple[time, time]:
    """schedules.<name> when it is {start, end}."""
    w = schedule(name)
    return hhmm(w["start"]), hhmm(w["end"])


def schedule_times(name: str) -> list[time]:
    """schedules.<name> when it is a list of 'HH:MM'."""
    return [hhmm(x) for x in schedule(name)]
