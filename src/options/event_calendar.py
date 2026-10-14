"""Seller-desk event calendar & dynamic blackouts (options_spec.md §16).

Layer 1: scheduled events pre-loaded via ``schedule_event()`` (RBI/Fed/Budget/
rebalance dates — manual entry once per year per the spec). Earnings dates are
NOT duplicated here — ``selling_blocked()`` reuses the existing
``src.market_data.earnings_calendar.EarningsCalendar`` directly. Layer 2:
dynamic MACRO blackouts raised by the news engine's classifier (``HALT``
action + ``MACRO`` scope), auto-expiring.

Both layers write to the same ``options_event_calendar`` table (migration
052) so a single ``selling_blocked()`` call answers "is selling blocked right
now" regardless of which layer raised it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.db.repositories.options import OptionsRepository
    from src.market_data.earnings_calendar import EarningsCalendar
    from src.news.classifier import NewsClassification

_SELLING_BLACKOUT_DAYS_MAJOR = 7
_SELLING_BLACKOUT_DAYS_IC = 21
_EARNINGS_BLACKOUT_DAYS = 5
_DYNAMIC_BLACKOUT_HOURS = 72


@dataclass
class BlackoutStatus:
    blocked: bool
    reason: str


class EventCalendar:
    def __init__(self, repo: OptionsRepository, earnings: EarningsCalendar | None = None) -> None:
        self._repo = repo
        self._earnings = earnings

    async def schedule_event(
        self,
        event_type: str,
        event_date: date,
        *,
        underlying: str | None = None,
        source: str = "manual",
    ) -> None:
        """Layer 1. Stores the event_date + the 7-day-major blackout window;
        the wider 21-day Iron Condor window is derived from event_date at
        query time in ``selling_blocked()``, not stored twice."""
        blackout_from = datetime.combine(
            event_date - timedelta(days=_SELLING_BLACKOUT_DAYS_MAJOR), datetime.min.time()
        )
        blackout_until = datetime.combine(event_date, datetime.min.time())
        await self._repo.insert_event_blackout(
            event_type,
            event_date=event_date,
            underlying=underlying,
            layer="scheduled",
            blackout_from=blackout_from,
            blackout_until=blackout_until,
            source=source,
        )

    async def raise_dynamic_blackout(
        self,
        classification: NewsClassification,
        *,
        now: datetime,
        underlying: str | None = None,
    ) -> bool:
        """Layer 2 auto-trigger (spec §16). Returns True iff a blackout was raised."""
        if classification.trading_action != "HALT" or classification.scope != "MACRO":
            return False
        await self._repo.insert_event_blackout(
            "macro_news",
            event_date=None,
            underlying=underlying,
            layer="dynamic",
            blackout_from=now,
            blackout_until=now + timedelta(hours=_DYNAMIC_BLACKOUT_HOURS),
            source="news_engine",
        )
        return True

    async def selling_blocked(
        self, underlying: str, now: datetime, *, is_iron_condor: bool = False
    ) -> BlackoutStatus:
        """True if premium selling is blocked for `underlying` right now."""
        if self._earnings is not None and self._earnings.is_blocked(underlying):
            days = self._earnings.days_to_earnings(underlying)
            if days is not None and days <= _EARNINGS_BLACKOUT_DAYS:
                return BlackoutStatus(True, f"earnings within {days}d")

        rows = await self._repo.active_blackouts(underlying, now)
        window_days = _SELLING_BLACKOUT_DAYS_IC if is_iron_condor else _SELLING_BLACKOUT_DAYS_MAJOR
        for row in rows:
            if row.get("layer") == "dynamic":
                return BlackoutStatus(True, f"{row.get('event_type', 'macro_news')} (dynamic)")
            event_date = row.get("event_date")
            if not isinstance(event_date, date):
                continue
            effective_from = event_date - timedelta(days=window_days)
            if effective_from <= now.date() <= event_date:
                return BlackoutStatus(
                    True,
                    f"{row.get('event_type', 'scheduled')} in {(event_date - now.date()).days}d",
                )
        return BlackoutStatus(False, "")
