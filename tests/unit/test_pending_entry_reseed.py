"""Tests for orchestrator._reseed_pending_entries (2026-08-28 fix).

Root cause: a swing entry is placed as an AMO after the close and rests until the
next open, but `_pending_entry_ids` and its MonitoredPosition template live only in
memory. The orchestrator restarts many times a day, so an AMO placed in the evening
was orphaned before it filled — nothing re-polled it, the fill was never detected,
_arm_deferred_protection never ran, and the position opened with no stop, no target
and no monitoring at all.

Observed live 2026-08-28: IFCI (145 @ 84.27) and JINDALSAW (40 @ 310.20) both filled
at the open, both engine-tagged AMOs, zero GTTs on the account.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.brokers.base import BrokerInterface
from src.execution.order_lifecycle import OrderLifecycleManager, OrderState

_ORDER_TYPES = {"CNC_swing_sl": "GTT", "CNC_swing_target": "GTT"}


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, query, *args):
        return self._rows


def _row(**overrides):
    row = {
        "agent_trade_id": "TRD-E79CE1F16545",
        "broker_order_id": "2093013611046805504",
        "symbol": "IFCI",
        "exchange": "NSE",
        "direction": "long",
        "quantity": 145,
        "intended_price": 85.90,
        "initial_sl": 80.00,
        "current_sl": 80.00,
        "initial_target": 95.00,
        "current_target": 95.00,
        "product": "CNC",
        "strategy_name": "oneil_canslim",
        "entry_timestamp": datetime(2026, 8, 27, 21, 55, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def _mgr() -> OrderLifecycleManager:
    return OrderLifecycleManager(MagicMock(spec=BrokerInterface))


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_registers_resting_entry_for_repoll() -> None:
    from src import orchestrator

    mgr = _mgr()
    added = await orchestrator._reseed_pending_entries(
        mgr, _FakePool([_row()]), paper_mode=False, order_types=_ORDER_TYPES
    )

    assert added == 1
    assert "TRD-E79CE1F16545" in mgr.pending_entry_ids()
    rec = mgr._trades["TRD-E79CE1F16545"]
    assert rec.broker_order_id == "2093013611046805504"
    assert rec.direction == "BUY"  # mapped from the DB's 'long'
    assert rec.sl_price == 80.00
    assert rec.target_price == 95.00


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseeded_record_is_open_so_a_fill_is_reachable() -> None:
    """SUBMITTED cannot transition straight to FILLED (VALID_TRANSITIONS).

    A resting AMO is exactly what comes back FILLED on the first poll after the open.
    Left at SUBMITTED, poll() would find the transition illegal and return the record
    unchanged — dropping the fill silently, which is how the position ends up live and
    unprotected in the first place.
    """
    from src import orchestrator

    mgr = _mgr()
    await orchestrator._reseed_pending_entries(
        mgr, _FakePool([_row()]), paper_mode=False, order_types=_ORDER_TYPES
    )

    from src.execution.order_lifecycle import VALID_TRANSITIONS

    rec = mgr._trades["TRD-E79CE1F16545"]
    assert rec.state == OrderState.OPEN
    assert OrderState.FILLED in VALID_TRANSITIONS[rec.state]
    assert OrderState.FILLED not in VALID_TRANSITIONS[OrderState.SUBMITTED]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseeded_cnc_entry_carries_deferred_protection_context() -> None:
    """Without protection_ctx the re-poll detects the fill and still arms nothing."""
    from src import orchestrator

    mgr = _mgr()
    await orchestrator._reseed_pending_entries(
        mgr, _FakePool([_row()]), paper_mode=False, order_types=_ORDER_TYPES
    )

    rec = mgr._trades["TRD-E79CE1F16545"]
    assert rec.defer_protection is True
    assert rec.protection_ctx["symbol"] == "IFCI"
    assert rec.protection_ctx["product"] == "CNC"
    assert rec.protection_ctx["order_types"] == _ORDER_TYPES


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseeded_entry_carries_a_monitor_template() -> None:
    """The monitor loop tracks the template on fill; without it the filled position
    is never registered with OrderMonitor at all."""
    from src import orchestrator

    mgr = _mgr()
    await orchestrator._reseed_pending_entries(
        mgr, _FakePool([_row()]), paper_mode=False, order_types=_ORDER_TYPES
    )

    tmpl = mgr._trades["TRD-E79CE1F16545"].monitor_template
    assert tmpl is not None
    assert tmpl.symbol == "IFCI"
    assert tmpl.quantity == 145
    assert tmpl.current_sl == 80.00
    assert tmpl.product == "CNC"
    assert tmpl.strategy_name == "oneil_canslim"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_is_idempotent() -> None:
    """Runs at startup AND on every reconciliation cycle — must not double-register."""
    from src import orchestrator

    mgr = _mgr()
    pool = _FakePool([_row()])
    first = await orchestrator._reseed_pending_entries(
        mgr, pool, paper_mode=False, order_types=_ORDER_TYPES
    )
    second = await orchestrator._reseed_pending_entries(
        mgr, pool, paper_mode=False, order_types=_ORDER_TYPES
    )

    assert (first, second) == (1, 0)
    assert len(mgr.pending_entry_ids()) == 1


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_skips_rows_with_no_broker_order_id() -> None:
    """Nothing to poll at the broker — registering it would spin on a KeyError."""
    from src import orchestrator

    mgr = _mgr()
    added = await orchestrator._reseed_pending_entries(
        mgr,
        _FakePool([_row(broker_order_id=None)]),
        paper_mode=False,
        order_types=_ORDER_TYPES,
    )

    assert added == 0
    assert mgr.pending_entry_ids() == set()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_intraday_entry_does_not_defer_protection() -> None:
    """MIS arms SL/target with the entry, so a reseeded MIS entry must not re-arm."""
    from src import orchestrator

    mgr = _mgr()
    await orchestrator._reseed_pending_entries(
        mgr, _FakePool([_row(product="MIS")]), paper_mode=False, order_types=_ORDER_TYPES
    )

    assert mgr._trades["TRD-E79CE1F16545"].defer_protection is False


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseeded_entry_arms_gtt_protection_when_it_fills() -> None:
    """End to end: restart, reseed, the AMO fills, a real GTT gets placed.

    This is the exact sequence that failed on 2026-08-28.
    """
    from src import orchestrator
    from src.brokers.base import Fill, Order

    broker = MagicMock(spec=BrokerInterface)
    broker.supports_gtt = MagicMock(return_value=True)
    broker.place_gtt = AsyncMock(return_value="GTT-771122")
    broker.get_order_status = AsyncMock(
        return_value=Order(
            broker_order_id="2093013611046805504",
            symbol="IFCI",
            direction="BUY",
            quantity=145,
            filled_quantity=145,
            pending_quantity=0,
            status="FILLED",
            order_type="LIMIT",
            product="CNC",
            average_price=84.27,
            tag="TRD-E79CE1F16545",
            placed_at=datetime(2026, 8, 28, 9, 7, tzinfo=UTC),
        )
    )
    broker.get_trades = AsyncMock(
        return_value=[
            Fill(
                order_id="2093013611046805504",
                trade_id="T1",
                quantity=145,
                price=84.27,
                exchange_time=datetime(2026, 8, 28, 9, 7, tzinfo=UTC),
            )
        ]
    )
    mgr = OrderLifecycleManager(broker)

    await orchestrator._reseed_pending_entries(
        mgr, _FakePool([_row()]), paper_mode=False, order_types=_ORDER_TYPES
    )
    filled = await mgr.poll_pending_entries()

    assert filled == ["TRD-E79CE1F16545"]
    broker.place_gtt.assert_awaited_once()
    gtt = broker.place_gtt.await_args.args[0]
    assert gtt.symbol == "IFCI"
    assert gtt.quantity == 145
    assert gtt.gtt_type == "TWO_LEG"
    assert mgr._trades["TRD-E79CE1F16545"].gtt_sl_id == "GTT-771122"


# ── Per-broker reseed (phase 2: exec_broker, NULL = legacy "kite") ──────────


class _BrokerPool:
    def __init__(self, rows, has_column=True):
        self._rows = rows
        self._has_column = has_column

    async def fetch(self, query, *args):
        if "exec_broker" not in query:
            return self._rows
        if not self._has_column:
            import asyncpg

            raise asyncpg.exceptions.UndefinedColumnError('column "exec_broker" does not exist')
        own = args[0]
        others = list(args[1]) if len(args) > 1 else None
        return [
            r
            for r in self._rows
            if (r.get("exec_broker") or "kite") == own
            or (others is not None and (r.get("exec_broker") or "kite") not in others)
        ]


_MIXED = [
    _row(agent_trade_id="T-KITE", exec_broker="kite"),
    _row(agent_trade_id="T-ANGEL", exec_broker="angel_one"),
    _row(agent_trade_id="T-NULL", exec_broker=None),
]
_STACKS = {"paper": object(), "live:kite": object(), "live:angel_one": object()}


async def _pending(key, has_column=True, stacks=_STACKS, legacy="kite"):
    from src import orchestrator

    mgr = _mgr()
    await orchestrator._reseed_pending_entries(
        mgr,
        _BrokerPool(_MIXED, has_column),
        orchestrator.stack_is_paper(key),
        _ORDER_TYPES,
        **orchestrator._reseed_scope(key, stacks, legacy),
    )
    return sorted(mgr.pending_entry_ids())


@pytest.mark.asyncio  # type: ignore[misc]
@pytest.mark.parametrize("legacy", ["kite", "angel_one"])  # type: ignore[misc]
async def test_pending_reseed_each_live_stack_only_its_own_rows(legacy) -> None:
    assert await _pending("live:kite", legacy=legacy) == ["T-KITE", "T-NULL"]
    assert await _pending("live:angel_one", legacy=legacy) == ["T-ANGEL"]
    assert await _pending("paper") == ["T-ANGEL", "T-KITE", "T-NULL"]  # paper unfiltered


@pytest.mark.asyncio  # type: ignore[misc]
async def test_pending_reseed_missing_column_kite_stack_only() -> None:
    assert await _pending("live:kite", has_column=False) == ["T-ANGEL", "T-KITE", "T-NULL"]
    assert await _pending("live:angel_one", has_column=False) == []
    # angel_one primary with no kite stack: never takes unstamped rows, column or not
    angel_only = {"paper": object(), "live:angel_one": object()}
    assert await _pending("live:angel_one", stacks=angel_only, legacy="angel_one") == ["T-ANGEL"]
    assert (
        await _pending("live:angel_one", has_column=False, stacks=angel_only, legacy="angel_one")
        == []
    )
