"""Notification event types — the vocabulary shared by every hook site.

Zero dependencies by design: this module is imported deep inside the execution
path (order_lifecycle, paper_engine), so it must never drag in aiohttp, asyncpg,
or config loading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventKind(StrEnum):
    ENTRY_FILL = "ENTRY_FILL"
    ENTRY_REJECTED = "ENTRY_REJECTED"
    EXIT = "EXIT"
    MODIFY_SL = "MODIFY_SL"
    PARTIAL_BOOK = "PARTIAL_BOOK"
    POSITION_UPDATE = "POSITION_UPDATE"
    EOD_SUMMARY = "EOD_SUMMARY"
    # A scheduled LLM-origination pass (llm_intraday_agent / llm_swing_agent)
    # produced NOTHING at all — every provider in the chain, plus retries, failed —
    # as opposed to a genuine "0 ideas" decision, which stays silent (see the
    # raw_llm_response is None check at each hook site). Added 2026-09-16 so a
    # total outage is visible on the operator's phone the day it happens, not the
    # next time someone asks "why no trade" and it takes a log dig to find out.
    STRATEGY_ERROR = "STRATEGY_ERROR"
    # Morning/evening/startup LLM model health check (src/llm/model_health.py) found a
    # provider or gate chain not working — or that everything recovered.
    LLM_HEALTH = "LLM_HEALTH"
    # One terminal step of an LLM-originated idea's journey (call -> response -> idea ->
    # entry check -> gates -> execution): where it died, or that it was placed. Added
    # 2026-09-24 after a day of LLM calls whose ideas vanished with no trace.
    LLM_FUNNEL = "LLM_FUNNEL"
    # The ONE message sent on a weekday NSE holiday (2026-10-02): every other scheduled
    # message and LLM call is skipped on non-trading days.
    MARKET_HOLIDAY = "MARKET_HOLIDAY"


class Segment(StrEnum):
    """Which book a trade event belongs to — user-requested separation on top
    of paper/live, so an alert reads e.g. "[LIVE][SWING]" not just "[LIVE]"."""

    INTRADAY = "INTRADAY"
    SWING = "SWING"
    OPTIONS = "OPTIONS"


def segment_for_product(product: str) -> Segment:
    """Equity hook sites derive Segment from the order's `product` field —
    MIS/INTRADAY -> Intraday, CNC/DELIVERY/anything else -> Swing. Options
    hook sites pass Segment.OPTIONS directly (no `product` field applies)."""
    return Segment.INTRADAY if str(product).upper() in ("MIS", "INTRADAY") else Segment.SWING


class Priority(StrEnum):
    """Delivery priority under queue/rate pressure.

    CRITICAL is never throttled and never dropped — it waits for a rate token
    instead. These are the events that represent money actually moving.
    """

    CRITICAL = "CRITICAL"
    NORMAL = "NORMAL"
    LOW = "LOW"


# Money moved -> CRITICAL. A stop ratcheting -> NORMAL (throttled). A periodic
# rollup -> LOW (droppable; the next one carries the same information).
_PRIORITY: dict[EventKind, Priority] = {
    EventKind.ENTRY_FILL: Priority.CRITICAL,
    EventKind.ENTRY_REJECTED: Priority.CRITICAL,
    EventKind.EXIT: Priority.CRITICAL,
    EventKind.PARTIAL_BOOK: Priority.CRITICAL,
    EventKind.MODIFY_SL: Priority.NORMAL,
    EventKind.POSITION_UPDATE: Priority.LOW,
    EventKind.EOD_SUMMARY: Priority.NORMAL,
    EventKind.STRATEGY_ERROR: Priority.NORMAL,
    EventKind.LLM_HEALTH: Priority.NORMAL,
    EventKind.LLM_FUNNEL: Priority.NORMAL,
    EventKind.MARKET_HOLIDAY: Priority.NORMAL,
}

# config/notifications.json "events" keys, per kind.
_CONFIG_KEY: dict[EventKind, str] = {
    EventKind.ENTRY_FILL: "entry_fill",
    EventKind.ENTRY_REJECTED: "entry_rejected",
    EventKind.EXIT: "exit",
    EventKind.MODIFY_SL: "modify_sl",
    EventKind.PARTIAL_BOOK: "partial_book",
    EventKind.POSITION_UPDATE: "position_update",
    EventKind.EOD_SUMMARY: "eod_summary",
    EventKind.STRATEGY_ERROR: "strategy_error",
    EventKind.LLM_HEALTH: "llm_health",
    EventKind.LLM_FUNNEL: "llm_funnel",
    EventKind.MARKET_HOLIDAY: "market_holiday",
}


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """One thing that happened, on its way to the operator's phone.

    `ts` is monotonic (not wall clock) — it is only ever used for throttle
    arithmetic, where a clock step backwards would be a correctness bug.

    `dedupe_key` is set only for once-ever events (entry/exit/reject/eod). It
    reaches the DB unique index, which is what stops a restart or the 30s ghost
    reconciler re-sending something already delivered. Recurring events
    (modifies, position updates) leave it None so the partial index never collides.
    """

    kind: EventKind
    is_paper: bool
    agent_trade_id: str = ""
    symbol: str = ""
    segment: Segment | str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0
    dedupe_key: str | None = None

    @property
    def priority(self) -> Priority:
        return _PRIORITY.get(self.kind, Priority.NORMAL)

    @property
    def config_key(self) -> str:
        return _CONFIG_KEY.get(self.kind, self.kind.value.lower())

    def get(self, name: str, default: Any = None) -> Any:
        """Payload accessor — hook sites pass arbitrary kwargs, formatters read
        them defensively. A missing field must render as a placeholder, never
        raise: a formatting bug must not be able to kill the worker."""
        return self.payload.get(name, default)

    def getf(self, name: str, default: float = 0.0) -> float:
        """Payload accessor coerced to float, tolerating None/Decimal/str."""
        try:
            raw = self.payload.get(name)
            return default if raw is None else float(raw)
        except (TypeError, ValueError):
            return default
