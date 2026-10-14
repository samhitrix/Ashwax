"""Seller-desk multi-leg execution — protection-first submission + partial-fill
triage + naked-exposure watchdog (options_spec.md §9).

THE KEY INVARIANT: protection (long) legs are submitted and confirmed FILLED
before income (short) legs are ever submitted. This makes naked short
exposure structurally impossible in the normal path — the watchdog is a
defense-in-depth backup for the abnormal path (partial fills, restarts),
never the primary safety mechanism.

HARD RULE (options_spec.md §9, non-configurable): no naked short option is
ever held for more than 60 seconds.

Simplification, stated plainly rather than silently narrowed: spec's CASE B
partial-fill triage allows a 5-minute retry of the unfilled income leg
before closing the orphaned protection leg. This implementation closes
immediately on zero income fills instead of retrying — fewer moving parts,
strictly more conservative (stops theta bleed sooner), documented here as a
deliberate scope cut, not an oversight.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.brokers.base import OrderRequest

if TYPE_CHECKING:
    from src.brokers.base import BrokerInterface, OrderMarginResult
    from src.options.structure_selector import OptionLeg

_LOG = logging.getLogger("options.leg_executor")

_POLL_INTERVAL_SECONDS = 2.0
_FILL_TIMEOUT_SECONDS = 30.0
_NAKED_EXPOSURE_MAX_SECONDS = 60.0  # HARD RULE — not configurable
_WATCHDOG_POLL_SECONDS = 1.0


@dataclass
class LegFillState:
    leg: OptionLeg
    order_id: str
    filled: bool = False
    rejected: bool = False


@dataclass
class ExecutionResult:
    position_id: str
    outcome: str = (
        "PENDING"  # ALL_FILLED | ALL_REJECTED | PARTIAL_DEGRADED | PARTIAL_EMERGENCY_CLOSED
    )
    protection_legs: list[LegFillState] = field(default_factory=list)
    income_legs: list[LegFillState] = field(default_factory=list)


def _split_symbol(tradingsymbol: str) -> tuple[str, str]:
    if ":" in tradingsymbol:
        exch, sym = tradingsymbol.split(":", 1)
        return exch, sym
    return "NFO", tradingsymbol


class OptionsLegExecutor:
    """Protection-first multi-leg executor with a naked-exposure watchdog."""

    def __init__(
        self,
        broker: BrokerInterface,
        *,
        poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
        fill_timeout_seconds: float = _FILL_TIMEOUT_SECONDS,
    ) -> None:
        self._broker = broker
        self._poll_interval = poll_interval_seconds
        self._fill_timeout = fill_timeout_seconds
        self._naked_positions: dict[str, float] = {}
        self._position_locks: dict[str, asyncio.Lock] = {}

    def hint_prices(self, prices: dict[str, float]) -> None:
        """Paper brokers only: hand the simulated fill the prices the decision was just made on (no extra
        Kite round trip). No-op for real brokers."""
        hint = getattr(self._broker, "hint_prices", None)
        if callable(hint):
            hint(prices)

    def lock_for(self, position_id: str) -> asyncio.Lock:
        """Shared per-position lock (options_spec.md §19) — also used by
        margin_monitor and position_manager so exactly one actor handles any
        given position at a time."""
        if position_id not in self._position_locks:
            self._position_locks[position_id] = asyncio.Lock()
        return self._position_locks[position_id]

    def _build_order_request(self, leg: OptionLeg, signal_id: str, tag: str) -> OrderRequest:
        exch, sym = _split_symbol(leg.tradingsymbol)
        return OrderRequest(
            symbol=sym,
            exchange=exch,
            direction=leg.direction,
            quantity=leg.quantity,
            order_type="MARKET",
            product="MARGIN",
            validity="DAY",
            agent_trade_id=f"{signal_id}-{leg.instrument_type}-{int(leg.strike)}"[:32],
            signal_id=signal_id,
            tag=tag,
        )

    async def get_order_margin_for_structure(
        self,
        protection_legs: list[OptionLeg],
        income_legs: list[OptionLeg],
        signal_id: str,
    ) -> OrderMarginResult:
        """Real pre-trade margin for a prospective structure — read-only,
        places nothing. Reuses the exact OrderRequest shape execute_structure
        would submit, so the margin call and the real order never diverge."""
        requests = [
            self._build_order_request(leg, signal_id, "opt-protection") for leg in protection_legs
        ] + [self._build_order_request(leg, signal_id, "opt-income") for leg in income_legs]
        return await self._broker.get_order_margin(requests)

    async def _place_leg(self, leg: OptionLeg, signal_id: str, tag: str) -> str:
        req = self._build_order_request(leg, signal_id, tag)
        order_id = await self._broker.place_order(req)
        _LOG.debug(
            "leg_executor_leg_placed signal=%s symbol=%s direction=%s strike=%s type=%s "
            "qty=%d tag=%s order_id=%s",
            signal_id,
            leg.tradingsymbol,
            leg.direction,
            leg.strike,
            leg.instrument_type,
            leg.quantity,
            tag,
            order_id,
        )
        return order_id

    async def _poll_until_filled(self, states: list[LegFillState], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = [s for s in states if not s.filled and not s.rejected]
            if not pending:
                return
            for s in pending:
                order = await self._broker.get_order_status(s.order_id)
                if order.status == "FILLED":
                    s.filled = True
                    _LOG.debug("poll_until_filled_found order_id=%s action=filled", s.order_id)
                elif order.status == "REJECTED":
                    s.rejected = True
                    _LOG.debug("poll_until_filled_found order_id=%s action=rejected", s.order_id)
            if all(s.filled or s.rejected for s in states):
                return
            await asyncio.sleep(self._poll_interval)

    async def _cancel_leg(self, order_id: str) -> None:
        try:
            await self._broker.cancel_order(order_id)
        except Exception as exc:  # noqa: BLE001 — best-effort cancel
            _LOG.warning("leg_cancel_failed order_id=%s err=%s", order_id, exc)

    async def close_leg(self, leg: OptionLeg, signal_id: str) -> float | None:
        """Public wrapper — used by position_manager/margin_monitor/expiry_guardian
        sweeps (via seller_desk.py) to close a single leg at market.

        Returns the LTP captured immediately before the close order is
        placed, as an APPROXIMATION of the exit fill price -- the market
        order itself is fire-and-forget (no fill-confirmation polling exists
        for this path, unlike the equity OrderMonitor), so the real fill may
        differ slightly from this quote (more so for illiquid strikes).
        ``None`` if the LTP fetch fails -- close still proceeds either way,
        this is for P&L bookkeeping only, never a gate on closing the leg.
        """
        return await self._market_close_leg(leg, signal_id)

    async def _market_close_leg(self, leg: OptionLeg, signal_id: str) -> float | None:
        exch, sym = _split_symbol(leg.tradingsymbol)
        exit_dir = "SELL" if leg.direction == "BUY" else "BUY"
        exit_price: float | None = None
        try:
            quotes = await self._broker.get_ltp([leg.tradingsymbol])
            exit_price = quotes.get(leg.tradingsymbol)
        except Exception as exc:  # noqa: BLE001 — LTP is for P&L bookkeeping, never blocks the close
            _LOG.warning("leg_close_ltp_fetch_failed symbol=%s err=%s", leg.tradingsymbol, exc)
        await self._broker.place_order(
            OrderRequest(
                symbol=sym,
                exchange=exch,
                direction=exit_dir,
                quantity=leg.quantity,
                order_type="MARKET",
                product="MARGIN",
                validity="DAY",
                agent_trade_id=f"{signal_id}-CLOSE-{int(leg.strike)}"[:32],
                signal_id=signal_id,
                tag="opt-emergency-close",
            )
        )
        return exit_price

    async def execute_structure(
        self,
        protection_legs: list[OptionLeg],
        income_legs: list[OptionLeg],
        signal_id: str,
    ) -> ExecutionResult:
        """Protection-first submission (§9). Returns the execution outcome."""
        position_id = f"OPT-{uuid.uuid4().hex[:12].upper()}"
        _LOG.debug(
            "execute_structure_received position=%s signal=%s protection_legs=%d income_legs=%d",
            position_id,
            signal_id,
            len(protection_legs),
            len(income_legs),
        )
        result = ExecutionResult(position_id=position_id)

        protection_states = [
            LegFillState(leg=leg, order_id=await self._place_leg(leg, signal_id, "opt-protection"))
            for leg in protection_legs
        ]
        result.protection_legs = protection_states
        await self._poll_until_filled(protection_states, self._fill_timeout)

        if not all(s.filled for s in protection_states):
            for s in protection_states:
                if not s.filled and not s.rejected:
                    await self._cancel_leg(s.order_id)
                elif s.filled:
                    await self._market_close_leg(s.leg, signal_id)
            result.outcome = (
                "ALL_REJECTED"
                if all(s.rejected for s in protection_states)
                else "PARTIAL_EMERGENCY_CLOSED"
            )
            _LOG.warning(
                "protection_legs_incomplete position_id=%s outcome=%s", position_id, result.outcome
            )
            return result

        income_states = [
            LegFillState(leg=leg, order_id=await self._place_leg(leg, signal_id, "opt-income"))
            for leg in income_legs
        ]
        result.income_legs = income_states
        await self._poll_until_filled(income_states, self._fill_timeout)

        if all(s.filled for s in income_states):
            result.outcome = "ALL_FILLED"
            _LOG.debug(
                "execute_structure_found position=%s outcome=ALL_FILLED protection_legs=%d "
                "income_legs=%d",
                position_id,
                len(protection_states),
                len(income_states),
            )
            return result

        for s in income_states:
            if not s.filled and not s.rejected:
                await self._cancel_leg(s.order_id)

        filled_income = [s for s in income_states if s.filled]
        if not filled_income:
            for s in protection_states:
                await self._market_close_leg(s.leg, signal_id)
            result.outcome = "PARTIAL_EMERGENCY_CLOSED"
        else:
            result.outcome = "PARTIAL_DEGRADED"
        _LOG.warning(
            "income_legs_incomplete position_id=%s outcome=%s", position_id, result.outcome
        )
        return result

    async def roll_leg(
        self, old_leg: OptionLeg, new_leg: OptionLeg, signal_id: str
    ) -> OptionLeg | None:
        """Close ``old_leg`` at market, then open ``new_leg`` at market and
        confirm its fill (options_spec.md §10 adjustment — rolling a single
        income leg to a new strike). Unlike ``execute_structure``, closing an
        existing SHORT leg first only ever *reduces* obligation, so there is
        no naked-exposure window to guard here (the reverse order used for a
        fresh structure's protection-first rule does not apply).

        Returns the filled ``new_leg`` (with its actual order id recorded via
        polling) on success, or ``None`` if the new leg fails to fill — the
        caller is left with one fewer income leg for this cycle rather than a
        forced retry, since a missing short leg is strictly safer than a
        stuck one.
        """
        await self._market_close_leg(old_leg, signal_id)
        order_id = await self._place_leg(new_leg, signal_id, "opt-adjustment")
        state = LegFillState(leg=new_leg, order_id=order_id)
        await self._poll_until_filled([state], self._fill_timeout)
        if not state.filled:
            if not state.rejected:
                await self._cancel_leg(order_id)
            _LOG.warning(
                "roll_leg_new_leg_unfilled position_id=%s symbol=%s",
                signal_id,
                new_leg.tradingsymbol,
            )
            return None
        return new_leg

    def mark_naked(self, position_id: str, detected_at: float | None = None) -> None:
        """Record a position as carrying naked short exposure — the watchdog
        acts on this dict. Used by partial-fill triage / restart recovery,
        not by the normal protection-first path (which never goes naked)."""
        self._naked_positions[position_id] = (
            detected_at if detected_at is not None else time.monotonic()
        )

    def clear_naked(self, position_id: str) -> None:
        self._naked_positions.pop(position_id, None)

    def naked_position_ids(self) -> list[str]:
        return list(self._naked_positions)

    async def naked_exposure_watchdog(
        self,
        close_callback: Any,
        *,
        stop: asyncio.Event,
        poll_seconds: float = _WATCHDOG_POLL_SECONDS,
    ) -> None:
        """HARD RULE: no naked short held > 60s. Bypasses all other gates —
        this is survival, not trading. ``close_callback(position_id)``
        performs the actual emergency market close."""
        while not stop.is_set():
            now = time.monotonic()
            for position_id, detected_at in list(self._naked_positions.items()):
                if now - detected_at > _NAKED_EXPOSURE_MAX_SECONDS:
                    lock = self.lock_for(position_id)
                    if not lock.locked():
                        async with lock:
                            if position_id in self._naked_positions:
                                await close_callback(position_id)
                                del self._naked_positions[position_id]
                                _LOG.critical(
                                    "naked_exposure_emergency_close position_id=%s", position_id
                                )
            await asyncio.sleep(poll_seconds)
