"""
Order lifecycle state machine.

States: PENDING_SUBMIT â†’ SUBMITTED â†’ OPEN â†’ PARTIAL â†’ FILLED â†’ CLOSED
Terminal: REJECTED | CANCELLED | TIMED_OUT

Usage:
  mgr = OrderLifecycleManager(broker)
  record = mgr.create_trade(order_request)
  broker_order_id = await mgr.submit(order.agent_trade_id)
  updated = await mgr.poll(order.agent_trade_id)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

_LOG = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.brokers.base import BrokerInterface
    from src.db.repositories.trades import TradeRepository
    from src.infrastructure.alert_system import AlertManager

from src.brokers.base import Fill, Order, OrderRequest  # noqa: E402
from src.market import sessions as _sessions  # noqa: E402
from src.notifications import EventKind, notify  # noqa: E402
from src.notifications.events import segment_for_product  # noqa: E402

_IST = ZoneInfo("Asia/Kolkata")


def _load_gap_breach_slippage_pct() -> float:
    """Paper gap-breach fill allowance (% of SL), from operator.json.

    hard_limits.intraday.gap_breach_slippage_pct — bounds the simulated exit at
    SL ± this much when the polling monitor detects a stop breach late.
    """
    try:
        import json as _j
        from pathlib import Path as _P

        _cfg = _j.loads((_P("config") / "operator.json").read_text())
        _intraday = _cfg.get("hard_limits", {}).get("intraday", {})
        if "gap_breach_slippage_pct" not in _intraday:
            _LOG.warning(
                "gap_breach_slippage_pct missing from operator.json "
                "hard_limits.intraday — using hardcoded fallback 0.05"
            )
        return float(_intraday.get("gap_breach_slippage_pct", 0.05))
    except Exception as exc:
        _LOG.warning("failed to read operator.json for gap_breach_slippage_pct: %s", exc)
        return 0.05


_GAP_BREACH_SLIPPAGE_PCT = _load_gap_breach_slippage_pct()


def _load_execution_monitoring_cfg() -> dict[str, float]:
    """monitoring.* from execution.json — cycle cadence, GTT re-check interval,
    breach-approaching threshold, chase cap. 2026-08-20: these used to be
    hardcoded module constants / magic numbers; config now owns them, same as
    order_types already does for SL/entry/target order types."""
    _defaults = {
        "cycle_seconds": 2.0,
        "gtt_check_interval_seconds": 30.0,
        "breach_approaching_pct": 0.5,
        "chase_cap_pct": 0.15,
        "chase_max_attempts": 3.0,
    }
    try:
        import json as _j
        from pathlib import Path as _P

        _cfg = _j.loads((_P("config") / "execution.json").read_text())
        _mon = _cfg.get("monitoring", {})
        _missing = [k for k in _defaults if k not in _mon]
        if _missing:
            _LOG.warning(
                "execution.json monitoring.* missing keys %s — using hardcoded fallbacks",
                _missing,
            )
        return {k: float(_mon.get(k, v)) for k, v in _defaults.items()}
    except Exception as exc:
        _LOG.warning("failed to read execution.json monitoring config: %s", exc)
        return _defaults


_MONITORING_CFG = _load_execution_monitoring_cfg()


def _load_gtt_leg_order_type() -> str:
    """execution.json order_types.CNC_swing_gtt_sl_leg — the order type placed on a
    GTT's triggered legs.

    Zerodha's GTT API accepts LIMIT only (kite.trade/docs/connect/v3/gtt: "order_type:
    LIMIT"). A MARKET leg is rejected with "Invalid trigger data." and the position is
    left with no protection at all — which is exactly what happened on 2026-08-28. A
    stop that gaps through its limit is caught instead by OrderMonitor's per-cycle
    SL_GAP_BREACH sweep, which market-exits.
    """
    try:
        import json as _j
        from pathlib import Path as _P

        _cfg = _j.loads((_P("config") / "execution.json").read_text())
        return str(_cfg.get("order_types", {}).get("CNC_swing_gtt_sl_leg", "LIMIT"))
    except Exception:
        return "LIMIT"


_GTT_LEG_ORDER_TYPE = _load_gtt_leg_order_type()

# Intraday entries used to be plain MARKET orders — fine on a calm tape, but on
# a fast/violent move (observed 2026-08-12: AIAENG SHORT filled then blew
# through its own SL 9 seconds later) the fill itself could already be at an
# extreme. A marketable limit caps how much adverse slippage an entry will
# accept: priced this far beyond the signal price, in the adverse direction,
# it still fills immediately in normal conditions but refuses (rather than
# chases) a price that has already run further than this by the time the
# order reaches the book.
_INTRADAY_ENTRY_CHASE_CAP_PCT = _MONITORING_CFG["chase_cap_pct"]


def _marketable_limit_price(signal_price: float, direction: str) -> float:
    offset = signal_price * _INTRADAY_ENTRY_CHASE_CAP_PCT / 100.0
    return signal_price + offset if direction.upper() == "BUY" else signal_price - offset


class OrderState(str, Enum):  # noqa: UP042
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


VALID_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PENDING_SUBMIT: frozenset({OrderState.SUBMITTED, OrderState.REJECTED}),
    OrderState.SUBMITTED: frozenset(
        {OrderState.OPEN, OrderState.REJECTED, OrderState.CANCELLED, OrderState.TIMED_OUT}
    ),
    OrderState.OPEN: frozenset(
        {
            OrderState.PARTIAL,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.TIMED_OUT,
        }
    ),
    OrderState.PARTIAL: frozenset(
        {
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.TIMED_OUT,
        }
    ),
    OrderState.FILLED: frozenset({OrderState.CLOSED}),
    OrderState.CLOSED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.TIMED_OUT: frozenset(),
}

TERMINAL_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.CLOSED,
        OrderState.REJECTED,
        OrderState.CANCELLED,
        OrderState.TIMED_OUT,
    }
)

# Broker canonical status â†’ OrderState
_BROKER_STATUS_MAP: dict[str, OrderState] = {
    "OPEN": OrderState.OPEN,
    "PARTIAL": OrderState.PARTIAL,
    "FILLED": OrderState.FILLED,
    "CANCELLED": OrderState.CANCELLED,
    "REJECTED": OrderState.REJECTED,
}


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""


@dataclass
class TradeRecord:
    """Tracks a single order through its full lifecycle."""

    agent_trade_id: str
    signal_id: str
    order_request: OrderRequest
    state: OrderState = OrderState.PENDING_SUBMIT
    broker_order_id: str = ""
    broker_order: Order | None = None
    fill_price: float = 0.0
    filled_quantity: int = 0
    fills: list[Fill] = field(default_factory=list)
    state_history: list[tuple[OrderState, datetime]] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(_IST))
    sl_price: float = 0.0
    direction: str = ""
    entry_price: float = 0.0
    target_price: float = 0.0
    sl_order_id: str = ""
    target_order_id: str = ""
    # Native broker GTT covering BOTH exit legs (a GTT is inherently OCO, so one
    # trigger id protects the stop and the target). Empty when protection was
    # placed as plain orders instead. Persisted so a restart can trail/verify it.
    gtt_sl_id: str = ""
    repeg_count: int = 0  # entry re-pegs applied while resting unfilled
    # Entry placed without protection (delivery); SL/target must be armed on fill,
    # including when the fill is detected later by the monitor's pending re-poll.
    defer_protection: bool = False
    protection_ctx: dict[str, Any] = field(default_factory=dict)
    # MonitoredPosition to register if this entry fills later, on the monitor loop
    # rather than inline at placement. Without it a late fill would be a live position
    # with money at risk and no SL/target/trail monitoring attached.
    monitor_template: Any = None
    # Risk cap this order was SIZED with (meta["max_risk_rupees"]); 0 = use the
    # manager's global cap. Without it a %-of-equity sized trade above the flat cap
    # would have its stop silently tightened by the post-fill check.
    max_risk_rupees: float = 0.0

    def __post_init__(self) -> None:
        if not self.state_history:
            self.state_history.append((self.state, self.created_at))

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def last_transition_at(self) -> datetime:
        return self.state_history[-1][1]


class OrderLifecycleManager:
    """
    Manages order state transitions and fill tracking.

    In-memory for Phase 1; Phase 2 adds DB persistence.
    """

    def __init__(
        self,
        broker: BrokerInterface,
        max_risk_rupees: float = 0.0,
        alert_manager: AlertManager | None = None,
        trade_repo: TradeRepository | None = None,
        is_paper: bool = False,
        idempotency: Any | None = None,
        rejection_handler: Any | None = None,
        broker_name: str | None = None,
    ) -> None:
        from src.monitoring.slippage_tracker import SlippageTracker

        self._broker = broker
        self._max_risk = max_risk_rupees
        self._alert_manager = alert_manager
        self._trade_repo = trade_repo
        # Routes persistence to the paper_trades table (is_paper=True) vs trades
        # (is_paper=False). The broker adapter still decides real vs simulated fills.
        self._is_paper = is_paper
        # Live broker that holds this stack's positions, stamped on every new live row as
        # trades.exec_broker (migration 089) so exits/reseeds follow it. None = legacy:
        # nothing stamped, and a NULL exec_broker means "kite".
        self._broker_name = broker_name
        # Optional DurableIdempotencyGuard (src/execution/idempotency.py). None = no guard,
        # i.e. exactly the previous behaviour.
        self._idempotency = idempotency
        # Optional RejectionHandler (src/execution/rejection_handler.py). None = no
        # classify/retry, i.e. exactly the previous behaviour (immediate escalation).
        self._rejection_handler = rejection_handler
        self._trades: dict[str, TradeRecord] = {}
        # Entry orders submitted but not yet filled, re-polled every monitor cycle by
        # poll_pending_entries(). Cleared on fill or on any terminal state.
        self._pending_entry_ids: set[str] = set()
        self.slippage_tracker = SlippageTracker()

    def create_trade(self, order: OrderRequest) -> TradeRecord:
        """Register an order request before submission."""
        record = TradeRecord(
            agent_trade_id=order.agent_trade_id,
            signal_id=order.signal_id,
            order_request=order,
        )
        self._trades[order.agent_trade_id] = record
        return record

    def transition(self, agent_trade_id: str, new_state: OrderState) -> None:
        """Apply a validated state transition. Raises InvalidTransitionError on illegal moves."""
        record = self._get_or_raise(agent_trade_id)
        current = record.state
        if new_state not in VALID_TRANSITIONS[current]:
            raise InvalidTransitionError(
                f"Cannot transition {current.value} â†’ {new_state.value} "
                f"for trade {agent_trade_id!r}"
            )
        record.state = new_state
        record.state_history.append((new_state, datetime.now(_IST)))

    async def submit(self, agent_trade_id: str) -> str:
        """
        Submit the order to the broker and advance to SUBMITTED.

        Returns the broker_order_id.
        """
        record = self._get_or_raise(agent_trade_id)
        _LOG.debug(
            "order_submit_received tid=%s symbol=%s direction=%s qty=%s product=%s",
            agent_trade_id,
            record.order_request.symbol,
            record.direction,
            record.order_request.quantity,
            record.order_request.product,
        )
        broker_order_id = await self._broker.place_order(record.order_request)
        record.broker_order_id = broker_order_id
        self.transition(agent_trade_id, OrderState.SUBMITTED)
        _LOG.debug(
            "order_submit_done tid=%s broker_order_id=%s state=%s",
            agent_trade_id,
            broker_order_id,
            OrderState.SUBMITTED.value,
        )
        return broker_order_id

    async def poll(self, agent_trade_id: str) -> TradeRecord:
        """
        Fetch current broker order status and advance state if changed.

        On FILLED: also fetches fills and computes weighted-avg fill_price.
        """
        record = self._get_or_raise(agent_trade_id)
        if record.is_terminal:
            return record

        order = await self._broker.get_order_status(record.broker_order_id)
        record.broker_order = order

        new_state = _BROKER_STATUS_MAP.get(order.status)
        _LOG.debug(
            "order_poll_received tid=%s broker_status=%s current_state=%s mapped_new_state=%s",
            agent_trade_id,
            order.status,
            record.state.value,
            new_state.value if new_state else None,
        )
        if new_state is None or new_state == record.state:
            return record

        allowed = VALID_TRANSITIONS.get(record.state, frozenset())
        if new_state not in allowed:
            # Genuinely unexpected transition (e.g. a broker status sequence this
            # table doesn't model). OPEN -> REJECTED used to land here too and was
            # dropped silently — that's fixed below by allowing it explicitly, so
            # this branch is now a pure safety net for the truly unmodeled cases.
            _LOG.error(
                "order_state_transition_refused tid=%s from=%s to=%s",
                agent_trade_id,
                record.state.value,
                new_state.value,
            )
            return record

        if new_state == OrderState.REJECTED:
            # No fill occurred, but MIS entries arm SL/target protection right
            # after submit (place_entry_and_protection), before a fill is ever
            # confirmed — so a rejection reported here can still have live
            # protection legs resting at the broker. cancel_protection() cancels
            # them, transitions to REJECTED, persists, and notifies (via
            # _persist_rejected) — the same cleanup path already used for a
            # post-fill rejection (slippage cap exceeded).
            await self.cancel_protection(agent_trade_id, note="broker rejected the order")
            return record

        record.state = new_state
        record.state_history.append((new_state, datetime.now(_IST)))

        if new_state == OrderState.FILLED:
            fills = await self._broker.get_trades(record.broker_order_id)
            record.fills = fills
            total_qty = sum(f.quantity for f in fills)
            record.filled_quantity = total_qty
            if total_qty > 0:
                record.fill_price = sum(f.quantity * f.price for f in fills) / total_qty
                self._record_slippage(record)
                await self._persist_fill(record)
                _LOG.debug(
                    "order_poll_filled tid=%s filled_qty=%s fill_price=%.2f",
                    agent_trade_id,
                    total_qty,
                    record.fill_price,
                )

            if record.sl_price > 0 and record.target_price > 0 and record.filled_quantity > 0:
                await self._reanchor_protection_on_fill(record)

            # After reanchor so the message shows the armed SL/target, not the
            # pre-reanchor signal levels (the fill can land anywhere; reanchor
            # preserves R:R off the actual fill, and can differ materially).
            if total_qty > 0:
                try:
                    notify(
                        EventKind.ENTRY_FILL,
                        is_paper=self._is_paper,
                        agent_trade_id=record.agent_trade_id,
                        symbol=record.order_request.symbol,
                        segment=segment_for_product(record.order_request.product),
                        dedupe_key=f"entry:{record.agent_trade_id}",
                        direction=record.direction,
                        qty=record.filled_quantity,
                        fill=record.fill_price,
                        sl=record.sl_price,
                        target=record.target_price,
                        product=record.order_request.product,
                    )
                except Exception:  # notify() must never fail the trade path
                    pass

        return record

    async def _reanchor_protection_on_fill(self, record: TradeRecord) -> None:
        """Post-fill: re-anchor SL+target to the actual fill (preserving R:R), apply
        to the live protection orders, and raise SLIPPAGE_RISK_BREACH if the cap had
        to tighten the stop. The signal levels were computed off a reference price;
        the fill can sit anywhere — arming stale levels fires protection instantly."""
        from src.execution.post_fill_risk import reanchor_protection

        ra = reanchor_protection(
            fill_price=record.fill_price,
            signal_entry=record.entry_price or record.fill_price,
            signal_sl=record.sl_price,
            signal_target=record.target_price,
            direction=record.direction or "BUY",
            quantity=record.filled_quantity,
            max_risk_rupees=record.max_risk_rupees or self._max_risk,
        )
        if ra.degenerate:
            _LOG.warning("post_fill_reanchor trade_id=%s %s", record.agent_trade_id, ra.message)
            return

        # Apply to the live protection orders (best-effort — never break the poll).
        try:
            if record.sl_order_id:
                await self._broker.modify_order(record.sl_order_id, {"trigger_price": ra.sl})
            if record.target_order_id:
                await self._broker.modify_order(record.target_order_id, {"price": ra.target})
        except Exception as exc:
            _LOG.warning("reanchor_apply_failed trade_id=%s err=%s", record.agent_trade_id, exc)
        record.sl_price = ra.sl
        record.target_price = ra.target
        # Write back to DB so the dashboard reflects the reanchored levels, not the
        # stale signal levels (which can appear above entry for a low-fill LONG).
        await self._persist_reanchor(record.agent_trade_id, ra.sl, ra.target)

        if ra.cap_breached:
            msg = f"{ra.incident_code}: {ra.message}"
            _LOG.warning("post_fill_risk trade_id=%s %s", record.agent_trade_id, msg)
            if self._alert_manager is not None:
                from src.infrastructure.alert_system import AlertTier

                self._alert_manager.raise_alert(
                    AlertTier.CRITICAL, msg, source=record.agent_trade_id
                )
        else:
            _LOG.info("post_fill_reanchor trade_id=%s %s", record.agent_trade_id, ra.message)

    def close_trade(self, agent_trade_id: str) -> None:
        """Mark a FILLED trade as CLOSED (exit executed, P&L booked)."""
        self.transition(agent_trade_id, OrderState.CLOSED)

    def _record_slippage(self, record: TradeRecord) -> None:
        """Record fill slippage. Expected price = order limit/trigger price; actual = fill_price."""
        from src.monitoring.slippage_tracker import ADVBucket, OrderType, TimeOfDay

        try:
            expected = float(record.order_request.price or record.fill_price)
            if expected == 0:
                return
            now_ist = datetime.now(_IST)
            h = now_ist.hour * 60 + now_ist.minute
            if h < 9 * 60 + 30 or h < 10 * 60 + 30:
                tod = TimeOfDay.MORNING
            elif h < 12 * 60:
                tod = TimeOfDay.MID_MORNING
            elif h < 13 * 60 + 30:
                tod = TimeOfDay.MIDDAY
            elif h < 14 * 60 + 30:
                tod = TimeOfDay.AFTERNOON
            else:
                tod = TimeOfDay.PRE_CLOSE
            order_type_str = str(getattr(record.order_request, "order_type", "MARKET")).upper()
            try:
                ot = OrderType(order_type_str)
            except ValueError:
                ot = OrderType.MARKET
            self.slippage_tracker.record_fill(
                order_id=record.agent_trade_id,
                symbol=str(record.order_request.symbol),
                order_type=ot,
                expected_price=expected,
                fill_price=record.fill_price,
                time_of_day=tod,
                adv_bucket=ADVBucket.MID,
            )
        except Exception:
            pass  # slippage tracking is non-critical

    def get_trade(self, agent_trade_id: str) -> TradeRecord:
        return self._get_or_raise(agent_trade_id)

    def get_open_trades(self) -> list[TradeRecord]:
        """Return all trades in non-terminal, non-pending states."""
        open_states = {OrderState.SUBMITTED, OrderState.OPEN, OrderState.PARTIAL}
        return [r for r in self._trades.values() if r.state in open_states]

    def get_all_trades(self) -> list[TradeRecord]:
        return list(self._trades.values())

    async def place_entry_and_protection(
        self,
        signal_id: str,
        symbol: str,
        direction: str,
        qty: int,
        entry_price: float,
        sl_price: float,
        target_price: float,
        product: str = "INTRADAY",
        exchange: str = "NSE",
        order_types: dict[str, str] | None = None,
        trade_id: str | None = None,
        meta: dict[str, Any] | None = None,
        defer_protection: bool = False,
    ) -> tuple[str, str, str]:
        """Place entry + SL + target atomically. SL is ALWAYS placed — no conditions.

        Returns (entry_order_id, sl_order_id, target_order_id).
        If SL placement fails the exception propagates — never silently skipped.

        ``trade_id`` may be supplied so the caller can subsequently ``poll(trade_id)``
        (used by the synchronous-fill paper path); when None a fresh id is generated.
        """
        import uuid

        _LOG.debug(
            "place_entry_and_protection_received signal=%s symbol=%s direction=%s qty=%s "
            "entry=%.2f sl=%.2f target=%.2f product=%s defer_protection=%s",
            signal_id,
            symbol,
            direction,
            qty,
            entry_price,
            sl_price,
            target_price,
            product,
            defer_protection,
        )
        # Callers (e.g. the swing scan) may pass an "EXCHANGE:SYMBOL" string built for
        # LTP/quote lookups. Kite's place_order takes tradingsymbol and exchange as
        # separate fields — sending the compound string as tradingsymbol gets every
        # order rejected with "instrument ... does not exist" (2026-09: CAPLIPOINT,
        # CLEAN, BEML all failed live this way). Normalize once here so every broker
        # call below (entry, SL, target, GTT) gets the bare symbol.
        symbol = symbol.split(":")[-1]

        if trade_id is None:
            trade_id = f"TRD-{uuid.uuid4().hex[:12].upper()}"
        exit_direction = "SELL" if direction == "BUY" else "BUY"

        _ot = order_types or {}
        is_intraday = product.upper() in ("MIS", "INTRADAY")
        # Marketable limit, not MARKET: caps entry slippage at
        # _INTRADAY_ENTRY_CHASE_CAP_PCT instead of accepting whatever price a
        # fast-moving tape hands back. Unfilled orders are cancelled by the
        # caller's cancel_entry_if_unfilled shortly after (chase avoidance,
        # not a real order left resting on the book).
        _entry_type = "LIMIT" if is_intraday else _ot.get("CNC_swing_entry", "LIMIT")
        _sl_type = (
            _ot.get("MIS_intraday", "SL-M") if is_intraday else _ot.get("CNC_swing_sl", "GTT")
        )
        _target_type = "LIMIT" if is_intraday else _ot.get("CNC_swing_target", "GTT")
        _entry_order_price = (
            _marketable_limit_price(entry_price, direction) if is_intraday else entry_price
        )
        # Refuse BEFORE the entry when its GTT protection cannot exist on this broker:
        # otherwise the entry fills first (deferred delivery path) and the position
        # sits naked until _place_protection fails.
        if "GTT" in (_sl_type.upper(), _target_type.upper()) and not self._broker.supports_gtt():
            _LOG.error(
                "entry_refused_no_gtt trade_id=%s symbol=%s broker=%s",
                trade_id,
                symbol,
                type(self._broker).__name__,
            )
            raise RuntimeError(
                f"entry refused: order_types asks for GTT protection but "
                f"{type(self._broker).__name__} reports supports_gtt()=False — "
                "no order was placed"
            )

        entry_req = OrderRequest(
            symbol=symbol,
            exchange=exchange,
            direction=direction,
            quantity=qty,
            order_type=_entry_type,
            product=product,
            validity="DAY",
            agent_trade_id=trade_id,
            signal_id=signal_id,
            price=_entry_order_price,
        )
        intent = None
        if self._idempotency is not None:
            intent = await self._idempotency.begin(entry_req, is_paper=self._is_paper)
            if not intent.proceed:  # enforce mode + exact duplicate only
                from src.execution.idempotency import DuplicateOrderError

                raise DuplicateOrderError(
                    f"duplicate order {symbol} {direction} x{qty} for signal {signal_id} "
                    f"(prior {intent.duplicate_state} order {intent.duplicate_order_id})"
                )
        record = self.create_trade(entry_req)
        record.sl_price = sl_price
        record.direction = direction
        record.entry_price = entry_price
        record.target_price = target_price
        try:
            record.max_risk_rupees = float((meta or {}).get("max_risk_rupees") or 0.0)
        except (TypeError, ValueError):
            record.max_risk_rupees = 0.0
        try:
            entry_order_id = await self._broker.place_order(entry_req)
        except Exception:
            if intent is not None:
                await self._idempotency.failed(intent)
            raise
        if intent is not None:
            await self._idempotency.submitted(intent, entry_order_id)
        record.broker_order_id = entry_order_id
        self.transition(trade_id, OrderState.SUBMITTED)
        await self._persist_entry(
            trade_id,
            entry_order_id,
            symbol,
            exchange,
            direction,
            qty,
            entry_price,
            sl_price,
            target_price,
            product,
            meta=meta,
            signal_ref=signal_id,
        )

        if defer_protection:
            # Delivery: do not arm SL/target until the entry actually fills. Arming them
            # up front sells stock not yet owned, and leaves both legs resting forever if
            # the entry never fills. place_entry_protection_and_poll arms them post-fill.
            record.defer_protection = True
            record.protection_ctx = {
                "signal_id": signal_id,
                "symbol": symbol,
                "exchange": exchange,
                "product": product,
                "order_types": dict(_ot),
            }
            _LOG.info(
                "entry_placed_protection_deferred trade_id=%s symbol=%s qty=%d",
                trade_id,
                symbol,
                qty,
            )
            return entry_order_id, "", ""

        try:
            sl_order_id, target_order_id, gtt_id = await self._place_protection(
                symbol=symbol,
                exchange=exchange,
                product=product,
                qty=qty,
                exit_direction=exit_direction,
                sl_price=sl_price,
                target_price=target_price,
                sl_type=_sl_type,
                target_type=_target_type,
                signal_id=signal_id,
                tid=trade_id,
                gtt_sl_leg_type=_ot.get("CNC_swing_gtt_sl_leg", _GTT_LEG_ORDER_TYPE),
            )
        except Exception as exc:
            # SL or target placement failed AFTER the entry was already persisted as
            # SUBMITTED. Cancel the entry (best-effort) and mark the DB row REJECTED so
            # it never orphans as a stale SUBMITTED trade. Re-raise so the caller logs it.
            _LOG.error(
                "protection_placement_failed trade_id=%s err=%s — cancelling entry",
                trade_id,
                exc,
            )
            import contextlib

            with contextlib.suppress(Exception):
                await self._broker.cancel_order(entry_order_id)
            # The cancel result proves nothing: a marketable entry has usually filled already
            # (Kite refuses the cancel), so writing it off would leave a real position
            # unmanaged. Only a broker-confirmed zero fill may be written off.
            try:
                _entry = await self._broker.get_order_status(entry_order_id)
            except Exception as status_exc:
                _LOG.error("entry_status_unreadable trade_id=%s err=%s", trade_id, status_exc)
                _entry = None
            if (
                _entry is not None
                and _entry.status in ("CANCELLED", "REJECTED")
                and not _entry.filled_quantity
            ):
                await self._persist_rejected(trade_id)
                raise
            _LOG.critical(
                "position_open_no_broker_stop trade_id=%s symbol=%s err=%s",
                trade_id,
                symbol,
                exc,
            )
            _notify_unprotected(
                trade_id,
                symbol,
                is_paper=self._is_paper,
                product=product,
                reason=f"Stop refused by broker: {str(exc)[:160]}. Software stop active, retrying.",
            )
            return entry_order_id, "", ""
        record.sl_order_id = sl_order_id
        record.target_order_id = target_order_id
        record.gtt_sl_id = gtt_id
        await self._persist_protection_ids(record)

        _LOG.info(
            "entry_and_protection_placed",
            extra={
                "trade_id": trade_id,
                "symbol": symbol,
                "qty": qty,
                "entry": entry_price,
                "sl": sl_price,
                "target": target_price,
                "gtt_id": gtt_id,
            },
        )
        return entry_order_id, sl_order_id, target_order_id

    async def place_entry_protection_and_poll(
        self,
        signal_id: str,
        symbol: str,
        direction: str,
        qty: int,
        entry_price: float,
        sl_price: float,
        target_price: float,
        product: str = "INTRADAY",
        exchange: str = "NSE",
        order_types: dict[str, str] | None = None,
        meta: dict[str, Any] | None = None,
        defer_protection: bool = False,
    ) -> TradeRecord:
        """Place entry + protection, then immediately poll once to realize the fill.

        For synchronous-fill adapters (the paper adapter fills against live LTP at
        placement) this drives the FILLED branch — recording the fill and re-anchoring
        SL/target to the actual fill — in one call, matching the legacy paper engine's
        synchronous reanchor. Returns the resulting TradeRecord.
        """
        import uuid

        # Normalized here too (not just in place_entry_and_protection) because the
        # deferred-protection branch below calls _arm_deferred_protection directly
        # with this function's own symbol param, not place_entry_and_protection's.
        symbol = symbol.split(":")[-1]

        trade_id = f"TRD-{uuid.uuid4().hex[:12].upper()}"
        await self.place_entry_and_protection(
            signal_id=signal_id,
            symbol=symbol,
            direction=direction,
            qty=qty,
            entry_price=entry_price,
            sl_price=sl_price,
            target_price=target_price,
            product=product,
            exchange=exchange,
            order_types=order_types,
            trade_id=trade_id,
            meta=meta,
            defer_protection=defer_protection,
        )
        # The state machine expects SUBMITTED → OPEN → FILLED; the paper adapter
        # fills synchronously and reports FILLED straight away, skipping OPEN. Bridge
        # the intermediate OPEN here so the single poll below can advance to FILLED
        # (and run the fill/reanchor branch). Local to the paper path — the shared
        # transition table and the live path are untouched.
        rec = self._get_or_raise(trade_id)
        if rec.state == OrderState.SUBMITTED:
            self.transition(trade_id, OrderState.OPEN)
        rec = await self.poll(trade_id)
        if defer_protection and rec.state == OrderState.FILLED:
            # poll() already re-anchored sl_price/target_price to the real fill, so the
            # protection goes on at the correct levels first time — no place-then-modify.
            await self._arm_deferred_protection(
                rec,
                signal_id=signal_id,
                symbol=symbol,
                exchange=exchange,
                product=product,
                order_types=order_types,
            )
        return rec

    async def _place_protection(
        self,
        *,
        symbol: str,
        exchange: str,
        product: str,
        qty: int,
        exit_direction: str,
        sl_price: float,
        target_price: float,
        sl_type: str,
        target_type: str,
        signal_id: str,
        tid: str,
        gtt_sl_leg_type: str,
    ) -> tuple[str, str, str]:
        """Place the SL and target legs. Returns (sl_order_id, target_order_id, gtt_id).

        An order_type of "GTT" (config/execution.json order_types, used for CNC swing)
        goes through the broker's native GTT API as ONE two-leg OCO trigger — a GTT is
        inherently OCO, so both exit legs live under a single trigger id and that id is
        returned as gtt_id with both plain order ids left empty.

        Until 2026-08-28 "GTT" was handed to place_order() as an order_type instead.
        Kite's order API accepts only MARKET/LIMIT/SL/SL-M, so every swing SL and target
        was rejected at the broker and no delivery position ever carried protection.
        """
        if "GTT" in (sl_type.upper(), target_type.upper()):
            if not self._broker.supports_gtt():
                raise RuntimeError(
                    f"order_types asks for GTT protection but {type(self._broker).__name__} "
                    "reports supports_gtt()=False — refusing to leave the position naked"
                )
            from src.brokers.base import GTTRequest

            gtt_id = await self._broker.place_gtt(
                GTTRequest(
                    symbol=symbol,
                    product=product,
                    gtt_type="TWO_LEG",
                    quantity=qty,
                    agent_trade_id=tid,
                    sl_trigger=sl_price,
                    sl_order_type=gtt_sl_leg_type,
                    sl_price=sl_price,
                    target_trigger=target_price,
                    target_price=target_price,
                )
            )
            return "", "", str(gtt_id)

        sl_req = OrderRequest(
            symbol=symbol,
            exchange=exchange,
            direction=exit_direction,
            quantity=qty,
            order_type=sl_type,
            product=product,
            validity="DAY",
            agent_trade_id=f"{tid}-SL",
            signal_id=signal_id,
            trigger_price=sl_price,
        )
        target_req = OrderRequest(
            symbol=symbol,
            exchange=exchange,
            direction=exit_direction,
            quantity=qty,
            order_type=target_type,
            product=product,
            validity="DAY",
            agent_trade_id=f"{tid}-TGT",
            signal_id=signal_id,
            price=target_price,
        )
        sl_order_id = await self._place_leg_with_retry(sl_req)  # SL. Always. No if.
        try:
            target_order_id = await self._place_leg_with_retry(target_req)
        except Exception as exc:
            # Raising here would lose the id of a stop that IS resting at the broker. The
            # monitor's software target still manages the exit.
            _LOG.error("target_placement_failed tid=%s err=%s", tid, exc)
            target_order_id = ""
        return sl_order_id, target_order_id, ""

    async def _place_leg_with_retry(self, order_req: OrderRequest) -> str:
        """Place one order leg, auto-retrying transient rejections.

        With no RejectionHandler configured this is exactly
        `self._broker.place_order(order_req)` — unchanged behaviour. With one
        configured, a rejection is classified and retried in place for the
        cases RejectionHandler knows how to recover (reduced quantity on
        margin shortfall, a wait-then-retry on a stale token); anything else
        — including exhausted retries — re-raises so the caller's existing
        except block (CRITICAL alert, "POSITION UNPROTECTED") runs exactly as
        it did before this method existed.
        """
        if self._rejection_handler is None:
            return await self._broker.place_order(order_req)

        from src.execution.rejection_handler import RejectionType

        while True:
            try:
                return await self._broker.place_order(order_req)
            except Exception as exc:
                result = self._rejection_handler.classify(
                    order_req.agent_trade_id, str(exc), order_req.quantity, order_req.product
                )
                if not result.should_retry:
                    raise
                if result.rejection_type == RejectionType.PRODUCT_TYPE_ERROR:
                    # RejectionHandler's suggested_product assumes a
                    # DELIVERY/INTRADAY naming pair; SL/target legs in this
                    # codebase carry CNC/MIS product strings instead, so
                    # applying the suggestion could build a broker-invalid
                    # product. Don't guess — fall back to the existing
                    # escalation path unchanged.
                    raise
                _LOG.warning(
                    "order_leg_rejection_retry tid=%s type=%s reason=%s",
                    order_req.agent_trade_id,
                    result.rejection_type.value,
                    exc,
                )
                if result.retry_after_seconds:
                    await asyncio.sleep(result.retry_after_seconds)
                if (
                    result.suggested_quantity is not None
                    and result.suggested_quantity != order_req.quantity
                ):
                    order_req = replace(order_req, quantity=result.suggested_quantity)

    async def _arm_deferred_protection(
        self,
        record: TradeRecord,
        *,
        signal_id: str,
        symbol: str,
        exchange: str,
        product: str,
        order_types: dict[str, str] | None,
    ) -> None:
        """Place SL + target for a delivery entry that has now filled."""
        _ot = order_types or {}
        exit_direction = "SELL" if (record.direction or "BUY").upper() == "BUY" else "BUY"
        qty = int(record.filled_quantity or record.order_request.quantity)
        tid = record.agent_trade_id
        try:
            (
                record.sl_order_id,
                record.target_order_id,
                record.gtt_sl_id,
            ) = await self._place_protection(
                symbol=symbol,
                exchange=exchange,
                product=product,
                qty=qty,
                exit_direction=exit_direction,
                sl_price=record.sl_price,
                target_price=record.target_price,
                sl_type=_ot.get("CNC_swing_sl", "GTT"),
                target_type=_ot.get("CNC_swing_target", "GTT"),
                signal_id=signal_id,
                tid=tid,
                gtt_sl_leg_type=_ot.get("CNC_swing_gtt_sl_leg", _GTT_LEG_ORDER_TYPE),
            )
        except Exception as exc:
            # The position is real and unprotected — loudest possible signal, but never
            # raise: swallowing the fill would orphan it from monitoring entirely.
            _LOG.critical(
                "deferred_protection_failed trade_id=%s sym=%s err=%s — POSITION UNPROTECTED",
                tid,
                symbol,
                exc,
            )
            if self._alert_manager is not None:
                from src.infrastructure.alert_system import AlertTier

                self._alert_manager.raise_alert(
                    AlertTier.CRITICAL,
                    f"deferred_protection_failed {symbol}: position unprotected",
                    source=tid,
                )
            return
        await self._persist_protection_ids(record)
        if self._alert_manager is not None:
            self._alert_manager.resolve_by_source(tid)
        _LOG.info(
            "deferred_protection_armed trade_id=%s sym=%s sl=%.2f target=%.2f gtt=%s",
            tid,
            symbol,
            record.sl_price,
            record.target_price,
            record.gtt_sl_id or "-",
        )

    async def _persist_protection_ids(self, record: TradeRecord) -> None:
        """Write the exit-leg ids to the DB row.

        gtt_sl_id/gtt_target_id have existed as columns since migration 018 and
        _reseed_order_monitor reads them on every restart, but nothing ever wrote
        them — so GTT trailing and the 30s GTT integrity check were permanently
        disabled for any position that outlived the process that opened it.
        """
        if self._trade_repo is None:
            return
        fields: dict[str, Any] = {}
        if record.gtt_sl_id:
            # One OCO trigger covers both legs, so both columns carry the same id.
            fields["gtt_sl_id"] = record.gtt_sl_id
            fields["gtt_target_id"] = record.gtt_sl_id
        if record.sl_order_id:
            fields["sl_order_id"] = record.sl_order_id
        if record.target_order_id:
            fields["target_order_id"] = record.target_order_id
        if not fields:
            return
        try:
            await self._trade_repo.update_state(
                record.agent_trade_id, "FILLED", is_paper=self._is_paper, **fields
            )
        except Exception as exc:
            _LOG.error(
                "protection_ids_persist_failed trade_id=%s err=%s", record.agent_trade_id, exc
            )

    def _get_or_raise(self, agent_trade_id: str) -> TradeRecord:
        if agent_trade_id not in self._trades:
            raise KeyError(f"Trade {agent_trade_id!r} not found")
        return self._trades[agent_trade_id]

    @staticmethod
    def _db_direction(direction: str) -> str:
        """Map broker BUY/SELL to the trades.direction CHECK domain (long/short)."""
        return "long" if direction.upper() == "BUY" else "short"

    async def _persist_entry(
        self,
        trade_id: str,
        broker_order_id: str,
        symbol: str,
        exchange: str,
        direction: str,
        qty: int,
        entry_price: float,
        sl_price: float,
        target_price: float,
        product: str,
        meta: dict[str, Any] | None = None,
        signal_ref: str | None = None,
    ) -> None:
        """INSERT the live trade row at entry. Best-effort — never blocks order placement."""
        if self._trade_repo is None:
            return
        # Warn loudly if the caller forgot required dashboard columns.  This fires
        # at runtime rather than silently leaving NULL values that show as blank tabs.
        if meta is not None:
            from src.execution.trade_meta import TradeMetadata

            _missing = TradeMetadata(meta).missing_fields()
            if _missing:
                _LOG.warning(
                    "trade_meta_incomplete trade_id=%s missing=%s — "
                    "strategy/regime will be blank in dashboard; use build_trade_meta()",
                    trade_id,
                    sorted(_missing),
                )
        try:
            row: dict[str, Any] = {
                "agent_trade_id": trade_id,
                "broker_order_id": broker_order_id,
                "symbol": symbol,
                "exchange": exchange,
                "direction": self._db_direction(direction),
                "quantity": qty,
                "product": product,
                "order_state": "SUBMITTED",
                "intended_price": Decimal(str(entry_price)),
                "initial_sl": Decimal(str(sl_price)),
                "current_sl": Decimal(str(sl_price)),
                "initial_target": Decimal(str(target_price)),
                "current_target": Decimal(str(target_price)),
            }
            if signal_ref:  # links the trade to its `signals` row (view signal_outcomes)
                row["signal_ref"] = signal_ref
            # G9 advisory LLM verdict + strategy context — all sourced from TradeMetadata.
            if meta is not None:
                _conf = meta.get("llm_confidence")
                if _conf is not None:
                    row["llm_confidence"] = Decimal(str(round(float(_conf), 2)))
                # Origination-vs-reviewer disagreement (Phase 1, observe only — see
                # migrations/085_llm_origination_gap.sql). Only set for LLM-originated
                # strategies; NULL for everything else.
                _orig_conf = meta.get("llm_origination_confidence")
                if _orig_conf is not None:
                    row["llm_origination_confidence"] = Decimal(str(round(float(_orig_conf), 2)))
                _gap = meta.get("llm_confidence_gap")
                if _gap is not None:
                    row["llm_confidence_gap"] = Decimal(str(round(float(_gap), 2)))
                row["llm_flag"] = meta.get("llm_flag")
                row["llm_reason"] = meta.get("llm_reason")
                if meta.get("strategy_name"):
                    row["strategy_name"] = meta["strategy_name"]
                if meta.get("regime_at_entry"):
                    row["regime_at_entry"] = meta["regime_at_entry"]
                _sq = meta.get("setup_quality_score")
                if _sq is not None:
                    row["setup_quality_score"] = Decimal(str(_sq))
            if self._broker_name and not self._is_paper:  # paper rows never hold a broker
                row["exec_broker"] = self._broker_name
            await self._trade_repo.insert(row, is_paper=self._is_paper)
        except Exception as exc:
            _LOG.error("trade_entry_persist_failed trade_id=%s err=%s", trade_id, exc)

    async def _persist_fill(self, record: TradeRecord) -> None:
        """UPDATE the live trade row with the actual fill price. Best-effort."""
        if self._trade_repo is None:
            return
        try:
            await self._trade_repo.update_fill(
                record.agent_trade_id,
                Decimal(str(record.fill_price)),
                datetime.now(_IST),
                is_paper=self._is_paper,
            )
        except Exception as exc:
            _LOG.error("trade_fill_persist_failed trade_id=%s err=%s", record.agent_trade_id, exc)

    async def _persist_reanchor(self, agent_trade_id: str, sl: float, target: float) -> None:
        """Write reanchored current_sl + current_target back to the DB row.

        _reanchor_protection_on_fill updates the in-memory TradeRecord but never
        persisted the new levels, so the dashboard showed the stale signal SL/target
        (which could be above entry for a LONG after a low fill, making it look wrong).
        """
        if self._trade_repo is None:
            return
        try:
            await self._trade_repo.update_state(
                agent_trade_id,
                "FILLED",
                is_paper=self._is_paper,
                current_sl=Decimal(str(round(sl, 2))),
                current_target=Decimal(str(round(target, 2))),
            )
        except Exception as exc:
            _LOG.error("trade_reanchor_persist_failed trade_id=%s err=%s", agent_trade_id, exc)

    async def _persist_terminal(self, agent_trade_id: str, state: str) -> None:
        """Write a terminal order_state to the DB row. Best-effort — a persist failure
        is logged, never raised into a background cancel task."""
        if self._trade_repo is None:
            return
        try:
            await self._trade_repo.update_state(agent_trade_id, state, is_paper=self._is_paper)
        except Exception as exc:
            _LOG.error(
                "trade_terminal_persist_failed trade_id=%s state=%s err=%s",
                agent_trade_id,
                state,
                exc,
            )

    async def _persist_rejected(
        self, agent_trade_id: str, note: str = "protection placement failed"
    ) -> None:
        """Mark a DB row REJECTED. Default note covers this method's original
        caller (protection placement failing after entry is persisted);
        cancel_protection() passes a more specific note for its other callers."""
        await self._persist_terminal(agent_trade_id, "REJECTED")
        record = self._trades.get(agent_trade_id)
        try:
            notify(
                EventKind.ENTRY_REJECTED,
                is_paper=self._is_paper,
                agent_trade_id=agent_trade_id,
                symbol=record.order_request.symbol if record else "",
                segment=segment_for_product(record.order_request.product) if record else "",
                dedupe_key=f"reject:{agent_trade_id}",
                note=note,
            )
        except Exception:  # notify() must never fail the trade path
            pass

    async def cancel_protection(self, agent_trade_id: str, note: str = "fill rejected") -> None:
        """Cancel the SL and target orders for an entry that is being rejected
        (e.g. fill drifted too far from signal entry, or the broker rejected the
        order outright before/without a fill). Marks the trade REJECTED in DB."""
        record = self._trades.get(agent_trade_id)
        if record is None:
            return
        for order_id, label in [
            (record.sl_order_id, "sl"),
            (record.target_order_id, "target"),
        ]:
            if order_id:
                try:
                    await self._broker.cancel_order(order_id)
                except Exception as exc:
                    _LOG.warning(
                        "cancel_protection_%s_failed tid=%s err=%s", label, agent_trade_id, exc
                    )
        # GTT protection is not an order — cancel_order cannot reach it, and a GTT left
        # behind here would sit at the exchange firing against a position that no
        # longer exists.
        if record.gtt_sl_id:
            try:
                await self._broker.cancel_gtt(record.gtt_sl_id)
            except Exception as exc:
                _LOG.warning(
                    "cancel_protection_gtt_failed tid=%s gtt=%s err=%s",
                    agent_trade_id,
                    record.gtt_sl_id,
                    exc,
                )
        self.transition(agent_trade_id, OrderState.REJECTED)
        await self._persist_rejected(agent_trade_id, note=note)
        _LOG.info(
            "cancel_protection_done tid=%s sym=%s", agent_trade_id, record.order_request.symbol
        )

    async def cancel_entry_if_unfilled(
        self, agent_trade_id: str, timeout_secs: float = 30.0
    ) -> None:
        """Background task: cancel the entry order if it is still SUBMITTED/OPEN after
        timeout_secs. Prevents zombie orders from staying live on the broker when the
        paper/live fill poll returns non-FILLED on the first check.

        Cancels the SL and target legs alongside the entry — they were placed before the
        fill was known and would otherwise rest against a position that never opened
        (naked exit orders, in live).

        2026-08-25: this used to transition to REJECTED, which is not reachable from
        OPEN — and place_entry_protection_and_poll always leaves the record OPEN. The
        InvalidTransitionError was swallowed by the fire-and-forget task that calls
        this, so _persist_* never ran and the DB row stayed SUBMITTED forever. CANCELLED
        is the honest terminal state here and is reachable from both SUBMITTED and OPEN.
        """
        import asyncio

        await asyncio.sleep(timeout_secs)
        record = self._trades.get(agent_trade_id)
        if record is None or record.state not in (OrderState.SUBMITTED, OrderState.OPEN):
            return
        for order_id, label in (
            (record.broker_order_id, "entry"),
            (record.sl_order_id, "sl"),
            (record.target_order_id, "target"),
        ):
            if not order_id:
                continue
            try:
                await self._broker.cancel_order(order_id)
            except Exception as exc:
                _LOG.warning(
                    "cancel_entry_timeout_%s_failed tid=%s err=%s", label, agent_trade_id, exc
                )
        self._pending_entry_ids.discard(agent_trade_id)
        # Persist even if the in-memory transition is refused — an unpersisted terminal
        # state is exactly the orphan this method exists to prevent.
        try:
            self.transition(agent_trade_id, OrderState.CANCELLED)
        except InvalidTransitionError as exc:
            _LOG.error("cancel_entry_transition_refused tid=%s err=%s", agent_trade_id, exc)
        await self._persist_terminal(agent_trade_id, "CANCELLED")
        _LOG.info(
            "entry_timed_out_cancelled tid=%s sym=%s after=%.0fs",
            agent_trade_id,
            record.order_request.symbol,
            timeout_secs,
        )

    # ── Pending entry re-check ────────────────────────────────────────────────
    # A resting entry order used to be checked for a fill exactly once, in the single
    # poll() inside place_entry_protection_and_poll. OrderMonitor.cycle() only ever
    # iterates filled positions, so nothing revisited an unfilled entry — it simply
    # stopped existing as far as the system was concerned. These two methods give the
    # monitor loop something to drive every cycle.

    def track_pending_entry(self, agent_trade_id: str) -> None:
        """Register an unfilled entry for per-cycle re-polling."""
        self._pending_entry_ids.add(agent_trade_id)

    def pending_entry_ids(self) -> set[str]:
        return set(self._pending_entry_ids)

    async def poll_pending_entries(
        self,
        repeg_after_secs: float = 4.0,
        max_repegs: int = 1,
        max_chase_r: float = 0.75,
    ) -> list[str]:
        """Re-poll every resting entry once. Returns the ids that filled this cycle.

        An entry still unfilled after `repeg_after_secs` is re-pegged toward the current
        LTP at most `max_repegs` times, and only while that price stays within
        `max_chase_r` × initial risk of the signal entry. Beyond that the trade is
        missed, not chased — the whole point of the marketable-limit entry.

        Filled and terminal entries are dropped from the registry. Errors on one entry
        never stop the others — this runs on the 2s monitor loop.
        """
        newly_filled: list[str] = []
        for tid in sorted(self._pending_entry_ids):
            record = self._trades.get(tid)
            if record is None or record.is_terminal:
                self._pending_entry_ids.discard(tid)
                continue
            try:
                record = await self.poll(tid)
            except Exception as exc:
                if "not found in today's order book" in str(exc).lower():
                    # Kite's order lookup only ever covers the current trading
                    # session — an order this old has aged out of it and this
                    # will NEVER resolve on any future poll, unlike a transient
                    # network/rate-limit error (found 2026-09-08:
                    # TRD-5F93A906747E, still SUBMITTED/never filled, polled
                    # every ~2s forever with this exact error). It never
                    # filled — filled_quantity stays 0 — so there is no
                    # position to protect or exit; stop tracking it and say so
                    # once instead of retrying a call that can't succeed.
                    self._pending_entry_ids.discard(tid)
                    try:
                        self.transition(tid, OrderState.CANCELLED)
                    except InvalidTransitionError as _te:
                        _LOG.error("stale_pending_entry_transition_refused tid=%s err=%s", tid, _te)
                    await self._persist_terminal(tid, "CANCELLED")
                    _LOG.critical(
                        "pending_entry_abandoned tid=%s sym=%s reason=order_unknown_to_broker "
                        "err=%s",
                        tid,
                        record.order_request.symbol,
                        exc,
                    )
                    if self._alert_manager is not None:
                        from src.infrastructure.alert_system import AlertTier

                        # This describes a completed action, not an ongoing problem —
                        # by the time it's raised, the order is already cancelled and
                        # there was never a position to protect. Nothing will ever
                        # come along to "resolve" it later (unlike a missing-SL or
                        # ghost-trade alert, there's no recovered state to detect), so
                        # it must resolve itself immediately or it sits active forever
                        # needing a human to manually clear a problem that isn't one
                        # anymore (found 2026-09-08).
                        _alert = self._alert_manager.raise_alert(
                            AlertTier.CRITICAL,
                            f"Entry order abandoned — broker no longer recognizes it, "
                            f"never filled: {tid} ({record.order_request.symbol})",
                            source=tid,
                        )
                        self._alert_manager.resolve(_alert.id)
                    continue
                _LOG.warning("pending_entry_poll_failed tid=%s err=%s", tid, exc)
                continue
            if record.state == OrderState.FILLED:
                self._pending_entry_ids.discard(tid)
                newly_filled.append(tid)
                # A delivery entry carries no protection until it fills, and this is
                # where a resting AMO's fill is detected. Without arming here the
                # position would go live with no stop at all.
                if record.defer_protection and not (record.sl_order_id or record.gtt_sl_id):
                    await self._arm_deferred_protection(record, **record.protection_ctx)
                _LOG.info(
                    "pending_entry_filled tid=%s sym=%s fill=%.2f",
                    tid,
                    record.order_request.symbol,
                    record.fill_price,
                )
                continue
            if record.is_terminal:
                self._pending_entry_ids.discard(tid)
                continue
            if record.repeg_count < max_repegs:
                _resting = (datetime.now(_IST) - record.last_transition_at).total_seconds()
                if _resting >= repeg_after_secs:
                    await self._repeg_pending_entry(record, max_chase_r)
        return newly_filled

    async def _repeg_pending_entry(self, record: TradeRecord, max_chase_r: float) -> None:
        """Move a resting entry limit to the current LTP, if that stays inside the
        risk-based chase allowance. Best-effort — never raises into the monitor loop."""
        symbol = record.order_request.symbol
        risk = abs(record.entry_price - record.sl_price)
        if risk <= 0 or not record.entry_price:
            return
        try:
            ltp = float((await self._broker.get_ltp([symbol])).get(symbol, 0.0))
        except Exception as exc:
            _LOG.warning("repeg_ltp_failed tid=%s err=%s", record.agent_trade_id, exc)
            return
        if not ltp:
            return
        # Drift measured in the direction that costs money: up for a BUY, down for a SELL.
        drift = ltp - record.entry_price
        if record.direction.upper() == "SELL":
            drift = -drift
        allowance = max_chase_r * risk
        if drift > allowance:
            _LOG.info(
                "pending_entry_repeg_skipped tid=%s sym=%s ltp=%.2f drift_r=%.2f max_r=%.2f",
                record.agent_trade_id,
                symbol,
                ltp,
                drift / risk,
                max_chase_r,
            )
            return
        try:
            await self._broker.modify_order(record.broker_order_id, {"price": ltp})
        except Exception as exc:
            # A failed attempt still counts against max_repegs — otherwise a
            # terminal broker rejection (e.g. Kite's "Maximum allowed order
            # modifications exceeded") is retried with the exact same doomed
            # call every cycle forever, since the counter below never advances.
            record.repeg_count += 1
            _LOG.warning(
                "repeg_modify_failed tid=%s attempt=%d err=%s",
                record.agent_trade_id,
                record.repeg_count,
                exc,
            )
            if self._alert_manager is not None:
                from src.infrastructure.alert_system import AlertTier

                self._alert_manager.raise_alert(
                    AlertTier.CRITICAL,
                    f"repeg_modify_failed {symbol}: {exc}",
                    source=record.agent_trade_id,
                )
            return
        record.repeg_count += 1
        record.order_request.price = ltp
        if self._alert_manager is not None:
            self._alert_manager.resolve_by_source(record.agent_trade_id)
        _LOG.info(
            "pending_entry_repegged tid=%s sym=%s new_price=%.2f attempt=%d drift_r=%.2f",
            record.agent_trade_id,
            symbol,
            ltp,
            record.repeg_count,
            drift / risk,
        )


def _notify_unprotected(
    trade_id: str, symbol: str, *, is_paper: bool, product: str, reason: str, title: str = ""
) -> None:
    """Telegram + Alerts tab: a position needs the operator's attention. Never raises."""
    try:
        notify(
            EventKind.STRATEGY_ERROR,
            is_paper=is_paper,
            agent_trade_id=trade_id,
            symbol=symbol,
            segment=segment_for_product(product),
            dedupe_key=f"unprotected:{trade_id}:{title or 'open'}",
            title=title or f"POSITION OPEN, NO BROKER STOP: {symbol}",
            strategy=trade_id,
            reason=reason,
        )
    except Exception:  # noqa: BLE001 -- notify() must never fail the trade path
        pass


