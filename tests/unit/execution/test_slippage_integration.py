"""Tests for SlippageTracker.all_stats() and its hook in OrderLifecycleManager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.monitoring.slippage_tracker import ADVBucket, OrderType, SlippageTracker, TimeOfDay


class TestSlippageTrackerAllStats:
    def test_empty_tracker_returns_empty_list(self) -> None:
        assert SlippageTracker().all_stats() == []

    def test_one_fill_returns_one_stat(self) -> None:
        tracker = SlippageTracker()
        tracker.record_fill(
            "t1", "RELIANCE", OrderType.LIMIT, 100.0, 100.5, TimeOfDay.MORNING, ADVBucket.MID
        )
        stats = tracker.all_stats()
        assert len(stats) == 1
        assert stats[0].order_type == OrderType.LIMIT.value

    def test_two_different_buckets_return_two_stats(self) -> None:
        tracker = SlippageTracker()
        tracker.record_fill(
            "t1", "REL", OrderType.LIMIT, 100.0, 100.3, TimeOfDay.MORNING, ADVBucket.MID
        )
        tracker.record_fill(
            "t2", "INF", OrderType.MARKET, 200.0, 200.1, TimeOfDay.AFTERNOON, ADVBucket.HIGH
        )
        assert len(tracker.all_stats()) == 2

    def test_same_bucket_multiple_fills_aggregated(self) -> None:
        tracker = SlippageTracker()
        for i in range(5):
            tracker.record_fill(
                f"t{i}", "SYM", OrderType.LIMIT, 100.0, 100.1, TimeOfDay.MORNING, ADVBucket.MID
            )
        stats = tracker.all_stats()
        assert len(stats) == 1
        assert stats[0].count == 5


class TestOrderLifecycleSlippageHook:
    def test_lifecycle_has_slippage_tracker(self) -> None:
        from src.execution.order_lifecycle import OrderLifecycleManager

        mgr = OrderLifecycleManager(MagicMock())
        assert isinstance(mgr.slippage_tracker, SlippageTracker)

    def test_slippage_tracker_starts_empty(self) -> None:
        from src.execution.order_lifecycle import OrderLifecycleManager

        mgr = OrderLifecycleManager(MagicMock())
        assert mgr.slippage_tracker.all_stats() == []

    async def test_fill_adds_slippage_record(self) -> None:
        import datetime as _dt

        from src.brokers.base import Fill, Order, OrderRequest
        from src.execution.order_lifecycle import OrderLifecycleManager, OrderState

        now = _dt.datetime.now()
        broker = AsyncMock()
        broker.get_order_status.return_value = Order(
            broker_order_id="B1",
            symbol="RELIANCE",
            direction="BUY",
            quantity=10,
            filled_quantity=10,
            pending_quantity=0,
            status="FILLED",
            order_type="LIMIT",
            product="CNC",
            average_price=100.5,
            tag="",
            placed_at=now,
        )
        broker.get_trades.return_value = [
            Fill(order_id="B1", trade_id="F1", quantity=10, price=100.5, exchange_time=now)
        ]

        mgr = OrderLifecycleManager(broker)
        req = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            direction="BUY",
            quantity=10,
            order_type="LIMIT",
            product="CNC",
            validity="DAY",
            agent_trade_id="T1",
            signal_id="S1",
            price=100.0,
        )
        record = mgr.create_trade(req)
        record.broker_order_id = "B1"
        record.state = OrderState.OPEN
        record.state_history.append((OrderState.OPEN, now))

        await mgr.poll(record.agent_trade_id)

        stats = mgr.slippage_tracker.all_stats()
        assert len(stats) == 1
        assert stats[0].count == 1
