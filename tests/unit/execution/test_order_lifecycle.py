"""Unit tests for src/execution/order_lifecycle.py."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from src.brokers.base import BrokerInterface, Fill, Order, OrderRequest
from src.execution.order_lifecycle import (
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    InvalidTransitionError,
    OrderLifecycleManager,
    OrderState,
    TradeRecord,
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


def _make_broker_order(status: str = "OPEN") -> Order:
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


def _make_mgr() -> tuple[OrderLifecycleManager, MagicMock]:
    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="BRK001")
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    broker.get_trades = AsyncMock(return_value=[])
    mgr = OrderLifecycleManager(broker)
    return mgr, broker


# ── OrderState ─────────────────────────────────────────────────────────────────


def test_terminal_states_are_correct() -> None:
    assert OrderState.CLOSED in TERMINAL_STATES
    assert OrderState.REJECTED in TERMINAL_STATES
    assert OrderState.CANCELLED in TERMINAL_STATES
    assert OrderState.TIMED_OUT in TERMINAL_STATES
    assert OrderState.OPEN not in TERMINAL_STATES
    assert OrderState.FILLED not in TERMINAL_STATES


def test_valid_transitions_complete() -> None:
    for state in OrderState:
        assert state in VALID_TRANSITIONS


# ── TradeRecord ────────────────────────────────────────────────────────────────


def test_trade_record_initial_state() -> None:
    req = _order_req()
    record = TradeRecord(agent_trade_id="AT001", signal_id="SIG001", order_request=req)
    assert record.state == OrderState.PENDING_SUBMIT
    assert len(record.state_history) == 1
    assert record.state_history[0][0] == OrderState.PENDING_SUBMIT


def test_trade_record_is_terminal_false_initially() -> None:
    req = _order_req()
    record = TradeRecord(agent_trade_id="AT001", signal_id="SIG001", order_request=req)
    assert record.is_terminal is False


def test_trade_record_is_terminal_true_for_closed() -> None:
    req = _order_req()
    record = TradeRecord(
        agent_trade_id="AT001",
        signal_id="SIG001",
        order_request=req,
        state=OrderState.CLOSED,
    )
    assert record.is_terminal is True


# ── create_trade ───────────────────────────────────────────────────────────────


def test_create_trade_registers_record() -> None:
    mgr, _ = _make_mgr()
    req = _order_req()
    record = mgr.create_trade(req)
    assert record.agent_trade_id == "AT001"
    assert record.state == OrderState.PENDING_SUBMIT
    assert mgr.get_trade("AT001") is record


def test_get_trade_unknown_raises() -> None:
    mgr, _ = _make_mgr()
    with pytest.raises(KeyError, match="AT999"):
        mgr.get_trade("AT999")


# ── transition ─────────────────────────────────────────────────────────────────


def test_valid_transition_updates_state() -> None:
    mgr, _ = _make_mgr()
    mgr.create_trade(_order_req())
    mgr.transition("AT001", OrderState.SUBMITTED)
    assert mgr.get_trade("AT001").state == OrderState.SUBMITTED


def test_transition_appends_to_history() -> None:
    mgr, _ = _make_mgr()
    mgr.create_trade(_order_req())
    mgr.transition("AT001", OrderState.SUBMITTED)
    history = mgr.get_trade("AT001").state_history
    assert len(history) == 2
    assert history[1][0] == OrderState.SUBMITTED


def test_invalid_transition_raises() -> None:
    mgr, _ = _make_mgr()
    mgr.create_trade(_order_req())
    with pytest.raises(InvalidTransitionError, match="PENDING_SUBMIT"):
        mgr.transition("AT001", OrderState.FILLED)


def test_terminal_state_cannot_transition() -> None:
    mgr, _ = _make_mgr()
    mgr.create_trade(_order_req())
    mgr.transition("AT001", OrderState.REJECTED)
    with pytest.raises(InvalidTransitionError):
        mgr.transition("AT001", OrderState.OPEN)


# ── submit ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_submit_calls_broker_and_returns_order_id() -> None:
    mgr, broker = _make_mgr()
    mgr.create_trade(_order_req())
    broker_order_id = await mgr.submit("AT001")
    assert broker_order_id == "BRK001"
    broker.place_order.assert_awaited_once()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_submit_advances_to_submitted() -> None:
    mgr, _ = _make_mgr()
    mgr.create_trade(_order_req())
    await mgr.submit("AT001")
    assert mgr.get_trade("AT001").state == OrderState.SUBMITTED


@pytest.mark.asyncio  # type: ignore[misc]
async def test_submit_stores_broker_order_id() -> None:
    mgr, _ = _make_mgr()
    mgr.create_trade(_order_req())
    await mgr.submit("AT001")
    assert mgr.get_trade("AT001").broker_order_id == "BRK001"


# ── poll ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_poll_advances_submitted_to_open() -> None:
    mgr, broker = _make_mgr()
    mgr.create_trade(_order_req())
    await mgr.submit("AT001")

    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    record = await mgr.poll("AT001")
    assert record.state == OrderState.OPEN


@pytest.mark.asyncio  # type: ignore[misc]
async def test_poll_advances_open_to_filled_and_fetches_fills() -> None:
    mgr, broker = _make_mgr()
    mgr.create_trade(_order_req())
    await mgr.submit("AT001")

    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    await mgr.poll("AT001")

    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))
    fill = Fill(order_id="BRK001", trade_id="T001", quantity=10, price=1500.0, exchange_time=_NOW)
    broker.get_trades = AsyncMock(return_value=[fill])

    record = await mgr.poll("AT001")
    assert record.state == OrderState.FILLED
    assert record.fill_price == 1500.0
    assert record.filled_quantity == 10


@pytest.mark.asyncio  # type: ignore[misc]
async def test_poll_weighted_avg_fill_price() -> None:
    mgr, broker = _make_mgr()
    mgr.create_trade(_order_req())
    await mgr.submit("AT001")

    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    await mgr.poll("AT001")

    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))
    fills = [
        Fill(order_id="BRK001", trade_id="T001", quantity=5, price=1500.0, exchange_time=_NOW),
        Fill(order_id="BRK001", trade_id="T002", quantity=5, price=1510.0, exchange_time=_NOW),
    ]
    broker.get_trades = AsyncMock(return_value=fills)

    record = await mgr.poll("AT001")
    assert record.fill_price == 1505.0  # (5*1500 + 5*1510) / 10


@pytest.mark.asyncio  # type: ignore[misc]
async def test_poll_terminal_trade_skips_broker_call() -> None:
    mgr, broker = _make_mgr()
    mgr.create_trade(_order_req())
    mgr.transition("AT001", OrderState.REJECTED)

    record = await mgr.poll("AT001")
    assert record.state == OrderState.REJECTED
    broker.get_order_status.assert_not_awaited()


# ── close_trade ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_close_trade_from_filled() -> None:
    mgr, broker = _make_mgr()
    mgr.create_trade(_order_req())
    await mgr.submit("AT001")

    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    await mgr.poll("AT001")

    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))
    broker.get_trades = AsyncMock(return_value=[])
    await mgr.poll("AT001")

    mgr.close_trade("AT001")
    assert mgr.get_trade("AT001").state == OrderState.CLOSED
    assert mgr.get_trade("AT001").is_terminal is True


# ── get_open_trades ────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_open_trades() -> None:
    mgr, _ = _make_mgr()
    mgr.create_trade(_order_req("AT001"))
    mgr.create_trade(_order_req("AT002"))
    await mgr.submit("AT001")  # SUBMITTED → shows in open
    # AT002 stays at PENDING_SUBMIT → not in open

    open_trades = mgr.get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0].agent_trade_id == "AT001"


# ── max_risk_rupees ────────────────────────────────────────────────────────────


def test_lifecycle_manager_stores_max_risk() -> None:
    broker = MagicMock(spec=BrokerInterface)
    mgr = OrderLifecycleManager(broker, max_risk_rupees=5000.0)
    assert mgr._max_risk == 5000.0


def test_lifecycle_manager_default_max_risk_zero() -> None:
    broker = MagicMock(spec=BrokerInterface)
    mgr = OrderLifecycleManager(broker)
    assert mgr._max_risk == 0.0


# ── place_entry_and_protection ─────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_entry_and_protection_registers_trade() -> None:
    mgr, broker = _make_mgr()
    broker.place_order = AsyncMock(side_effect=["E001", "SL001", "TGT001"])

    entry_id, sl_id, tgt_id = await mgr.place_entry_and_protection(
        signal_id="SIG001",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
    )

    assert entry_id == "E001"
    assert sl_id == "SL001"
    assert tgt_id == "TGT001"
    trades = mgr.get_all_trades()
    assert len(trades) == 1
    rec = trades[0]
    assert rec.sl_price == 1450.0
    assert rec.direction == "BUY"
    assert rec.broker_order_id == "E001"
    assert rec.state == OrderState.SUBMITTED


@pytest.mark.asyncio  # type: ignore[misc]
async def test_intraday_entry_is_marketable_limit_not_market() -> None:
    # Regression (2026-08-12): intraday entries were plain MARKET orders — no cap
    # on entry slippage. A fast move (AIAENG SHORT filled then breached its own
    # SL 9s later) could enter at an already-extreme price. Entries are now a
    # LIMIT capped _INTRADAY_ENTRY_CHASE_CAP_PCT beyond the signal price, in the
    # adverse direction: worse than the cap and the order simply doesn't fill.
    mgr, broker = _make_mgr()
    broker.place_order = AsyncMock(side_effect=["E001", "SL001", "TGT001"])

    await mgr.place_entry_and_protection(
        signal_id="SIG001",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="MIS",
    )

    entry_req = broker.place_order.call_args_list[0].args[0]
    assert entry_req.order_type == "LIMIT"
    assert entry_req.price == pytest.approx(1500.0 * 1.0015)  # BUY: cap above signal price


@pytest.mark.asyncio  # type: ignore[misc]
async def test_intraday_short_entry_limit_price_below_signal() -> None:
    mgr, broker = _make_mgr()
    broker.place_order = AsyncMock(side_effect=["E001", "SL001", "TGT001"])

    await mgr.place_entry_and_protection(
        signal_id="SIG002",
        symbol="INFY",
        direction="SELL",
        qty=10,
        entry_price=1500.0,
        sl_price=1550.0,
        target_price=1400.0,
        product="MIS",
    )

    entry_req = broker.place_order.call_args_list[0].args[0]
    assert entry_req.order_type == "LIMIT"
    assert entry_req.price == pytest.approx(1500.0 * 0.9985)  # SELL: cap below signal price


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_entry_and_protection_registered_trade_is_pollable() -> None:
    mgr, broker = _make_mgr()
    broker.place_order = AsyncMock(side_effect=["E001", "SL001", "TGT001"])
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))

    await mgr.place_entry_and_protection(
        signal_id="SIG001",
        symbol="INFY",
        direction="BUY",
        qty=5,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
    )
    trade_id = mgr.get_all_trades()[0].agent_trade_id
    record = await mgr.poll(trade_id)
    assert record.state == OrderState.OPEN


# ── post_fill_risk in poll ─────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_poll_filled_no_sl_skips_post_fill_check() -> None:
    """When sl_price == 0 (old-style trade), post_fill_risk is never called."""
    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="BRK001")
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    broker.get_trades = AsyncMock(
        return_value=[
            Fill(order_id="BRK001", trade_id="T1", quantity=10, price=1500.0, exchange_time=_NOW)
        ]
    )
    mgr = OrderLifecycleManager(broker, max_risk_rupees=5000.0)
    mgr.create_trade(_order_req())
    await mgr.submit("AT001")
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    await mgr.poll("AT001")
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))

    record = await mgr.poll("AT001")
    assert record.state == OrderState.FILLED
    assert record.fill_price == 1500.0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_poll_filled_with_sl_no_breach() -> None:
    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(side_effect=["E001", "SL001", "TGT001"])
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    fill = Fill(order_id="E001", trade_id="T1", quantity=10, price=1500.0, exchange_time=_NOW)
    broker.get_trades = AsyncMock(return_value=[fill])
    mgr = OrderLifecycleManager(broker, max_risk_rupees=5000.0)

    await mgr.place_entry_and_protection(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
    )
    trade_id = mgr.get_all_trades()[0].agent_trade_id
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    await mgr.poll(trade_id)
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))

    record = await mgr.poll(trade_id)
    # fill=1500, sl=1450 → risk=500 < cap=5000 → no breach
    assert record.state == OrderState.FILLED
    assert record.fill_price == 1500.0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_poll_filled_with_sl_breach_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(side_effect=["E001", "SL001", "TGT001"])
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    # fill=1490, sl=1450, qty=10 → risk=400; cap=100 → breach
    fill = Fill(order_id="E001", trade_id="T1", quantity=10, price=1490.0, exchange_time=_NOW)
    broker.get_trades = AsyncMock(return_value=[fill])
    mgr = OrderLifecycleManager(broker, max_risk_rupees=100.0)

    await mgr.place_entry_and_protection(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
    )
    trade_id = mgr.get_all_trades()[0].agent_trade_id
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    await mgr.poll(trade_id)
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))

    with caplog.at_level(logging.WARNING):
        record = await mgr.poll(trade_id)

    assert record.state == OrderState.FILLED
    assert any("SLIPPAGE_RISK_BREACH" in m for m in caplog.messages)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_post_fill_cap_uses_the_cap_the_order_was_sized_with(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A trade sized at a %-of-equity cap above the manager's flat cap must not
    have its stop tightened post-fill (meta max_risk_rupees wins)."""
    import logging

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(side_effect=["E001", "SL001", "TGT001"])
    broker.modify_order = AsyncMock()
    fill = Fill(order_id="E001", trade_id="T1", quantity=10, price=1490.0, exchange_time=_NOW)
    broker.get_trades = AsyncMock(return_value=[fill])
    mgr = OrderLifecycleManager(broker, max_risk_rupees=100.0)  # flat cap far below 500 risk

    await mgr.place_entry_and_protection(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        meta={"max_risk_rupees": 1_000.0},
    )
    trade_id = mgr.get_all_trades()[0].agent_trade_id
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    await mgr.poll(trade_id)
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))
    with caplog.at_level(logging.WARNING):
        record = await mgr.poll(trade_id)

    assert record.state == OrderState.FILLED
    assert not any("SLIPPAGE_RISK_BREACH" in m for m in caplog.messages)
    assert record.sl_price == pytest.approx(1440.0)  # re-anchored only, not cap-tightened


