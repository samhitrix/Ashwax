"""Every notification the notifier sends is ALSO put on the dashboard Alerts tab (2026-09-24).

Before: only supervisor-raised criticals ever reached the Alerts tab, so Telegram was full of
proposals / refusals / fills while the tab read "No System Alerts".
"""

from __future__ import annotations

import pytest
from src.infrastructure import operator_alerts as oa
from src.infrastructure.alert_system import AlertManager, AlertTier
from src.notifications.config import NotificationConfig, ThrottleConfig, TransportConfig
from src.notifications.events import EventKind, NotificationEvent
from src.notifications.notifier import Notifier


class _Transport:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> tuple[bool, str]:
        self.sent.append(text)
        return True, ""

    async def aclose(self) -> None:
        pass


def _cfg() -> NotificationConfig:
    return NotificationConfig(
        enabled=True,
        include_paper=True,
        include_live=True,
        bot_token="T",
        chat_id="C",
        events=None,
        throttle=ThrottleConfig(startup_quiet_seconds=0.0),
        transport=TransportConfig(),
        queue_max_size=50,
    )


@pytest.fixture(autouse=True)
def _mgr():
    oa._reset_for_testing()
    m = AlertManager()
    oa.configure(m)
    yield m
    oa._reset_for_testing()


async def _send(ev: NotificationEvent) -> _Transport:
    tr = _Transport()
    await Notifier(_cfg(), tr)._handle(ev)
    return tr


@pytest.mark.asyncio
async def test_routine_info_messages_stay_off_the_alerts_tab(_mgr) -> None:
    """Idea proposed / trade opened etc. go to Telegram only (2026-09-28: INFO cluttered the tab)."""
    ev = NotificationEvent(
        EventKind.LLM_FUNNEL,
        is_paper=True,
        symbol="AXISBANK",
        payload={
            "agent": "llm_intraday_agent",
            "stage": "idea_published",
            "outcome": "proposed",
            "reason": "validated",
            "detail": "SHORT entry=1180",
        },
    )
    tr = await _send(ev)
    assert tr.sent  # Telegram still got it
    assert _mgr.active() == []


@pytest.mark.asyncio
async def test_only_failures_reach_the_alerts_tab_and_never_as_info(_mgr) -> None:
    base = {"agent": "llm_intraday_agent", "stage": "execution", "reason": "x", "detail": ""}
    await _send(
        NotificationEvent(
            EventKind.LLM_FUNNEL, is_paper=True, symbol="A", payload={**base, "outcome": "refused"}
        )
    )
    await _send(
        NotificationEvent(
            EventKind.LLM_FUNNEL, is_paper=True, symbol="B", payload={**base, "outcome": "placed"}
        )
    )
    await _send(NotificationEvent(EventKind.ENTRY_REJECTED, is_paper=True, symbol="C"))
    active = _mgr.active()
    assert len(active) == 2  # refused + rejected; the successful 'placed' is not there
    assert all(a.tier == AlertTier.ALERT for a in active)
    assert not any(a.source.split(":")[-1] == "B" for a in active)


@pytest.mark.asyncio
async def test_alerts_already_raised_on_the_dashboard_are_not_duplicated(_mgr) -> None:
    oa.raise_alert("provider anthropic FAILED", source="llm_failover:swing:anthropic")
    # raise_alert pushed via the notifier path too; the mirror must skip it (payload flag)
    ev = NotificationEvent(
        EventKind.LLM_HEALTH,
        is_paper=True,
        payload={
            "trigger": "LLM swing",
            "overall": "FAIL",
            "failures": ["x"],
            "_dashboard_done": True,
        },
    )
    await _send(ev)
    assert len(_mgr.active()) == 1


@pytest.mark.asyncio
async def test_mirror_is_bounded_so_the_alerts_file_cannot_grow_forever(_mgr, monkeypatch) -> None:
    monkeypatch.setattr(oa, "MIRROR_CAP", 5)
    for i in range(12):
        await _send(NotificationEvent(EventKind.ENTRY_REJECTED, is_paper=True, symbol=f"S{i}"))
    assert len(_mgr.active()) == 5
    assert "S11" in _mgr.active()[-1].message  # newest kept, oldest auto-resolved


@pytest.mark.asyncio
async def test_a_broken_alert_manager_never_blocks_telegram(_mgr) -> None:
    class _Boom:
        def raise_alert(self, *a, **k):
            raise RuntimeError("manager down")

        def resolve(self, *a, **k):
            raise RuntimeError("manager down")

    oa.configure(_Boom())
    tr = await _send(NotificationEvent(EventKind.ENTRY_FILL, is_paper=True, symbol="X"))
    assert tr.sent  # Telegram delivery unaffected