@dataclass
class MonitoredPosition:
    """Runtime view of a filled position being actively monitored."""

    agent_trade_id: str
    symbol: str
    direction: str
    quantity: int
    fill_price: float
    initial_sl: float
    current_sl: float
    initial_target: float
    current_target: float
    sl_order_id: str = ""
    target_order_id: str = ""
    gtt_sl_id: str = ""
    gtt_target_id: str = ""
    product: str = ""  # "MIS" | "CNC" — used by mis_flat_by enforcer
    strategy_name: str = ""  # carried through to _record_closed_trade for attribution/expectancy
    partial_pnl_booked: float = 0.0  # running realized P&L from book_partial() sales,
    # added to the final leg's net at close so pnl_realized = whole trade — DB source of truth (migration 060)
    partial_qty_booked: int = 0  # shares sold by book_partial(); original size = quantity + this
    partial_costs: dict[str, float] = field(
        default_factory=dict
    )  # itemised charges of those slices
    # Exit chase state (2026-08-20) — a resting re-peg exit order in flight,
    # shared by SL and target breaches since a position is only ever chasing
    # one exit at a time. chase_reason is "SL" or "TARGET".
    chase_order_id: str = ""
    chase_attempts: int = 0
    chase_reason: str = ""
    # Live exit in flight: closed only once the broker reports this order FILLED.
    exit_order_id: str = ""
    exit_reason: str = ""
    exit_fallback_price: float = 0.0
    exit_checks: int = 0
    exit_busy: bool = False  # an exit is cancelling legs; the stop check must not re-arm meanwhile
    last_stop_attempt: datetime | None = None
    stop_alerted: bool = False
    entry_ts: str = (
        ""  # IST ISO timestamp when position opened — used for LTCG vs STCG tax classification
    )
    last_gtt_check: datetime = field(default_factory=lambda: datetime.now(_IST))
    last_trail_update: datetime = field(default_factory=lambda: datetime.now(_IST))


