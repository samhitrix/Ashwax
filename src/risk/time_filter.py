"""
TimeFilter — CRM Gate 3.

Enforces time-of-day trading windows per IST. The windows come from
config/market_hours.json via src/market/sessions.py (the single source of
truth for all session/AMO times) — nothing is defined here. The post-close and
pre-open SWING (AMO) windows are derived from the market's amo.open/amo.close.
Returns (allowed: bool, reason: str).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.infrastructure.logger import get_logger
from src.market import sessions

_IST = ZoneInfo("Asia/Kolkata")
logger = get_logger(__name__)


class TimeFilter:
    """Determines whether new entries are allowed at a given IST time."""

    def __init__(self, market: str = "nse_equity") -> None:
        self._windows = sessions.entry_windows(market)

    def check(
        self,
        now: datetime,
        strategy_type: str = "ALL",
        llm_confidence: float = 1.0,
        confidence_bar: bool = True,
    ) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        now must be timezone-aware; converted to IST internally.
        confidence_bar=False skips only the per-window LLM-confidence minimum (market
        hours and the exit-only pre-close window still apply).
        """
        ist_time = now.astimezone(_IST).time()
        logger.debug(
            "time_filter_received ist_time=%s strategy_type=%s llm_confidence=%.2f",
            ist_time,
            strategy_type,
            llm_confidence,
        )

        def _decide() -> tuple[bool, str]:
            for start, end, allowed, min_conf in self._windows:
                if start <= ist_time < end:
                    if allowed == "EXIT_ONLY":
                        return False, f"pre-close {start}-{end} IST â€” exits only"
                    if allowed == "ORB_ONLY" and strategy_type != "ORB":
                        return False, "ORB build window â€” ORB strategies only"
                    if allowed == "SWING" and strategy_type not in ("SWING", "CNC", "DELIVERY"):
                        return False, f"window {start}-{end} IST â€” swing entries only"
                    if confidence_bar and llm_confidence < min_conf:
                        return False, (
                            f"window {start}-{end} requires LLM confidence "
                            f">= {min_conf:.0%}, got {llm_confidence:.0%}"
                        )
                    return True, f"window {start}-{end} OK"

            return False, "outside market hours"

        allowed_result, reason = _decide()
        logger.debug("time_filter_found allowed=%s reason=%s", allowed_result, reason)
        return allowed_result, reason

    def check_swing(self, now: datetime) -> tuple[bool, str]:
        """Swing/delivery entries are decided on DAILY candles, so the clock carries no
        signal: allowed whenever the exchange accepts the order (regular session or the
        AMO windows), with no time-of-day confidence bar and no pre-close exit-only block.
        2026-10-06 (operator): the 14:30-15:15 85% bar is an intraday idea and only ever
        hit swing (CPPLUS refused at 65%). Only the 15:30-AMO-open gap is refused."""
        ist_time = now.astimezone(_IST).time()
        for start, end, _allowed, _min_conf in self._windows:
            if start <= ist_time < end:
                return True, f"swing: window {start}-{end} accepts orders"
        return False, "outside market hours (no regular session or AMO window)"
