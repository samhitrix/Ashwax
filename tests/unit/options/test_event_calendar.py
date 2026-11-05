"""Tests for the seller-desk event calendar (options_spec.md §16)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.options.event_calendar import EventCalendar

_IST = ZoneInfo("Asia/Kolkata")


class _FakeRepo:
    def __init__(self, active_rows: list[dict] | None = None) -> None:
        self.inserted: list[dict] = []
        self._active_rows = active_rows or []

    async def insert_event_blackout(self, event_type: str, **kwargs: object) -> None:
        self.inserted.append({"event_type": event_type, **kwargs})

    async def active_blackouts(self, underlying: str, now: datetime) -> list[dict]:
        return self._active_rows


class _FakeEarnings:
    def __init__(self, blocked: bool, days: int | None) -> None:
        self._blocked = blocked
        self._days = days

    def is_blocked(self, symbol: str) -> bool:
        return self._blocked

    def days_to_earnings(self, symbol: str) -> int | None:
        return self._days


async def test_schedule_event_inserts_correct_window() -> None:
    repo = _FakeRepo()
    cal = EventCalendar(repo)
    await cal.schedule_event("rbi_policy", date(2026, 8, 12))
    assert len(repo.inserted) == 1
    row = repo.inserted[0]
    assert row["event_date"] == date(2026, 8, 12)
    assert row["blackout_from"].date() == date(2026, 8, 5)  # 7 days before
    assert row["blackout_until"].date() == date(2026, 8, 12)
    assert row["layer"] == "scheduled"


async def test_raise_dynamic_blackout_ignores_non_halt() -> None:
    repo = _FakeRepo()
    cal = EventCalendar(repo)
    classification = SimpleNamespace(trading_action="MONITOR", scope="MACRO")
    raised = await cal.raise_dynamic_blackout(classification, now=datetime.now(_IST))
    assert raised is False
    assert repo.inserted == []


async def test_raise_dynamic_blackout_ignores_non_macro() -> None:
    repo = _FakeRepo()
    cal = EventCalendar(repo)
    classification = SimpleNamespace(trading_action="HALT", scope="COMPANY")
    raised = await cal.raise_dynamic_blackout(classification, now=datetime.now(_IST))
    assert raised is False


async def test_raise_dynamic_blackout_halt_macro_inserts_72h_window() -> None:
    repo = _FakeRepo()
    cal = EventCalendar(repo)
    now = datetime.now(_IST)
    classification = SimpleNamespace(trading_action="HALT", scope="MACRO")
    raised = await cal.raise_dynamic_blackout(classification, now=now)
    assert raised is True
    row = repo.inserted[0]
    assert row["layer"] == "dynamic"
    assert row["blackout_until"] - row["blackout_from"] == timedelta(hours=72)


async def test_selling_blocked_by_earnings() -> None:
    repo = _FakeRepo()
    cal = EventCalendar(repo, earnings=_FakeEarnings(blocked=True, days=3))
    status = await cal.selling_blocked("RELIANCE", datetime.now(_IST))
    assert status.blocked is True
    assert "earnings" in status.reason


async def test_selling_blocked_by_dynamic_row() -> None:
    repo = _FakeRepo(active_rows=[{"layer": "dynamic", "event_type": "macro_news"}])
    cal = EventCalendar(repo)
    status = await cal.selling_blocked("NIFTY", datetime.now(_IST))
    assert status.blocked is True


async def test_selling_blocked_scheduled_within_major_window() -> None:
    now = datetime(2026, 8, 6, tzinfo=_IST)  # 6 days before event
    repo = _FakeRepo(
        active_rows=[
            {"layer": "scheduled", "event_type": "rbi_policy", "event_date": date(2026, 8, 12)}
        ]
    )
    cal = EventCalendar(repo)
    status = await cal.selling_blocked("NIFTY", now)
    assert status.blocked is True


async def test_selling_blocked_scheduled_outside_major_but_inside_ic_window() -> None:
    now = datetime(2026, 7, 29, tzinfo=_IST)  # 14 days before event: outside 7d, inside 21d
    repo = _FakeRepo(
        active_rows=[
            {"layer": "scheduled", "event_type": "rbi_policy", "event_date": date(2026, 8, 12)}
        ]
    )
    cal = EventCalendar(repo)
    not_ic = await cal.selling_blocked("NIFTY", now, is_iron_condor=False)
    assert not_ic.blocked is False
    ic = await cal.selling_blocked("NIFTY", now, is_iron_condor=True)
    assert ic.blocked is True


async def test_selling_not_blocked_when_nothing_applies() -> None:
    repo = _FakeRepo(active_rows=[])
    cal = EventCalendar(repo, earnings=_FakeEarnings(blocked=False, days=None))
    status = await cal.selling_blocked("NIFTY", datetime.now(_IST))
    assert status.blocked is False