@pytest.mark.asyncio  # type: ignore[misc]
async def test_poll_filled_no_alert_manager_no_crash() -> None:
    """alert_manager=None (default) must not crash on breach."""
    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(side_effect=["E001", "SL001", "TGT001"])
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    fill = Fill(order_id="E001", trade_id="T1", quantity=10, price=1490.0, exchange_time=_NOW)
    broker.get_trades = AsyncMock(return_value=[fill])
    mgr = OrderLifecycleManager(broker, max_risk_rupees=100.0)  # no alert_manager

    await mgr.place_entry_and_protection(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
    )
    trade_id = mgr.get_all_trades()[0].agent_trade_id
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    await mgr.poll(trade_id)
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))
    record = await mgr.poll(trade_id)
    assert record.state == OrderState.FILLED  # no exception


@pytest.mark.asyncio  # type: ignore[misc]
async def test_poll_filled_breach_calls_alert_manager() -> None:
    """Injected AlertManager.raise_alert called with CRITICAL tier on breach."""
    from unittest.mock import MagicMock as MM

    from src.infrastructure.alert_system import AlertManager, AlertTier

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(side_effect=["E001", "SL001", "TGT001"])
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    fill = Fill(order_id="E001", trade_id="T1", quantity=10, price=1490.0, exchange_time=_NOW)
    broker.get_trades = AsyncMock(return_value=[fill])

    mock_am = MM(spec=AlertManager)
    mgr = OrderLifecycleManager(broker, max_risk_rupees=100.0, alert_manager=mock_am)

    await mgr.place_entry_and_protection(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
    )
    trade_id = mgr.get_all_trades()[0].agent_trade_id
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))
    await mgr.poll(trade_id)
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))
    await mgr.poll(trade_id)

    mock_am.raise_alert.assert_called_once()
    call_args = mock_am.raise_alert.call_args
    assert call_args.args[0] == AlertTier.CRITICAL


# ── Stage 1 unification: is_paper persistence routing ──────────────────────────


def _filled_broker(fill_price: float) -> MagicMock:
    """Broker whose entry/SL/target place + that reports a synchronous FILLED."""
    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(side_effect=["E001", "SL001", "TGT001"])
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))
    broker.get_trades = AsyncMock(
        return_value=[
            Fill(order_id="E001", trade_id="T1", quantity=10, price=fill_price, exchange_time=_NOW)
        ]
    )
    broker.modify_order = AsyncMock(return_value="OK")
    return broker


@pytest.mark.asyncio  # type: ignore[misc]
async def test_is_paper_true_routes_persistence_to_paper_trades() -> None:
    """is_paper=True → repo.insert/update_fill receive is_paper=True (paper_trades)."""
    broker = _filled_broker(1500.0)
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=1)
    repo.update_fill = AsyncMock(return_value=None)
    mgr = OrderLifecycleManager(broker, trade_repo=repo, is_paper=True)

    await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="MIS",
    )

    repo.insert.assert_awaited_once()
    assert repo.insert.await_args.kwargs["is_paper"] is True
    repo.update_fill.assert_awaited_once()
    assert repo.update_fill.await_args.kwargs["is_paper"] is True


@pytest.mark.asyncio  # type: ignore[misc]
async def test_is_paper_defaults_false_routes_to_trades() -> None:
    """Default (live) → repo calls carry is_paper=False (trades table)."""
    broker = _filled_broker(1500.0)
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=1)
    repo.update_fill = AsyncMock(return_value=None)
    mgr = OrderLifecycleManager(broker, trade_repo=repo)  # is_paper omitted

    await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="MIS",
    )

    assert repo.insert.await_args.kwargs["is_paper"] is False
    assert repo.update_fill.await_args.kwargs["is_paper"] is False


@pytest.mark.asyncio  # type: ignore[misc]
async def test_meta_llm_verdict_persisted_on_entry() -> None:
    """The G9 verdict in crm_signal.meta lands in the entry INSERT row."""
    broker = _filled_broker(1500.0)
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=1)
    repo.update_fill = AsyncMock(return_value=None)
    mgr = OrderLifecycleManager(broker, trade_repo=repo, is_paper=True)

    await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="MIS",
        meta={"llm_confidence": 0.82, "llm_flag": "CONFIRM", "llm_reason": "ok"},
    )

    row = repo.insert.await_args.args[0]
    assert float(row["llm_confidence"]) == pytest.approx(0.82)
    assert row["llm_flag"] == "CONFIRM"
    assert row["llm_reason"] == "ok"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_no_meta_omits_llm_columns() -> None:
    """Without meta the LLM columns are not written (stay NULL)."""
    broker = _filled_broker(1500.0)
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=1)
    repo.update_fill = AsyncMock(return_value=None)
    mgr = OrderLifecycleManager(broker, trade_repo=repo, is_paper=True)

    await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="MIS",
    )

    row = repo.insert.await_args.args[0]
    assert "llm_confidence" not in row
    assert "llm_flag" not in row


