"""Tests for orchestrator._reseed_order_monitor (2026-08-17 fix).

Root cause: OrderMonitor.track() is only ever called at the moment of entry.
Its _positions dict is in-memory only, so every process restart silently
dropped SL/target monitoring for positions that were already open — found via
14 open swing trades showing "Orders Watched: 0" on the dashboard despite
having live SL/target prices in the DB.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, query, *args):
        return self._rows


def _row(**overrides):
    row = {
        "agent_trade_id": "TRD-SCI",
        "symbol": "NSE:SCI",
        "direction": "long",
        "quantity": 40,
        "fill_price": 308.40,
        "initial_sl": 283.73,
        "current_sl": 283.73,
        "initial_target": 370.08,
        "current_target": 370.08,
        "sl_order_id": None,
        "target_order_id": None,
        "gtt_sl_id": None,
        "gtt_target_id": None,
        "product": "CNC",
        "entry_timestamp": datetime(2026, 8, 7, 15, 47, 42, tzinfo=UTC),
        "strategy_name": "orb_intraday_smart",
        "partial_pnl_booked": 0,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_tracks_open_db_trade_not_in_memory() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import OrderMonitor

    pool = _FakePool([_row()])
    mon = OrderMonitor(MagicMock())
    added = await orchestrator._reseed_order_monitor(mon, pool, paper_mode=True)

    assert added == 1
    tracked = mon.get_monitored()
    assert len(tracked) == 1
    pos = tracked[0]
    assert pos.symbol == "NSE:SCI"
    assert pos.direction == "BUY"  # mapped from DB's 'long'
    assert pos.current_sl == 283.73
    assert pos.product == "CNC"
    assert pos.strategy_name == "orb_intraday_smart"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_maps_short_direction() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import OrderMonitor

    pool = _FakePool([_row(direction="short")])
    mon = OrderMonitor(MagicMock())
    await orchestrator._reseed_order_monitor(mon, pool, paper_mode=True)

    assert mon.get_monitored()[0].direction == "SELL"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_is_idempotent_skips_already_tracked() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import MonitoredPosition, OrderMonitor

    pool = _FakePool([_row()])
    mon = OrderMonitor(MagicMock())
    mon.track(
        MonitoredPosition(
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
    )
    added = await orchestrator._reseed_order_monitor(mon, pool, paper_mode=True)

    assert added == 0
    assert len(mon.get_monitored()) == 1


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_returns_zero_when_pool_is_none() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import OrderMonitor

    mon = OrderMonitor(MagicMock())
    added = await orchestrator._reseed_order_monitor(mon, None, paper_mode=True)

    assert added == 0
    assert mon.get_monitored() == []


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_skips_rows_missing_agent_trade_id() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import OrderMonitor

    pool = _FakePool([_row(agent_trade_id=None)])
    mon = OrderMonitor(MagicMock())
    added = await orchestrator._reseed_order_monitor(mon, pool, paper_mode=True)

    assert added == 0
    assert mon.get_monitored() == []


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_survives_fetch_error() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import OrderMonitor

    class _BrokenPool:
        async def fetch(self, query, *args):
            raise RuntimeError("connection lost")

    mon = OrderMonitor(MagicMock())
    added = await orchestrator._reseed_order_monitor(mon, _BrokenPool(), paper_mode=True)

    assert added == 0
    assert mon.get_monitored() == []


# ── Per-broker reseed (phase 2: exec_broker, NULL = legacy "kite") ──────────
# Each live stack must reseed ONLY the rows its own broker holds; tracking another broker's
# row would send that position's exits to the wrong account. Unstamped (NULL), unknown and
# pre-089 rows belong to the KITE stack (NULL means kite everywhere), never to the primary.


class _BrokerPool:
    """Emulates `AND (COALESCE(exec_broker,'kite') = $1 [OR ... <> ALL($2)])` on fake rows,
    and the orphan check's GROUP BY."""

    def __init__(self, rows, has_column=True):
        self._rows = rows
        self._has_column = has_column
        self.queries: list[str] = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        if "exec_broker" not in query:
            if "COUNT(*)" in query:
                return [{"n": len(self._rows)}]
            return self._rows
        if not self._has_column:
            import asyncpg

            raise asyncpg.exceptions.UndefinedColumnError('column "exec_broker" does not exist')
        if "GROUP BY" in query:
            counts: dict = {}
            for r in self._rows:
                counts[r.get("exec_broker")] = counts.get(r.get("exec_broker"), 0) + 1
            return [{"exec_broker": k, "n": v} for k, v in counts.items()]
        own = args[0]
        known = list(args[1]) if len(args) > 1 else None

        def _keep(r):
            b = r.get("exec_broker") or "kite"
            return b == own or (known is not None and b not in known)

        return [r for r in self._rows if _keep(r)]


