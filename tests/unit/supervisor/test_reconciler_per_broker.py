"""Phase 2 task 9: reconciliation runs once per live broker (its own `trades` rows vs its own
positions/holdings, orphans tracked per broker), and the live held-symbol guard is the UNION
over brokers (R3). With one live broker the output is what it was."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.brokers.base import BrokerInterface
from src.supervisor.reconciler import Reconciler, ReconciliationGap, ReconciliationResult

from tests.unit.supervisor.test_reconciler import _holding, _position

_OLD_SQL = """
                    SELECT symbol, quantity, fill_price, agent_trade_id, product, entry_timestamp
                    FROM trades
                    WHERE order_state = 'FILLED'
                    """


def _broker(positions=None, holdings=None, *, fail: bool = False) -> MagicMock:  # type: ignore[no-untyped-def]
    b = MagicMock(spec=BrokerInterface)
    if fail:
        b.get_positions = AsyncMock(side_effect=RuntimeError("broker down"))
    else:
        b.get_positions = AsyncMock(return_value=positions or [])
    b.get_holdings = AsyncMock(return_value=holdings or [])
    return b


class _Conn:
    """fetch() double that applies the per-broker exec_broker filter to in-memory rows."""

    def __init__(self, rows: list[dict[str, Any]], *, no_column: bool = False) -> None:
        self.rows = rows
        self.no_column = no_column
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if not args:
            return list(self.rows)
        if self.no_column:
            raise RuntimeError('column "exec_broker" does not exist')
        name = args[0]
        known = args[1] if len(args) > 1 else None

        def _own(r: dict[str, Any]) -> bool:
            eb = r.get("exec_broker") or "kite"
            return eb == name or (known is not None and eb not in known)

        return [r for r in self.rows if _own(r)]


class _Pool:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def acquire(self) -> Any:
        conn = self.conn

        class _Ctx:
            async def __aenter__(self) -> Any:
                return conn

            async def __aexit__(self, *a: Any) -> bool:
                return False

        return _Ctx()


def _row(sym: str, tid: str, exec_broker: str | None, product: str = "MIS") -> dict[str, Any]:
    return {
        "symbol": sym,
        "quantity": 10,
        "fill_price": 100.0,
        "agent_trade_id": tid,
        "product": product,
        "entry_timestamp": None,
        "exec_broker": exec_broker,
    }


# ── Reconciler: per-broker rows ──────────────────────────────────────────────


async def test_legacy_reconciler_sql_is_unchanged() -> None:
    conn = _Conn([_row("A", "t1", "angel_one")])
    rows = await Reconciler(_broker(), _Pool(conn))._fetch_db_open_trades()
    assert conn.calls == [(_OLD_SQL, ())]
    assert set(rows) == {"A"}  # no broker name: every live row, as before


async def test_per_broker_rows_follow_exec_broker_and_null_is_kite() -> None:
    rows = [
        _row("K", "t1", "kite"),
        _row("N", "t2", None),
        _row("A", "t3", "angel_one"),
        _row("U", "t4", "mystery"),  # unregistered value: belongs to kite (Task 5 rule)
    ]
    conn = _Conn(rows)
    known = ["angel_one", "kite"]
    kite = await Reconciler(
        _broker(), _Pool(conn), broker_name="kite", known_brokers=known
    )._fetch_db_open_trades()
    angel = await Reconciler(
        _broker(), _Pool(conn), broker_name="angel_one", known_brokers=known
    )._fetch_db_open_trades()
    assert set(kite) == {"K", "N", "U"}
    assert set(angel) == {"A"}
    assert "COALESCE(exec_broker, 'kite') = $1" in conn.calls[0][0]
    assert "<> ALL($2::text[])" in conn.calls[0][0]
    assert "<> ALL" not in conn.calls[1][0] and conn.calls[1][1] == ("angel_one",)


async def test_pre_089_database_kite_unfiltered_other_brokers_none() -> None:
    conn = _Conn([_row("K", "t1", None)], no_column=True)
    assert set(await Reconciler(_broker(), _Pool(conn), "kite")._fetch_db_open_trades()) == {"K"}
    assert await Reconciler(_broker(), _Pool(conn), "angel_one")._fetch_db_open_trades() == {}


# ── orchestrator: one reconcile per live broker ──────────────────────────────


def _stacks(kite: Any, angel: Any | None = None) -> dict[str, Any]:
    s: dict[str, Any] = {
        "paper": {"broker": _broker(), "order_monitor": "M-paper"},
        "live:kite": {"broker": kite, "order_monitor": "M-kite"},
    }
    if angel is not None:
        s["live:angel_one"] = {"broker": angel, "order_monitor": "M-angel"}
    return s


def _record_ghost_closes(monkeypatch) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    import src.orchestrator as orch

    calls: list[dict[str, Any]] = []

    async def _fake(res, broker, repo, monitor, alert_manager=None, **kw):  # type: ignore[no-untyped-def]
        calls.append(
            {
                "broker": broker,
                "monitor": monitor,
                "ghosts": {g.agent_trade_id for g in res.gaps if g.kind == "GHOST_DB_TRADE"},
                **kw,
            }
        )
        return len(calls)

    monkeypatch.setattr(orch, "_auto_close_ghost_trades", _fake)
    monkeypatch.setattr(orch, "_known_live_brokers", lambda: ["angel_one", "kite"])
    return calls


async def test_two_brokers_each_with_a_ghost_and_an_orphan_are_independent(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.orchestrator import _reconcile_live_brokers

    calls = _record_ghost_closes(monkeypatch)
    rows = [_row("KGHOST", "tk", None), _row("AGHOST", "ta", "angel_one")]
    kite = _broker([_position("KORPH", product="INTRADAY")])
    angel = _broker([_position("AORPH", product="INTRADAY")])
    shared: dict[str, Any] = {"legacy_live_broker": "kite"}
    runs = await _reconcile_live_brokers(
        shared, _stacks(kite, angel), None, _Pool(_Conn(rows)), "repo", None, call_timeout=5.0
    )
    assert [r["key"] for r in runs] == ["live:kite", "live:angel_one"]
    gaps = {r["key"]: {(g.kind, g.symbol) for g in r["result"].gaps} for r in runs}
    assert gaps["live:kite"] == {
        ("GHOST_DB_TRADE", "KGHOST"),
        ("ORPHANED_BROKER_POSITION", "KORPH"),
    }
    assert gaps["live:angel_one"] == {
        ("GHOST_DB_TRADE", "AGHOST"),
        ("ORPHANED_BROKER_POSITION", "AORPH"),
    }
    # each broker's ghosts are closed through ITS broker and monitor only
    assert [(c["broker"], c["monitor"], c["ghosts"]) for c in calls] == [
        (kite, "M-kite", {"tk"}),
        (angel, "M-angel", {"ta"}),
    ]
    assert all(c["current_ghost_ids"] == {"tk", "ta"} and c["resolve_stale"] for c in calls)
    assert [r["auto_closed"] for r in runs] == [1, 2]


async def test_secondary_failure_is_skipped_and_primary_failure_raises(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.orchestrator import _reconcile_live_brokers

    calls = _record_ghost_closes(monkeypatch)
    kite = _broker()

    async def _hang() -> list[Any]:
        await asyncio.sleep(5)
        return []

    angel = _broker()
    angel.get_positions = AsyncMock(side_effect=_hang)
    shared: dict[str, Any] = {"legacy_live_broker": "kite"}
    pool = _Pool(_Conn([_row("KGHOST", "tk", None)]))
    runs = await _reconcile_live_brokers(
        shared, _stacks(kite, angel), None, pool, "repo", None, call_timeout=0.05
    )
    assert [r["key"] for r in runs] == ["live:kite"]
    assert calls[0]["resolve_stale"] is False  # angel's ghost alerts must not be resolved
    with pytest.raises(TimeoutError):
        await _reconcile_live_brokers(
            shared, _stacks(angel, kite), None, pool, "repo", None, call_timeout=0.05
        )


async def test_no_live_stack_reconciles_the_fallback_broker_unfiltered(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.orchestrator import _reconcile_live_brokers, _reconcile_targets

    calls = _record_ghost_closes(monkeypatch)
    fb = _broker()
    stacks = {"paper": {"broker": fb, "order_monitor": "M-paper"}}
    assert _reconcile_targets({}, stacks, fb) == [(None, fb, None)]
    conn = _Conn([_row("X", "tx", "angel_one")])
    runs = await _reconcile_live_brokers(
        {}, stacks, fb, _Pool(conn), "repo", None, call_timeout=5.0
    )
    assert conn.calls == [(_OLD_SQL, ())]
    assert calls[0]["monitor"] is None and calls[0]["broker"] is fb
    assert [g.symbol for g in runs[0]["result"].gaps] == ["X"]


def test_primary_is_the_legacy_brokers_stack_first() -> None:
    from src.orchestrator import _reconcile_targets

    a, k = _broker(), _broker()
    stacks = {"live:angel_one": {"broker": a, "order_monitor": 1}, "live:kite": {"broker": k}}
    assert [t[0] for t in _reconcile_targets({"legacy_live_broker": "kite"}, stacks, None)] == [
        "live:kite",
        "live:angel_one",
    ]


# ── held symbols: union across brokers (R3) ──────────────────────────────────


async def test_held_symbols_are_the_union_over_live_brokers() -> None:
    from src.orchestrator import _held_symbols_live_union, _reconcile_targets

    kite = _broker([_position("INFY")], [_holding("IFCI")])
    angel = _broker([_position("TCS")], [_holding("INFY")])
    shared: dict[str, Any] = {"legacy_live_broker": "kite"}
    targets = _reconcile_targets(shared, _stacks(kite, angel), None)
    union, primary, loaded = await _held_symbols_live_union(shared, targets, call_timeout=5.0)
    assert union == {"INFY", "IFCI", "TCS"} and primary == {"INFY", "IFCI"} and loaded


async def test_secondary_books_failure_keeps_last_known_and_fails_closed_until_loaded() -> None:
    from src.orchestrator import _held_symbols_live_union, _reconcile_targets

    kite = _broker([_position("INFY")])
    shared: dict[str, Any] = {"legacy_live_broker": "kite"}
    down = _reconcile_targets(shared, _stacks(kite, _broker(fail=True)), None)
    union, _p, loaded = await _held_symbols_live_union(shared, down, call_timeout=5.0)
    assert union == {"INFY"} and loaded is False  # angel never loaded: entries stay blocked
    up = _reconcile_targets(shared, _stacks(kite, _broker([_position("TCS")])), None)
    assert (await _held_symbols_live_union(shared, up, call_timeout=5.0))[2] is True
    union, _p, loaded = await _held_symbols_live_union(shared, down, call_timeout=5.0)
    assert union == {"INFY", "TCS"} and loaded  # never shrinks on a later failure
    with pytest.raises(RuntimeError):
        await _held_symbols_live_union(
            shared, _reconcile_targets(shared, _stacks(_broker(fail=True)), None), call_timeout=5.0
        )


async def test_single_broker_holdings_failure_keeps_last_set_as_before() -> None:
    from src.orchestrator import _held_symbols_live_union, _reconcile_targets

    shared: dict[str, Any] = {"legacy_live_broker": "kite"}
    first = _reconcile_targets(shared, _stacks(_broker([], [_holding("IFCI")])), None)
    assert (await _held_symbols_live_union(shared, first, call_timeout=5.0))[0] == {"IFCI"}
    bad = _broker([_position("INFY")])
    bad.get_holdings = AsyncMock(side_effect=RuntimeError("x"))
    union, primary, loaded = await _held_symbols_live_union(
        shared, _reconcile_targets(shared, _stacks(bad), None), call_timeout=5.0
    )
    assert union == primary == {"INFY", "IFCI"} and loaded


# ── reconciliation_log: legacy output identical; other brokers appended ─────


def _gap(kind: str, sym: str, **kw: Any) -> ReconciliationGap:
    return ReconciliationGap(kind=kind, symbol=sym, **kw)


def test_single_broker_log_fields_equal_the_legacy_computation() -> None:
    from src.orchestrator import _reconciliation_log_fields

    res = ReconciliationResult(gaps=[_gap("GHOST_DB_TRADE", "A", db_qty=3, agent_trade_id="t")])
    db_open = {"A": [{}], "B": [{}]}
    run = {"key": "live:kite", "db_open": db_open, "result": res, "auto_closed": 0}
    dn, bn, matched, gaps_json, by_broker = _reconciliation_log_fields([run], {"A", "C"}, {"P"})
    legacy_gaps = json.dumps(
        [
            {"kind": g.kind, "symbol": g.symbol, "broker_qty": g.broker_qty, "db_qty": g.db_qty}
            for g in res.gaps
        ]
    )
    assert (dn, bn, matched, gaps_json) == (2, 3, max(2 - 1, 0), legacy_gaps)
    assert json.loads(by_broker) == {
        "kite": {
            "db_positions": 2,
            "matched": 1,
            "auto_closed": 0,
            "gaps": json.loads(legacy_gaps),
        }
    }


def test_other_brokers_gaps_are_appended_and_tagged() -> None:
    from src.orchestrator import _reconciliation_log_fields

    k = ReconciliationResult(gaps=[_gap("GHOST_DB_TRADE", "A")])
    a = ReconciliationResult(gaps=[_gap("ORPHANED_BROKER_POSITION", "Z", broker_qty=5)])
    runs = [
        {"key": "live:kite", "db_open": {"A": [{}]}, "result": k},
        {"key": "live:angel_one", "db_open": {}, "result": a},
    ]
    dn, _bn, matched, gaps_json, by_broker = _reconciliation_log_fields(runs, set(), set())
    assert (dn, matched) == (1, 0)  # top-level stays the primary's
    gaps = json.loads(gaps_json)
    assert gaps[0] == {"kind": "GHOST_DB_TRADE", "symbol": "A", "broker_qty": 0, "db_qty": 0}
    assert gaps[1]["broker"] == "angel_one" and gaps[1]["symbol"] == "Z"
    assert set(json.loads(by_broker)) == {"kite", "angel_one"}


# ── ghost alerts: one broker never resolves another's ────────────────────────


async def test_ghost_alert_of_another_broker_is_not_resolved() -> None:
    from src.orchestrator import _auto_close_ghost_trades

    resolved: list[str] = []
    alerts = [
        SimpleNamespace(id="a1", source="ghost:ta"),
        SimpleNamespace(id="a2", source="ghost:old"),
    ]
    am = SimpleNamespace(active=lambda: alerts, resolve=resolved.append)
    res = ReconciliationResult(gaps=[])
    await _auto_close_ghost_trades(
        res, None, None, None, alert_manager=am, current_ghost_ids={"ta"}
    )
    assert resolved == ["a2"]
    resolved.clear()
    await _auto_close_ghost_trades(
        res, None, None, None, alert_manager=am, current_ghost_ids={"ta"}, resolve_stale=False
    )
    assert resolved == []


# ── migrations 090 / 091 (static) ────────────────────────────────────────────

_MIG = Path(__file__).parents[3] / "migrations"


def test_migration_090_is_additive_and_ordered() -> None:
    sql = (_MIG / "090_external_positions_exec_broker_key.sql").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    create = body.index("CREATE UNIQUE INDEX IF NOT EXISTS uq_external_positions_open_broker")
    drop = body.index("DROP INDEX IF EXISTS uq_external_positions_open;")
    assert create < drop  # never a window without a uniqueness guard
    assert "(symbol, product, (COALESCE(exec_broker, 'kite')))" in body
    assert "WHERE status = 'OPEN'" in body
    for bad in ("DELETE", "UPDATE", "DROP TABLE", "DROP COLUMN", "TRUNCATE"):
        assert bad not in body.upper(), bad


def test_migration_091_is_additive() -> None:
    sql = (_MIG / "091_reconciliation_log_by_broker.sql").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--")).strip()
    assert body == "ALTER TABLE reconciliation_log ADD COLUMN IF NOT EXISTS by_broker JSONB;"


# ── external_positions per broker (static SQL + scratch Postgres) ────────────


async def test_external_positions_sync_scopes_every_statement_to_the_broker() -> None:
    from src.db.repositories.external_positions import ExternalPositionRepository

    seen: list[tuple[str, tuple[Any, ...]]] = []

    class _C:
        async def fetch(self, sql: str, *a: Any) -> list[Any]:
            seen.append((sql, a))
            return [{"symbol": "OLD", "product": "INTRADAY", "acknowledged": False}]

        async def execute(self, sql: str, *a: Any) -> str:
            seen.append((sql, a))
            return "OK"

        def transaction(self) -> Any:
            class _T:
                async def __aenter__(self) -> None:
                    return None

                async def __aexit__(self, *a: Any) -> bool:
                    return False

            return _T()

    await ExternalPositionRepository(_Pool(_C())).sync(  # type: ignore[arg-type]
        [("NEW", "INTRADAY", 5, 10.0)], exec_broker="angel_one"
    )
    assert len(seen) == 3  # select, insert NEW, close OLD
    for _sql, args in seen:
        assert args[-1] == "angel_one"
    assert "COALESCE(exec_broker, 'kite') = $1" in seen[0][0]
    assert "exec_broker) VALUES ($1, $2, $3, $4, $5)" in seen[1][0]
    assert "COALESCE(exec_broker, 'kite') = $3" in seen[2][0]


_DB = os.environ.get("DB_URL")


@pytest.mark.skipif(not _DB or "scratch" not in (_DB or ""), reason="scratch DB_URL not set")
async def test_per_broker_reconcile_and_orphans_on_postgres(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncpg
    from src.orchestrator import _reconcile_live_brokers

    _record_ghost_closes(monkeypatch)
    conn = await asyncpg.connect(_DB)
    tx = conn.transaction()
    await tx.start()
    try:
        if not await conn.fetchval(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'uq_external_positions_open_broker'"
        ):
            pytest.skip("migration 090 not applied on this DB")
        await conn.execute("UPDATE trades SET order_state='CLOSED' WHERE order_state='FILLED'")
        await conn.execute("UPDATE external_positions SET status='CLOSED' WHERE status='OPEN'")
        for tid, sym, eb in (("T9-K", "KGHOST", None), ("T9-A", "AGHOST", "angel_one")):
            await conn.execute(
                "INSERT INTO trades (agent_trade_id, symbol, direction, quantity, order_state, "
                "product, fill_price, exec_broker) VALUES ($1, $2, 'long', 10, 'FILLED', 'MIS', "
                "100, $3)",
                tid,
                sym,
                eb,
            )
        # the SAME symbol is an orphan on both brokers: two OPEN rows (needs migration 090)
        kite = _broker([_position("BOTH", product="INTRADAY")])
        angel = _broker([_position("BOTH", product="INTRADAY")])
        shared: dict[str, Any] = {"legacy_live_broker": "kite"}
        runs = await _reconcile_live_brokers(
            shared, _stacks(kite, angel), None, _Pool(conn), "repo", None, call_timeout=5.0
        )
        got = {r["key"]: {(g.kind, g.symbol) for g in r["result"].gaps} for r in runs}
        assert got["live:kite"] == {
            ("GHOST_DB_TRADE", "KGHOST"),
            ("ORPHANED_BROKER_POSITION", "BOTH"),
        }
        assert got["live:angel_one"] == {
            ("GHOST_DB_TRADE", "AGHOST"),
            ("ORPHANED_BROKER_POSITION", "BOTH"),
        }
        open_rows = await conn.fetch(
            "SELECT symbol, exec_broker FROM external_positions WHERE status='OPEN'"
        )
        assert sorted((r["symbol"], r["exec_broker"]) for r in open_rows) == [
            ("BOTH", "angel_one"),
            ("BOTH", "kite"),
        ]
        # kite's orphan disappears: only kite's row closes, angel's stays OPEN
        kite.get_positions = AsyncMock(return_value=[])
        await _reconcile_live_brokers(
            shared, _stacks(kite, angel), None, _Pool(conn), "repo", None, call_timeout=5.0
        )
        open_rows = await conn.fetch(
            "SELECT symbol, exec_broker FROM external_positions WHERE status='OPEN'"
        )
        assert [(r["symbol"], r["exec_broker"]) for r in open_rows] == [("BOTH", "angel_one")]
    finally:
        await tx.rollback()
        await conn.close()


# ── I3: a broker whose books never load blocks only ITS live entries, and alerts ──


async def test_never_loaded_broker_is_recorded_and_alerted_once_per_hour(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.orchestrator import _held_symbols_live_union, _reconcile_targets

    alerts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.infrastructure.operator_alerts.raise_alert",
        lambda msg, **kw: alerts.append({"msg": msg, **kw}) or True,
    )
    shared: dict[str, Any] = {"legacy_live_broker": "kite"}
    down = _reconcile_targets(
        shared, _stacks(_broker([_position("INFY")]), _broker(fail=True)), None
    )
    await _held_symbols_live_union(shared, down, call_timeout=5.0)
    assert shared["held_symbols_unloaded_keys"] == {"live:angel_one"}
    assert len(alerts) == 1 and alerts[0]["tier"] == "CRITICAL"
    assert alerts[0]["source"] == "held_symbols_not_loaded:live:angel_one"
    assert alerts[0]["dedupe_seconds"] == 3600 and "angel_one" in alerts[0]["msg"]
    up = _reconcile_targets(shared, _stacks(_broker(), _broker([_position("TCS")])), None)
    await _held_symbols_live_union(shared, up, call_timeout=5.0)
    assert shared["held_symbols_unloaded_keys"] == set()


def test_unloaded_broker_blocks_only_its_own_live_entries() -> None:
    from src.orchestrator import _held_symbols_unknown_for

    stacks = _stacks(_broker(), _broker())
    shared: dict[str, Any] = {
        "stacks": stacks,
        "held_symbols_loaded": True,
        "held_symbols_unloaded_keys": {"live:angel_one"},
    }
    assert _held_symbols_unknown_for(shared, True, stacks["paper"]) is False  # paper trades
    assert _held_symbols_unknown_for(shared, False, stacks["live:kite"]) is False
    assert _held_symbols_unknown_for(shared, False, stacks["live:angel_one"]) is True
    # Before the first reconciliation everything stays fail-closed, as before.
    shared["held_symbols_loaded"] = False
    assert _held_symbols_unknown_for(shared, True, stacks["paper"]) is True
    assert _held_symbols_unknown_for(shared, False, stacks["live:kite"]) is True


def test_unloaded_brokers_engine_positions_still_guard_other_live_brokers() -> None:
    from types import SimpleNamespace

    from src.orchestrator import _unloaded_brokers_tracked_symbols

    mon = SimpleNamespace(get_monitored=lambda: [SimpleNamespace(symbol="NSE:TCS")])
    shared: dict[str, Any] = {
        "stacks": {"live:angel_one": {"order_monitor": mon}, "live:kite": {"order_monitor": None}},
        "held_symbols_unloaded_keys": {"live:angel_one"},
    }
    assert _unloaded_brokers_tracked_symbols(shared) == {"TCS"}  # R3 across brokers
    shared["held_symbols_unloaded_keys"] = set()
    assert _unloaded_brokers_tracked_symbols(shared) == set()


async def test_intraday_paper_entry_not_blocked_by_an_unloaded_live_broker() -> None:
    from datetime import datetime
    from types import SimpleNamespace

    from src.orchestrator import _execute_intraday_signal

    mon = SimpleNamespace(get_monitored=lambda: [SimpleNamespace(symbol="NSE:BPCL")])
    stacks = {
        "paper": {"broker": _broker(), "order_monitor": mon, "lifecycle": None},
        "live:angel_one": {"broker": _broker(), "order_monitor": None, "lifecycle": None},
    }
    shared: dict[str, Any] = {
        "paper_mode": True,
        "stacks": stacks,
        "held_symbols_loaded": True,
        "held_symbols_unloaded_keys": {"live:angel_one"},
    }
    sig = SimpleNamespace(
        entry_price=100.0, sl=98.0, target=104.0, direction=SimpleNamespace(value="LONG")
    )
    # Reaches the per-symbol held check (BPCL is open in paper) instead of failing closed
    # on another broker's unknown books.
    now = datetime(2026, 6, 30, 9, 31)
    assert await _execute_intraday_signal(shared, "orb_x", "BPCL", sig, now) is None
    assert shared["_intraday_refusals"]["BPCL"][0] == "already_held_or_open_order"
