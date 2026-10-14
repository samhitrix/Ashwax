"""Notifier tests: fake transport, no network. Covers the hard constraint
that submit()/notify() can never raise or block the caller."""

from __future__ import annotations

import asyncio

from src.notifications.config import NotificationConfig, ThrottleConfig, TransportConfig
from src.notifications.events import EventKind, NotificationEvent
from src.notifications.notifier import Notifier, get_notifier, init_notifier, notify


class _FakeTransport:
    def __init__(self, ok: bool = True, error: str = "") -> None:
        self.ok = ok
        self.error = error
        self.sent: list[str] = []
        self.raise_on_send = False

    async def send(self, text: str) -> tuple[bool, str]:
        if self.raise_on_send:
            raise RuntimeError("transport exploded")
        self.sent.append(text)
        return self.ok, self.error

    async def aclose(self) -> None:
        pass


def _cfg(**overrides: object) -> NotificationConfig:
    base = dict(
        enabled=True,
        include_paper=True,
        include_live=True,
        bot_token="T",
        chat_id="C",
        events=None,
        throttle=ThrottleConfig(startup_quiet_seconds=0.0),
        transport=TransportConfig(),
        queue_max_size=10,
    )
    base.update(overrides)
    return NotificationConfig(**base)  # type: ignore[arg-type]


def test_submit_is_synchronous_and_returns_immediately() -> None:
    n = Notifier(_cfg(), _FakeTransport())
    ev = NotificationEvent(EventKind.ENTRY_FILL, is_paper=True, symbol="X")
    n.submit(ev)  # must not raise, must not require an event loop
    assert n._queue.qsize() == 1


def test_submit_never_raises_when_queue_full() -> None:
    n = Notifier(_cfg(queue_max_size=1), _FakeTransport())
    n.submit(NotificationEvent(EventKind.ENTRY_FILL, is_paper=True, symbol="A"))
    n.submit(NotificationEvent(EventKind.ENTRY_FILL, is_paper=True, symbol="B"))  # queue full
    assert n._queue.qsize() == 1  # second one silently dropped, no raise


def test_notify_never_raises_when_uninitialised() -> None:
    import src.notifications.notifier as mod

    saved = mod._NOTIFIER
    mod._NOTIFIER = None
    try:
        notify(EventKind.ENTRY_FILL, is_paper=True, symbol="X")  # must not raise
    finally:
        mod._NOTIFIER = saved


def test_disabled_config_makes_notify_a_noop() -> None:
    # init_notifier reads config via load_notification_config(); simplest robust
    # check is via a Notifier constructed with enabled=False and driven directly.
    fake = _FakeTransport()
    disabled_notifier = Notifier(_cfg(enabled=False), fake)
    disabled_notifier.submit(NotificationEvent(EventKind.ENTRY_FILL, is_paper=True, symbol="X"))
    assert disabled_notifier._queue.qsize() == 0


async def test_transport_exception_does_not_stop_worker() -> None:
    transport = _FakeTransport()
    transport.raise_on_send = True
    n = Notifier(_cfg(), transport)
    n.submit(NotificationEvent(EventKind.ENTRY_FILL, is_paper=True, symbol="X", dedupe_key="e:1"))
    stop = asyncio.Event()
    task = asyncio.create_task(n.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    # Worker survived the transport exception rather than crashing the task.
    assert task.done() and task.exception() is None


async def test_worker_sends_via_transport_and_drains() -> None:
    transport = _FakeTransport(ok=True)
    n = Notifier(_cfg(), transport)
    n.submit(
        NotificationEvent(
            EventKind.ENTRY_FILL,
            is_paper=True,
            agent_trade_id="T1",
            symbol="RELIANCE",
            dedupe_key="entry:T1",
            payload={"direction": "BUY", "qty": 1, "fill": 100},
        )
    )
    stop = asyncio.Event()
    task = asyncio.create_task(n.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert len(transport.sent) == 1
    assert "RELIANCE" in transport.sent[0]


async def test_worker_drains_and_exits_on_stop() -> None:
    n = Notifier(_cfg(), _FakeTransport())
    stop = asyncio.Event()
    task = asyncio.create_task(n.run(stop))
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()


async def test_aclose_drains_pending_and_closes_transport() -> None:
    transport = _FakeTransport(ok=True)
    n = Notifier(_cfg(shutdown_drain_seconds=1.0), transport)
    n.submit(
        NotificationEvent(
            EventKind.EXIT, is_paper=True, agent_trade_id="T1", symbol="X", dedupe_key="exit:T1"
        )
    )
    await n.aclose()
    assert len(transport.sent) == 1


def test_init_notifier_returns_none_without_credentials(monkeypatch) -> None:
    import src.notifications.config as cfgmod

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    cfgmod.reset_config_cache()
    try:
        result = init_notifier(None)
        assert result is None
        assert get_notifier() is None
    finally:
        cfgmod.reset_config_cache()


async def test_entry_fill_is_not_swallowed_by_the_startup_quiet_window() -> None:
    """2026-10-12: an options entry filled 39s after a restart and its Telegram alert was dropped by the
    60s boot-quiet window. ENTRY_FILL and EXIT must always get through; other noise is still suppressed."""
    transport = _FakeTransport(ok=True)
    n = Notifier(_cfg(throttle=ThrottleConfig(startup_quiet_seconds=60.0)), transport)
    n.submit(
        NotificationEvent(
            EventKind.ENTRY_FILL,
            is_paper=True,
            agent_trade_id="T1",
            symbol="NIFTY",
            dedupe_key="entry:T1",
            payload={"direction": "x", "qty": 3, "fill": 15308},
        )
    )
    n.submit(NotificationEvent(EventKind.POSITION_UPDATE, is_paper=True, symbol="NIFTY"))
    stop = asyncio.Event()
    task = asyncio.create_task(n.run(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert len(transport.sent) == 1 and "NIFTY" in transport.sent[0]  # the entry, not the update