_MIXED = [
    _row(agent_trade_id="T-KITE", exec_broker="kite"),
    _row(agent_trade_id="T-ANGEL", exec_broker="angel_one"),
    _row(agent_trade_id="T-NULL", exec_broker=None),
    _row(agent_trade_id="T-BAD", exec_broker="kitee"),
]
_STACKS = {"paper": object(), "live:kite": object(), "live:angel_one": object()}
_ANGEL_ONLY = {"paper": object(), "live:angel_one": object()}


async def _tracked(key, rows=_MIXED, stacks=_STACKS, legacy="kite", has_column=True):
    from src import orchestrator
    from src.execution.order_lifecycle import OrderMonitor

    mon = OrderMonitor(MagicMock())
    pool = _BrokerPool(rows, has_column)
    await orchestrator._reseed_order_monitor(
        mon,
        pool,
        orchestrator.stack_is_paper(key),
        **orchestrator._reseed_scope(key, stacks, legacy),
    )
    return sorted(p.agent_trade_id for p in mon.get_monitored())


@pytest.mark.asyncio  # type: ignore[misc]
@pytest.mark.parametrize("legacy", ["kite", "angel_one"])  # type: ignore[misc]
async def test_kite_stack_owns_null_unknown_and_kite_rows_whoever_is_primary(legacy) -> None:
    assert await _tracked("live:kite", legacy=legacy) == ["T-BAD", "T-KITE", "T-NULL"]
    assert await _tracked("live:angel_one", legacy=legacy) == ["T-ANGEL"]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_paper_reseed_is_unfiltered() -> None:
    pool = _BrokerPool(_MIXED)
    from src import orchestrator
    from src.execution.order_lifecycle import OrderMonitor

    mon = OrderMonitor(MagicMock())
    added = await orchestrator._reseed_order_monitor(
        mon, pool, True, **orchestrator._reseed_scope("paper", _STACKS, "kite")
    )
    assert added == 4
    assert "exec_broker" not in pool.queries[0] and "paper_trades" in pool.queries[0]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_kite_only_config_reseeds_the_same_rows_as_before() -> None:
    stacks = {"paper": object(), "live:kite": object()}
    rows = [r for r in _MIXED if r["agent_trade_id"] != "T-ANGEL"]
    assert await _tracked("live:kite", rows=rows, stacks=stacks) == ["T-BAD", "T-KITE", "T-NULL"]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_angel_primary_without_kite_stack_never_takes_unstamped_rows() -> None:
    # Scenario A: legacy angel_one, a boot DB read failure built no kite stack, NULL (Kite)
    # positions are open -> the angel monitor must not track (and later exit) them.
    assert await _tracked("live:angel_one", stacks=_ANGEL_ONLY, legacy="angel_one") == ["T-ANGEL"]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_missing_column_only_the_kite_stack_reseeds_unfiltered() -> None:
    # A DB without migration 089 only holds pre-routing rows, all placed through Kite.
    assert await _tracked("live:kite", has_column=False) == ["T-ANGEL", "T-BAD", "T-KITE", "T-NULL"]
    assert await _tracked("live:angel_one", has_column=False) == []
    # Scenario B: legacy angel_one is primary -> still nothing unfiltered for it
    assert (
        await _tracked("live:angel_one", stacks=_ANGEL_ONLY, legacy="angel_one", has_column=False)
        == []
    )


