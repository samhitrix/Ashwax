"""Tests for _auto_close_ghost_trades — the automatic reconciliation-driven
closer for live DB trades the broker already exited outside the bot.

Built 2026-09-02 after IFCI sat "open" in the DB (and "active" on the
dashboard) for days after being sold outside the bot with no bot-visible
exit order recorded. The user explicitly asked for this to be automatic —
"why is it not checking live kite positions and updating the database" —
not a manual fix applied once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.brokers.base import Holding, Order
from src.orchestrator import _auto_close_ghost_trades
from src.supervisor.reconciler import ReconciliationGap, ReconciliationResult


def _gap(
    symbol: str = "IFCI", tid: str = "TRD-1", kind: str = "GHOST_DB_TRADE"
) -> ReconciliationGap:
    return ReconciliationGap(kind=kind, symbol=symbol, db_qty=145, agent_trade_id=tid)


def _order(
    symbol: str = "IFCI",
    direction: str = "SELL",
    product: str = "DELIVERY",
    status: str = "FILLED",
    filled_qty: int = 145,
    avg_price: float = 98.22,
    placed_at: datetime = datetime(2026, 9, 2, 10, 0, 0),
    order_id: str = "BRK-EXIT-1",
) -> Order:
    return Order(
        broker_order_id=order_id,
        symbol=symbol,
        direction=direction,
        quantity=filled_qty,
        filled_quantity=filled_qty,
        pending_quantity=0,
        status=status,
        order_type="MARKET",
        product=product,
        average_price=avg_price,
        tag="",
        placed_at=placed_at,
    )


def _holding(symbol: str = "IFCI", avg_price: float = 84.27, qty: int = 0) -> Holding:
    return Holding(
        symbol=symbol,
        exchange="NSE",
        quantity=qty,
        average_price=avg_price,
        last_price=avg_price,
        pnl=0.0,
    )


@dataclass
class _FakeOrderMonitor:
    untracked: list[str] = field(default_factory=list)

    def untrack(self, tid: str) -> None:
        self.untracked.append(tid)


def _make_trade_repo(row: dict | None) -> MagicMock:
    repo = MagicMock()
    repo.get_by_agent_id = AsyncMock(return_value=row)
    repo.update_pnl = AsyncMock()
    repo.update_state = AsyncMock()
    return repo


def _make_broker(
    orders: list[Order] | None = None, holdings: list[Holding] | None = None
) -> MagicMock:
    broker = MagicMock()
    broker.get_all_orders = AsyncMock(return_value=orders or [])
    broker.get_holdings = AsyncMock(return_value=holdings or [])
    return broker


@pytest.mark.asyncio  # type: ignore[misc]
async def test_closes_long_position_using_holdings_entry_price() -> None:
    row = {"direction": "LONG", "quantity": 145, "fill_price": 98.22}  # corrupted DB fill_price
    repo = _make_trade_repo(row)
    broker = _make_broker(
        orders=[_order(direction="SELL", avg_price=98.22)],
        holdings=[_holding(avg_price=84.27, qty=0)],  # true cost basis, survives full sell
    )
    om = _FakeOrderMonitor()

    result = ReconciliationResult(gaps=[_gap()])
    closed = await _auto_close_ghost_trades(result, broker, repo, om)

    assert closed == 1
    repo.update_pnl.assert_awaited_once()
    args = repo.update_pnl.call_args.args
    assert args[0] == "TRD-1"
    assert float(args[1]) == pytest.approx(2022.75)  # (98.22 - 84.27) * 145, entry from holdings
    assert float(args[3]) == pytest.approx(98.22)  # exit_price
    # the reason goes in the SAME update as the close, so the ledger's FINAL_EXIT carries it
    assert repo.update_pnl.call_args.kwargs["exit_reason"] == "GHOST_TRADE_AUTO_RECONCILED"
    assert om.untracked == ["TRD-1"]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_short_position_pnl_sign() -> None:
    row = {"direction": "SHORT", "quantity": 10, "fill_price": 100.0}
    repo = _make_trade_repo(row)
    broker = _make_broker(
        orders=[_order(direction="BUY", avg_price=90.0, filled_qty=10)],
        holdings=[],
    )
    closed = await _auto_close_ghost_trades(ReconciliationResult(gaps=[_gap()]), broker, repo, None)

    assert closed == 1
    args = repo.update_pnl.call_args.args
    assert float(args[1]) == pytest.approx(100.0)  # (entry 100 - exit 90) * 10


@pytest.mark.asyncio  # type: ignore[misc]
async def test_leaves_open_when_no_matching_exit_order_found() -> None:
    row = {"direction": "LONG", "quantity": 145, "fill_price": 84.27}
    repo = _make_trade_repo(row)
    broker = _make_broker(orders=[], holdings=[])  # nothing in the broker's order book

    closed = await _auto_close_ghost_trades(ReconciliationResult(gaps=[_gap()]), broker, repo, None)

    assert closed == 0
    repo.update_pnl.assert_not_awaited()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_left_open_raises_a_critical_alert_when_alert_manager_given() -> None:
    """This CRITICAL used to be log-only — a real 'DB open, broker won't confirm
    an exit' gap never reached the dashboard's alert system at all (2026-09-07)."""
    row = {"direction": "LONG", "quantity": 145, "fill_price": 84.27}
    repo = _make_trade_repo(row)
    broker = _make_broker(orders=[], holdings=[])
    alerts = MagicMock()

    closed = await _auto_close_ghost_trades(
        ReconciliationResult(gaps=[_gap()]), broker, repo, None, alert_manager=alerts
    )

    assert closed == 0
    alerts.raise_alert.assert_called_once()
    assert alerts.raise_alert.call_args.kwargs["source"] == "ghost:TRD-1"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_left_open_does_not_re_raise_while_already_active() -> None:
    """2026-09-08: this reconciliation runs every ~30-60s, and while the same
    condition simply persists it raised a brand-new duplicate alert every single
    cycle (CGCL alerted 4x in under 90s) instead of leaving the existing one
    active."""
    from src.infrastructure.alert_system import AlertManager

    row = {"direction": "LONG", "quantity": 145, "fill_price": 84.27}
    repo = _make_trade_repo(row)
    broker = _make_broker(orders=[], holdings=[])
    alerts = AlertManager()

    await _auto_close_ghost_trades(
        ReconciliationResult(gaps=[_gap()]), broker, repo, None, alert_manager=alerts
    )
    await _auto_close_ghost_trades(
        ReconciliationResult(gaps=[_gap()]), broker, repo, None, alert_manager=alerts
    )

    assert len(alerts.active()) == 1


@pytest.mark.asyncio  # type: ignore[misc]
async def test_alert_auto_resolves_once_the_trade_stops_being_a_ghost() -> None:
    """2026-09-08: once a trade stops being flagged as a gap at all (fixed
    upstream in Reconciler, or genuinely closed), its earlier alert must clear
    on the very next reconciliation cycle — this function only ever sees the
    CURRENT gap list, so a trade that quietly stops recurring (as opposed to
    being explicitly closed by the code below) needs this to notice "not a
    problem anymore" and clear the stale alert."""
    from src.infrastructure.alert_system import AlertManager

    row = {"direction": "LONG", "quantity": 145, "fill_price": 84.27}
    repo = _make_trade_repo(row)
    broker = _make_broker(orders=[], holdings=[])
    alerts = AlertManager()

    await _auto_close_ghost_trades(
        ReconciliationResult(gaps=[_gap()]), broker, repo, None, alert_manager=alerts
    )
    assert len(alerts.active()) == 1

    # Next cycle: this trade is no longer in the gap list at all (reconciled).
    await _auto_close_ghost_trades(
        ReconciliationResult(gaps=[]), broker, repo, None, alert_manager=alerts
    )

    assert alerts.active() == []