# ── Stage 1 unification: place_entry_protection_and_poll fills + reanchors ──────

# EICHERMOT-style long: signal target sits BELOW the actual fill (the instant-loss
# scenario). risk = 7600-7591.12 = 8.88; ratio = (7608.88-7600)/8.88 = 1.0.
_SIG_ENTRY, _SIG_SL, _SIG_TGT = 7600.0, 7591.12, 7608.88
_FILL = 7615.61


@pytest.mark.asyncio  # type: ignore[misc]
async def test_place_entry_protection_and_poll_fills_and_reanchors() -> None:
    broker = _filled_broker(_FILL)
    mgr = OrderLifecycleManager(broker, max_risk_rupees=0.0, is_paper=True)

    record = await mgr.place_entry_protection_and_poll(
        signal_id="ORB-EICHERMOT-093251",
        symbol="EICHERMOT",
        direction="BUY",
        qty=10,
        entry_price=_SIG_ENTRY,
        sl_price=_SIG_SL,
        target_price=_SIG_TGT,
        product="MIS",
    )

    assert record.state == OrderState.FILLED
    assert record.fill_price == pytest.approx(_FILL)
    # Stale target (7608.88) sat below the fill (7615.61); after reanchor the target
    # must sit ABOVE the fill so the protection can't fire instantly. sl < fill < tgt.
    assert record.sl_price < _FILL < record.target_price
    # reanchor applied to the live protection orders
    broker.modify_order.assert_awaited()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_unified_reanchor_matches_canonical() -> None:
    """Parity: the unified path's reanchored levels equal reanchor_protection() —
    the exact function the legacy PaperTradingEngine uses — for the same fill."""
    from src.execution.post_fill_risk import reanchor_protection

    broker = _filled_broker(_FILL)
    mgr = OrderLifecycleManager(broker, max_risk_rupees=0.0, is_paper=True)

    record = await mgr.place_entry_protection_and_poll(
        signal_id="ORB-EICHERMOT-093251",
        symbol="EICHERMOT",
        direction="BUY",
        qty=10,
        entry_price=_SIG_ENTRY,
        sl_price=_SIG_SL,
        target_price=_SIG_TGT,
        product="MIS",
    )

    expected = reanchor_protection(
        fill_price=_FILL,
        signal_entry=_SIG_ENTRY,
        signal_sl=_SIG_SL,
        signal_target=_SIG_TGT,
        direction="BUY",
        quantity=10,
        max_risk_rupees=0.0,
    )
    assert record.sl_price == pytest.approx(expected.sl)
    assert record.target_price == pytest.approx(expected.target)


# ── OrderMonitor.execute_events ────────────────────────────────────────────────

from src.execution.order_lifecycle import MonitoredPosition  # noqa: E402


def _monitored(tid: str = "TRD001", symbol: str = "INFY") -> MonitoredPosition:
    return MonitoredPosition(
        agent_trade_id=tid,
        symbol=symbol,
        direction="BUY",
        quantity=10,
        fill_price=1500.0,
        initial_sl=1450.0,
        current_sl=1450.0,
        initial_target=1600.0,
        current_target=1600.0,
        sl_order_id="SL001",
        target_order_id="TGT001",
    )


@pytest.mark.asyncio  # type: ignore[misc]
async def test_update_sl_persists_current_sl_to_db() -> None:
    """update_sl() must write current_sl back to the DB, not just modify the
    broker order and the in-memory position — /api/positions and
    /api/swing_positions read current_sl only from the DB, so a trail/
    breakeven move that never reaches it would never show up anywhere."""
    from decimal import Decimal

    from src.execution.order_lifecycle import OrderMonitor

    broker = MagicMock(spec=BrokerInterface)
    broker.modify_order = AsyncMock(return_value=True)
    trade_repo = MagicMock()
    trade_repo.update_state = AsyncMock()

    mon = OrderMonitor(broker, trade_repo=trade_repo)
    mon.track(_monitored())
    await mon.update_sl("TRD001", 1475.5)

    trade_repo.update_state.assert_awaited_once()
    args, kwargs = trade_repo.update_state.call_args
    assert args[0] == "TRD001"
    assert kwargs["current_sl"] == Decimal("1475.5")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_book_partial_persists_reduced_quantity_to_db() -> None:
    """book_partial() reduces pos.quantity in memory — the DB row (source of
    truth for every dashboard table) must reflect the reduced size too."""
    from src.execution.order_lifecycle import OrderMonitor

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="PARTIAL001")
    trade_repo = MagicMock()
    trade_repo.update_state = AsyncMock()

    mon = OrderMonitor(broker, trade_repo=trade_repo)
    mon.track(_monitored())  # quantity=10
    await mon.book_partial("TRD001", 0.5, 1550.0)

    trade_repo.update_state.assert_awaited_once()
    args, kwargs = trade_repo.update_state.call_args
    assert args[0] == "TRD001"
    assert kwargs["quantity"] == 5


@pytest.mark.asyncio  # type: ignore[misc]
async def test_execute_events_target_limit_missed_places_market() -> None:
    broker = MagicMock(spec=BrokerInterface)
    broker.cancel_order = AsyncMock(return_value=True)
    broker.place_order = AsyncMock(return_value="MKT001")
    from src.execution.order_lifecycle import OrderMonitor

    mon = OrderMonitor(broker)
    pos = _monitored()
    mon.track(pos)
    events = [{"type": "TARGET_LIMIT_MISSED", "agent_trade_id": "TRD001", "symbol": "INFY"}]
    await mon.execute_events(events)
    broker.cancel_order.assert_awaited_once_with("TGT001")
    broker.place_order.assert_awaited_once()
    req = broker.place_order.call_args.args[0]
    # Marketable limit, not MARKET (2026-08-20): caps worst-case slippage instead
    # of accepting an unbounded fill — same technique already used for entries.
    assert req.order_type == "LIMIT"
    assert req.price is not None
    assert mon.get_monitored() == []  # untracked after exit


@pytest.mark.asyncio  # type: ignore[misc]
async def test_execute_events_sl_gap_breach_emergency_exit() -> None:
    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="EMRG001")
    from src.execution.order_lifecycle import OrderMonitor

    mon = OrderMonitor(broker)
    mon.track(_monitored())
    events = [{"type": "SL_GAP_BREACH", "agent_trade_id": "TRD001", "symbol": "INFY"}]
    await mon.execute_events(events)
    broker.place_order.assert_awaited_once()
    req = broker.place_order.call_args.args[0]
    # Marketable limit, not MARKET (2026-08-20) — see test above.
    assert req.order_type == "LIMIT"
    assert req.price is not None
    assert req.direction == "SELL"
    assert mon.get_monitored() == []


@pytest.mark.asyncio  # type: ignore[misc]
async def test_execute_events_target_limit_missed_uses_position_product() -> None:
    """CNC (swing) position: the exit order must square off as CNC, not the
    hardcoded INTRADAY (MIS) — placing an MIS sell against a CNC holding is a
    different broker instruction, not a clean square-off of that holding."""
    broker = MagicMock(spec=BrokerInterface)
    broker.cancel_order = AsyncMock(return_value=True)
    broker.place_order = AsyncMock(return_value="MKT001")
    from src.execution.order_lifecycle import OrderMonitor

    mon = OrderMonitor(broker)
    pos = _monitored()
    pos.product = "CNC"
    mon.track(pos)
    events = [{"type": "TARGET_LIMIT_MISSED", "agent_trade_id": "TRD001", "symbol": "INFY"}]
    await mon.execute_events(events)
    req = broker.place_order.call_args.args[0]
    assert req.product == "CNC"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_execute_events_sl_gap_breach_uses_position_product() -> None:
    """Same as above for the SL emergency-exit path."""
    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="EMRG001")
    from src.execution.order_lifecycle import OrderMonitor

    mon = OrderMonitor(broker)
    pos = _monitored()
    pos.product = "CNC"
    mon.track(pos)
    events = [{"type": "SL_GAP_BREACH", "agent_trade_id": "TRD001", "symbol": "INFY"}]
    await mon.execute_events(events)
    req = broker.place_order.call_args.args[0]
    assert req.product == "CNC"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_execute_events_unknown_type_ignored() -> None:
    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock()
    from src.execution.order_lifecycle import OrderMonitor

    mon = OrderMonitor(broker)
    mon.track(_monitored())
    await mon.execute_events([{"type": "UNKNOWN_EVENT", "agent_trade_id": "TRD001"}])
    broker.place_order.assert_not_awaited()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_execute_events_broker_error_does_not_propagate() -> None:
    broker = MagicMock(spec=BrokerInterface)
    broker.cancel_order = AsyncMock(side_effect=RuntimeError("broker down"))
    broker.place_order = AsyncMock(side_effect=RuntimeError("broker down"))
    from src.execution.order_lifecycle import OrderMonitor

    mon = OrderMonitor(broker)
    mon.track(_monitored())
    # Must not raise
    await mon.execute_events([{"type": "TARGET_LIMIT_MISSED", "agent_trade_id": "TRD001"}])


# ── OrderMonitor.cycle — direction-aware breach detection ──────────────────────