class OrderMonitor:
    """
    Monitors filled positions every 2-3 seconds during market hours.

    Per docs/05-order-lifecycle.md Action Decision Tree:
    - TARGET_LIMIT_MISSED â†’ cancel limit, place MARKET immediately
    - SL gap/fast market â†’ emergency MARKET exit
    - GTT integrity check every 30s â†’ re-arm if missing
    - Price truth check delegated to PriceTruthEngine (called by caller)
    """

    def __init__(
        self,
        broker: BrokerInterface,
        alert_manager: AlertManager | None = None,
        mis_flat_by: str | None = None,
        is_paper: bool = False,
        breach_approaching_pct: float = _MONITORING_CFG["breach_approaching_pct"],
        gtt_check_interval_seconds: float = _MONITORING_CFG["gtt_check_interval_seconds"],
        chase_max_attempts: int = int(_MONITORING_CFG["chase_max_attempts"]),
        trade_repo: TradeRepository | None = None,
        broker_name: str | None = None,
    ) -> None:
        self._broker = broker
        self._alert_manager = alert_manager
        # Live broker of this stack: its rate card prices charges on partials/closes.
        # None (paper, legacy) = active_broker() as before.
        self._broker_name = None if is_paper else broker_name
        # Paper mode bounds gap-breach exit fills at SL ± slippage allowance
        # (polling detects breaches late; raw LTP exaggerates slippage vs GTT).
        self._is_paper = is_paper
        # DB truth for current_sl/current_target/quantity — without this, update_sl()
        # and book_partial() only ever touched the in-memory MonitoredPosition and the
        # broker-side order; the DB (what every dashboard endpoint reads) kept showing
        # the entry-time SL/target/qty forever, even after a real trail/partial fired
        # (found 2026-08-21: /api/swing_positions is documented "DB truth" but current_sl
        # never actually reached the DB once trailing was wired). Best-effort — a DB
        # write failure must never block the broker-side action it's recording.
        self._trade_repo = trade_repo
        # execution.json monitoring.* (2026-08-20, was hardcoded 0.005 / 30):
        self._breach_approaching_pct = breach_approaching_pct / 100.0
        self._chase_max_attempts = chase_max_attempts
        self._gtt_check_interval_seconds = gtt_check_interval_seconds
        self._positions: dict[str, MonitoredPosition] = {}
        # Pending (SUBMITTED/OPEN) entry orders registered before fill confirmation.
        # Maps agent_trade_id → submitted_at datetime (UTC). Cleared on fill/reject.
        self._pending_entries: dict[str, Any] = {}
        self.closed_trades: list[dict[str, object]] = []  # drained by orchestrator _monitor_loop
        # Display-only proximity/breach snapshots, recomputed each cycle() from the
        # same ltp_map; persisted to order_monitor_breaches by the orchestrator. These
        # never drive execution — execute_events() acts solely on cycle()'s return value.
        self.breach_snapshots: list[dict[str, object]] = []
        # None -> config/market_hours.json markets.nse_equity.mis_flat_by
        _flat = _sessions.hhmm(mis_flat_by) if mis_flat_by else _sessions.mis_flat_by()
        self._flatten_orders: dict[str, str] = {}  # symbol -> close-time sweep order id
        self._mis_flat_hour = _flat.hour
        self._mis_flat_minute = _flat.minute

    def track(self, pos: MonitoredPosition) -> None:
        """Register a filled position for monitoring."""
        self._positions[pos.agent_trade_id] = pos

    def untrack(self, agent_trade_id: str) -> None:
        self._positions.pop(agent_trade_id, None)
        self._pending_entries.pop(agent_trade_id, None)  # clean up if still pending

    def track_pending(self, agent_trade_id: str, submitted_at: Any) -> None:
        """Register a SUBMITTED entry order for housekeeping. Cleared when filled/rejected."""
        self._pending_entries[agent_trade_id] = submitted_at

    def resolve_pending(self, agent_trade_id: str) -> None:
        """Remove from pending registry once fill is confirmed or entry is rejected."""
        self._pending_entries.pop(agent_trade_id, None)

    def get_monitored(self) -> list[MonitoredPosition]:
        return list(self._positions.values())

    def get(self, agent_trade_id: str) -> MonitoredPosition | None:
        return self._positions.get(agent_trade_id)

    async def update_sl(self, agent_trade_id: str, new_sl: float) -> None:
        """Smart-exit SL ratchet (break-even / trail). Update the tracked SL and
        modify the live SL order. Adapter-agnostic — paper simulates the modify,
        live sends it to the broker. Best-effort; never raises into the scan loop."""
        pos = self._positions.get(agent_trade_id)
        if pos is None:
            return
        pos.current_sl = new_sl
        if pos.sl_order_id:
            try:
                await self._broker.modify_order(pos.sl_order_id, {"trigger_price": new_sl})
            except Exception as exc:
                _LOG.warning("update_sl_failed tid=%s err=%s", agent_trade_id, exc)
        # Swing protection is a GTT, and a ratchet that only moved a plain SL order left
        # the GTT sitting at the ENTRY stop for the life of the trade — the trail was
        # cosmetic on exactly the positions it matters most for (2026-08-27).
        if pos.gtt_sl_id:
            try:
                # sl_trigger only — the adapters rebuild the rest of the GTT spec.
                _new_id = await self._broker.modify_gtt(pos.gtt_sl_id, {"sl_trigger": new_sl})
                if _new_id:
                    pos.gtt_sl_id = str(_new_id)
                _LOG.info(
                    "gtt_sl_trailed tid=%s sym=%s gtt=%s new_sl=%.2f",
                    agent_trade_id,
                    pos.symbol,
                    pos.gtt_sl_id,
                    new_sl,
                )
            except Exception as exc:
                # The GTT is gone (paper GTTs are in-memory and die on every
                # restart), so there is nothing to modify. Re-arm at the NEW stop now
                # instead of leaving the position with no protection until the 30s
                # integrity check notices.
                try:
                    _rearmed = await self._arm_protection_gtt(pos)
                    pos.gtt_sl_id = pos.gtt_target_id = _rearmed
                    await self._persist_position_field(
                        agent_trade_id, gtt_sl_id=_rearmed, gtt_target_id=_rearmed
                    )
                    _LOG.warning(
                        "gtt_sl_trail_rearmed tid=%s gtt=%s new_sl=%.2f (modify failed: %s)",
                        agent_trade_id,
                        _rearmed,
                        new_sl,
                        exc,
                    )
                except Exception as exc2:
                    _LOG.error(
                        "gtt_sl_trail_failed tid=%s gtt=%s err=%s rearm_err=%s — GTT still at old stop",
                        agent_trade_id,
                        pos.gtt_sl_id,
                        exc,
                        exc2,
                    )
        await self._persist_position_field(
            agent_trade_id, current_sl=Decimal(str(round(new_sl, 2)))
        )
        # Throttler decides breakeven-vs-trail and materiality itself from these
        # fields — no call site of update_sl needs to change.
        try:
            notify(
                EventKind.MODIFY_SL,
                is_paper=self._is_paper,
                agent_trade_id=agent_trade_id,
                symbol=pos.symbol,
                segment=segment_for_product(pos.product),
                new_sl=new_sl,
                fill_price=pos.fill_price,
                initial_sl=pos.initial_sl,
                direction=pos.direction,
                quantity=pos.quantity,
            )
        except Exception:  # notify() must never fail the trade path
            pass

    async def book_partial(self, agent_trade_id: str, fraction: float, price: float) -> None:
        """Smart-exit partial book: market-exit `fraction` of the position, keep the
        rest under monitoring. Shared by paper + live."""
        pos = self._positions.get(agent_trade_id)
        if pos is None or pos.quantity <= 0 or pos.exit_order_id or pos.exit_busy:
            return
        if pos.quantity <= 1:
            # Can't split a single share — forcing a full close here would exit
            # the trade at the partial-book checkpoint instead of its actual
            # target/trail/reversal signal. Leave it fully open and let normal
            # exit management decide when to close.
            return
        qty = max(1, int(pos.quantity * fraction))
        if qty >= pos.quantity:
            await self.force_exit(agent_trade_id, price, "PARTIAL_FULL")
            return
        exit_dir = "SELL" if pos.direction.upper() == "BUY" else "BUY"
        mkt = OrderRequest(
            symbol=pos.symbol,
            exchange="NSE",
            direction=exit_dir,
            quantity=qty,
            order_type="MARKET",
            product=pos.product or "INTRADAY",
            validity="DAY",
            agent_trade_id=f"{pos.agent_trade_id}-PARTIAL",
            signal_id="",
        )
        try:
            _partial_oid = await self._broker.place_order(mkt)
        except Exception as exc:
            _LOG.warning("book_partial_failed tid=%s err=%s", agent_trade_id, exc)
            return
        if not self._is_paper:
            try:
                _o = await self._broker.get_order_status(_partial_oid)
                if _o.status == "FILLED" and _o.average_price:
                    price = float(_o.average_price)
            except Exception as exc:
                _LOG.warning("book_partial_fill_unread tid=%s err=%s", agent_trade_id, exc)
        pos.quantity -= qty
        pos.partial_qty_booked += qty
        # The GTT still rests for the ORIGINAL quantity. Left alone it would try to
        # sell more stock than is still held when it triggers — an oversell the
        # exchange rejects, taking the whole stop with it. Resize it to what remains.
        if pos.gtt_sl_id:
            try:
                _new_id = await self._broker.modify_gtt(pos.gtt_sl_id, {"quantity": pos.quantity})
                if _new_id:
                    pos.gtt_sl_id = str(_new_id)
            except Exception as exc:
                _LOG.error(
                    "gtt_resize_failed tid=%s gtt=%s remaining=%d err=%s — "
                    "GTT still sized for the full position",
                    agent_trade_id,
                    pos.gtt_sl_id,
                    pos.quantity,
                    exc,
                )
        await self._account_slice(pos, qty, price)

    async def _account_slice(self, pos: MonitoredPosition, qty: int, price: float) -> None:
        """Book, persist and announce shares already sold (`pos.quantity` already reduced)."""
        agent_trade_id = pos.agent_trade_id
        # Realized P&L for the slice just sold — book_partial() previously left this
        # completely unrecorded: the position stays open (order_state never becomes
        # CLOSED), so pnl_realized/update_pnl (close-only) never fire for it, and the
        # rupees actually banked by the partial exit had no record anywhere.
        from src.risk.real_pnl import CostBreakdown, ProductType, calculate_real_pnl

        try:
            product = ProductType.INTRADAY if pos.product.upper() == "MIS" else ProductType.DELIVERY
            slice_pnl = calculate_real_pnl(
                pos.symbol,
                pos.fill_price,
                price,
                qty,
                product,
                is_short=pos.direction.upper() == "SELL",
                broker=self._broker_name,
            )
            pos.partial_pnl_booked += slice_pnl.net_pnl
            pos.partial_costs = (
                CostBreakdown.from_dict(pos.partial_costs).add(slice_pnl.costs).as_dict()
            )
        except Exception as exc:
            _LOG.warning("book_partial_pnl_calc_failed tid=%s err=%s", agent_trade_id, exc)
        _LOG.info(
            "book_partial symbol=%s tid=%s qty=%d remaining=%d booked_pnl=%.2f",
            pos.symbol,
            agent_trade_id,
            qty,
            pos.quantity,
            pos.partial_pnl_booked,
        )
        await self._persist_position_field(
            agent_trade_id,
            quantity=pos.quantity,
            partial_pnl_booked=Decimal(str(round(pos.partial_pnl_booked, 2))),
            partial_qty_booked=pos.partial_qty_booked,
            last_partial_price=Decimal(str(round(price, 4))),
            charges_breakdown=json.dumps(pos.partial_costs),
        )
        try:
            notify(
                EventKind.PARTIAL_BOOK,
                is_paper=self._is_paper,
                agent_trade_id=agent_trade_id,
                symbol=pos.symbol,
                segment=segment_for_product(pos.product),
                booked_qty=qty,
                remaining_qty=pos.quantity,
                price=price,
                fill_price=pos.fill_price,
            )
        except Exception:  # notify() must never fail the trade path
            pass

    async def _persist_position_field(self, agent_trade_id: str, **fields: Any) -> None:
        """Write current_sl/current_target/quantity back to the DB row — the same
        table /api/positions, /api/swing_positions, and every dashboard trade table
        read as truth. Best-effort: a DB hiccup must never block the broker-side
        action (SL modify / partial exit) it's recording after the fact."""
        if self._trade_repo is None:
            return
        try:
            await self._trade_repo.update_state(
                agent_trade_id, "FILLED", is_paper=self._is_paper, **fields
            )
        except Exception as exc:
            _LOG.warning("position_field_persist_failed tid=%s err=%s", agent_trade_id, exc)

    async def force_exit(self, agent_trade_id: str, price: float, reason: str) -> None:
        """Smart-exit full close (reversal / exit-window). Market-exit the remainder
        and record the close (drained + persisted by the monitor loop). Shared path."""
        pos = self._positions.get(agent_trade_id)
        if pos is None:
            return
        if not self._is_paper:
            await self._live_exit(pos, reason, "FORCE", price)
            return
        exit_dir = "SELL" if pos.direction.upper() == "BUY" else "BUY"
        mkt = OrderRequest(
            symbol=pos.symbol,
            exchange="NSE",
            direction=exit_dir,
            quantity=pos.quantity,
            order_type="MARKET",
            product=pos.product or "INTRADAY",
            validity="DAY",
            agent_trade_id=f"{pos.agent_trade_id}-FORCE",
            signal_id="",
        )
        try:
            await self._broker.place_order(mkt)
        except Exception as exc:
            _LOG.warning("force_exit_failed tid=%s err=%s", agent_trade_id, exc)
        self._record_closed_trade(pos, price, reason)
        self.untrack(agent_trade_id)
        _LOG.info(
            "force_exit symbol=%s tid=%s reason=%s price=%.2f",
            pos.symbol,
            agent_trade_id,
            reason,
            price,
        )

    def _record_closed_trade(self, pos: MonitoredPosition, exit_price: float, reason: str) -> None:
        """Calculate all-in real P&L and append to closed_trades. Exception-guarded.

        net_pnl is the WHOLE trade: the last leg (`pos.quantity` still open) plus
        everything book_partial() already banked. Recording only the last leg lost the
        partial from every downstream stat and showed a partial-win-then-stop trade as a
        loser (earlier SYMBOL). Every exit path funnels through here.
        """
        from src.risk.real_pnl import (
            CostBreakdown,
            ProductType,
            active_broker,
            calculate_real_pnl,
        )

        try:
            product = ProductType.INTRADAY if pos.product.upper() == "MIS" else ProductType.DELIVERY
            is_short = pos.direction.upper() == "SELL"
            pnl = calculate_real_pnl(
                pos.symbol,
                pos.fill_price,
                exit_price,
                pos.quantity,
                product,
                is_short=is_short,
                broker=self._broker_name,
            )
            original_qty = pos.quantity + pos.partial_qty_booked
            total_net = pnl.net_pnl + pos.partial_pnl_booked
            # Whole-trade costs (partial slices + last leg): gross = net + costs downstream.
            total_costs = CostBreakdown.from_dict(pos.partial_costs).add(pnl.costs)
            risk = abs(pos.fill_price - pos.initial_sl) * original_qty or 1.0
            self.closed_trades.append(
                {
                    "agent_trade_id": pos.agent_trade_id,
                    "symbol": pos.symbol,
                    "product": pos.product or "CNC",
                    "direction": pos.direction,
                    "quantity": original_qty,
                    "entry_price": pos.fill_price,
                    "exit_price": exit_price,
                    "gross_pnl": round(total_net + total_costs.total, 2),
                    "total_costs": round(total_costs.total, 2),
                    "charges_breakdown": total_costs.as_dict(),
                    "broker": self._broker_name or active_broker(for_paper=self._is_paper),
                    "net_pnl": total_net,
                    "partial_pnl_booked": pos.partial_pnl_booked,
                    "partial_qty_booked": pos.partial_qty_booked,
                    "pnl_r": round(total_net / risk, 4),
                    "risk_rupees": round(risk, 2),
                    "reason": reason,
                    "entry_ts": pos.entry_ts or datetime.now(_IST).isoformat(),
                    "exit_ts": datetime.now(_IST).isoformat(),
                    "strategy": pos.strategy_name,
                }
            )
        except Exception as exc:
            _LOG.warning("real_pnl_failed symbol=%s err=%s", pos.symbol, exc)

    async def cycle(self) -> list[dict[str, object]]:
        """
        One monitoring cycle â€” call every 2-3 seconds during market hours.

        Returns list of action events (TARGET_LIMIT_MISSED, SL_GAP_BREACH, GTT_MISSING).
        Caller is responsible for executing the actions.
        """
        events: list[dict[str, object]] = []
        self.breach_snapshots = []
        if not self._positions:
            return events

        symbols = list({p.symbol for p in self._positions.values()})
        try:
            ltp_map = await self._broker.get_ltp(symbols)
        except Exception:
            _LOG.debug("monitor_cycle_ltp_fetch_failed positions=%d", len(self._positions))
            if not self._mis_flat_passed(datetime.now(_IST)):
                return events  # broker unreachable; supervisor detects staleness
            ltp_map = {}  # past close time a missing quote must not stop an MIS exit
        _LOG.debug(
            "monitor_cycle_received positions=%d symbols=%d ltp_fetched=%d",
            len(self._positions),
            len(symbols),
            len(ltp_map),
        )

        now = datetime.now(_IST)

        mis_flat_passed = self._mis_flat_passed(now)

        await self._check_broker_exit_orders()

        for tid, pos in list(self._positions.items()):
            if pos.exit_order_id or pos.exit_busy:
                continue  # an exit is at the broker; _check_broker_exit_orders owns it
            # MIS flat-by enforcer — execution.json windows.mis_flat_by. Ahead of the price
            # check: a missing quote must never let an MIS position ride past close time.
            if mis_flat_passed and pos.product.upper() == "MIS":
                events.append(
                    {
                        "type": "MIS_FORCE_EXIT",
                        "agent_trade_id": tid,
                        "symbol": pos.symbol,
                        "action": "MARKET_EXIT",
                    }
                )
                continue

            ltp = ltp_map.get(pos.symbol, 0.0)
            if ltp <= 0:
                continue

            is_long = pos.direction.upper() == "BUY"

            # Display-only proximity/breach snapshot (Order Monitor+ dashboard). Never
            # drives execution; the actionable events are emitted below independently.
            _near = self._breach_approaching_pct  # execution.json monitoring.breach_approaching_pct
            _tgt, _sl = pos.current_target, pos.current_sl
            if _tgt > 0:
                _hit = ltp >= _tgt if is_long else ltp <= _tgt
                _close = (_tgt - ltp) / _tgt <= _near if is_long else (ltp - _tgt) / _tgt <= _near
                if _hit or _close:
                    self.breach_snapshots.append(
                        {
                            "agent_trade_id": tid,
                            "symbol": pos.symbol,
                            "breach_type": "TARGET_BREACH" if _hit else "TARGET_APPROACHING",
                            "ltp": ltp,
                            "expected_price": _tgt,
                            "gap_pct": (ltp - _tgt) / _tgt * 100.0,
                            "order_type": "LIMIT",
                        }
                    )
            if _sl > 0:
                _hit = ltp <= _sl if is_long else ltp >= _sl
                _close = (ltp - _sl) / _sl <= _near if is_long else (_sl - ltp) / _sl <= _near
                if _hit or _close:
                    self.breach_snapshots.append(
                        {
                            "agent_trade_id": tid,
                            "symbol": pos.symbol,
                            "breach_type": "SL_BREACH" if _hit else "SL_APPROACHING",
                            "ltp": ltp,
                            "expected_price": _sl,
                            "gap_pct": (ltp - _sl) / _sl * 100.0,
                            "order_type": "SL",
                        }
                    )

            _exit_dir = "SELL" if is_long else "BUY"

            # TARGET LIMIT MISSED â€” LTP crossed past target but limit not filled.
            # Long: price ran above the target; short: price ran below it.
            target_missed = (
                ltp >= pos.current_target * 1.001 if is_long else ltp <= pos.current_target * 0.999
            )
            if pos.current_target > 0 and target_missed:
                # Adjust the resting exit order as price moves (2026-08-20) instead
                # of a one-shot force exit — see _advance_chase docstring. Escalates
                # to the guaranteed exit below only after chase_max_attempts.
                if await self._advance_chase(pos, "TARGET", ltp, _exit_dir):
                    events.append(
                        {
                            "type": "TARGET_LIMIT_MISSED",
                            "agent_trade_id": tid,
                            "symbol": pos.symbol,
                            "ltp": ltp,
                            "target": pos.current_target,
                            "action": "CANCEL_LIMIT_PLACE_MARKET",
                        }
                    )
            elif (
                # Price recovered back inside range before the chase resolved —
                # cancel the stray resting order rather than leave it live.
                pos.chase_reason == "TARGET"
                and pos.chase_order_id
                and await self._cancel_chase_checked(pos, ltp) != "proceed"
            ):
                continue

            # SL GAP / FAST MARKET â€” LTP past SL but SL order still open.
            # Long: price gapped below SL; short: price gapped above it.
            # Root cause of ~52% of all intraday SL exits landing here instead of a
            # clean "SL Hit" (found 2026-08-11: 45 SL_GAP_BREACH vs 42 SL Hit out of
            # closed MIS trades, averaging ~RsN/trade worse): the "clean" SL
            # path (SmartExitTracker) only re-evaluates on the 5-min bar-close exit
            # pass, while THIS loop is the only thing watching price continuously
            # (every 2s) in paper mode -- there is no real broker GTT firing on its
            # own to catch it sooner; pos.sl_order_id is a paper placeholder that
            # nothing else clears. Waiting for price to move a further 0.1% past the
            # stop before even trying to exit meant most stops were taken later and
            # worse than they needed to be. Fire on the crossing itself, not past it.
            # No `and pos.sl_order_id` gate here (removed 2026-08-17): a position
            # reseeded into OrderMonitor after a restart (see _reseed_order_monitor
            # in orchestrator.py) may have no sl_order_id on file, which silently
            # suppressed this exit entirely — the position rode straight through
            # its stop with monitoring active but unable to act. Missing an order
            # id is a reason to exit harder, not a reason to skip the check.
            sl_gapped = ltp <= pos.current_sl if is_long else ltp >= pos.current_sl
            if pos.current_sl > 0 and sl_gapped:
                # Adjust the resting exit order as price moves (2026-08-20) instead
                # of a one-shot force exit — see _advance_chase docstring. Escalates
                # to the guaranteed exit below only after chase_max_attempts.
                if await self._advance_chase(pos, "SL", ltp, _exit_dir):
                    events.append(
                        {
                            "type": "SL_GAP_BREACH",
                            "agent_trade_id": tid,
                            "symbol": pos.symbol,
                            "ltp": ltp,
                            "sl": pos.current_sl,
                            "action": "EMERGENCY_MARKET_EXIT",
                        }
                    )
            elif (
                pos.chase_reason == "SL"
                and pos.chase_order_id
                and await self._cancel_chase_checked(pos, ltp) != "proceed"
            ):
                continue

            # GTT INTEGRITY â€” verify every 30 seconds.
            # This used to be gated on `and pos.gtt_sl_id`, which had it exactly
            # backwards: a delivery position carrying NO protection at all — the one
            # state that actually needs re-arming — was skipped, because there was no
            # id to look up. A first arm that failed (broker reject, network) was
            # therefore never retried, and the position simply stayed naked forever
            # (earlier: SYMBOL + SYMBOL, arm rejected once at 20:37, never again).
            _delivery = pos.product.upper() in ("CNC", "DELIVERY")
            _unprotected = _delivery and not pos.gtt_sl_id and not pos.sl_order_id
            if (now - pos.last_gtt_check).total_seconds() >= self._gtt_check_interval_seconds and (
                pos.gtt_sl_id or _unprotected
            ):
                try:
                    if _unprotected:
                        events.append(
                            {
                                "type": "GTT_MISSING",
                                "agent_trade_id": tid,
                                "symbol": pos.symbol,
                                "gtt_sl_id": "",
                                "action": "REARM_GTT",
                            }
                        )
                    else:
                        gtts = await self._broker.get_all_gtts()
                        gtt_ids = {g.gtt_id for g in gtts}
                        if pos.gtt_sl_id not in gtt_ids:
                            events.append(
                                {
                                    "type": "GTT_MISSING",
                                    "agent_trade_id": tid,
                                    "symbol": pos.symbol,
                                    "gtt_sl_id": pos.gtt_sl_id,
                                    "action": "REARM_GTT",
                                }
                            )
                except Exception as exc:
                    _LOG.debug("gtt_check_failed err=%s", exc)
                pos.last_gtt_check = now

        return events

    async def execute_events(self, events: list[dict[str, object]]) -> None:
        """Execute broker actions for events returned by cycle(). Errors are logged, never raised."""
        for event in events:
            etype = str(event.get("type", ""))
            tid = str(event.get("agent_trade_id", ""))
            pos = self._positions.get(tid)
            if pos is None:
                continue
            _LOG.debug(
                "monitor_event_found type=%s tid=%s symbol=%s action=%s",
                etype,
                tid,
                pos.symbol,
                event.get("action", "-"),
            )
            try:
                if etype == "MIS_FORCE_EXIT":
                    await self._exec_mis_force_exit(pos)
                elif etype == "TARGET_LIMIT_MISSED":
                    await self._exec_target_limit_missed(pos)
                elif etype == "SL_GAP_BREACH":
                    await self._exec_sl_gap_breach(pos)
                elif etype == "GTT_MISSING":
                    await self._exec_gtt_missing(pos)
            except Exception as exc:
                _LOG.error("execute_event_failed type=%s tid=%s err=%s", etype, tid, exc)

    async def _exec_mis_force_exit(self, pos: MonitoredPosition) -> None:
        if not self._is_paper:
            try:
                _ltp = float(
                    (await self._broker.get_ltp([pos.symbol])).get(pos.symbol, pos.fill_price)
                )
            except Exception:
                _ltp = pos.fill_price
            await self._live_exit(pos, "MIS_FORCE_EXIT", "MIS-FLAT", _ltp)
            return
        exit_dir = "SELL" if pos.direction.upper() == "BUY" else "BUY"
        mkt = OrderRequest(
            symbol=pos.symbol,
            exchange="NSE",
            direction=exit_dir,
            quantity=pos.quantity,
            order_type="MARKET",
            product="INTRADAY",
            validity="DAY",
            agent_trade_id=f"{pos.agent_trade_id}-MIS-FLAT",
            signal_id="",
        )
        await self._broker.place_order(mkt)
        try:
            _ltp = float((await self._broker.get_ltp([pos.symbol])).get(pos.symbol, pos.fill_price))
        except Exception:
            _ltp = pos.fill_price
        self._record_closed_trade(pos, _ltp, "MIS_FORCE_EXIT")
        self.untrack(pos.agent_trade_id)
        _LOG.warning("mis_force_exit symbol=%s tid=%s", pos.symbol, pos.agent_trade_id)
        if self._alert_manager is not None:
            from src.infrastructure.alert_system import AlertTier

            self._alert_manager.raise_alert(
                AlertTier.WARNING,
                f"MIS position {pos.symbol} force-exited at mis_flat_by deadline",
                source=pos.agent_trade_id,
            )

    def _clear_chase(self, pos: MonitoredPosition) -> None:
        pos.chase_order_id = ""
        pos.chase_attempts = 0
        pos.chase_reason = ""

    async def _advance_chase(
        self, pos: MonitoredPosition, reason: str, ltp: float, exit_dir: str
    ) -> bool:
        """Drive one cycle of the exit-chase state machine for a breached SL/target
        (2026-08-20 — "adjust the order as price moves" instead of one-shot exit).

        Returns True when the caller should escalate to the existing guaranteed
        exit (SL_GAP_BREACH/TARGET_LIMIT_MISSED event → force market exit);
        False if a chase order is still in flight (placed/re-pegged this cycle)
        or already resolved here (filled — position closed+untracked directly).

        Works identically in paper and live: the paper fill simulator only
        checks a LIMIT order's fill ONCE, at placement (see fill_simulator.py) —
        so an aggressive-but-unfilled limit genuinely sits open until re-pegged,
        the same as a real broker order would. That symmetry is why this can be
        validated in paper before it's ever exercised live.
        """
        import contextlib

        if pos.exit_order_id or pos.exit_busy:
            return False
        if not self._is_paper and not pos.chase_order_id:
            # First chase order: the resting stop/target must be off the book first, or this
            # sell and theirs both fill. Nothing is placed until that is proven.
            pos.exit_busy = True
            try:
                if await self._settle_resting_exits(pos) != "proceed":
                    return False
            finally:
                pos.exit_busy = False
        if pos.chase_order_id and pos.chase_reason == reason:
            try:
                _status = await self._broker.get_order_status(pos.chase_order_id)
            except Exception as exc:
                _LOG.warning("chase_status_check_failed tid=%s err=%s", pos.agent_trade_id, exc)
                return True  # can't verify — escalate rather than risk an orphaned position
            if _status.status == "FILLED":
                _fill_px = float(_status.average_price or ltp)
                self._record_closed_trade(pos, _fill_px, f"{reason}_CHASE_FILLED")
                self.untrack(pos.agent_trade_id)
                _LOG.info(
                    "chase_exit_filled symbol=%s tid=%s reason=%s price=%.2f attempts=%d",
                    pos.symbol,
                    pos.agent_trade_id,
                    reason,
                    _fill_px,
                    pos.chase_attempts,
                )
                return False
            if pos.chase_attempts >= self._chase_max_attempts:
                # Attempts exhausted — escalate to guaranteed exit, once proven safe.
                return await self._cancel_chase_checked(pos, ltp) == "proceed"
            _attempts = pos.chase_attempts
            if await self._cancel_chase_checked(pos, ltp) != "proceed":
                return False
            pos.chase_attempts = _attempts  # same chase, next re-peg

        # (Re)price and place a fresh marketable-limit exit order.
        _px = _marketable_limit_price(ltp, exit_dir)
        _req = OrderRequest(
            symbol=pos.symbol,
            exchange="NSE",
            direction=exit_dir,
            quantity=pos.quantity,
            order_type="LIMIT",
            product=pos.product or "INTRADAY",
            validity="DAY",
            agent_trade_id=f"{pos.agent_trade_id}-CHASE{pos.chase_attempts + 1}",
            signal_id="",
            price=_px,
        )
        try:
            _oid = await self._broker.place_order(_req)
        except Exception as exc:
            _LOG.warning("chase_place_failed tid=%s err=%s", pos.agent_trade_id, exc)
            return True  # can't place — escalate
        pos.chase_order_id = _oid
        pos.chase_reason = reason
        pos.chase_attempts += 1
        _LOG.info(
            "chase_exit_repeg symbol=%s tid=%s reason=%s attempt=%d price=%.2f",
            pos.symbol,
            pos.agent_trade_id,
            reason,
            pos.chase_attempts,
            _px,
        )
        # Same-cycle fill check: paper fills synchronously inside place_order, so
        # a same-cycle fill should close now rather than waiting a full extra
        # cycle to notice.
        with contextlib.suppress(Exception):
            _status2 = await self._broker.get_order_status(_oid)
            if _status2.status == "FILLED":
                _fill_px = float(_status2.average_price or _px)
                self._record_closed_trade(pos, _fill_px, f"{reason}_CHASE_FILLED")
                self.untrack(pos.agent_trade_id)
        return False

    async def _exec_target_limit_missed(self, pos: MonitoredPosition) -> None:
        import contextlib

        exit_dir = "SELL" if pos.direction.upper() == "BUY" else "BUY"
        if not self._is_paper:
            try:
                _ltp = float(
                    (await self._broker.get_ltp([pos.symbol])).get(pos.symbol, pos.current_target)
                )
            except Exception:
                _ltp = pos.current_target
            _px = _marketable_limit_price(_ltp, exit_dir)
            await self._live_exit(pos, "TARGET_LIMIT_MISSED", "TGT-MKT", _px, limit_price=_px)
            return
        if pos.target_order_id:
            with contextlib.suppress(Exception):
                await self._broker.cancel_order(pos.target_order_id)
        try:
            _ltp = float(
                (await self._broker.get_ltp([pos.symbol])).get(pos.symbol, pos.current_target)
            )
        except Exception:
            _ltp = pos.current_target
        # Marketable limit, not MARKET (2026-08-20): same chase-avoidance technique
        # already used for entries (_marketable_limit_price) — fills immediately
        # like a market order but caps the worst-case fill instead of accepting
        # whatever price the book hands back.
        _exit_px = _marketable_limit_price(_ltp, exit_dir)
        mkt = OrderRequest(
            symbol=pos.symbol,
            exchange="NSE",
            direction=exit_dir,
            quantity=pos.quantity,
            order_type="LIMIT",
            product=pos.product or "INTRADAY",
            validity="DAY",
            agent_trade_id=f"{pos.agent_trade_id}-TGT-MKT",
            signal_id="",
            price=_exit_px,
        )
        await self._broker.place_order(mkt)
        self._record_closed_trade(pos, _exit_px, "TARGET_LIMIT_MISSED")
        self.untrack(pos.agent_trade_id)
        _LOG.warning(
            "target_limit_missed_market_exit symbol=%s tid=%s", pos.symbol, pos.agent_trade_id
        )
        if self._alert_manager is not None:
            from src.infrastructure.alert_system import AlertTier

            self._alert_manager.raise_alert(
                AlertTier.ALERT,
                f"Target limit missed for {pos.symbol} — market exit placed",
                source=pos.agent_trade_id,
            )

    async def _exec_sl_gap_breach(self, pos: MonitoredPosition) -> None:
        exit_dir = "SELL" if pos.direction.upper() == "BUY" else "BUY"
        try:
            _ltp = float((await self._broker.get_ltp([pos.symbol])).get(pos.symbol, pos.current_sl))
        except Exception:
            _ltp = pos.current_sl
        if not self._is_paper:
            _px = _marketable_limit_price(_ltp, exit_dir)
            await self._live_exit(pos, "SL_GAP_BREACH", "SL-EMRG", _px, limit_price=_px)
            return
        if self._is_paper:
            # Paper monitoring polls every few seconds, so a breach is detected
            # LATE and raw LTP books exaggerated slippage vs a real broker GTT
            # that fires near the stop (3/6 trades on earlier exited this
            # way). Bound the simulated fill at SL ± a slippage allowance.
            _allow = pos.current_sl * _GAP_BREACH_SLIPPAGE_PCT / 100.0
            if pos.direction.upper() == "BUY":  # long: exit no worse than SL - allowance
                _ltp = max(_ltp, pos.current_sl - _allow)
            else:  # short: exit no worse than SL + allowance
                _ltp = min(_ltp, pos.current_sl + _allow)
        # Marketable limit, not MARKET (2026-08-20): same chase-avoidance technique
        # already used for entries — fills immediately like a market order but
        # caps the worst-case fill at a known offset instead of an unbounded one.
        # Paper mode already has its own tuned slippage bound above (the SL ±
        # _GAP_BREACH_SLIPPAGE_PCT clamp) — stacking the entry-side offset on
        # top of it would double-count slippage, so only apply it live.
        _exit_px = _ltp if self._is_paper else _marketable_limit_price(_ltp, exit_dir)
        mkt = OrderRequest(
            symbol=pos.symbol,
            exchange="NSE",
            direction=exit_dir,
            quantity=pos.quantity,
            order_type="LIMIT",
            product=pos.product or "INTRADAY",
            validity="DAY",
            agent_trade_id=f"{pos.agent_trade_id}-SL-EMRG",
            signal_id="",
            price=_exit_px,
        )
        await self._broker.place_order(mkt)
        self._record_closed_trade(pos, _exit_px, "SL_GAP_BREACH")
        self.untrack(pos.agent_trade_id)
        _LOG.critical(
            "emergency_market_exit symbol=%s tid=%s sl=%.2f",
            pos.symbol,
            pos.agent_trade_id,
            pos.current_sl,
        )
        if self._alert_manager is not None:
            from src.infrastructure.alert_system import AlertTier

            self._alert_manager.raise_alert(
                AlertTier.EMERGENCY,
                f"SL GAP BREACH: emergency market exit for {pos.symbol}",
                source=pos.agent_trade_id,
            )

    # ── Live exit truth (2026-11-05) ─────────────────────────────────────────────
    # A position is closed only when the broker says its exit order FILLED, at the
    # broker's average price. Paper keeps its own modelled fill and books immediately.

    async def _live_exit(
        self,
        pos: MonitoredPosition,
        reason: str,
        suffix: str,
        fallback_price: float,
        limit_price: float | None = None,
    ) -> None:
        """Take the resting legs off, then send one exit order and wait for its fill."""
        if pos.exit_order_id or pos.exit_busy:
            return
        pos.exit_busy = True
        try:
            if await self._settle_resting_exits(pos) != "proceed":
                return
            if (
                pos.chase_order_id
                and await self._cancel_chase_checked(pos, fallback_price) != "proceed"
            ):
                return
            oid = await self._place_exit_order(pos, suffix, limit_price)
            if oid is None:
                return
            pos.exit_order_id = oid
            pos.exit_reason = reason
            pos.exit_fallback_price = fallback_price
            pos.exit_checks = 0
            await self._resolve_exit_order(pos, None)
        finally:
            pos.exit_busy = False

    async def _place_exit_order(
        self, pos: MonitoredPosition, suffix: str, limit_price: float | None
    ) -> str | None:
        req = OrderRequest(
            symbol=pos.symbol,
            exchange="NSE",
            direction="SELL" if pos.direction.upper() == "BUY" else "BUY",
            quantity=pos.quantity,
            order_type="LIMIT" if limit_price is not None else "MARKET",
            product=pos.product or "INTRADAY",
            validity="DAY",
            agent_trade_id=f"{pos.agent_trade_id}-{suffix}",
            signal_id="",
            price=limit_price,
        )
        try:
            return await self._broker.place_order(req)
        except Exception as exc:
            _LOG.critical(
                "live_exit_place_failed symbol=%s tid=%s err=%s — still open, still tracked",
                pos.symbol,
                pos.agent_trade_id,
                exc,
            )
            _notify_unprotected(
                pos.agent_trade_id,
                pos.symbol,
                is_paper=False,
                product=pos.product,
                title=f"EXIT REFUSED, POSITION STILL OPEN: {pos.symbol}",
                reason=str(exc)[:200],
            )
            return None

    def _close_at_broker_fill(self, pos: MonitoredPosition, price: float, reason: str) -> None:
        self._record_closed_trade(pos, price, reason)
        self.untrack(pos.agent_trade_id)
        _LOG.warning(
            "live_exit_filled symbol=%s tid=%s reason=%s price=%.2f",
            pos.symbol,
            pos.agent_trade_id,
            reason,
            price,
        )

    async def _book_fill_slice(self, pos: MonitoredPosition, qty: int, price: float) -> None:
        """Shares a broker exit order already sold, with the rest of the position still open."""
        pos.quantity -= qty
        pos.partial_qty_booked += qty
        await self._account_slice(pos, qty, price)

    async def _settle_resting_exits(self, pos: MonitoredPosition) -> str:
        """Cancel the stop and target, then prove from the broker that neither can still sell.

        Returns "proceed" (a new exit is safe), "closed" (a leg had already filled the whole
        position: booked, nothing more to send) or "retry" (unproven: send nothing this cycle).
        """
        import contextlib

        legs = (("sl_order_id", "SL_HIT"), ("target_order_id", "TARGET_HIT"))
        for attr, _label in legs:
            if getattr(pos, attr):
                with contextlib.suppress(Exception):  # refused once filled; the read decides
                    await self._broker.cancel_order(getattr(pos, attr))
        for attr, label in legs:
            oid = getattr(pos, attr)
            if not oid:
                continue
            try:
                order = await self._broker.get_order_status(oid)
            except Exception as exc:
                _LOG.error(
                    "settle_exit_unread tid=%s order=%s err=%s", pos.agent_trade_id, oid, exc
                )
                return "retry"
            if order.status not in ("FILLED", "CANCELLED", "REJECTED"):
                return "retry"  # e.g. CANCEL PENDING: can still fill
            filled = int(order.filled_quantity or 0)
            if filled >= pos.quantity:
                self._close_at_broker_fill(pos, float(order.average_price), label)
                return "closed"
            if filled:
                await self._book_fill_slice(pos, filled, float(order.average_price))
            setattr(pos, attr, "")
        if pos.product.upper() == "MIS" and not await self._broker_held_qty(pos):
            _LOG.warning(
                "live_exit_skipped symbol=%s tid=%s — broker flat or unread, no sell sent",
                pos.symbol,
                pos.agent_trade_id,
            )
            return "retry"
        return "proceed"

    async def _cancel_chase_checked(self, pos: MonitoredPosition, ltp: float) -> str:
        """Cancel a resting chase order and prove its outcome. Same return values as settle."""
        import contextlib

        if self._is_paper:
            with contextlib.suppress(Exception):
                await self._broker.cancel_order(pos.chase_order_id)
            self._clear_chase(pos)
            return "proceed"
        oid, reason = pos.chase_order_id, pos.chase_reason
        with contextlib.suppress(Exception):
            await self._broker.cancel_order(oid)
        try:
            order = await self._broker.get_order_status(oid)
        except Exception as exc:
            _LOG.error("chase_cancel_unread tid=%s err=%s", pos.agent_trade_id, exc)
            return "retry"
        if order.status not in ("FILLED", "CANCELLED", "REJECTED"):
            return "retry"
        filled = int(order.filled_quantity or 0)
        self._clear_chase(pos)
        if filled >= pos.quantity:
            self._close_at_broker_fill(
                pos, float(order.average_price or ltp), f"{reason}_CHASE_FILLED"
            )
            return "closed"
        if filled:
            await self._book_fill_slice(pos, filled, float(order.average_price or ltp))
        return "proceed"

    async def _broker_held_qty(self, pos: MonitoredPosition) -> int | None:
        """Shares the broker holds on this position's side (MIS), or None when unreadable. Every
        exit-side order is checked against this: selling a position someone already closed in
        Kite would open a naked short."""
        try:
            positions = await self._broker.get_positions()
        except Exception as exc:
            _LOG.warning("held_qty_unread symbol=%s err=%s", pos.symbol, exc)
            return None
        side = "long" if pos.direction.upper() == "BUY" else "short"
        return sum(
            int(p.quantity)
            for p in positions
            if p.symbol == pos.symbol and p.product == "INTRADAY" and p.direction == side
        )

    async def _resolve_exit_order(self, pos: MonitoredPosition, order: Order | None) -> None:
        """Advance an in-flight exit: FILLED closes at the real price; refused is alerted and the
        breach/flat-by trigger re-fires it; cancelled goes back out at market; still resting past
        the chase allowance is cancelled for that re-send."""
        if order is None:
            try:
                order = await self._broker.get_order_status(pos.exit_order_id)
            except Exception as exc:
                _LOG.warning("exit_status_unread tid=%s err=%s", pos.agent_trade_id, exc)
                return
        if order.status == "FILLED":
            pos.exit_order_id = ""
            self._close_at_broker_fill(
                pos,
                float(order.average_price or pos.exit_fallback_price),
                pos.exit_reason,
            )
            return
        if order.status == "REJECTED":
            _LOG.critical(
                "live_exit_rejected symbol=%s tid=%s reason=%s",
                pos.symbol,
                pos.agent_trade_id,
                order.rejection_reason,
            )
            _notify_unprotected(
                pos.agent_trade_id,
                pos.symbol,
                is_paper=False,
                product=pos.product,
                title=f"EXIT REFUSED, POSITION STILL OPEN: {pos.symbol}",
                reason=str(order.rejection_reason or "")[:200],
            )
            pos.exit_order_id = ""
            return
        if order.status == "CANCELLED":
            filled = int(order.filled_quantity or 0)
            if filled:
                await self._book_fill_slice(pos, filled, float(order.average_price))
            pos.exit_order_id = ""
            if not await self._broker_held_qty(pos):
                return
            new_id = await self._place_exit_order(pos, "EXIT-MKT", None)
            if new_id:
                pos.exit_order_id = new_id
                pos.exit_checks = 0
                await self._resolve_exit_order(pos, None)
            return
        pos.exit_checks += 1
        if pos.exit_checks >= self._chase_max_attempts:
            import contextlib

            with contextlib.suppress(Exception):  # the next read shows FILLED or CANCELLED
                await self._broker.cancel_order(pos.exit_order_id)

    def _mis_flat_passed(self, now: datetime) -> bool:
        return now.hour > self._mis_flat_hour or (
            now.hour == self._mis_flat_hour and now.minute >= self._mis_flat_minute
        )

    async def flatten_untracked_mis(
        self, *, in_session: bool, exempt: set[str] | None = None
    ) -> None:
        """Operator rule: every MIS position closes at close time, whoever opened it. Tracked
        positions exit through the flat-by above; this closes what nothing tracks, including
        trades placed by hand in Kite. ``exempt`` are the options desk's own legs, which close
        at their own exit time. Live only: every paper position is a DB row the monitor tracks."""
        if self._is_paper or not in_session or not self._mis_flat_passed(datetime.now(_IST)):
            return
        try:
            positions = await self._broker.get_positions()
        except Exception as exc:
            _LOG.warning("mis_flatten_positions_unread err=%s", exc)
            return
        tracked = {p.symbol for p in self._positions.values() if p.product.upper() == "MIS"}
        todo = [
            p
            for p in positions
            if p.product == "INTRADAY"
            and p.quantity
            and p.symbol not in tracked
            and p.symbol not in (exempt or set())
        ]
        if not todo:
            return
        try:
            orders = await self._broker.get_all_orders()
        except Exception as exc:
            _LOG.warning("mis_flatten_orders_unread err=%s", exc)
            return
        for p in todo:
            try:
                await self._flatten_one(p, orders)
            except Exception as exc:  # noqa: BLE001 -- one symbol must not stop the others
                _LOG.critical(
                    "mis_flatten_failed symbol=%s qty=%d err=%s — retrying next cycle",
                    p.symbol,
                    p.quantity,
                    exc,
                )
                _notify_unprotected(
                    f"mis-flatten-{p.symbol}",
                    p.symbol,
                    is_paper=False,
                    product="MIS",
                    title=f"MIS NOT CLOSED AT CLOSE TIME: {p.symbol}",
                    reason=f"Close refused: {str(exc)[:160]}. Retrying.",
                )

    async def _flatten_one(self, p: Any, orders: list[Order]) -> None:
        exit_dir = "SELL" if p.direction == "long" else "BUY"
        mine = {o.broker_order_id: o for o in orders if o.symbol == p.symbol}
        sent = mine.get(self._flatten_orders.get(p.symbol, ""))
        if sent is not None and sent.status == "OPEN":
            return  # our close order is still working
        # The position read can lag a fill by moments; selling on it would open a short.
        recent = datetime.now(_IST) - timedelta(seconds=self._gtt_check_interval_seconds)
        for o in mine.values():
            if o.direction == exit_dir and o.status == "FILLED" and o.placed_at > recent:
                return
        for o in mine.values():
            if o.direction == exit_dir and o.status == "OPEN" and o.product == "INTRADAY":
                await self._broker.cancel_order(o.broker_order_id)
                if (await self._broker.get_order_status(o.broker_order_id)).status != "CANCELLED":
                    return  # could still fill: prove it gone before selling
        self._flatten_orders[p.symbol] = await self._broker.place_order(
            OrderRequest(
                symbol=p.symbol,
                exchange=p.exchange,
                direction=exit_dir,
                quantity=p.quantity,
                order_type="MARKET",
                product="INTRADAY",
                validity="DAY",
                agent_trade_id=f"MISFLAT-{p.symbol}"[:32],
                signal_id="",
            )
        )
        _LOG.critical("mis_flatten_sent symbol=%s qty=%d side=%s", p.symbol, p.quantity, exit_dir)
        _notify_unprotected(
            f"mis-flatten-{p.symbol}",
            p.symbol,
            is_paper=False,
            product="MIS",
            title=f"MIS CLOSED AT CLOSE TIME: {p.symbol}",
            reason=f"{exit_dir} {p.quantity} at market. No trade in this system owned it.",
        )

    async def _check_broker_exit_orders(self) -> None:
        """The continuous live check: every cycle, read the broker's order book and reconcile
        each live MIS position's exit orders against it. Paper has no broker order book."""
        if self._is_paper:
            return
        watched = [
            p for p in self._positions.values() if p.exit_order_id or p.product.upper() == "MIS"
        ]
        if not watched:
            return
        try:
            orders = {o.broker_order_id: o for o in await self._broker.get_all_orders()}
        except Exception as exc:
            _LOG.warning("broker_order_check_failed err=%s", exc)
            return
        for pos in watched:
            if pos.exit_busy:
                continue
            pos.exit_busy = True
            try:
                if pos.exit_order_id:
                    await self._resolve_exit_order(pos, orders.get(pos.exit_order_id))
                elif not pos.chase_order_id:
                    if await self._book_broker_leg_fills(pos, orders):
                        continue
                    await self._sync_resting_leg_sizes(pos, orders)
                    await self._ensure_broker_stop(pos, orders)
            except Exception as exc:  # noqa: BLE001 -- one position must not stop the others
                _LOG.error("broker_order_check_error tid=%s err=%s", pos.agent_trade_id, exc)
            finally:
                pos.exit_busy = False

    async def _book_broker_leg_fills(
        self, pos: MonitoredPosition, orders: dict[str, Order]
    ) -> bool:
        """A stop or target filled at the broker. Cancel its sibling at once, which otherwise
        stays live and can open a naked position. True when the position is now closed."""
        import contextlib

        for attr, other, label in (
            ("sl_order_id", "target_order_id", "SL_HIT"),
            ("target_order_id", "sl_order_id", "TARGET_HIT"),
        ):
            order = orders.get(getattr(pos, attr))
            if order is None or order.status != "FILLED" or not order.filled_quantity:
                continue
            if getattr(pos, other):
                with contextlib.suppress(Exception):
                    await self._broker.cancel_order(getattr(pos, other))
            if int(order.filled_quantity) >= pos.quantity:
                self._close_at_broker_fill(pos, float(order.average_price), label)
                return True
            await self._book_fill_slice(pos, int(order.filled_quantity), float(order.average_price))
            setattr(pos, attr, "")
        return False

    async def _sync_resting_leg_sizes(
        self, pos: MonitoredPosition, orders: dict[str, Order]
    ) -> None:
        """Keep each resting leg no bigger than what is left: after a partial target fill or a
        partial book, a full-size stop would oversell and leave a short."""
        sl, tgt = orders.get(pos.sl_order_id), orders.get(pos.target_order_id)
        for leg, other in ((sl, tgt), (tgt, sl)):
            if leg is None or leg.status != "OPEN":
                continue
            wanted = pos.quantity - (int(other.filled_quantity or 0) if other is not None else 0)
            if wanted > 0 and wanted != leg.quantity:
                try:
                    await self._broker.modify_order(leg.broker_order_id, {"quantity": wanted})
                    leg.quantity = wanted
                except Exception as exc:
                    _LOG.error(
                        "leg_resize_failed tid=%s order=%s wanted=%d err=%s",
                        pos.agent_trade_id,
                        leg.broker_order_id,
                        wanted,
                        exc,
                    )

    async def _ensure_broker_stop(self, pos: MonitoredPosition, orders: dict[str, Order]) -> None:
        """Every live MIS position keeps a stop resting at the broker. A missing or refused stop
        is re-placed, spaced by gtt_check_interval_seconds, until the position is flat."""
        if pos.current_sl <= 0:
            return
        if pos.sl_order_id:
            order = orders.get(pos.sl_order_id)
            if order is None:
                # A bad read must not create a second stop, so alert and leave it.
                if not pos.stop_alerted:
                    pos.stop_alerted = True
                    _LOG.critical(
                        "broker_stop_not_in_book tid=%s order=%s",
                        pos.agent_trade_id,
                        pos.sl_order_id,
                    )
                    _notify_unprotected(
                        pos.agent_trade_id,
                        pos.symbol,
                        is_paper=False,
                        product=pos.product,
                        title=f"STOP NOT IN ORDER BOOK: {pos.symbol}",
                        reason=f"Order {pos.sl_order_id} missing. Check Kite.",
                    )
                return
            if order.status not in ("REJECTED", "CANCELLED"):
                return
        now = datetime.now(_IST)
        if (
            pos.last_stop_attempt is not None
            and (now - pos.last_stop_attempt).total_seconds() < self._gtt_check_interval_seconds
        ):
            return
        if not await self._broker_held_qty(pos):
            return  # flat or unread: an exit-side order could open a short
        pos.last_stop_attempt = now
        try:
            new_id = await self._broker.place_order(
                OrderRequest(
                    symbol=pos.symbol,
                    exchange="NSE",
                    direction="SELL" if pos.direction.upper() == "BUY" else "BUY",
                    quantity=pos.quantity,
                    order_type="SL-M",
                    product=pos.product,
                    validity="DAY",
                    agent_trade_id=f"{pos.agent_trade_id}-SL",
                    signal_id="",
                    trigger_price=pos.current_sl,
                )
            )
        except Exception as exc:
            _LOG.critical(
                "broker_stop_place_failed symbol=%s tid=%s sl=%.2f err=%s — software stop active",
                pos.symbol,
                pos.agent_trade_id,
                pos.current_sl,
                exc,
            )
            if not pos.stop_alerted:
                pos.stop_alerted = True
                _notify_unprotected(
                    pos.agent_trade_id,
                    pos.symbol,
                    is_paper=False,
                    product=pos.product,
                    reason=f"Stop refused: {str(exc)[:160]}. Software stop active, retrying.",
                )
            return
        pos.sl_order_id = str(new_id)
        await self._persist_position_field(pos.agent_trade_id, sl_order_id=pos.sl_order_id)
        _LOG.warning(
            "broker_stop_placed symbol=%s tid=%s sl=%.2f qty=%d order=%s",
            pos.symbol,
            pos.agent_trade_id,
            pos.current_sl,
            pos.quantity,
            pos.sl_order_id,
        )
        if pos.stop_alerted:
            pos.stop_alerted = False
            _notify_unprotected(
                pos.agent_trade_id,
                pos.symbol,
                is_paper=False,
                product=pos.product,
                title=f"BROKER STOP NOW PLACED: {pos.symbol}",
                reason=f"Stop resting at {pos.current_sl:.2f} for {pos.quantity}.",
            )

    async def _exec_gtt_missing(self, pos: MonitoredPosition) -> None:
        """Re-arm the protection GTT for a position that has none.

        This used to log CRITICAL and raise a "manual re-arm required" alert — the
        monitor detected the exact problem it exists to solve and then handed it to a
        human. It now places the GTT itself and only escalates if the broker refuses.
        """
        _LOG.warning(
            "gtt_missing symbol=%s tid=%s gtt_id=%s — re-arming",
            pos.symbol,
            pos.agent_trade_id,
            pos.gtt_sl_id or "none",
        )
        try:
            gtt_id = await self._arm_protection_gtt(pos)
        except Exception as exc:
            _LOG.critical(
                "gtt_rearm_failed symbol=%s tid=%s err=%s — POSITION UNPROTECTED",
                pos.symbol,
                pos.agent_trade_id,
                exc,
            )
            if self._alert_manager is not None:
                from src.infrastructure.alert_system import AlertTier

                self._alert_manager.raise_alert(
                    AlertTier.CRITICAL,
                    f"GTT re-arm failed for {pos.symbol}: {exc}",
                    source=pos.agent_trade_id,
                )
            return
        pos.gtt_sl_id = gtt_id
        pos.gtt_target_id = gtt_id
        await self._persist_position_field(
            pos.agent_trade_id, gtt_sl_id=gtt_id, gtt_target_id=gtt_id
        )
        if self._alert_manager is not None:
            self._alert_manager.resolve_by_source(pos.agent_trade_id)
        _LOG.warning(
            "gtt_rearmed symbol=%s tid=%s gtt=%s sl=%.2f target=%.2f qty=%d",
            pos.symbol,
            pos.agent_trade_id,
            gtt_id,
            pos.current_sl,
            pos.current_target,
            pos.quantity,
        )

    async def _arm_protection_gtt(self, pos: MonitoredPosition) -> str:
        """Place the two-leg OCO GTT protecting `pos` at its CURRENT sl/target/qty.

        Uses current_* (not initial_*) so a re-arm after a trail or a partial book
        restores the levels and size the position actually has now, not the ones it
        opened with.
        """
        if not self._broker.supports_gtt():
            raise RuntimeError(
                f"{type(self._broker).__name__} reports supports_gtt()=False — cannot re-arm"
            )
        from src.brokers.base import GTTRequest

        return str(
            await self._broker.place_gtt(
                GTTRequest(
                    symbol=pos.symbol,
                    product=pos.product or "CNC",
                    gtt_type="TWO_LEG",
                    quantity=pos.quantity,
                    agent_trade_id=pos.agent_trade_id,
                    sl_trigger=pos.current_sl,
                    sl_order_type=_GTT_LEG_ORDER_TYPE,
                    sl_price=pos.current_sl,
                    target_trigger=pos.current_target,
                    target_price=pos.current_target,
                )
            )
        )
