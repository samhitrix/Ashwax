"""DurableIdempotencyGuard: additive, fail-open, blocks only exact duplicates in enforce mode."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.brokers.base import BrokerInterface, OrderRequest
from src.execution.idempotency import DuplicateOrderError, DurableIdempotencyGuard
from src.execution.order_lifecycle import OrderLifecycleManager


def _order(signal_id: str = "SIG1", price: float = 100.0) -> OrderRequest:
    return OrderRequest(
        symbol="ABC",
        exchange="NSE",
        direction="BUY",
        quantity=10,
        order_type="LIMIT",
        product="MIS",
        validity="DAY",
        agent_trade_id="TRD-1",
        signal_id=signal_id,
        price=price,
    )


class _MemRepo:
    """In-memory stand-in reproducing OrderIntentRepository.begin's claim semantics."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.calls = 0

    async def begin(self, key, *, window_seconds, **kw):  # type: ignore[no-untyped-def]
        self.calls += 1
        row = self.rows.get(key)
        if row is None or row["state"] == "FAILED" or row.get("stale"):
            self.rows[key] = {"state": "PENDING", "order_id": None}
            return True, None, None
        return False, row["state"], row["order_id"]

    async def mark_submitted(self, key, broker_order_id):  # type: ignore[no-untyped-def]
        self.rows[key].update(state="SUBMITTED", order_id=broker_order_id)

    async def mark_failed(self, key):  # type: ignore[no-untyped-def]
        self.rows[key]["state"] = "FAILED"


async def test_first_attempt_proceeds_and_is_recorded() -> None:
    repo = _MemRepo()
    g = DurableIdempotencyGuard(repo, mode="enforce")
    d = await g.begin(_order(), is_paper=True)
    assert d.proceed and d.key is not None
    await g.submitted(d, "E1")
    assert repo.rows[d.key]["state"] == "SUBMITTED"


async def test_enforce_refuses_exact_duplicate() -> None:
    g = DurableIdempotencyGuard(_MemRepo(), mode="enforce")
    d1 = await g.begin(_order(), is_paper=True)
    await g.submitted(d1, "E1")
    d2 = await g.begin(_order(), is_paper=True)
    assert d2.proceed is False
    assert d2.duplicate_order_id == "E1"


async def test_shadow_logs_duplicate_but_never_blocks() -> None:
    g = DurableIdempotencyGuard(_MemRepo(), mode="shadow")
    d1 = await g.begin(_order(), is_paper=True)
    await g.submitted(d1, "E1")
    d2 = await g.begin(_order(), is_paper=True)
    assert d2.proceed is True


async def test_failed_previous_attempt_never_blocks_a_retry() -> None:
    g = DurableIdempotencyGuard(_MemRepo(), mode="enforce")
    d1 = await g.begin(_order(), is_paper=True)
    await g.failed(d1)
    assert (await g.begin(_order(), is_paper=True)).proceed is True


async def test_different_signal_or_price_is_not_a_duplicate() -> None:
    g = DurableIdempotencyGuard(_MemRepo(), mode="enforce")
    await g.begin(_order("SIG1"), is_paper=True)
    assert (await g.begin(_order("SIG2"), is_paper=True)).proceed is True
    assert (await g.begin(_order("SIG1", price=101.0), is_paper=True)).proceed is True


async def test_repo_error_fails_open() -> None:
    repo = MagicMock()
    repo.begin = AsyncMock(side_effect=RuntimeError("db down"))
    d = await DurableIdempotencyGuard(repo, mode="enforce").begin(_order(), is_paper=True)
    assert d.proceed is True and d.key is None


async def test_repo_timeout_fails_open() -> None:
    async def _slow(*a, **k):  # type: ignore[no-untyped-def]
        await asyncio.sleep(5)

    repo = MagicMock()
    repo.begin = _slow
    g = DurableIdempotencyGuard(repo, mode="enforce", timeout_seconds=0.05)
    assert (await g.begin(_order(), is_paper=True)).proceed is True


async def test_off_mode_never_touches_the_db() -> None:
    repo = _MemRepo()
    d = await DurableIdempotencyGuard(repo, mode="off").begin(_order(), is_paper=True)
    assert d.proceed is True and repo.calls == 0


def _mgr(guard):  # type: ignore[no-untyped-def]
    broker = MagicMock(spec=BrokerInterface)
    return OrderLifecycleManager(broker, is_paper=True, idempotency=guard), broker


async def _place(mgr, **kw):  # type: ignore[no-untyped-def]
    return await mgr.place_entry_and_protection(
        signal_id="SIG1",
        symbol="ABC",
        direction="BUY",
        qty=10,
        entry_price=100.0,
        sl_price=98.0,
        target_price=104.0,
        product="MIS",
        **kw,
    )


async def test_lifecycle_without_guard_is_unchanged() -> None:
    mgr, broker = _mgr(None)
    broker.place_order = AsyncMock(side_effect=["E1", "SL1", "T1"])
    assert await _place(mgr) == ("E1", "SL1", "T1")


async def test_lifecycle_enforce_blocks_second_identical_placement_before_the_broker() -> None:
    mgr, broker = _mgr(DurableIdempotencyGuard(_MemRepo(), mode="enforce"))
    broker.place_order = AsyncMock(side_effect=["E1", "SL1", "T1", "E2", "SL2", "T2"])
    await _place(mgr)
    calls_after_first = broker.place_order.call_count
    with pytest.raises(DuplicateOrderError):
        await _place(mgr)
    assert broker.place_order.call_count == calls_after_first  # broker never called again


async def test_lifecycle_shadow_lets_the_duplicate_through() -> None:
    mgr, broker = _mgr(DurableIdempotencyGuard(_MemRepo(), mode="shadow"))
    broker.place_order = AsyncMock(side_effect=["E1", "SL1", "T1", "E2", "SL2", "T2"])
    await _place(mgr)
    assert (await _place(mgr))[0] == "E2"


async def test_lifecycle_broker_failure_marks_failed_so_retry_is_allowed() -> None:
    repo = _MemRepo()
    mgr, broker = _mgr(DurableIdempotencyGuard(repo, mode="enforce"))
    broker.place_order = AsyncMock(
        side_effect=[RuntimeError("AMOs can only be placed after trading hours"), "E2", "SL2", "T2"]
    )
    with pytest.raises(RuntimeError):
        await _place(mgr)
    assert next(iter(repo.rows.values()))["state"] == "FAILED"
    assert (await _place(mgr))[0] == "E2"  # the retry is not blocked