def _monitored_short(tid: str = "TRD002", symbol: str = "INFY") -> MonitoredPosition:
    """Short position: target BELOW entry, SL ABOVE entry."""
    return MonitoredPosition(
        agent_trade_id=tid,
        symbol=symbol,
        direction="SELL",
        quantity=10,
        fill_price=1500.0,
        initial_sl=1550.0,
        current_sl=1550.0,
        initial_target=1400.0,
        current_target=1400.0,
        sl_order_id="SL001",
        target_order_id="TGT001",
    )


def _never_fills_broker(ltp: dict[str, float]) -> MagicMock:
    """Broker whose place_order/get_order_status simulate a resting exit order
    that never gets a matching fill — drives the chase loop to escalation
    (2026-08-20: SL/target breaches now re-peg the exit order for
    chase_max_attempts cycles — see OrderMonitor._advance_chase — before
    falling back to the guaranteed SL_GAP_BREACH/TARGET_LIMIT_MISSED exit)."""
    broker = MagicMock(spec=BrokerInterface)
    broker.get_ltp = AsyncMock(return_value=ltp)
    broker.place_order = AsyncMock(side_effect=lambda req: f"CHASE-{req.agent_trade_id}")
    broker.get_order_status = AsyncMock(
        return_value=Order(
            broker_order_id="x",
            symbol="INFY",
            direction="SELL",
            quantity=10,
            filled_quantity=0,
            pending_quantity=10,
            status="OPEN",
            order_type="LIMIT",
            product="INTRADAY",
            average_price=0.0,
            tag="",
            placed_at=_NOW,
        )
    )
    broker.cancel_order = AsyncMock(return_value=True)
    return broker


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cycle_long_detects_target_and_sl() -> None:
    from src.execution.order_lifecycle import OrderMonitor

    # LTP gapped above the long target — chase for chase_max_attempts cycles,
    # then TARGET_LIMIT_MISSED expected once attempts are exhausted.
    broker = _never_fills_broker({"INFY": 1650.0})
    mon = OrderMonitor(broker)
    mon.track(_monitored())
    for _ in range(mon._chase_max_attempts):
        events = await mon.cycle()
        assert not any(e["type"] == "TARGET_LIMIT_MISSED" for e in events)
    events = await mon.cycle()
    assert any(e["type"] == "TARGET_LIMIT_MISSED" for e in events)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cycle_short_detects_target_limit_missed() -> None:
    from src.execution.order_lifecycle import OrderMonitor

    # LTP fell below the short target — same chase-then-escalate flow.
    broker = _never_fills_broker({"INFY": 1395.0})
    mon = OrderMonitor(broker)
    mon.track(_monitored_short())
    for _ in range(mon._chase_max_attempts):
        await mon.cycle()
    events = await mon.cycle()
    assert any(e["type"] == "TARGET_LIMIT_MISSED" for e in events)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cycle_short_detects_sl_gap_breach() -> None:
    from src.execution.order_lifecycle import OrderMonitor

    # LTP gapped above the short SL — chase-then-escalate to SL_GAP_BREACH.
    broker = _never_fills_broker({"INFY": 1560.0})
    mon = OrderMonitor(broker)
    mon.track(_monitored_short())
    for _ in range(mon._chase_max_attempts):
        await mon.cycle()
    events = await mon.cycle()
    assert any(e["type"] == "SL_GAP_BREACH" for e in events)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cycle_long_detects_sl_breach_at_exact_sl_no_extra_slippage_wait() -> None:
    from src.execution.order_lifecycle import OrderMonitor

    # Regression (2026-08-11): the breach used to require price 0.1% PAST the SL
    # before firing, so ~half of all intraday stop-outs sat unhandled until a
    # later, worse price. LTP exactly AT the SL (1450, per _monitored()) must
    # start chasing immediately — no further adverse move required to trigger.
    broker = _never_fills_broker({"INFY": 1450.0})
    mon = OrderMonitor(broker)
    mon.track(_monitored())
    events = await mon.cycle()
    assert mon.get("TRD001").chase_reason == "SL"  # chase started this cycle
    for _ in range(mon._chase_max_attempts):
        events = await mon.cycle()
    assert any(e["type"] == "SL_GAP_BREACH" for e in events)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cycle_sl_gap_breach_fires_without_sl_order_id() -> None:
    from src.execution.order_lifecycle import MonitoredPosition, OrderMonitor

    # Regression (2026-08-17): a position reseeded into OrderMonitor after a
    # restart (see orchestrator._reseed_order_monitor) may have no sl_order_id
    # on file. The breach must still be caught and eventually exited — a
    # missing order id used to silently suppress the emergency exit entirely.
    pos = MonitoredPosition(
        agent_trade_id="TRD003",
        symbol="INFY",
        direction="BUY",
        quantity=10,
        fill_price=1500.0,
        initial_sl=1450.0,
        current_sl=1450.0,
        initial_target=1600.0,
        current_target=1600.0,
        sl_order_id="",
        target_order_id="",
    )
    broker = _never_fills_broker({"INFY": 1440.0})
    mon = OrderMonitor(broker)
    mon.track(pos)
    for _ in range(mon._chase_max_attempts):
        await mon.cycle()
    events = await mon.cycle()
    assert any(e["type"] == "SL_GAP_BREACH" for e in events)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_chase_fills_closes_position_without_escalating() -> None:
    from src.execution.order_lifecycle import OrderMonitor

    # Re-pegged chase order actually fills — position closes via the chase
    # path itself, never reaching the guaranteed-exit escalation.
    broker = MagicMock(spec=BrokerInterface)
    broker.get_ltp = AsyncMock(return_value={"INFY": 1450.0})
    broker.place_order = AsyncMock(return_value="CHASE1")
    broker.get_order_status = AsyncMock(
        return_value=Order(
            broker_order_id="CHASE1",
            symbol="INFY",
            direction="SELL",
            quantity=10,
            filled_quantity=10,
            pending_quantity=0,
            status="FILLED",
            order_type="LIMIT",
            product="INTRADAY",
            average_price=1448.5,
            tag="",
            placed_at=_NOW,
        )
    )
    mon = OrderMonitor(broker)
    mon.track(_monitored())
    events = await mon.cycle()
    assert not any(e["type"] == "SL_GAP_BREACH" for e in events)
    assert mon.get("TRD001") is None  # closed by the chase fill, not escalation
    assert mon.closed_trades[-1]["exit_price"] == 1448.5
    assert mon.closed_trades[-1]["reason"] == "SL_CHASE_FILLED"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_chase_cancelled_when_price_recovers() -> None:
    from src.execution.order_lifecycle import OrderMonitor

    broker = _never_fills_broker({"INFY": 1450.0})
    mon = OrderMonitor(broker)
    pos = _monitored()
    mon.track(pos)
    await mon.cycle()  # starts a chase
    assert pos.chase_order_id

    broker.get_ltp = AsyncMock(return_value={"INFY": 1500.0})  # back to entry, inside range
    await mon.cycle()
    broker.cancel_order.assert_awaited_with("CHASE-TRD001-CHASE1")
    assert pos.chase_order_id == ""
    assert pos.chase_attempts == 0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cycle_short_no_breach_when_in_range() -> None:
    from src.execution.order_lifecycle import OrderMonitor

    # LTP between SL (1550) and target (1400) → no breach events.
    broker = MagicMock(spec=BrokerInterface)
    broker.get_ltp = AsyncMock(return_value={"INFY": 1490.0})
    mon = OrderMonitor(broker)
    mon.track(_monitored_short())
    events = await mon.cycle()
    assert not any(e["type"] in ("TARGET_LIMIT_MISSED", "SL_GAP_BREACH") for e in events)


# ── Unified smart-exit application (shared by paper + live) ────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_force_exit_market_exits_and_records_close() -> None:
    from src.execution.order_lifecycle import OrderMonitor

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="FORCE001")
    mon = OrderMonitor(broker)
    mon.track(_monitored())

    await mon.force_exit("TRD001", 1480.0, "REVERSAL")

    broker.place_order.assert_awaited_once()
    assert mon.get("TRD001") is None  # untracked after exit
    assert len(mon.closed_trades) == 1
    assert mon.closed_trades[0]["reason"] == "REVERSAL"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_force_exit_records_close_with_strategy_name() -> None:
    """Closed-trade record must carry the real strategy_name, not '' (blank strategy
    silently broke attribution/expectancy grouping — see MonitoredPosition.strategy_name)."""
    from src.execution.order_lifecycle import OrderMonitor

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="FORCE002")
    mon = OrderMonitor(broker)
    pos = _monitored()
    pos.strategy_name = "orb_intraday_smart"
    mon.track(pos)

    await mon.force_exit("TRD001", 1480.0, "REVERSAL")

    assert mon.closed_trades[0]["strategy"] == "orb_intraday_smart"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_update_sl_modifies_live_sl_order() -> None:
    from src.execution.order_lifecycle import OrderMonitor

    broker = MagicMock(spec=BrokerInterface)
    broker.modify_order = AsyncMock(return_value="OK")
    mon = OrderMonitor(broker)
    mon.track(_monitored())

    await mon.update_sl("TRD001", 1475.0)

    assert mon.get("TRD001").current_sl == 1475.0
    broker.modify_order.assert_awaited_once()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_book_partial_reduces_quantity_keeps_monitoring() -> None:
    from src.execution.order_lifecycle import OrderMonitor

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="PART001")
    mon = OrderMonitor(broker)
    mon.track(_monitored())  # qty=10

    await mon.book_partial("TRD001", 0.5, 1560.0)

    broker.place_order.assert_awaited_once()
    pos = mon.get("TRD001")
    assert pos is not None and pos.quantity == 5  # half booked, rest still monitored


