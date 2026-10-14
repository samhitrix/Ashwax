"""Tests for orchestrator._process_manual_order_actions (2026-08-17 fix).

Root cause: the dashboard's "Market Exit" button (resolveBreachEvent in
advancedStore.ts) only mutated local frontend state — no backend call existed,
so the position was never touched and the next breach poll showed the same
breach again. manual_order_actions (migration 057) + this handler are the
real command channel from POST /api/orders/{id}/force-exit through to
OrderMonitor.force_exit().
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


class _FakePool:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        return self._rows

    async def execute(self, query, *args):
        self.executed.append((query, args))


def _pending_row(action="MARKET_EXIT", tid="TRD-SCI"):
    return {"id": 1, "agent_trade_id": tid, "action": action}


@pytest.mark.asyncio  # type: ignore[misc]
async def test_market_exit_calls_force_exit_and_marks_done() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import MonitoredPosition

    pool = _FakePool([_pending_row()])
    order_monitor = MagicMock()
    pos = MonitoredPosition(
        agent_trade_id="TRD-SCI",
        symbol="NSE:SCI",
        direction="BUY",
        quantity=40,
        fill_price=308.40,
        initial_sl=283.73,
        current_sl=283.73,
        initial_target=370.08,
        current_target=370.08,
    )
    order_monitor.get.return_value = pos
    order_monitor.force_exit = AsyncMock()
    broker = MagicMock()
    broker.get_ltp = AsyncMock(return_value={"NSE:SCI": 286.75})

    await orchestrator._process_manual_order_actions(order_monitor, pool, broker)

    order_monitor.force_exit.assert_awaited_once_with("TRD-SCI", 286.75, "MANUAL_MARKET_EXIT")
    assert any("status='DONE'" in q for q, _ in pool.executed)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_market_exit_falls_back_to_current_sl_when_ltp_unavailable() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import MonitoredPosition

    pool = _FakePool([_pending_row()])
    order_monitor = MagicMock()
    pos = MonitoredPosition(
        agent_trade_id="TRD-SCI",
        symbol="NSE:SCI",
        direction="BUY",
        quantity=40,
        fill_price=308.40,
        initial_sl=283.73,
        current_sl=283.73,
        initial_target=370.08,
        current_target=370.08,
    )
    order_monitor.get.return_value = pos
    order_monitor.force_exit = AsyncMock()
    broker = MagicMock()
    broker.get_ltp = AsyncMock(side_effect=RuntimeError("broker down"))

    await orchestrator._process_manual_order_actions(order_monitor, pool, broker)

    order_monitor.force_exit.assert_awaited_once_with("TRD-SCI", 283.73, "MANUAL_MARKET_EXIT")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_marks_failed_when_position_not_tracked() -> None:
    from src import orchestrator

    pool = _FakePool([_pending_row(tid="TRD-GONE")])
    order_monitor = MagicMock()
    order_monitor.get.return_value = None
    broker = MagicMock()

    await orchestrator._process_manual_order_actions(order_monitor, pool, broker)

    order_monitor.force_exit.assert_not_called()  # type: ignore[attr-defined]
    assert any(
        "status='FAILED'" in q and "not_tracked_or_already_closed" in q
        for q, _args in pool.executed
    )


@pytest.mark.asyncio  # type: ignore[misc]
async def test_unsupported_action_marks_failed() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import MonitoredPosition

    pool = _FakePool([_pending_row(action="MODIFY_ORDER")])
    order_monitor = MagicMock()
    order_monitor.get.return_value = MonitoredPosition(
        agent_trade_id="TRD-SCI",
        symbol="NSE:SCI",
        direction="BUY",
        quantity=40,
        fill_price=308.40,
        initial_sl=283.73,
        current_sl=283.73,
        initial_target=370.08,
        current_target=370.08,
    )
    broker = MagicMock()

    await orchestrator._process_manual_order_actions(order_monitor, pool, broker)

    order_monitor.force_exit.assert_not_called()  # type: ignore[attr-defined]
    assert any("unsupported_action" in q for q, _args in pool.executed)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_no_op_when_pool_is_none() -> None:
    from src import orchestrator

    order_monitor = MagicMock()
    await orchestrator._process_manual_order_actions(order_monitor, None, MagicMock())
    order_monitor.get.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_survives_fetch_error() -> None:
    from src import orchestrator

    class _BrokenPool:
        async def fetch(self, query, *args):
            raise RuntimeError("connection lost")

    order_monitor = MagicMock()
    await orchestrator._process_manual_order_actions(order_monitor, _BrokenPool(), MagicMock())
    order_monitor.get.assert_not_called()  # type: ignore[attr-defined]
