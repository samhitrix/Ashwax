"""The notifier core: a process-wide singleton exposing one sync hook-site
function, `notify()`, backed by an asyncio.Queue drained by a background
worker task.

Hard constraint (see plan): a notification must never be able to fail, delay,
or block a trade. This is enforced structurally, not by discipline:
  - notify() only stamps the event and does a non-blocking queue put. It does
    no I/O, awaits nothing, and is wrapped in a bare except so it truly cannot
    raise into a caller on the trade path.
  - All I/O (Telegram HTTP, DB persistence) happens exclusively inside run(),
    the worker coroutine, which executes as its own asyncio task.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from src.notifications.config import NotificationConfig, load_notification_config
from src.notifications.events import EventKind, NotificationEvent, Segment
from src.notifications.formatter import format_event
from src.notifications.telegram import TelegramTransport
from src.notifications.throttle import Decision, Throttler

if TYPE_CHECKING:
    from src.db.repositories.notifications import NotificationRepository

_LOG = logging.getLogger(__name__)  # "src.notifications.notifier" -> notifications.log


class Notifier:
    """One instance per orchestrator process. Construct via init_notifier()."""

    def __init__(
        self,
        cfg: NotificationConfig,
        transport: TelegramTransport,
        repo: NotificationRepository | None = None,
    ) -> None:
        self._cfg = cfg
        self._transport = transport
        self._repo = repo
        self._queue: asyncio.Queue[NotificationEvent] = asyncio.Queue(maxsize=cfg.queue_max_size)
        self._throttler = Throttler(cfg.throttle)
        self._dropped_since_last_send = 0
        self._consecutive_failures = 0
        self._alert_manager: Any = None  # wired lazily by orchestrator, best-effort

    def set_alert_manager(self, alert_manager: Any) -> None:
        """Optional: after N consecutive Telegram failures, raise a WARNING
        through the existing AlertManager so the outage itself is visible on
        the dashboard. Best-effort — never required for notify() to work."""
        self._alert_manager = alert_manager

    # -- hook-site API ---------------------------------------------------------

    def submit(self, ev: NotificationEvent) -> None:
        """SYNC. Never raises, never blocks, never touches the network.
        Safe to call from any sync or async context on the trade path."""
        try:
            if not self._cfg.enabled or not self._cfg.mode_enabled(ev.is_paper):
                return
            if not self._cfg.event_enabled(ev.config_key):
                return
            stamped = NotificationEvent(
                kind=ev.kind,
                is_paper=ev.is_paper,
                agent_trade_id=ev.agent_trade_id,
                symbol=ev.symbol,
                segment=ev.segment,
                payload=ev.payload,
                ts=time.monotonic(),
                dedupe_key=ev.dedupe_key,
            )
            self._queue.put_nowait(stamped)
        except asyncio.QueueFull:
            self._dropped_since_last_send += 1
            _LOG.warning("notification_queue_full kind=%s dropped", ev.kind)
        except Exception as exc:
            # Must never propagate into the trade path.
            _LOG.warning("notification_submit_failed err=%s", exc)

    # -- worker ------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """The worker task. Drains the queue, applies throttling, sends via
        Telegram, persists an audit row. Wrapped by the caller in its own
        restart-on-crash supervisor loop (asyncio.gather uses
        return_exceptions=True, so a dead task here is otherwise silent)."""
        while not stop.is_set():
            try:
                ev = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOG.error("notifier_queue_get_failed err=%s", exc)
                await asyncio.sleep(1.0)
                continue

            try:
                await self._handle(ev)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOG.error("notifier_handle_failed kind=%s err=%s", ev.kind, exc)

    @staticmethod
    def _mirror_to_alerts_tab(ev: NotificationEvent, text: str) -> None:
        """Failures/rejections that go to Telegram also go on the dashboard Alerts tab as ALERT tier.
        Routine INFO messages (idea proposed, trade opened, exits...) stay Telegram-only: the Alerts
        tab is for things that need attention (2026-09-28). Independent of the Telegram send
        result, so the tab is complete even when Telegram is down."""
        try:
            from src.infrastructure import operator_alerts
            from src.strategies.llm_intraday.funnel import FAILURE_OUTCOMES

            failed = (
                ev.kind in (EventKind.ENTRY_REJECTED, EventKind.STRATEGY_ERROR)
                or (ev.kind == EventKind.LLM_HEALTH and ev.get("overall") not in (None, "OK"))
                or (ev.kind == EventKind.LLM_FUNNEL and ev.get("outcome") in FAILURE_OUTCOMES)
            )
            if not failed:
                return
            operator_alerts.mirror_notification(
                text,
                source=f"telegram:{ev.kind.value}:{ev.symbol or ev.agent_trade_id or '-'}",
                tier="ALERT",
            )
        except Exception:  # noqa: BLE001
            pass

    async def _handle(self, ev: NotificationEvent) -> None:
        # The quiet window drops boot-time reseed/late-poll noise. EXIT and ENTRY_FILL are real trade events
        # that must always reach the operator (2026-10-12: an options entry filled 39s after a restart and
        # its alert was silently swallowed here); duplicates are still blocked by the dedupe key.
        if self._throttler.in_startup_quiet_window() and ev.kind not in (
            EventKind.EXIT,
            EventKind.ENTRY_FILL,
            EventKind.PARTIAL_BOOK,
        ):
            return

        decision = self._throttler.admit(ev)
        if decision == Decision.SUPPRESS:
            return
        if decision == Decision.COALESCE:
            # Latest-wins: park nothing extra — the next admissible modify for
            # this trade will naturally carry the newest new_sl. We still count
            # it as "not yet sent" so it can surface in the next position update.
            return

        if ev.kind == EventKind.MODIFY_SL:
            trails_since = self._throttler.suppressed_count_for_trade(ev.agent_trade_id)
            if trails_since:
                ev = NotificationEvent(
                    kind=ev.kind,
                    is_paper=ev.is_paper,
                    agent_trade_id=ev.agent_trade_id,
                    symbol=ev.symbol,
                    segment=ev.segment,
                    payload={**ev.payload, "trails_since_update": trails_since},
                    ts=ev.ts,
                    dedupe_key=ev.dedupe_key,
                )

        wait_s = self._throttler.take_token()
        if wait_s > 0:
            await asyncio.sleep(min(wait_s, 30.0))
            self._throttler.take_token()  # consume now that we've waited

        if self._dropped_since_last_send:
            ev = NotificationEvent(
                kind=ev.kind,
                is_paper=ev.is_paper,
                agent_trade_id=ev.agent_trade_id,
                symbol=ev.symbol,
                segment=ev.segment,
                payload={**ev.payload, "dropped_count": self._dropped_since_last_send},
                ts=ev.ts,
                dedupe_key=ev.dedupe_key,
            )

        text = format_event(ev)
        if not ev.payload.get("_dashboard_done"):
            self._mirror_to_alerts_tab(ev, text)
        ok, error = await self._transport.send(text)

        if ok:
            self._dropped_since_last_send = 0
            self._consecutive_failures = 0
            self._throttler.note_sent(ev)
            if ev.kind == EventKind.EXIT:
                self._throttler.forget_trade(ev.agent_trade_id)
        else:
            self._consecutive_failures += 1
            _LOG.warning("telegram_send_failed kind=%s err=%s", ev.kind, error)
            self._maybe_raise_outage_alert()

        if self._repo is not None:
            try:
                await self._repo.record(
                    kind=ev.kind.value,
                    body=text,
                    is_paper=ev.is_paper,
                    agent_trade_id=ev.agent_trade_id or None,
                    symbol=ev.symbol or None,
                    dedupe_key=ev.dedupe_key,
                    status="SENT" if ok else "FAILED",
                    error=error or None,
                )
            except Exception as exc:
                _LOG.warning("notification_persist_failed err=%s", exc)

    def _maybe_raise_outage_alert(self) -> None:
        threshold = self._cfg.transport.alert_after_consecutive_failures
        if self._alert_manager is None or threshold <= 0:
            return
        if self._consecutive_failures != threshold:
            return  # raise once per outage, not on every subsequent failure
        try:
            from src.infrastructure.alert_system import AlertTier

            self._alert_manager.raise_alert(
                AlertTier.WARNING,
                f"Telegram notifications failing ({self._consecutive_failures} in a row) — "
                "trades are executing normally, only phone alerts are affected.",
                source="notifications",
            )
        except Exception as exc:
            _LOG.warning("notification_outage_alert_failed err=%s", exc)

    async def aclose(self) -> None:
        """Drain briefly so the last exit/critical message actually reaches
        Telegram before process shutdown, then close the HTTP session."""
        deadline = time.monotonic() + self._cfg.shutdown_drain_seconds
        while not self._queue.empty() and time.monotonic() < deadline:
            try:
                ev = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                await self._handle(ev)
            except Exception as exc:
                _LOG.warning("notifier_shutdown_drain_failed err=%s", exc)
        await self._transport.aclose()


_NOTIFIER: Notifier | None = None


def init_notifier(repo: NotificationRepository | None = None) -> Notifier | None:
    """Call once at orchestrator boot. Returns None (and logs once) when
    disabled or credentials are absent — every downstream notify() call is
    then a guaranteed no-op, so callers never need to check for this."""
    global _NOTIFIER
    cfg = load_notification_config()
    if not cfg.enabled:
        _LOG.info("notifications_disabled reason=config_disabled")
        _NOTIFIER = None
        return None
    if not cfg.credentials_present:
        _LOG.info("notifications_disabled reason=no_credentials")
        _NOTIFIER = None
        return None
    transport = TelegramTransport(cfg.bot_token, cfg.chat_id, cfg.transport)
    _NOTIFIER = Notifier(cfg, transport, repo)
    _LOG.info("notifications_enabled paper=%s live=%s", cfg.include_paper, cfg.include_live)
    return _NOTIFIER


async def already_sent(dedupe_key: str) -> bool:
    """True if a SENT notification with this dedupe_key is already persisted (survives restarts)."""
    repo = _NOTIFIER._repo if _NOTIFIER is not None else None
    if repo is None:
        return False
    try:
        return bool(await repo.already_sent(dedupe_key))
    except Exception as exc:
        _LOG.warning("notification_already_sent_check_failed err=%s", exc)
        return False


def get_notifier() -> Notifier | None:
    return _NOTIFIER


def notify(
    kind: EventKind,
    *,
    is_paper: bool,
    agent_trade_id: str = "",
    symbol: str = "",
    segment: Segment | str = "",
    dedupe_key: str | None = None,
    **payload: Any,
) -> None:
    """THE hook-site API. Sync, one line, cannot raise, cannot block. No-op
    when the notifier is absent (disabled / no credentials).

    segment: Segment.INTRADAY / SWING / OPTIONS — user-requested separation
    on top of paper/live (e.g. "[LIVE][SWING]"). Equity hook sites derive it
    from the order's product via segment_for_product(); options hook sites
    pass Segment.OPTIONS directly. Aggregate messages (position update, EOD
    summary) leave it unset — they already group by segment internally."""
    try:
        n = _NOTIFIER
        if n is None:
            return
        n.submit(
            NotificationEvent(
                kind=kind,
                is_paper=is_paper,
                agent_trade_id=agent_trade_id,
                symbol=symbol,
                segment=segment,
                payload=payload,
                dedupe_key=dedupe_key,
            )
        )
    except Exception as exc:
        _LOG.warning("notify_failed kind=%s err=%s", kind, exc)