# ── Fix A: strategy_name + regime_at_entry persisted from meta ─────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_meta_strategy_and_regime_persisted_on_entry() -> None:
    """strategy_name + regime_at_entry in meta land in the entry INSERT row."""
    broker = _filled_broker(1500.0)
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=1)
    repo.update_fill = AsyncMock(return_value=None)
    mgr = OrderLifecycleManager(broker, trade_repo=repo, is_paper=True)

    await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="MIS",
        meta={
            "strategy_name": "orb_intraday_smart",
            "regime_at_entry": "BULL",
            "llm_confidence": 0.90,
            "llm_flag": "CONFIRM",
            "llm_reason": "ok",
        },
    )

    row = repo.insert.await_args.args[0]
    assert row["strategy_name"] == "orb_intraday_smart"
    assert row["regime_at_entry"] == "BULL"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_meta_missing_strategy_regime_omitted_from_row() -> None:
    """No strategy_name/regime_at_entry in meta → those keys absent from row (stay NULL)."""
    broker = _filled_broker(1500.0)
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=1)
    repo.update_fill = AsyncMock(return_value=None)
    mgr = OrderLifecycleManager(broker, trade_repo=repo, is_paper=True)

    await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="MIS",
        meta={"llm_confidence": 0.85, "llm_flag": "CONFIRM", "llm_reason": "ok"},
    )

    row = repo.insert.await_args.args[0]
    assert "strategy_name" not in row
    assert "regime_at_entry" not in row


# ── Fix C: failed SL placement → entry cancelled + DB marked REJECTED ──────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_failed_sl_placement_marks_trade_rejected_and_cancels_entry() -> None:
    """If SL order fails after entry is persisted, the DB row must be REJECTED and
    the entry order cancelled so no orphaned SUBMITTED record is left behind."""
    broker = MagicMock(spec=BrokerInterface)
    # entry succeeds, SL raises, target never reached
    broker.place_order = AsyncMock(side_effect=["E001", RuntimeError("network")])
    broker.cancel_order = AsyncMock(return_value=None)
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=1)
    repo.update_state = AsyncMock(return_value=None)
    mgr = OrderLifecycleManager(broker, trade_repo=repo, is_paper=True)

    with pytest.raises(RuntimeError, match="network"):
        await mgr.place_entry_and_protection(
            signal_id="SIG1",
            symbol="INFY",
            direction="BUY",
            qty=10,
            entry_price=1500.0,
            sl_price=1450.0,
            target_price=1600.0,
        )

    # Entry was persisted as SUBMITTED then immediately corrected to REJECTED
    repo.insert.assert_awaited_once()
    repo.update_state.assert_awaited_once()
    call_args = repo.update_state.await_args
    assert call_args.args[1] == "REJECTED"
    # Entry order was cancelled
    broker.cancel_order.assert_awaited_once_with("E001")


# ── reanchor writes current_sl + current_target back to DB ─────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reanchor_persists_sl_and_target_to_db() -> None:
    """After fill+reanchor the DB current_sl/current_target must reflect the
    reanchored levels, not the stale signal values shown on dashboard."""
    from src.execution.order_lifecycle import OrderLifecycleManager

    broker = _filled_broker(7615.61)  # fill above signal entry — triggers reanchor
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=1)
    repo.update_fill = AsyncMock(return_value=None)
    repo.update_state = AsyncMock(return_value=None)
    mgr = OrderLifecycleManager(broker, trade_repo=repo, is_paper=True)

    record = await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="EICHERMOT",
        direction="BUY",
        qty=10,
        entry_price=7600.0,
        sl_price=7591.12,
        target_price=7608.88,  # stale target sits BELOW fill — must be reanchored up
        product="MIS",
    )

    # update_state must have been called to persist reanchored levels
    repo.update_state.assert_awaited()
    call_kwargs = repo.update_state.await_args
    assert "current_sl" in call_kwargs.kwargs or (
        len(call_kwargs.args) > 2 and "current_sl" in str(call_kwargs)
    )
    # the in-memory record has reanchored levels (sl < fill < target)
    assert record.sl_price < 7615.61 < record.target_price


@pytest.mark.asyncio
async def test_paper_gap_breach_exit_bounded_at_sl_allowance() -> None:
    """Paper polling detects SL breaches late — the simulated fill must be
    bounded at SL ± gap_breach_slippage_pct, not the (much worse) raw LTP."""
    from src.execution.order_lifecycle import OrderMonitor

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="EMRG002")
    broker.get_ltp = AsyncMock(return_value={"INFY": 1400.0})  # far through SL 1450
    mon = OrderMonitor(broker, is_paper=True)
    mon.track(_monitored())  # long INFY, fill 1500, SL 1450
    events = [{"type": "SL_GAP_BREACH", "agent_trade_id": "TRD001", "symbol": "INFY"}]
    await mon.execute_events(events)
    assert mon.get_monitored() == []
    closed = mon.closed_trades[-1]
    # bounded at SL - allowance (0.05% of 1450 ≈ 0.725), NOT 1400
    assert float(closed["exit_price"]) >= 1450.0 - 1450.0 * 0.0005 - 1e-6


# ── Pending-entry lifecycle (2026-08-25) ───────────────────────────────────────
# Regression cover for the four orb_intraday_smart rows stranded as SUBMITTED on
# 2026-08-25 (AFFLE/BLS/BRIGADE/CEATLTD): the entry never filled, the +8s cancel
# raised InvalidTransitionError inside a fire-and-forget task, and nothing ever
# re-checked the resting order against LTP again.


def _make_pending_broker() -> tuple[OrderLifecycleManager, MagicMock]:
    """_make_mgr() plus a cancel_order stub — the cancel path needs one."""
    mgr, broker = _make_mgr()
    broker.cancel_order = AsyncMock(return_value=True)
    return mgr, broker


async def _submitted_open_record(mgr: OrderLifecycleManager, tid: str = "AT-PEND") -> TradeRecord:
    """Drive a record to the OPEN state the real entry path always leaves it in."""
    req = _order_req(tid)
    rec = mgr.create_trade(req)
    rec.broker_order_id = await mgr.submit(tid)
    mgr.transition(tid, OrderState.OPEN)
    return rec


@pytest.mark.asyncio
async def test_cancel_entry_if_unfilled_from_open_reaches_terminal_state() -> None:
    """OPEN → REJECTED is not a legal transition, so the old code raised inside the
    background task and the DB row stayed SUBMITTED forever. The cancel must reach a
    terminal state from OPEN, which is the only state the entry path produces."""
    mgr, broker = _make_pending_broker()
    rec = await _submitted_open_record(mgr)

    await mgr.cancel_entry_if_unfilled(rec.agent_trade_id, timeout_secs=0.0)

    assert mgr._trades[rec.agent_trade_id].state in TERMINAL_STATES


@pytest.mark.asyncio
async def test_cancel_entry_if_unfilled_persists_terminal_state_to_db() -> None:
    """The stranded rows had updated_at == entry_timestamp: _persist_* never ran."""
    mgr, broker = _make_pending_broker()
    repo = MagicMock()
    repo.update_state = AsyncMock()
    mgr._trade_repo = repo
    rec = await _submitted_open_record(mgr)

    await mgr.cancel_entry_if_unfilled(rec.agent_trade_id, timeout_secs=0.0)

    assert repo.update_state.await_count == 1
    assert repo.update_state.await_args.args[0] == rec.agent_trade_id


@pytest.mark.asyncio
async def test_cancel_entry_if_unfilled_also_cancels_protection_orders() -> None:
    """Only the entry was cancelled — the SL and target legs were left resting on a
    position that never existed. In live that is a naked exit order."""
    mgr, broker = _make_pending_broker()
    rec = await _submitted_open_record(mgr)
    rec.sl_order_id = "BRK-SL"
    rec.target_order_id = "BRK-TGT"

    await mgr.cancel_entry_if_unfilled(rec.agent_trade_id, timeout_secs=0.0)

    cancelled = {c.args[0] for c in broker.cancel_order.await_args_list}
    assert {"BRK001", "BRK-SL", "BRK-TGT"} <= cancelled


@pytest.mark.asyncio
async def test_poll_pending_entries_fills_when_price_returns() -> None:
    """The per-cycle re-check: a resting entry that becomes marketable must fill."""
    mgr, broker = _make_pending_broker()
    rec = await _submitted_open_record(mgr)
    mgr.track_pending_entry(rec.agent_trade_id)
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))
    broker.get_trades = AsyncMock(
        return_value=[
            Fill(order_id="BRK001", trade_id="T1", quantity=10, price=100.0, exchange_time=_NOW)
        ]
    )

    filled = await mgr.poll_pending_entries()

    assert rec.agent_trade_id in filled
    assert mgr._trades[rec.agent_trade_id].state == OrderState.FILLED
    assert rec.agent_trade_id not in mgr.pending_entry_ids()


@pytest.mark.asyncio
async def test_poll_pending_entries_drops_terminal_entries() -> None:
    """A pending entry that went terminal must leave the registry, not be polled forever."""
    mgr, broker = _make_pending_broker()
    rec = await _submitted_open_record(mgr)
    mgr.track_pending_entry(rec.agent_trade_id)
    mgr.transition(rec.agent_trade_id, OrderState.CANCELLED)

    await mgr.poll_pending_entries()

    assert rec.agent_trade_id not in mgr.pending_entry_ids()


