"""Regression tests for the OPEN -> REJECTED transition fix.

Before this fix, VALID_TRANSITIONS[OrderState.OPEN] did not include REJECTED.
place_entry_protection_and_poll bridges SUBMITTED -> OPEN unconditionally before
the first poll(), so a broker rejection reported on that first poll (price band,
margin, risk check, etc.) landed on the "not in allowed" branch: no state
change, no DB persist, no cleanup of any SL/target legs already placed for MIS
entries (which arm protection immediately after submit, before a fill is
confirmed) — the order was stuck looking OPEN forever with orphaned protection.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from src.brokers.base import BrokerInterface, Order, OrderRequest
from src.execution.order_lifecycle import (
    VALID_TRANSITIONS,
    OrderLifecycleManager,
    OrderState,
)

_IST = ZoneInfo("Asia/Kolkata")
_NOW = datetime(2026, 1, 15, 9, 30, 0, tzinfo=_IST)


def _order_req(agent_trade_id: str = "AT001") -> OrderRequest:
    return OrderRequest(
        symbol="INFY",
        exchange="NSE",
        direction="BUY",
        quantity=10,
        order_type="MARKET",
        product="INTRADAY",
        validity="DAY",
        agent_trade_id=agent_trade_id,
        signal_id="SIG001",
    )


def _broker_order(status: str) -> Order:
    return Order(
        broker_order_id="BRK001",
        symbol="INFY",
        direction="BUY",
        quantity=10,
        filled_quantity=0,
        pending_quantity=10,
        status=status,
        order_type="MARKET",
        product="INTRADAY",
        average_price=0.0,
        tag="",
        placed_at=_NOW,
    )


def test_open_allows_rejected_in_the_transition_table() -> None:
    """The literal table fix: OPEN must legally reach REJECTED."""
    assert OrderState.REJECTED in VALID_TRANSITIONS[OrderState.OPEN]
    # Sanity: the states OPEN already allowed are untouched.
    assert OrderState.CANCELLED in VALID_TRANSITIONS[OrderState.OPEN]
    assert OrderState.TIMED_OUT in VALID_TRANSITIONS[OrderState.OPEN]
    assert OrderState.FILLED in VALID_TRANSITIONS[OrderState.OPEN]
    assert OrderState.PARTIAL in VALID_TRANSITIONS[OrderState.OPEN]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_poll_from_open_to_rejected_actually_commits_and_persists() -> None:
    """The core regression: a REJECTED status reported while the record is OPEN
    must actually move the record to REJECTED and persist it — not silently
    return the record unchanged."""
    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="BRK001")
    broker.get_order_status = AsyncMock(return_value=_broker_order("REJECTED"))
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=1)
    repo.update_state = AsyncMock()

    mgr = OrderLifecycleManager(broker, trade_repo=repo)
    record = mgr.create_trade(_order_req("AT001"))
    mgr.transition("AT001", OrderState.SUBMITTED)
    mgr.transition("AT001", OrderState.OPEN)
    assert record.state == OrderState.OPEN  # pre-fix starting point

    updated = await mgr.poll("AT001")

    assert updated.state == OrderState.REJECTED
    repo.update_state.assert_awaited()
    args, kwargs = repo.update_state.call_args
    assert args[0] == "AT001"
    assert args[1] == "REJECTED"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_poll_rejection_cancels_orphaned_protection_legs() -> None:
    """MIS entries arm SL/target immediately after submit, before fill
    confirmation — a rejection detected here must cancel those resting legs,
    not leave them orphaned at the broker."""
    broker = MagicMock(spec=BrokerInterface)
    broker.get_order_status = AsyncMock(return_value=_broker_order("REJECTED"))
    broker.cancel_order = AsyncMock(return_value=True)
    broker.cancel_gtt = AsyncMock(return_value=True)

    mgr = OrderLifecycleManager(broker)
    record = mgr.create_trade(_order_req("AT002"))
    record.sl_order_id = "SL001"
    record.target_order_id = "TGT001"
    mgr.transition("AT002", OrderState.SUBMITTED)
    mgr.transition("AT002", OrderState.OPEN)

    updated = await mgr.poll("AT002")

    assert updated.state == OrderState.REJECTED
    assert broker.cancel_order.await_count == 2  # sl leg + target leg
    broker.cancel_order.assert_any_await("SL001")
    broker.cancel_order.assert_any_await("TGT001")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_poll_rejection_notifies(monkeypatch) -> None:
    """The rejection must reach the notifier, with a note describing what
    actually happened (not the unrelated default 'protection placement
    failed' text meant for a different caller of _persist_rejected)."""
    import src.execution.order_lifecycle as ol_mod

    calls: list[tuple] = []
    monkeypatch.setattr(ol_mod, "notify", lambda kind, **kw: calls.append((kind, kw)))

    broker = MagicMock(spec=BrokerInterface)
    broker.get_order_status = AsyncMock(return_value=_broker_order("REJECTED"))

    mgr = OrderLifecycleManager(broker, is_paper=True)
    mgr.create_trade(_order_req("AT003"))
    mgr.transition("AT003", OrderState.SUBMITTED)
    mgr.transition("AT003", OrderState.OPEN)

    await mgr.poll("AT003")

    assert len(calls) == 1
    kind, kwargs = calls[0]
    assert kind == ol_mod.EventKind.ENTRY_REJECTED
    assert kwargs["agent_trade_id"] == "AT003"
    assert kwargs["dedupe_key"] == "reject:AT003"
    assert "broker rejected" in kwargs["note"]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_poll_rejection_from_submitted_still_works_unchanged() -> None:
    """SUBMITTED -> REJECTED was already legal before this fix — must keep
    working exactly the same way (no regression on the already-working path)."""
    broker = MagicMock(spec=BrokerInterface)
    broker.get_order_status = AsyncMock(return_value=_broker_order("REJECTED"))

    mgr = OrderLifecycleManager(broker)
    mgr.create_trade(_order_req("AT004"))
    mgr.transition("AT004", OrderState.SUBMITTED)

    updated = await mgr.poll("AT004")

    assert updated.state == OrderState.REJECTED


@pytest.mark.asyncio  # type: ignore[misc]
async def test_filled_to_rejected_is_still_illegal_and_safe() -> None:
    """Guards the EMMVEE invariant (orchestrator.py:_execute_intraday_signal
    comment, 2026-08-13 incident): a REJECTED status reported for an
    already-FILLED record must NOT call cancel_protection() (which would
    raise InvalidTransitionError trying to re-transition a terminal-bound
    FILLED record) — it must fall through to the safety-net branch and leave
    the FILLED record untouched, exactly as before this fix."""
    broker = MagicMock(spec=BrokerInterface)
    broker.get_order_status = AsyncMock(return_value=_broker_order("REJECTED"))
    broker.cancel_order = AsyncMock(return_value=True)

    mgr = OrderLifecycleManager(broker)
    mgr.create_trade(_order_req("AT005"))
    mgr.transition("AT005", OrderState.SUBMITTED)
    mgr.transition("AT005", OrderState.OPEN)
    mgr.transition("AT005", OrderState.FILLED)  # simulates an already-realized fill

    updated = await mgr.poll("AT005")  # must not raise

    assert updated.state == OrderState.FILLED  # untouched
    broker.cancel_order.assert_not_awaited()  # cancel_protection() never called
