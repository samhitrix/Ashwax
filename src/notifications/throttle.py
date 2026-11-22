"""Throttling/coalescing — keeps a flooding SL trail from becoming a flood of
Telegram messages, without ever dropping something the operator needs to see.

Pure and deterministic: the clock is injected so tests never sleep. This module
does no I/O and never imports aiohttp/asyncpg — it only decides SEND / SUPPRESS
/ COALESCE and hands the worker a wait time for the global rate limiter.

Rules (see docs/superpowers/... plan for the measured rationale):
  1. Materiality gate: a MODIFY_SL is only sent if the stop has moved by at
     least `modify_min_r_moved` R since the last one actually sent for that
     trade — R = abs(fill_price - initial_sl), fixed at first sight per trade.
  2. Per-trade time floor between MODIFY_SL sends.
  3. Coalesce window: a newer modify inside the window replaces (not queues
     behind) the pending one — only the latest value is ever sent.
  4. Always-send overrides bypass 1-3: break-even crossing, and any event of
     CRITICAL priority (ENTRY/EXIT/REJECT/PARTIAL_BOOK) is never throttled.
  5. Per-trade daily cap on MODIFY_SL sends; once hit, further modifies are
     suppressed but counted, so the count can be surfaced in the next
     position update rather than silently lost.
  6. Global token bucket bounds total throughput regardless of per-trade
     behaviour, protecting against Telegram's own per-chat rate ceiling.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from src.notifications.config import ThrottleConfig
from src.notifications.events import EventKind, NotificationEvent, Priority


class Decision(StrEnum):
    SEND = "SEND"
    SUPPRESS = "SUPPRESS"
    COALESCE = "COALESCE"


@dataclass
class _TradeModifyState:
    last_sent_sl: float | None = None
    last_sent_ts: float = 0.0
    initial_risk: float | None = None
    sent_today: int = 0
    suppressed_today: int = 0
    day_key: str = ""


@dataclass
class Throttler:
    """One Throttler per Notifier (process-wide). `_clock` defaults to
    time.monotonic but tests inject a fake for deterministic control."""

    cfg: ThrottleConfig
    _clock: Callable[[], float] = time.monotonic
    _trades: dict[str, _TradeModifyState] = field(default_factory=dict)
    _dedupe_seen: dict[str, float] = field(default_factory=dict)
    _bucket_tokens: float = 0.0
    _bucket_last_refill: float = 0.0
    _started_at: float = 0.0
    _total_suppressed: int = 0

    def __post_init__(self) -> None:
        now = self._clock()
        self._bucket_tokens = float(self.cfg.global_burst)
        self._bucket_last_refill = now
        self._started_at = now

    # -- startup quiet window -------------------------------------------------

    def in_startup_quiet_window(self) -> bool:
        """True for the first `startup_quiet_seconds` after the worker started.
        Suppresses non-EXIT noise from reseed/late-poll bursts at boot."""
        return (self._clock() - self._started_at) < self.cfg.startup_quiet_seconds

    # -- dedupe (in-memory front line ahead of the DB unique index) ----------

    def is_duplicate(self, dedupe_key: str | None) -> bool:
        if dedupe_key is None:
            return False
        self._prune_dedupe()
        return dedupe_key in self._dedupe_seen

    def note_dedupe(self, dedupe_key: str | None) -> None:
        if dedupe_key is not None:
            self._dedupe_seen[dedupe_key] = self._clock()

    def _prune_dedupe(self) -> None:
        cutoff = self._clock() - self.cfg.dedupe_ttl_seconds
        stale = [k for k, ts in self._dedupe_seen.items() if ts < cutoff]
        for k in stale:
            del self._dedupe_seen[k]

    # -- admission decision ----------------------------------------------------

    def admit(self, ev: NotificationEvent) -> Decision:
        """Decide what to do with `ev`. Does NOT mutate state for SEND — call
        note_sent() once the send actually succeeds (or is queued for send)."""
        if self.is_duplicate(ev.dedupe_key):
            return Decision.SUPPRESS

        if ev.priority == Priority.CRITICAL:
            return Decision.SEND

        if ev.kind == EventKind.MODIFY_SL:
            return self._admit_modify(ev)

        return Decision.SEND

    def _admit_modify(self, ev: NotificationEvent) -> Decision:
        st = self._trade_state(ev.agent_trade_id)
        self._maybe_roll_day(st)

        new_sl = ev.getf("new_sl")
        fill_price = ev.getf("fill_price")
        initial_sl = ev.getf("initial_sl")

        if st.initial_risk is None:
            risk = abs(fill_price - initial_sl)
            st.initial_risk = risk if risk > 0 else 1.0

        is_breakeven = bool(ev.get("is_breakeven"))
        if not is_breakeven and fill_price:
            direction = str(ev.get("direction", "BUY")).upper()
            crossed = new_sl >= fill_price if direction == "BUY" else new_sl <= fill_price
            was_worse = st.last_sent_sl is None or (
                st.last_sent_sl < fill_price if direction == "BUY" else st.last_sent_sl > fill_price
            )
            is_breakeven = crossed and was_worse

        if is_breakeven:
            return Decision.SEND  # always-send override

        if st.sent_today >= self.cfg.modify_max_per_trade_per_day:
            st.suppressed_today += 1
            self._total_suppressed += 1
            return Decision.SUPPRESS

        now = self._clock()
        if st.last_sent_ts and (now - st.last_sent_ts) < self.cfg.modify_coalesce_seconds:
            return Decision.COALESCE

        if st.last_sent_sl is not None:
            moved = abs(new_sl - st.last_sent_sl)
            if moved < self.cfg.modify_min_r_moved * st.initial_risk:
                self._total_suppressed += 1
                return Decision.SUPPRESS

        if st.last_sent_ts and (now - st.last_sent_ts) < self.cfg.modify_min_seconds_per_trade:
            self._total_suppressed += 1
            return Decision.SUPPRESS

        return Decision.SEND

    def note_sent(self, ev: NotificationEvent) -> None:
        """Call after a SEND decision's send actually completes (or is handed
        to the queue). Updates per-trade bookkeeping used by future admit()s."""
        self.note_dedupe(ev.dedupe_key)
        if ev.kind != EventKind.MODIFY_SL:
            return
        st = self._trade_state(ev.agent_trade_id)
        st.last_sent_sl = ev.getf("new_sl")
        st.last_sent_ts = self._clock()
        st.sent_today += 1

    def suppressed_count_for_trade(self, agent_trade_id: str) -> int:
        """How many MODIFY_SL events for this trade were suppressed since the
        last note_sent — used to fold '+N trails' into the next position update."""
        st = self._trades.get(agent_trade_id)
        return st.suppressed_today if st else 0

    def total_suppressed(self) -> int:
        return self._total_suppressed

    def _trade_state(self, agent_trade_id: str) -> _TradeModifyState:
        st = self._trades.get(agent_trade_id)
        if st is None:
            st = _TradeModifyState()
            self._trades[agent_trade_id] = st
        return st

    def _maybe_roll_day(self, st: _TradeModifyState) -> None:
        # Day boundary is approximate (wall-clock date), which is fine here:
        # this only resets a per-trade daily message cap, not trading state.
        import datetime as _dt

        today = _dt.date.today().isoformat()
        if st.day_key != today:
            st.day_key = today
            st.sent_today = 0
            st.suppressed_today = 0

    def forget_trade(self, agent_trade_id: str) -> None:
        """Drop per-trade throttle state once a trade closes — otherwise the
        dict grows unbounded over a long-running process."""
        self._trades.pop(agent_trade_id, None)

    # -- global token bucket ---------------------------------------------------

    def take_token(self) -> float:
        """Returns seconds to wait before the caller may send (0 = go now).
        Consumes a token when one is available; otherwise reports the wait
        without consuming, so the caller can sleep and retry."""
        now = self._clock()
        elapsed = now - self._bucket_last_refill
        if elapsed > 0:
            refill = elapsed / self.cfg.global_refill_seconds
            self._bucket_tokens = min(self.cfg.global_burst, self._bucket_tokens + refill)
            self._bucket_last_refill = now

        if self._bucket_tokens >= 1.0:
            self._bucket_tokens -= 1.0
            return 0.0

        deficit = 1.0 - self._bucket_tokens
        return deficit * self.cfg.global_refill_seconds