@pytest.mark.asyncio
async def test_poll_pending_entries_repegs_once_within_risk_cap() -> None:
    """Re-peg an unfilled entry toward current LTP, bounded by max_entry_chase_r × risk."""
    mgr, broker = _make_pending_broker()
    broker.modify_order = AsyncMock(return_value=True)
    broker.get_ltp = AsyncMock(return_value={"INFY": 1502.0})
    rec = await _submitted_open_record(mgr, "AT-REPEG")
    rec.direction = "BUY"
    rec.entry_price = 1500.0
    rec.sl_price = 1490.0  # risk = 10.0, so chase allowance = 7.5
    mgr.track_pending_entry(rec.agent_trade_id)

    await mgr.poll_pending_entries(repeg_after_secs=0.0, max_repegs=1)

    assert broker.modify_order.await_count == 1
    assert broker.modify_order.await_args.args[1]["price"] == pytest.approx(1502.0)

    await mgr.poll_pending_entries(repeg_after_secs=0.0, max_repegs=1)
    assert broker.modify_order.await_count == 1  # only ever once


@pytest.mark.asyncio
async def test_poll_pending_entries_does_not_repeg_beyond_risk_cap() -> None:
    """Price ran further than max_entry_chase_r × risk — the trade is missed, not chased."""
    mgr, broker = _make_pending_broker()
    broker.modify_order = AsyncMock(return_value=True)
    broker.get_ltp = AsyncMock(return_value={"INFY": 1520.0})  # 20.0 drift vs 7.5 allowance
    rec = await _submitted_open_record(mgr, "AT-NOCHASE")
    rec.direction = "BUY"
    rec.entry_price = 1500.0
    rec.sl_price = 1490.0
    mgr.track_pending_entry(rec.agent_trade_id)

    await mgr.poll_pending_entries(repeg_after_secs=0.0, max_repegs=1)

    assert broker.modify_order.await_count == 0


@pytest.mark.asyncio
async def test_poll_pending_entries_stops_repegging_after_modify_failure() -> None:
    """A terminal broker rejection (e.g. Kite's "Maximum allowed order modifications
    exceeded") must still consume the repeg attempt — otherwise repeg_count never
    advances and the monitor loop retries the identical doomed call every cycle
    forever (observed live: CGCL's sibling order TRD-5F93A906747E, 2026-09-07)."""
    mgr, broker = _make_pending_broker()
    broker.modify_order = AsyncMock(
        side_effect=Exception("Maximum allowed order modifications exceeded.")
    )
    broker.get_ltp = AsyncMock(return_value={"INFY": 1502.0})
    rec = await _submitted_open_record(mgr, "AT-REPEGFAIL")
    rec.direction = "BUY"
    rec.entry_price = 1500.0
    rec.sl_price = 1490.0
    mgr.track_pending_entry(rec.agent_trade_id)

    await mgr.poll_pending_entries(repeg_after_secs=0.0, max_repegs=1)
    assert broker.modify_order.await_count == 1
    assert rec.repeg_count == 1

    await mgr.poll_pending_entries(repeg_after_secs=0.0, max_repegs=1)
    assert broker.modify_order.await_count == 1  # not retried again


@pytest.mark.asyncio
async def test_poll_pending_entries_abandons_order_unknown_to_broker() -> None:
    """2026-09-08: TRD-5F93A906747E, still SUBMITTED/never filled, polled every
    ~2s forever with Kite's "Order '...' not found in today's order book" —
    that lookup only ever covers the CURRENT trading session, so an order this
    old can never resolve on any future poll. Unlike a transient network/rate-
    limit error, this must stop tracking the entry (it never filled, so there's
    no position to protect) instead of retrying a call that can't succeed."""
    from src.infrastructure.alert_system import AlertManager

    mgr, broker = _make_pending_broker()
    broker.get_order_status = AsyncMock(
        side_effect=Exception("Order '2095820112903839744' not found in today's order book")
    )
    alerts = AlertManager()
    mgr._alert_manager = alerts
    rec = await _submitted_open_record(mgr, "TRD-5F93A906747E")
    mgr.track_pending_entry(rec.agent_trade_id)

    await mgr.poll_pending_entries()

    assert rec.agent_trade_id not in mgr.pending_entry_ids()
    assert mgr._trades[rec.agent_trade_id].state == OrderState.CANCELLED
    # Raised AND immediately resolved — this describes a completed action (the
    # order is already cancelled, nothing left to protect), not an ongoing
    # problem, so it must not sit active on the dashboard forever waiting for
    # a human to clear something that isn't a problem anymore.
    assert alerts.active() == []
    all_alerts = [alerts.get(i) for i in alerts._order]
    assert len(all_alerts) == 1
    assert all_alerts[0].source == "TRD-5F93A906747E"
    assert all_alerts[0].resolved is True


@pytest.mark.asyncio
async def test_poll_pending_entries_keeps_retrying_transient_poll_errors() -> None:
    """A genuinely transient failure (network blip, rate limit) must NOT be
    treated as abandonment — only the specific unknown-to-broker case is
    terminal."""
    mgr, broker = _make_pending_broker()
    broker.get_order_status = AsyncMock(side_effect=Exception("connection reset"))
    rec = await _submitted_open_record(mgr, "AT-TRANSIENT")
    mgr.track_pending_entry(rec.agent_trade_id)

    await mgr.poll_pending_entries()

    assert rec.agent_trade_id in mgr.pending_entry_ids()
    assert mgr._trades[rec.agent_trade_id].state != OrderState.CANCELLED


# ── Deferred protection for delivery orders (2026-08-27) ───────────────────────
# CNC swing arms SL+target as GTTs. They used to be placed immediately after the entry,
# before any fill was known — which arms a sell for stock not yet owned, and orphans
# both legs entirely if the entry never fills (JINDALSAW, 2026-08-27).


@pytest.mark.asyncio
async def test_defer_protection_places_no_protection_before_fill() -> None:
    mgr, broker = _make_pending_broker()
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))

    rec = await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="CNC",
        defer_protection=True,
    )

    assert rec.state != OrderState.FILLED
    assert broker.place_order.await_count == 1  # entry only — no SL, no target
    assert rec.sl_order_id == ""
    assert rec.target_order_id == ""


@pytest.mark.asyncio
async def test_defer_protection_arms_protection_once_filled() -> None:
    """Swing protection must go through the native GTT API, not place_order.

    order_types says "GTT" (config/execution.json). Until 2026-08-28 that string was
    handed to place_order() as an order_type — Kite accepts only MARKET/LIMIT/SL/SL-M,
    so every swing SL and target was rejected and the position ran naked.
    """
    mgr, broker = _make_pending_broker()
    broker.place_order = AsyncMock(side_effect=["BRK001"])
    broker.place_gtt = AsyncMock(return_value="GTT-9001")
    broker.supports_gtt = MagicMock(return_value=True)
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))
    broker.get_trades = AsyncMock(
        return_value=[
            Fill(order_id="BRK001", trade_id="T1", quantity=10, price=1500.0, exchange_time=_NOW)
        ]
    )

    rec = await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="CNC",
        defer_protection=True,
    )

    assert rec.state == OrderState.FILLED
    assert broker.place_order.await_count == 1  # entry only — the exits are a GTT
    broker.place_gtt.assert_awaited_once()
    gtt = broker.place_gtt.await_args.args[0]
    assert gtt.gtt_type == "TWO_LEG"  # one OCO trigger covers stop AND target
    assert gtt.symbol == "INFY"
    assert gtt.quantity == 10
    assert gtt.sl_trigger == rec.sl_price
    assert gtt.target_trigger == rec.target_price
    # One trigger id protects both legs; no plain exit orders exist to cancel.
    assert rec.gtt_sl_id == "GTT-9001"
    assert rec.sl_order_id == ""
    assert rec.target_order_id == ""


@pytest.mark.asyncio
async def test_defer_protection_strips_exchange_prefix_from_symbol() -> None:
    """Regression (2026-09-03): the swing scan passes "NSE:SYMBOL" (built for LTP
    lookups) straight through as symbol=. Kite's place_order takes tradingsymbol and
    exchange as separate fields, so sending the compound string as tradingsymbol got
    every live swing entry rejected with "instrument ... does not exist" (CAPLIPOINT,
    CLEAN, BEML). The entry order AND the deferred GTT must both see the bare symbol.
    """
    mgr, broker = _make_pending_broker()
    broker.place_order = AsyncMock(side_effect=["BRK001"])
    broker.place_gtt = AsyncMock(return_value="GTT-9001")
    broker.supports_gtt = MagicMock(return_value=True)
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))
    broker.get_trades = AsyncMock(
        return_value=[
            Fill(order_id="BRK001", trade_id="T1", quantity=10, price=1500.0, exchange_time=_NOW)
        ]
    )

    await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="NSE:INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="CNC",
        defer_protection=True,
    )

    entry_req = broker.place_order.await_args_list[0].args[0]
    assert entry_req.symbol == "INFY"
    gtt = broker.place_gtt.await_args.args[0]
    assert gtt.symbol == "INFY"


@pytest.mark.asyncio
async def test_gtt_protection_refused_when_broker_cannot_do_gtt() -> None:
    """A broker without GTT support must fail loudly, never leave the fill naked."""
    mgr, broker = _make_pending_broker()
    broker.place_order = AsyncMock(side_effect=["BRK001"])
    broker.supports_gtt = MagicMock(return_value=False)
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))
    broker.get_trades = AsyncMock(
        return_value=[
            Fill(order_id="BRK001", trade_id="T1", quantity=10, price=1500.0, exchange_time=_NOW)
        ]
    )

    rec = await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="CNC",
        defer_protection=True,
    )

    # _arm_deferred_protection swallows and screams rather than orphaning the fill.
    assert rec.gtt_sl_id == ""
    assert rec.sl_order_id == ""


