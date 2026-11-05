"""Every crm.evaluate() decision is journalled in `signals`, and a trade carries its signal's id, so
"what happened to signal X" is answerable (2026-09-28: `signals` had zero writers and trades.signal_id
was NULL on all 473 rows; an approved signal that never became a trade left no trace).

Runs the real migration SQL inside a rolled-back transaction. Skipped when no DB is reachable."""

from __future__ import annotations

import asyncio
import os
import pathlib
from contextlib import asynccontextmanager
from typing import Any

import pytest
from src.risk.crm import CentralRiskManager, CRMContext, CRMSignal
from src.risk.signal_journal import SignalJournal, build_row

_ROOT = pathlib.Path(__file__).parents[3]
_DB = os.environ.get("DB_URL")
if not _DB:
    try:
        from dotenv import dotenv_values

        _DB = dotenv_values(_ROOT / ".env").get("DB_URL")
    except Exception:  # noqa: BLE001
        _DB = None

needs_db = pytest.mark.skipif(not _DB, reason="no DB_URL available")
_MIGRATIONS = [
    ("083_trade_executions_ledger.sql", "SELECT to_regclass('public.trade_executions')"),
    (
        "084_signals_decision_journal.sql",
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'signals' AND column_name = 'signal_id'",
    ),
]


def _ctx(**kw: Any) -> CRMContext:
    base: dict[str, Any] = dict(
        total_capital=200_000.0,
        max_risk_per_trade=1_000.0,
        max_daily_loss=2_000.0,
        max_open_positions=5,
        max_new_trades_per_day=6,
        min_risk_reward_ratio=1.8,
        max_stop_loss_pct=0.08,
        current_equity=200_000.0,
        master_switch_on=False,  # G0 blocks -> a deterministic BLOCKED decision
        session_open_hour=0,
        session_close_hour=24,
    )
    base.update(kw)
    return CRMContext(**base)


def _sig(**kw: Any) -> CRMSignal:
    base: dict[str, Any] = dict(
        signal_id="SWING-TESTSYM-20260928",
        symbol="TESTSYM",
        direction="BUY",
        strategy_name="llm_swing_agent",
        intended_quantity=10,
        stop_loss_pct=0.05,
        target_pct=0.10,
        risk_rupees=500.0,
        entry_price=100.0,
        regime="BULL",
        engine="swing",
        meta={"autonomy_mode": "autonomous", "llm_confidence": 0.8, "llm_reason": "clean"},
    )
    base.update(kw)
    return CRMSignal(**base)


class _ConnAsPool:
    def __init__(self, conn: Any) -> None:
        self._c = conn

    async def fetch(self, *a: Any) -> Any:
        return await self._c.fetch(*a)

    def acquire(self) -> Any:
        conn = self._c

        class _Ctx:
            async def __aenter__(self_inner) -> Any:
                return conn

            async def __aexit__(self_inner, *a: Any) -> bool:
                return False

        return _Ctx()


@asynccontextmanager
async def _tx():
    import asyncpg

    conn = await asyncpg.connect(_DB)
    tx = conn.transaction()
    await tx.start()
    try:
        # Only apply what is missing: re-running DDL takes table locks that could collide with the
        # live service on a database where the migrations are already applied.
        for m, applied_sql in _MIGRATIONS:
            if not await conn.fetchval(applied_sql):
                await conn.execute((_ROOT / "migrations" / m).read_text(encoding="utf-8"))
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


def test_a_blocked_decision_becomes_a_complete_row() -> None:
    sig, ctx = _sig(), _ctx()
    row = build_row(sig, ctx, CentralRiskManager().evaluate(sig, ctx))
    assert row["disposition"] == "BLOCKED" and row["blocking_gate"] == "G0"
    assert row["direction"] == "long"  # the table only accepts long/short, not BUY/SELL
    assert row["segment"] == "swing" and row["signal_id"] == "SWING-TESTSYM-20260928"
    assert row["sl_price"] == 95.0 and row["target_price"] == 110.0  # derived from entry +/- pct
    assert row["gate_results"][0]["gate"] == "G0" and row["llm_confidence"] == 0.8


def test_evaluate_still_returns_its_decision_when_the_sink_explodes() -> None:
    crm = CentralRiskManager()

    def boom(*_a: Any) -> None:
        raise RuntimeError("sink down")

    crm.decision_sink = boom
    d = crm.evaluate(_sig(), _ctx())
    assert d.approved is False and d.blocking_gate == "G0"


def test_no_running_loop_is_logged_not_raised() -> None:
    crm = CentralRiskManager()
    crm.decision_sink = SignalJournal(repo=None)  # called outside an event loop
    assert crm.evaluate(_sig(), _ctx()).approved is False


@needs_db
@pytest.mark.asyncio
async def test_evaluate_writes_a_signals_row_and_the_view_flags_unexecuted_approvals() -> None:
    from src.db.repositories.signals import SignalRepository

    async with _tx() as c:
        repo = SignalRepository(_ConnAsPool(c))
        journal = SignalJournal(repo)
        crm = CentralRiskManager()
        crm.decision_sink = journal
        crm.evaluate(_sig(signal_id="SIG-BLOCKED"), _ctx())
        await asyncio.gather(*journal._tasks)
        out = {r["signal_id"]: r for r in await repo.outcomes()}
        assert out["SIG-BLOCKED"]["outcome"] == "REJECTED_BY_CRM"
        assert out["SIG-BLOCKED"]["blocking_gate"] == "G0"

        # An APPROVED signal with no trade is visible as APPROVED_NOT_EXECUTED ...
        await repo.record(
            {**build_row(_sig(signal_id="SIG-OK"), _ctx(), _approved()), "signal_id": "SIG-OK"}
        )
        out = {r["signal_id"]: r for r in await repo.outcomes()}
        assert out["SIG-OK"]["outcome"] == "APPROVED_NOT_EXECUTED"
        # ... and becomes EXECUTED once a trade row carries its signal_ref (paper or live).
        await c.execute(
            "INSERT INTO paper_trades (agent_trade_id, symbol, exchange, product, direction, quantity, "
            "order_state, intended_price, strategy_name, is_paper, signal_ref) "
            "VALUES ('T-SIG','TESTSYM','NSE','CNC','long',10,'FILLED',100,'llm_swing_agent',true,'SIG-OK')"
        )
        out = {r["signal_id"]: r for r in await repo.outcomes()}
        assert out["SIG-OK"]["outcome"] == "EXECUTED" and out["SIG-OK"]["agent_trade_id"] == "T-SIG"


def _approved() -> Any:
    from src.risk.crm import CRMDecision, GateDecision, GateResult

    return CRMDecision(
        signal_id="SIG-OK",
        approved=True,
        final_quantity=10,
        gate_results=[GateResult("G0", GateDecision.PASS, "")],
    )


def test_the_trade_entry_row_carries_the_signal_id() -> None:
    import inspect

    from src.execution.order_lifecycle import OrderLifecycleManager

    assert "signal_ref" in inspect.signature(OrderLifecycleManager._persist_entry).parameters
    assert "signal_ref=signal_id" in inspect.getsource(OrderLifecycleManager)
