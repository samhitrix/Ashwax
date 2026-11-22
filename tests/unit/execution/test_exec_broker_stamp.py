"""exec_broker (migration 089): every new LIVE trade row records the broker that holds it, and
charges / the closed-trade rate card follow that broker (phase 2, per-strategy live routing).

NULL exec_broker means legacy "kite", so paper rows and legacy (broker_name=None) lifecycles
must not stamp anything: one live broker with no routing stays byte-for-byte as before."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.brokers.base import BrokerInterface
from src.execution.order_lifecycle import MonitoredPosition, OrderLifecycleManager, OrderMonitor


class _Repo:
    def __init__(self) -> None:
        self.rows: list[tuple[dict[str, Any], bool]] = []

    async def insert(self, row: dict[str, Any], is_paper: bool = False) -> int:
        self.rows.append((dict(row), is_paper))
        return 1


async def _persist(**kw: Any) -> tuple[dict[str, Any], bool]:
    repo = _Repo()
    mgr = OrderLifecycleManager(MagicMock(spec=BrokerInterface), trade_repo=repo, **kw)  # type: ignore[arg-type]
    await mgr._persist_entry(
        "TRD-1", "BRK-1", "INFY", "NSE", "BUY", 10, 100.0, 95.0, 110.0, "MIS", meta=None
    )
    assert len(repo.rows) == 1
    return repo.rows[0]


# ── entry stamp ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_lifecycle_stamps_its_broker() -> None:
    row, is_paper = await _persist(is_paper=False, broker_name="angel_one")
    assert row["exec_broker"] == "angel_one"
    assert is_paper is False


@pytest.mark.asyncio
async def test_paper_lifecycle_stamps_nothing() -> None:
    row, is_paper = await _persist(is_paper=True)
    assert "exec_broker" not in row
    assert is_paper is True


@pytest.mark.asyncio
async def test_paper_lifecycle_never_stamps_even_if_given_a_name() -> None:
    row, _ = await _persist(is_paper=True, broker_name="kite")
    assert "exec_broker" not in row


@pytest.mark.asyncio
async def test_legacy_live_lifecycle_stamps_nothing() -> None:
    row, _ = await _persist(is_paper=False)  # broker_name=None -> DB NULL -> "kite"
    assert "exec_broker" not in row


# ── charges / closed-trade rate card ────────────────────────────────────────


def _pos(**kw: Any) -> MonitoredPosition:
    base: dict[str, Any] = dict(
        agent_trade_id="TRD-1",
        symbol="INFY",
        direction="BUY",
        quantity=10,
        fill_price=100.0,
        initial_sl=95.0,
        current_sl=95.0,
        initial_target=110.0,
        current_target=110.0,
        product="MIS",
    )
    base.update(kw)
    return MonitoredPosition(**base)


def _spy_pnl(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    import src.risk.real_pnl as rp

    seen: list[Any] = []
    real = rp.calculate_real_pnl

    def _spy(*a: Any, **k: Any) -> Any:
        seen.append(k.get("broker", "<absent>"))
        return real(*a, **k)

    monkeypatch.setattr(rp, "calculate_real_pnl", _spy)
    return seen


def test_closed_trade_uses_the_monitor_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy_pnl(monkeypatch)
    mon = OrderMonitor(MagicMock(), is_paper=False, broker_name="angel_one")
    mon._record_closed_trade(_pos(), 105.0, "TARGET")
    assert seen == ["angel_one"]
    assert mon.closed_trades[0]["broker"] == "angel_one"


def test_closed_trade_legacy_keeps_active_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.risk.real_pnl import active_broker

    seen = _spy_pnl(monkeypatch)
    live = OrderMonitor(MagicMock(), is_paper=False)
    live._record_closed_trade(_pos(), 105.0, "TARGET")
    paper = OrderMonitor(MagicMock(), is_paper=True)
    paper._record_closed_trade(_pos(), 105.0, "TARGET")
    assert seen == [None, None]
    assert live.closed_trades[0]["broker"] == active_broker(for_paper=False)
    assert paper.closed_trades[0]["broker"] == active_broker(for_paper=True)


@pytest.mark.asyncio
async def test_partial_booking_uses_the_monitor_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy_pnl(monkeypatch)
    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="BRK-P")
    mon = OrderMonitor(broker, is_paper=False, broker_name="angel_one")
    mon.track(_pos())
    await mon.book_partial("TRD-1", 0.5, 105.0)
    assert seen == ["angel_one"]


def test_calculate_real_pnl_uses_the_given_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.risk.real_pnl as rp

    asked: list[tuple[str, Any]] = []
    real_rates = rp.rates

    def _rates(segment: str, broker: str | None = None) -> dict[str, float]:
        asked.append((segment, broker))
        return real_rates(segment, broker)

    monkeypatch.setattr(rp, "rates", _rates)
    rp.calculate_real_pnl("INFY", 100.0, 105.0, 10, rp.ProductType.INTRADAY, broker="angel_one")
    rp.calculate_real_pnl("INFY", 100.0, 105.0, 10, rp.ProductType.INTRADAY)
    assert asked == [("equity", "angel_one"), ("equity", None)]


# ── TradeRepository: exec_broker against a DB with / without migration 089 ──


def _pool(column_exists: bool) -> tuple[MagicMock, AsyncMock]:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(
        side_effect=lambda sql, *a: column_exists if "information_schema" in sql else 7
    )
    pool = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def _insert_sqls(conn: AsyncMock) -> list[tuple[str, tuple[Any, ...]]]:
    return [(c.args[0], c.args[1:]) for c in conn.fetchval.await_args_list if "INSERT" in c.args[0]]


@pytest.mark.asyncio
async def test_repo_writes_exec_broker_when_the_column_exists() -> None:
    from src.db.repositories.trades import TradeRepository

    pool, conn = _pool(column_exists=True)
    repo = TradeRepository(pool)
    await repo.insert({"agent_trade_id": "T1", "exec_broker": "angel_one"})
    await repo.insert({"agent_trade_id": "T2", "exec_broker": "angel_one"})
    (sql1, args1), (sql2, _) = _insert_sqls(conn)
    assert "exec_broker" in sql1 and "angel_one" in args1
    assert "exec_broker" in sql2
    # the column check is cached per process: one information_schema query for two inserts
    checks = [c for c in conn.fetchval.await_args_list if "information_schema" in c.args[0]]
    assert len(checks) == 1


@pytest.mark.asyncio
async def test_repo_strips_exec_broker_when_the_column_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.infrastructure.operator_alerts as oa
    from src.db.repositories.trades import TradeRepository

    sent: list[str] = []
    monkeypatch.setattr(oa, "raise_alert", lambda msg, **kw: sent.append(msg) or True)
    pool, conn = _pool(column_exists=False)
    new_id = await TradeRepository(pool).insert({"agent_trade_id": "T1", "exec_broker": "kite"})
    assert new_id == 7  # the row is still written (best-effort entry persistence)
    ((sql, args),) = _insert_sqls(conn)
    assert "exec_broker" not in sql and args == ("T1",)
    assert sent == []  # a stripped "kite" is still read back correctly (NULL = kite)


@pytest.mark.asyncio
async def test_repo_strip_of_a_non_kite_broker_is_critical_but_row_is_written(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    import src.infrastructure.operator_alerts as oa
    from src.db.repositories.trades import TradeRepository

    caplog.set_level(logging.CRITICAL)
    sent: list[str] = []
    monkeypatch.setattr(oa, "raise_alert", lambda msg, **kw: sent.append(msg) or True)
    pool, conn = _pool(column_exists=False)
    new_id = await TradeRepository(pool).insert(
        {"agent_trade_id": "T1", "exec_broker": "angel_one"}
    )
    assert new_id == 7
    ((sql, _),) = _insert_sqls(conn)
    assert "exec_broker" not in sql
    assert len(sent) == 1 and "angel_one" in sent[0] and "T1" in sent[0]
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_unknown_charges_broker_warns_once_and_uses_defaults(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    import src.risk.real_pnl as rp

    caplog.set_level(logging.WARNING)
    rp._WARNED_UNKNOWN_BROKERS.discard("nobroker")
    raw = {"brokers": {"kite": {"equity": {"gst_rate": 0.18}}}}
    for _ in range(3):
        got = rp._resolve_rates(raw, "nobroker", "equity")
        assert got == rp._DEFAULTS_BY_SEGMENT["equity"]
    rp._resolve_rates(raw, "kite", "equity")
    hits = [r for r in caplog.records if "charges_broker_unknown" in r.getMessage()]
    assert len(hits) == 1 and "nobroker" in hits[0].getMessage()


@pytest.mark.asyncio
async def test_repo_without_exec_broker_never_checks_the_column() -> None:
    from src.db.repositories.trades import TradeRepository

    pool, conn = _pool(column_exists=True)
    await TradeRepository(pool).insert({"agent_trade_id": "T1"}, is_paper=True)
    assert all("information_schema" not in c.args[0] for c in conn.fetchval.await_args_list)


@pytest.mark.asyncio
async def test_open_trades_optional_exec_broker_filter() -> None:
    from src.db.repositories.trades import TradeRepository

    pool, conn = _pool(column_exists=True)
    conn.fetch = AsyncMock(return_value=[])
    repo = TradeRepository(pool)
    await repo.open_trades(is_paper=False)
    await repo.open_trades(is_paper=False, exec_broker="angel_one")
    (plain_sql, *plain_args), (filt_sql, *filt_args) = (c.args for c in conn.fetch.await_args_list)
    assert "exec_broker" not in plain_sql and len(plain_args) == 4
    assert "COALESCE(exec_broker, 'kite') = $5" in filt_sql and filt_args[-1] == "angel_one"


@pytest.mark.asyncio
async def test_update_pnl_never_touches_exec_broker() -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from src.db.repositories.trades import TradeRepository

    pool, conn = _pool(column_exists=True)
    await TradeRepository(pool).update_pnl(
        "T1", Decimal("1"), Decimal("0"), Decimal("100"), datetime.now(UTC), broker="angel_one"
    )
    sql = conn.execute.await_args.args[0]
    assert "exec_broker" not in sql and "broker = COALESCE($10, broker)" in sql