@pytest.mark.asyncio  # type: ignore[misc]
async def test_alert_for_a_different_trade_is_untouched_by_resolution() -> None:
    """The auto-resolve must only clear THIS trade's stale ghost alert, never
    an unrelated one sharing the AlertManager instance."""
    from src.infrastructure.alert_system import AlertManager

    row = {"direction": "LONG", "quantity": 145, "fill_price": 84.27}
    repo = _make_trade_repo(row)
    broker = _make_broker(orders=[], holdings=[])
    alerts = AlertManager()
    from src.infrastructure.alert_system import AlertTier

    alerts.raise_alert(AlertTier.CRITICAL, "unrelated GTT failure", source="TRD-OTHER")

    await _auto_close_ghost_trades(
        ReconciliationResult(gaps=[_gap()]), broker, repo, None, alert_manager=alerts
    )
    await _auto_close_ghost_trades(
        ReconciliationResult(gaps=[]), broker, repo, None, alert_manager=alerts
    )

    assert [a.source for a in alerts.active()] == ["TRD-OTHER"]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_left_open_does_not_require_an_alert_manager() -> None:
    """alert_manager stays optional — every existing call site without one must
    keep working exactly as before."""
    row = {"direction": "LONG", "quantity": 145, "fill_price": 84.27}
    repo = _make_trade_repo(row)
    broker = _make_broker(orders=[], holdings=[])

    closed = await _auto_close_ghost_trades(ReconciliationResult(gaps=[_gap()]), broker, repo, None)

    assert closed == 0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_falls_back_to_db_fill_price_without_holdings_row() -> None:
    row = {"direction": "LONG", "quantity": 40, "fill_price": 310.2}
    repo = _make_trade_repo(row)
    broker = _make_broker(
        orders=[_order(symbol="JINDALSAW", direction="SELL", avg_price=320.0, filled_qty=40)],
        holdings=[],  # no holdings record at all for this symbol
    )
    closed = await _auto_close_ghost_trades(
        ReconciliationResult(gaps=[_gap(symbol="JINDALSAW")]), broker, repo, None
    )

    assert closed == 1
    args = repo.update_pnl.call_args.args
    assert float(args[1]) == pytest.approx((320.0 - 310.2) * 40)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_ignores_non_ghost_gaps() -> None:
    repo = _make_trade_repo(None)
    broker = _make_broker()
    result = ReconciliationResult(gaps=[_gap(kind="QTY_MISMATCH")])

    closed = await _auto_close_ghost_trades(result, broker, repo, None)

    assert closed == 0
    repo.get_by_agent_id.assert_not_called()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_no_gaps_is_a_noop() -> None:
    repo = _make_trade_repo(None)
    broker = _make_broker()
    closed = await _auto_close_ghost_trades(ReconciliationResult(gaps=[]), broker, repo, None)
    assert closed == 0
    broker.get_all_orders.assert_not_awaited()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_wrong_side_order_is_not_treated_as_exit() -> None:
    # A BUY order for a LONG position must never be mistaken for its exit.
    row = {"direction": "LONG", "quantity": 145, "fill_price": 84.27}
    repo = _make_trade_repo(row)
    broker = _make_broker(orders=[_order(direction="BUY", avg_price=84.27)], holdings=[])

    closed = await _auto_close_ghost_trades(ReconciliationResult(gaps=[_gap()]), broker, repo, None)

    assert closed == 0
    repo.update_pnl.assert_not_awaited()
