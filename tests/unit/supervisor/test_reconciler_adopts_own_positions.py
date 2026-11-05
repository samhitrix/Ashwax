"""The reconciler adopts a position this agent really opened (proved by the broker order), keeps
the user's own trades external, and doesn't call the options desk's legs "external"."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from src.brokers.base import BrokerInterface, Order, Position
from src.supervisor import reconciler as rec_mod
from src.supervisor.reconciler import Reconciler


def _broker(symbol: str = "INFY") -> MagicMock:
    b = MagicMock(spec=BrokerInterface)
    b.get_positions = AsyncMock(
        return_value=[Position(symbol, "NSE", "long", 10, 1500.0, 1500.0, 0.0, "INTRADAY")]
    )
    b.get_holdings = AsyncMock(return_value=[])
    return b


def _filled(oid: str = "E1", filled: int = 10) -> Order:
    return Order(
        broker_order_id=oid,
        symbol="INFY",
        direction="BUY",
        quantity=10,
        filled_quantity=filled,
        pending_quantity=10 - filled,
        status="FILLED" if filled else "CANCELLED",
        order_type="LIMIT",
        product="INTRADAY",
        average_price=1500.5,
        tag="",
        placed_at=datetime.now(UTC),
    )


class _Pool:
    """Answers only the queries these helpers issue."""

    def __init__(self, written_off: list[dict], option_legs: list | None = None) -> None:
        self.written_off = written_off
        self.option_legs = option_legs or []
        self.executed: list[tuple] = []

    async def fetch(self, sql: str, *args):
        if "options_positions" in sql:
            return [{"legs": legs} for legs in self.option_legs]
        return self.written_off

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


def _reconciler(broker: MagicMock, pool: _Pool) -> Reconciler:
    rec = Reconciler(broker, pool=pool, broker_name="kite")
    rec._fetch_db_open_trades = AsyncMock(return_value={})  # type: ignore[method-assign]
    rec._track_external_positions = AsyncMock(side_effect=lambda g: g)  # type: ignore[method-assign]
    return rec


async def test_adopts_written_off_row_when_broker_order_proves_the_fill(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(rec_mod, "notify", lambda kind, **kw: calls.append((kind, kw)))
    b = _broker()
    b.get_order_status = AsyncMock(return_value=_filled())
    pool = _Pool([{"agent_trade_id": "T1", "broker_order_id": "E1"}])
    res = await _reconciler(b, pool).reconcile()
    assert res.gaps == []
    sql, args = pool.executed[0]
    assert "order_state = 'FILLED'" in sql
    assert args[0] == "T1"
    assert 1500.5 in [float(a) for a in args if isinstance(a, (int, float))] or any(
        str(a) == "1500.5" for a in args
    )
    assert calls  # operator is told


async def test_manual_position_without_order_proof_is_not_adopted() -> None:
    b = _broker()
    b.get_order_status = AsyncMock(return_value=_filled(filled=0))
    pool = _Pool([{"agent_trade_id": "T1", "broker_order_id": "E1"}])
    res = await _reconciler(b, pool).reconcile()
    assert [g.kind for g in res.gaps] == ["ORPHANED_BROKER_POSITION"]
    assert pool.executed == []


async def test_no_candidate_row_stays_orphan() -> None:
    b = _broker()
    pool = _Pool([])
    res = await _reconciler(b, pool).reconcile()
    assert [g.symbol for g in res.gaps] == ["INFY"]
    assert pool.executed == []


async def test_order_read_failure_never_adopts_and_never_hides_the_orphan() -> None:
    b = _broker()
    b.get_order_status = AsyncMock(side_effect=RuntimeError("timeout"))
    pool = _Pool([{"agent_trade_id": "T1", "broker_order_id": "E1"}])
    res = await _reconciler(b, pool).reconcile()
    assert [g.symbol for g in res.gaps] == ["INFY"]
    assert pool.executed == []


async def test_open_option_legs_are_not_orphans() -> None:
    b = _broker("INDEX26JAN20000CE")
    legs = {"income": [{"tradingsymbol": "BFO:INDEX26JAN20000CE"}], "protection": [], "closed": []}
    res = await _reconciler(b, _Pool([], [legs])).reconcile()
    assert res.gaps == []


async def test_new_unknown_intraday_position_alerts_once(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(rec_mod, "notify", lambda kind, **kw: calls.append((kind, kw)))

    class _Repo:
        def __init__(self, pool) -> None:  # type: ignore[no-untyped-def]
            pass

        async def sync(self, orphans, exec_broker=None):  # type: ignore[no-untyped-def]
            s = MagicMock()
            s.new, s.closed, s.acknowledged = (
                [("INFY", "INTRADAY"), ("LIQUIDBEES", "DELIVERY")],
                [],
                set(),
            )
            return s

    from src.db.repositories import external_positions as ep

    monkeypatch.setattr(ep, "ExternalPositionRepository", _Repo)
    rec = Reconciler(_broker(), pool=_Pool([]), broker_name="kite")
    rec._fetch_db_open_trades = AsyncMock(return_value={})  # type: ignore[method-assign]
    await rec.reconcile()
    assert len(calls) == 1  # the delivery holding is not an alert
    assert "INFY" in str(calls[0][1])