# ── RejectionHandler wired into SL/target leg placement ─────────────────────────


@pytest.mark.asyncio
async def test_sl_leg_margin_rejection_retries_at_reduced_quantity() -> None:
    """A transient margin-shortfall rejection on the SL leg must self-heal via
    RejectionHandler (retry at reduced qty) instead of escalating immediately."""
    from src.execution.rejection_handler import RejectionHandler

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(
        side_effect=["E001", RuntimeError("insufficient margin"), "SL001", "TGT001"]
    )
    repo = MagicMock()
    repo.insert = AsyncMock(return_value=1)
    repo.update_state = AsyncMock(return_value=None)
    mgr = OrderLifecycleManager(
        broker, trade_repo=repo, is_paper=True, rejection_handler=RejectionHandler()
    )

    entry_id, sl_id, target_id = await mgr.place_entry_and_protection(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
    )

    assert (entry_id, sl_id, target_id) == ("E001", "SL001", "TGT001")
    assert broker.place_order.await_count == 4  # entry, SL fail, SL retry, target
    retried_sl_req = broker.place_order.await_args_list[2].args[0]
    assert retried_sl_req.quantity == 7  # RejectionHandler's margin policy: qty * 0.75


@pytest.mark.asyncio
async def test_sl_leg_unretryable_rejection_still_hits_critical_alert_unchanged() -> None:
    """A rejection RejectionHandler won't retry (or an exhausted retry) must still
    reach today's existing "POSITION UNPROTECTED" CRITICAL alert path, unchanged —
    the retry wiring is additive, never a replacement for the fail-loud fallback."""
    from src.execution.rejection_handler import RejectionHandler
    from src.infrastructure.alert_system import AlertManager, AlertTier

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(side_effect=["BRK001", RuntimeError("gateway timeout")])
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))
    broker.get_trades = AsyncMock(
        return_value=[
            Fill(order_id="BRK001", trade_id="T1", quantity=10, price=1500.0, exchange_time=_NOW)
        ]
    )
    mock_am = MagicMock(spec=AlertManager)
    mgr = OrderLifecycleManager(
        broker, alert_manager=mock_am, is_paper=True, rejection_handler=RejectionHandler()
    )

    rec = await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="CNC",
        # Non-GTT leg types so this exercises the plain place_order retry path
        # (_place_leg_with_retry), not the native GTT branch in _place_protection.
        order_types={"CNC_swing_sl": "SL-M", "CNC_swing_target": "LIMIT"},
        defer_protection=True,
    )

    assert rec.sl_order_id == ""  # never armed — matches pre-existing fallback behaviour
    mock_am.raise_alert.assert_called_once()
    assert mock_am.raise_alert.call_args.args[0] == AlertTier.CRITICAL


@pytest.mark.asyncio
async def test_intraday_path_still_places_protection_up_front() -> None:
    """Deferring is opt-in: MIS must keep arming protection with the entry."""
    mgr, broker = _make_pending_broker()
    broker.place_order = AsyncMock(side_effect=["BRK001", "BRK-SL", "BRK-TGT"])
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))

    await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="MIS",
    )

    assert broker.place_order.await_count == 3


@pytest.mark.asyncio
async def test_update_sl_trails_the_gtt_not_just_the_sl_order() -> None:
    """Swing protection is a GTT. A ratchet that only moved sl_order_id left the GTT
    resting at the entry stop for the whole trade."""
    from src.execution.order_lifecycle import MonitoredPosition, OrderMonitor

    broker = MagicMock(spec=BrokerInterface)
    broker.modify_order = AsyncMock(return_value=True)
    broker.modify_gtt = AsyncMock(return_value="GTT-NEW")
    mon = OrderMonitor(broker)
    mon.track(
        MonitoredPosition(
            agent_trade_id="T1",
            symbol="INFY",
            direction="BUY",
            quantity=10,
            fill_price=100.0,
            initial_sl=95.0,
            current_sl=95.0,
            initial_target=110.0,
            current_target=110.0,
            gtt_sl_id="GTT-OLD",
        )
    )

    await mon.update_sl("T1", 98.0)

    broker.modify_gtt.assert_awaited_once()
    # sl_trigger only: the adapter re-fetches and rebuilds the rest of the GTT spec,
    # because Kite's modify_gtt requires the complete payload (see adapter docstring).
    assert broker.modify_gtt.await_args.args[1] == {"sl_trigger": 98.0}
    assert mon.get("T1").gtt_sl_id == "GTT-NEW"


@pytest.mark.asyncio
async def test_late_fill_of_deferred_entry_still_arms_protection() -> None:
    """A swing AMO fills at the next open, detected by the monitor's pending re-poll —
    not by the single poll at placement. Without arming here the position goes live
    with no stop at all."""
    mgr, broker = _make_pending_broker()
    broker.place_order = AsyncMock(side_effect=["BRK001"])
    broker.place_gtt = AsyncMock(return_value="GTT-9002")
    broker.supports_gtt = MagicMock(return_value=True)
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("OPEN"))

    rec = await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="CNC",
        defer_protection=True,
    )
    assert rec.sl_order_id == ""
    assert rec.gtt_sl_id == ""
    mgr.track_pending_entry(rec.agent_trade_id)

    # next session: the resting AMO fills
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))
    broker.get_trades = AsyncMock(
        return_value=[
            Fill(order_id="BRK001", trade_id="T1", quantity=10, price=1500.0, exchange_time=_NOW)
        ]
    )

    filled = await mgr.poll_pending_entries()

    assert rec.agent_trade_id in filled
    broker.place_gtt.assert_awaited_once()
    assert rec.gtt_sl_id == "GTT-9002"


# ── Protection ids reach the DB (2026-08-28) ──────────────────────────────────


@pytest.mark.asyncio
async def test_arming_gtt_persists_the_trigger_id_to_the_db() -> None:
    """gtt_sl_id/gtt_target_id have been columns since migration 018 and
    _reseed_order_monitor reads them on every restart — but nothing ever wrote them,
    so GTT trailing and the 30s integrity check were dead for any position that
    outlived the process that opened it."""
    repo = MagicMock()
    repo.update_state = AsyncMock()
    mgr, broker = _make_pending_broker()
    mgr._trade_repo = repo
    broker.place_order = AsyncMock(side_effect=["BRK001"])
    broker.place_gtt = AsyncMock(return_value="GTT-9003")
    broker.supports_gtt = MagicMock(return_value=True)
    broker.get_order_status = AsyncMock(return_value=_make_broker_order("FILLED"))
    broker.get_trades = AsyncMock(
        return_value=[
            Fill(order_id="BRK001", trade_id="T1", quantity=10, price=1500.0, exchange_time=_NOW)
        ]
    )

    await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1450.0,
        target_price=1600.0,
        product="CNC",
        defer_protection=True,
    )

    _persisted = [c.kwargs for c in repo.update_state.await_args_list if "gtt_sl_id" in c.kwargs]
    assert _persisted, "the GTT trigger id never reached the DB row"
    # One OCO trigger protects both legs, so both columns carry the same id.
    assert _persisted[-1]["gtt_sl_id"] == "GTT-9003"
    assert _persisted[-1]["gtt_target_id"] == "GTT-9003"


@pytest.mark.asyncio
async def test_persist_protection_ids_is_a_noop_without_ids() -> None:
    """No ids to record means no pointless UPDATE — and never a row of empty strings."""
    repo = MagicMock()
    repo.update_state = AsyncMock()
    mgr, _broker = _make_pending_broker()
    mgr._trade_repo = repo
    rec = mgr.create_trade(_order_req("AT-NOIDS"))

    await mgr._persist_protection_ids(rec)

    repo.update_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_protection_cancels_the_gtt_too() -> None:
    """cancel_order cannot reach a GTT; one left behind fires against a position
    that no longer exists."""
    mgr, broker = _make_pending_broker()
    broker.cancel_gtt = AsyncMock(return_value=True)
    rec = mgr.create_trade(_order_req("AT-GTTCANCEL"))
    rec.gtt_sl_id = "GTT-9004"
    mgr.transition("AT-GTTCANCEL", OrderState.SUBMITTED)

    await mgr.cancel_protection("AT-GTTCANCEL")

    broker.cancel_gtt.assert_awaited_once_with("GTT-9004")


# ── Monitor auto-arms missing protection (2026-08-28) ─────────────────────────


def _cnc_pos(**kw):
    from src.execution.order_lifecycle import MonitoredPosition

    base = dict(
        agent_trade_id="T-IFCI",
        symbol="IFCI",
        direction="BUY",
        quantity=145,
        fill_price=84.27,
        initial_sl=77.40,
        current_sl=77.40,
        initial_target=101.45,
        current_target=101.45,
        product="CNC",
    )
    base.update(kw)
    return MonitoredPosition(**base)


def _gtt_broker():
    broker = MagicMock(spec=BrokerInterface)
    broker.supports_gtt = MagicMock(return_value=True)
    broker.place_gtt = AsyncMock(return_value="GTT-55")
    broker.get_all_gtts = AsyncMock(return_value=[])
    broker.get_ltp = AsyncMock(return_value={"IFCI": 89.96})
    return broker


@pytest.mark.asyncio
async def test_cycle_flags_a_delivery_position_with_no_protection() -> None:
    """The integrity check used to require an existing gtt_sl_id, so a position with
    NO protection — the one state that most needs re-arming — was invisible to it."""
    from datetime import timedelta

    from src.execution.order_lifecycle import OrderMonitor

    mon = OrderMonitor(_gtt_broker())
    pos = _cnc_pos()
    pos.last_gtt_check -= timedelta(seconds=120)
    mon.track(pos)

    events = await mon.cycle()

    assert [e["type"] for e in events if e["type"] == "GTT_MISSING"] == ["GTT_MISSING"]