@pytest.mark.asyncio  # type: ignore[misc]
async def test_explicit_broker_without_fallback_reseeds_nothing_on_missing_column() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import OrderMonitor

    mon = OrderMonitor(MagicMock())
    added = await orchestrator._reseed_order_monitor(
        mon, _BrokerPool(_MIXED, has_column=False), False, broker_name="kite"
    )
    assert added == 0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_missing_column_error_is_logged_once_per_condition(caplog) -> None:
    import logging

    caplog.set_level(logging.WARNING)
    from src import orchestrator

    orchestrator._RESEED_LOGGED.clear()
    for _ in range(3):
        await _tracked("live:angel_one", has_column=False)
    hits = [r for r in caplog.records if "reseed_exec_broker_missing" in r.getMessage()]
    assert len(hits) == 1


def test_reseed_scope() -> None:
    from src import orchestrator
    from src.brokers.registry import list_brokers

    known = sorted(set(list_brokers()) - {"paper"})
    kite_scope = {"broker_name": "kite", "known_brokers": known, "unfiltered_if_no_column": True}
    assert orchestrator._reseed_scope("paper", _STACKS, "kite") == {}
    assert orchestrator._reseed_scope("live:kite", _STACKS, "kite") == kite_scope
    # the primary does not matter: kite owns unstamped rows
    assert orchestrator._reseed_scope("live:kite", _STACKS, "angel_one") == kite_scope
    assert orchestrator._reseed_scope("live:angel_one", _STACKS, "angel_one") == {
        "broker_name": "angel_one"
    }
    # legacy bare "live" key (old fixtures) is the legacy broker's stack
    assert orchestrator._reseed_scope("live", {"live": object()}, "kite")["broker_name"] == "kite"


# ── orphaned open live rows: no stack may reseed them ────────────────────────


@pytest.fixture
def _alerts(monkeypatch):
    import src.infrastructure.operator_alerts as oa
    from src import orchestrator

    sent: list[str] = []
    monkeypatch.setattr(oa, "raise_alert", lambda msg, **kw: sent.append(msg) or True)
    orchestrator._ORPHANS_LOGGED.clear()
    return sent


@pytest.mark.asyncio  # type: ignore[misc]
async def test_orphaned_unstamped_rows_raise_critical_once(_alerts, caplog) -> None:
    import logging

    from src import orchestrator

    caplog.set_level(logging.CRITICAL)
    rows = [r for r in _MIXED if r["agent_trade_id"] != "T-ANGEL"]  # NULL, kite, kitee
    for _ in range(3):
        n = await orchestrator._report_orphaned_live_rows(_BrokerPool(rows), _ANGEL_ONLY)
        assert n == 3
    crit = [r for r in caplog.records if "reseed_orphaned_live_rows" in r.getMessage()]
    assert len(crit) == 1 and "kite=3" in crit[0].getMessage()
    assert len(_alerts) == 1


@pytest.mark.asyncio  # type: ignore[misc]
async def test_no_orphans_when_every_owner_has_a_stack(_alerts) -> None:
    from src import orchestrator

    assert await orchestrator._report_orphaned_live_rows(_BrokerPool(_MIXED), _STACKS) == 0
    assert _alerts == []


@pytest.mark.asyncio  # type: ignore[misc]
async def test_orphan_check_on_pre_089_db_counts_rows_as_kite(_alerts) -> None:
    from src import orchestrator

    pool = _BrokerPool(_MIXED, has_column=False)
    assert await orchestrator._report_orphaned_live_rows(pool, _ANGEL_ONLY) == 4
    assert await orchestrator._report_orphaned_live_rows(pool, _STACKS) == 0


def test_every_orphan_report_call_is_best_effort() -> None:
    """The orphan report is an alert, never a reason to abort boot or the 30 s loop: every
    call site (boot and reconcile) runs it under contextlib.suppress(Exception)."""
    import inspect

    from src import orchestrator

    # The boot call sits directly in _run, so contextlib must be a module-level name there
    # (the nested loops import it locally; that does not cover _run).
    assert inspect.ismodule(getattr(orchestrator, "contextlib", None))
    lines = inspect.getsource(orchestrator).splitlines()
    calls = [i for i, ln in enumerate(lines) if "await _report_orphaned_live_rows(" in ln]
    assert len(calls) >= 2
    for i in calls:
        assert lines[i - 1].strip() == "with contextlib.suppress(Exception):", lines[i]
