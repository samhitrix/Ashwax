"""
EarningsCalendar â€" fetches NSE board meeting / result dates and gates entries.

Schedule: daily 07:00 IST (called by APScheduler in orchestrator).

Rules:
  - Earnings within 5 trading days â†' REJECT swing entries (binary risk)
  - Open position reports earnings within 5 days â†' flag for review, tighten SL

Data source: NSE corporate calendar API (public, unauthenticated).
Fallback: stale cached JSON if API unreachable.

Writes logs/earnings_calendar.json for CRM and supervisor to read.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.infrastructure.logger import get_logger

_IST = ZoneInfo("Asia/Kolkata")
_LOG_PATH = Path("logs/earnings_calendar.json")
_BLOCK_DAYS = 5

logger = get_logger(__name__)


@dataclass
class EarningsEvent:
    symbol: str
    event_date: str  # ISO date YYYY-MM-DD
    event_type: str  # "BOARD_MEETING" | "RESULT"
    days_away: int


class EarningsCalendar:
    """
    Maintains upcoming earnings events for the trading universe.

    is_blocked(symbol)       â†' bool   (within 5 trading days of result)
    days_to_earnings(symbol) â†' int | None
    """

    def __init__(self, log_path: Path = _LOG_PATH) -> None:
        self._log_path = log_path
        self._events: dict[str, EarningsEvent] = {}
        self._last_fetch: datetime | None = None
        self._load_cache()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_blocked(self, symbol: str) -> bool:
        ev = self._events.get(symbol.upper())
        return ev is not None and ev.days_away <= _BLOCK_DAYS

    def days_to_earnings(self, symbol: str) -> int | None:
        ev = self._events.get(symbol.upper())
        return ev.days_away if ev else None

    async def refresh(self, session: Any) -> None:
        """
        Fetch NSE earnings calendar.
        session: aiohttp.ClientSession (type-hinted loosely to avoid hard dep).
        """
        import aiohttp  # deferred import

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/",
        }
        try:
            # Step 1: establish NSE session cookie
            async with session.get(
                "https://www.nseindia.com",
                headers={**headers, "Accept": "text/html,application/xhtml+xml"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as _:
                pass  # cookies stored in session's cookie jar

            # Step 2: call API with session cookies
            async with session.get(
                "https://www.nseindia.com/api/event-calendar",
                headers={**headers, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning("NSE calendar API returned %d", resp.status)
                    return
                data = await resp.json(content_type=None)
        except Exception as exc:
            logger.warning("NSE calendar fetch failed: %s - using cache", exc)
            return

        if not isinstance(data, list):
            logger.warning("NSE calendar unexpected response type=%s", type(data).__name__)
            return

        logger.info("NSE calendar raw events fetched: %d", len(data))
        self._parse_and_store(data)

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_date(raw: str) -> date | None:
        """Parse NSE date — handles ISO (YYYY-MM-DD) and DD-Mon-YYYY formats."""
        raw = raw.strip()[:11]
        for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    def _parse_and_store(self, data: list[dict[str, Any]]) -> None:
        if data:
            logger.info("NSE calendar sample item: %s", str(data[0])[:200])
        today = date.today()
        events: dict[str, EarningsEvent] = {}

        for item in data:
            try:
                symbol = item.get("symbol", "").upper()
                if not symbol:
                    continue
                raw_date = str(item.get("date") or item.get("bm_date") or "")
                event_date = self._parse_date(raw_date)
                if event_date is None:
                    continue
                event_type = (
                    "RESULT" if "result" in item.get("purpose", "").lower() else "BOARD_MEETING"
                )
                days_away = (event_date - today).days
                if 0 <= days_away <= 30:
                    existing = events.get(symbol)
                    if existing is None or days_away < existing.days_away:
                        events[symbol] = EarningsEvent(
                            symbol=symbol,
                            event_date=event_date.isoformat(),
                            event_type=event_type,
                            days_away=days_away,
                        )
            except (KeyError, ValueError):
                continue

        self._events = events
        self._last_fetch = datetime.now(_IST)
        self._save_cache()

        blocked = [s for s, e in events.items() if e.days_away <= _BLOCK_DAYS]
        logger.info(
            "Earnings calendar refreshed: %d events, %d blocked (within %d days)",
            len(events),
            len(blocked),
            _BLOCK_DAYS,
        )

    def _save_cache(self) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": self._last_fetch.isoformat() if self._last_fetch else "",
            "events": [asdict(e) for e in self._events.values()],
        }
        tmp = self._log_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._log_path)

    def _load_cache(self) -> None:
        try:
            data = json.loads(self._log_path.read_text(encoding="utf-8"))
            today = date.today()
            for item in data.get("events", []):
                ev = EarningsEvent(**item)
                ev.days_away = (date.fromisoformat(ev.event_date) - today).days
                if ev.days_away >= 0:
                    self._events[ev.symbol] = ev
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            pass