@pytest.mark.asyncio
async def test_exec_gtt_missing_places_the_gtt_itself() -> None:
    """It used to log 'manual re-arm required' — the monitor found the exact problem
    it exists to fix and then handed it to a human."""
    from src.execution.order_lifecycle import OrderMonitor

    broker = _gtt_broker()
    mon = OrderMonitor(broker)
    mon.track(_cnc_pos())

    await mon.execute_events(
        [{"type": "GTT_MISSING", "agent_trade_id": "T-IFCI", "symbol": "IFCI", "gtt_sl_id": ""}]
    )

    broker.place_gtt.assert_awaited_once()
    gtt = broker.place_gtt.await_args.args[0]
    assert gtt.symbol == "IFCI"
    assert gtt.quantity == 145
    assert gtt.gtt_type == "TWO_LEG"
    assert gtt.sl_trigger == 77.40
    assert gtt.target_trigger == 101.45
    assert gtt.sl_order_type == "LIMIT"  # Zerodha GTT legs are LIMIT-only
    assert mon.get("T-IFCI").gtt_sl_id == "GTT-55"


@pytest.mark.asyncio
async def test_rearm_uses_current_levels_not_entry_levels() -> None:
    """After a trail and a partial book the re-arm must restore what the position has
    NOW, not the size and stop it opened with."""
    from src.execution.order_lifecycle import OrderMonitor

    broker = _gtt_broker()
    mon = OrderMonitor(broker)
    mon.track(_cnc_pos(current_sl=84.27, quantity=72))

    await mon.execute_events(
        [{"type": "GTT_MISSING", "agent_trade_id": "T-IFCI", "symbol": "IFCI", "gtt_sl_id": ""}]
    )

    gtt = broker.place_gtt.await_args.args[0]
    assert gtt.sl_trigger == 84.27  # trailed to breakeven
    assert gtt.quantity == 72  # half already booked


@pytest.mark.asyncio
async def test_exec_gtt_missing_success_resolves_prior_alert_for_this_trade() -> None:
    """A successful re-arm must clear any earlier gtt_rearm_failed/deferred_
    protection_failed alert for this trade — otherwise a fixed position sits
    flagged as unprotected on the dashboard forever."""
    from src.execution.order_lifecycle import OrderMonitor

    broker = _gtt_broker()
    alerts = MagicMock()
    mon = OrderMonitor(broker, alert_manager=alerts)
    mon.track(_cnc_pos())

    await mon.execute_events(
        [{"type": "GTT_MISSING", "agent_trade_id": "T-IFCI", "symbol": "IFCI", "gtt_sl_id": ""}]
    )

    alerts.resolve_by_source.assert_called_once_with("T-IFCI")


@pytest.mark.asyncio
async def test_rearm_failure_escalates_and_leaves_id_empty() -> None:
    """A failed re-arm must stay flagged so the next cycle tries again."""
    from src.execution.order_lifecycle import OrderMonitor

    broker = _gtt_broker()
    broker.place_gtt = AsyncMock(side_effect=RuntimeError("Invalid trigger data."))
    alerts = MagicMock()
    mon = OrderMonitor(broker, alert_manager=alerts)
    mon.track(_cnc_pos())

    await mon.execute_events(
        [{"type": "GTT_MISSING", "agent_trade_id": "T-IFCI", "symbol": "IFCI", "gtt_sl_id": ""}]
    )

    assert mon.get("T-IFCI").gtt_sl_id == ""
    alerts.raise_alert.assert_called_once()


@pytest.mark.asyncio
async def test_intraday_position_is_not_gtt_rearmed() -> None:
    """MIS protection is a real SL order, not a GTT — it must not be swept up here."""
    from datetime import timedelta

    from src.execution.order_lifecycle import OrderMonitor

    broker = _gtt_broker()
    mon = OrderMonitor(broker)
    pos = _cnc_pos(product="MIS")
    pos.last_gtt_check -= timedelta(seconds=120)
    mon.track(pos)

    events = await mon.cycle()

    assert not [e for e in events if e["type"] == "GTT_MISSING"]


# ── Partial-then-close P&L: the closed record must carry the WHOLE trade ───────
# 2026-09-24 LAURUSLABS: 26 sh, partial sold 13 (+1812), remaining 13 sold at the
# same price. pnl_realized held only the last leg (+1812) — half the trade vanished
# from the Closed tab, attribution and win/loss stats.


@pytest.mark.asyncio  # type: ignore[misc]
async def test_book_partial_persists_sold_quantity_with_booked_pnl() -> None:
    """The original size must stay recoverable after quantity shrinks (pnl_r basis)."""
    from src.execution.order_lifecycle import OrderMonitor

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="PARTIAL001")
    trade_repo = MagicMock()
    trade_repo.update_state = AsyncMock()
    mon = OrderMonitor(broker, trade_repo=trade_repo)
    mon.track(_monitored())  # qty=10

    await mon.book_partial("TRD001", 0.5, 1550.0)

    kwargs = trade_repo.update_state.call_args.kwargs
    assert kwargs["partial_qty_booked"] == 5
    assert mon.get("TRD001").partial_qty_booked == 5
    assert float(kwargs["last_partial_price"]) == 1550.0  # the partial's price is kept


@pytest.mark.asyncio  # type: ignore[misc]
async def test_close_after_partial_reports_total_pnl_and_original_quantity() -> None:
    from src.execution.order_lifecycle import OrderMonitor

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="X")
    mon = OrderMonitor(broker)
    mon.track(_monitored())  # qty=10, entry 1500, initial_sl 1450

    await mon.book_partial("TRD001", 0.5, 1550.0)
    partial = mon.get("TRD001").partial_pnl_booked
    assert partial > 0
    await mon.force_exit("TRD001", 1550.0, "MANUAL_MARKET_EXIT")

    rec = mon.closed_trades[0]
    leg = rec["net_pnl"] - partial
    assert leg == pytest.approx(partial, rel=1e-6)  # same qty, same price -> equal legs
    assert rec["net_pnl"] == pytest.approx(2 * partial, rel=1e-6)  # BOTH legs counted
    assert rec["quantity"] == 10  # original size, not the 5 that remained
    assert rec["partial_pnl_booked"] == pytest.approx(partial)
    assert rec["partial_qty_booked"] == 5
    # pnl_r is over the ORIGINAL risk (10 sh x 50), not the remaining 5 sh
    assert rec["risk_rupees"] == pytest.approx(500.0)
    assert rec["pnl_r"] == pytest.approx(rec["net_pnl"] / 500.0, abs=1e-4)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_close_without_partial_is_unchanged() -> None:
    from src.execution.order_lifecycle import OrderMonitor

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="X")
    mon = OrderMonitor(broker)
    mon.track(_monitored())

    await mon.force_exit("TRD001", 1480.0, "REVERSAL")

    rec = mon.closed_trades[0]
    assert rec["quantity"] == 10
    assert rec["partial_pnl_booked"] == 0.0
    assert rec["risk_rupees"] == pytest.approx(500.0)
    assert rec["net_pnl"] < 0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_partial_win_then_breakeven_stop_is_not_recorded_as_a_loss() -> None:
    """Bank a partial, then stop the remainder out below entry after costs: the
    last leg alone is negative but the TRADE is a winner. Recording only the last
    leg mislabelled such trades as losses in every downstream stat."""
    from src.execution.order_lifecycle import OrderMonitor

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="X")
    mon = OrderMonitor(broker)
    mon.track(_monitored())  # qty=10 @1500

    await mon.book_partial("TRD001", 0.5, 1600.0)  # big win on 5
    await mon.force_exit("TRD001", 1499.0, "SL_GAP_BREACH")  # tiny loss on 5

    rec = mon.closed_trades[0]
    assert rec["net_pnl"] - rec["partial_pnl_booked"] < 0  # last leg alone: a loss
    assert rec["net_pnl"] > 0  # the trade: a win


@pytest.mark.asyncio  # type: ignore[misc]
async def test_close_after_partial_accumulates_charges_across_both_legs() -> None:
    """total_costs / charges_breakdown cover partial slice + last leg, so gross = net + costs."""
    from src.execution.order_lifecycle import OrderMonitor
    from src.risk.real_pnl import ProductType, calculate_real_pnl

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="X")
    trade_repo = MagicMock()
    trade_repo.update_state = AsyncMock()
    mon = OrderMonitor(broker, trade_repo=trade_repo)
    pos = _monitored()  # qty=10 @1500
    mon.track(pos)

    await mon.book_partial("TRD001", 0.5, 1550.0)
    assert "charges_breakdown" in trade_repo.update_state.call_args.kwargs  # persisted for restart
    await mon.force_exit("TRD001", 1550.0, "MANUAL_MARKET_EXIT")

    rec = mon.closed_trades[0]
    leg = calculate_real_pnl(
        pos.symbol,
        1500.0,
        1550.0,
        5,
        ProductType.DELIVERY if pos.product.upper() != "MIS" else ProductType.INTRADAY,
    )
    assert rec["total_costs"] == pytest.approx(2 * leg.costs.total, abs=0.02)
    assert sum(rec["charges_breakdown"].values()) == pytest.approx(rec["total_costs"], abs=0.02)
    assert rec["gross_pnl"] == pytest.approx(rec["net_pnl"] + rec["total_costs"], abs=0.02)
    assert rec["broker"]
