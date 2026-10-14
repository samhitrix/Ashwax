"""Reconciler records broker positions this agent did not open in external_positions once,
stops reporting acknowledged ones, and never hides an orphan because tracking failed."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from src.brokers.base import BrokerInterface, Holding
from src.db.repositories import external_positions as ep
from src.supervisor.reconciler import Reconciler


def _holding(symbol: str, qty: int = 10) -> Holding:
    return Holding(
        symbol=symbol,
        exchange="NSE",
        quantity=qty,
        average_price=100.0,
        last_price=100.0,
        pnl=0.0,
        t1_quantity=0,
    )


class _FakeRepo:
    """In-memory stand-in for ExternalPositionRepository.sync's semantics."""

    rows: dict[tuple[str, str], dict] = {}

    def __init__(self, pool) -> None:  # type: ignore[no-untyped-def]
        pass

    async def sync(self, orphans):  # type: ignore[no-untyped-def]
        current = {(s, p) for s, p, _q, _a in orphans}
        new = [k for k in current if k not in self.rows]
        for k in new:
            self.rows[k] = {"ack": False}
        closed = [k for k in list(self.rows) if k not in current]
        for k in closed:
            del self.rows[k]
        return ep.ExternalSync(
            new=new, closed=closed, acknowledged={k for k in current if self.rows[k]["ack"]}
        )


@pytest.fixture(autouse=True)
def _patch_repo(monkeypatch):  # type: ignore[no-untyped-def]
    _FakeRepo.rows = {}
    monkeypatch.setattr(ep, "ExternalPositionRepository", _FakeRepo)


def _reconciler(holdings: list[Holding]) -> Reconciler:
    broker = MagicMock(spec=BrokerInterface)
    broker.get_positions = AsyncMock(return_value=[])
    broker.get_holdings = AsyncMock(return_value=holdings)
    return Reconciler(broker, pool=object())


async def test_orphan_is_reported_and_recorded_once(caplog) -> None:  # type: ignore[no-untyped-def]
    rec = _reconciler([_holding("LIQUIDBEES")])
    with caplog.at_level("WARNING", logger="supervisor.reconciler"):
        r1 = await rec.reconcile({})
        r2 = await rec.reconcile({})
    assert [g.symbol for g in r1.gaps] == ["LIQUIDBEES"] and r1.gaps[0].product == "DELIVERY"
    assert len(r2.gaps) == 1  # still reported until acknowledged
    detected = [m for m in caplog.messages if "external_position_detected" in m]
    assert len(detected) == 1  # ...but the "new" alert fires only on first sight


async def test_acknowledged_orphan_is_no_longer_a_gap() -> None:
    rec = _reconciler([_holding("LIQUIDBEES")])
    await rec.reconcile({})
    _FakeRepo.rows[("LIQUIDBEES", "DELIVERY")]["ack"] = True
    result = await rec.reconcile({})
    assert result.gaps == [] and result.clean


async def test_orphan_that_disappears_is_closed(caplog) -> None:  # type: ignore[no-untyped-def]
    rec = _reconciler([_holding("SYMBIOTEC")])
    await rec.reconcile({})
    rec._broker.get_holdings = AsyncMock(return_value=[])
    with caplog.at_level("INFO", logger="supervisor.reconciler"):
        await rec.reconcile({})
    assert ("SYMBIOTEC", "DELIVERY") not in _FakeRepo.rows
    assert any("external_position_closed" in m for m in caplog.messages)


async def test_tracking_failure_never_hides_an_orphan(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class _Broken(_FakeRepo):
        async def sync(self, orphans):  # type: ignore[no-untyped-def]
            raise RuntimeError("db down")

    monkeypatch.setattr(ep, "ExternalPositionRepository", _Broken)
    result = await _reconciler([_holding("LIQUIDBEES")]).reconcile({})
    assert [g.symbol for g in result.gaps] == ["LIQUIDBEES"]


async def test_non_orphan_gaps_are_untouched_by_acknowledgement() -> None:
    rec = _reconciler([])
    trade = {"qty": 5, "fill_price": 10.0, "agent_trade_id": "T1", "product": "MIS"}
    result = await rec.reconcile({"GHOSTY": [trade]})
    assert [g.kind for g in result.gaps] == ["GHOST_DB_TRADE"]
