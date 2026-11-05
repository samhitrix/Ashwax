"""
Idempotency guard — prevent duplicate order submissions.

Key = signal_id + symbol + direction + quantity + round(price, 2)
If the same key arrives twice, second call returns cached result without
hitting the broker again.

Phase 1: in-memory (TTL 24h). Phase 2: Redis-backed for restart survival.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from src.brokers.base import OrderRequest


@dataclass
class IdempotencyRecord:
    key: str
    broker_order_id: str
    created_at: float
    agent_trade_id: str


class IdempotencyGuard:
    """In-memory idempotency store with 24-hour TTL."""

    TTL_SECONDS = 86_400

    def __init__(self) -> None:
        self._store: dict[str, IdempotencyRecord] = {}

    def key_for(self, order: OrderRequest) -> str:
        raw = "|".join(
            [
                order.signal_id,
                order.symbol,
                order.direction,
                str(order.quantity),
                str(round(order.price or 0.0, 2)),
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def check(self, key: str) -> IdempotencyRecord | None:
        record = self._store.get(key)
        if record is None:
            return None
        if time.time() - record.created_at > self.TTL_SECONDS:
            del self._store[key]
            return None
        return record

    def record(self, key: str, broker_order_id: str, agent_trade_id: str) -> None:
        self._store[key] = IdempotencyRecord(
            key=key,
            broker_order_id=broker_order_id,
            created_at=time.time(),
            agent_trade_id=agent_trade_id,
        )

    def evict_expired(self) -> int:
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v.created_at > self.TTL_SECONDS]
        for k in expired:
            del self._store[k]
        return len(expired)


# ---------------------------------------------------------------------------
# Durable guard (migration 072) -- additive; the in-memory IdempotencyGuard above
# is unchanged. Wired into OrderLifecycleManager.place_entry_and_protection only.
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import logging  # noqa: E402
from typing import Any  # noqa: E402

# NOT "execution.idempotency" (a flat name orphaned from logger.py's _COMPONENTS
# hierarchy, which maps "src.execution" -> execution.log) -- __name__ correctly
# resolves to "src.execution.idempotency", nesting under that handler. The prior
# flat name meant every warning below only ever reached root/console, never the file.
_LOG = logging.getLogger(__name__)


class DuplicateOrderError(Exception):
    """Raised (enforce mode only) when an identical order attempt is already PENDING or
    SUBMITTED inside the duplicate window."""


@dataclass
class IntentDecision:
    proceed: bool
    key: str | None = None  # None -> nothing was recorded (mode off / guard error)
    duplicate_state: str | None = None
    duplicate_order_id: str | None = None


class DurableIdempotencyGuard:
    """Postgres-backed duplicate-order guard that can never itself stop a trade by failing.

    Modes (config/execution.json ``idempotency.mode``):
      off      -- does nothing.
      shadow   -- records intents and LOGS duplicates; never blocks. (default)
      enforce  -- additionally refuses an exact duplicate: same signal/symbol/side/qty/price
                  whose previous attempt is PENDING/SUBMITTED within ``window_seconds``.

    A previous attempt that FAILED, or is older than the window, never blocks -- so a genuine
    retry (e.g. the same-day AMO retry, 5 minutes apart) always goes through. Every DB error
    or timeout fails OPEN (the order proceeds).
    """

    def __init__(
        self,
        repo: Any,
        *,
        mode: str = "shadow",
        window_seconds: float = 120.0,
        timeout_seconds: float = 2.0,
    ) -> None:
        self._repo = repo
        self._mode = mode if mode in ("off", "shadow", "enforce") else "shadow"
        self._window = window_seconds
        self._timeout = timeout_seconds
        self._keyer = IdempotencyGuard()

    @property
    def mode(self) -> str:
        return self._mode

    async def begin(self, order: OrderRequest, *, is_paper: bool) -> IntentDecision:
        _LOG.debug(
            "idempotency_begin_received mode=%s symbol=%s side=%s qty=%s signal=%s is_paper=%s",
            self._mode,
            order.symbol,
            order.direction,
            order.quantity,
            order.signal_id,
            is_paper,
        )
        if self._mode == "off" or self._repo is None:
            return IntentDecision(proceed=True)
        key = self._keyer.key_for(order)
        try:
            claimed, state, order_id = await asyncio.wait_for(
                self._repo.begin(
                    key,
                    agent_trade_id=order.agent_trade_id,
                    signal_id=order.signal_id,
                    symbol=order.symbol,
                    direction=order.direction,
                    quantity=order.quantity,
                    price=order.price,
                    is_paper=is_paper,
                    window_seconds=self._window,
                ),
                self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — fail OPEN: a guard fault must never stop an order
            _LOG.warning("idempotency_guard_error key=%s err=%s -- proceeding", key, exc)
            return IntentDecision(proceed=True)
        if claimed:
            _LOG.debug("idempotency_begin_found key=%s claimed=True action=proceed", key)
            return IntentDecision(proceed=True, key=key)
        _LOG.warning(
            "idempotency_duplicate_detected mode=%s symbol=%s side=%s qty=%s signal=%s "
            "prior_state=%s prior_order=%s",
            self._mode,
            order.symbol,
            order.direction,
            order.quantity,
            order.signal_id,
            state,
            order_id,
        )
        return IntentDecision(
            proceed=self._mode != "enforce",
            key=None,
            duplicate_state=state,
            duplicate_order_id=order_id,
        )

    async def submitted(self, decision: IntentDecision, broker_order_id: str) -> None:
        if decision.key is None:
            return
        try:
            await asyncio.wait_for(
                self._repo.mark_submitted(decision.key, broker_order_id), self._timeout
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("idempotency_mark_submitted_failed key=%s err=%s", decision.key, exc)

    async def failed(self, decision: IntentDecision) -> None:
        if decision.key is None:
            return
        try:
            await asyncio.wait_for(self._repo.mark_failed(decision.key), self._timeout)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("idempotency_mark_failed_failed key=%s err=%s", decision.key, exc)
