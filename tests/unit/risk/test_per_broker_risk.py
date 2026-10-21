"""Phase 2 task 8: optional per-broker equity, rupee caps and entry blocks
(config/global.json system.per_broker_equity, default false).

Flag off: nothing changes (mode labels stay "paper"/"live", the CRM context is used as is).
Flag on: a routed live signal is risk-gated on ITS broker's own equity, baseline, caps,
counters and breaker; a broker without a fresh equity figure takes no new entries."""

from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from src.brokers.routing import BrokerRoute
from src.risk import budget as budget_module
from src.risk.crm import (
    CentralRiskManager,
    CRMContext,
    broker_view,
    intraday_risk_cap,
    mode_of,
    per_key_rupee_caps,
)

from tests.unit.risk.test_crm import _ctx, _pin_clock_in_trading_window, _sig  # noqa: F401

_IST = ZoneInfo("Asia/Kolkata")
_PINNED_DAY = "2026-01-15"  # test_crm's autouse clock fixture
crm = CentralRiskManager()
_EQ = {"live:kite": 1_000_000.0, "live:angel_one": 200_000.0}


@pytest.fixture(autouse=True)
def _reset_budget_equity():
    yield
    budget_module._current_equity = None
    budget_module._equity_by_key.clear()


def _live_ctx(**kw: Any) -> CRMContext:
    ctx = _ctx(
        segment_paper_mode={"intraday": False, "swing": False, "options": False},
        total_capital=2_000_000.0,
        current_equity=2_000_000.0,
        **kw,
    )
    ctx.strategy_routes = {"ANG": BrokerRoute("angel_one", None), "KIT": BrokerRoute("kite", None)}
    return ctx


def _on(ctx: CRMContext, equity: dict[str, float] | None = None, baseline=None) -> CRMContext:  # type: ignore[no-untyped-def]
    equity = dict(_EQ if equity is None else equity)
    ctx.per_broker_equity = True
    ctx.legacy_live_broker = "kite"
    ctx.mode_current_equity = equity
    ctx.mode_total_capital = dict(baseline or equity)
    ctx.mode_rupee_caps = {
        k: per_key_rupee_caps(budget_module.get_budget(), v) for k, v in equity.items()
    }
    return ctx


def _intraday(name: str, qty: int = 50) -> Any:
    return _sig(
        strategy_name=name,
        engine="intraday",
        entry_price=1000.0,
        intended_quantity=qty,
        risk_rupees=100.0,  # within both brokers' intraday shared-risk ceilings (G7)
    )


# ── flag off: unchanged ──────────────────────────────────────────────────────


def test_flag_off_mode_labels_and_view_are_unchanged() -> None:
    ctx = _live_ctx()
    assert mode_of(_intraday("ANG"), ctx) == "live"
    assert mode_of(_intraday("OTHER"), ctx) == "live"
    assert broker_view(ctx, "live") is ctx
    assert broker_view(ctx, "live:angel_one") is ctx  # flag off: never a per-broker view


def test_flag_off_decisions_ignore_any_per_broker_fields() -> None:
    plain = _live_ctx()
    noisy = _live_ctx()
    _on(noisy, equity={"live:angel_one": 1.0})
    noisy.per_broker_equity = False
    noisy.entries_blocked_brokers = {"angel_one": _PINNED_DAY}
    noisy.min_viable_equity_rupees = 1e12
    for name in ("ANG", "KIT", "OTHER"):
        a, b = crm.evaluate(_intraday(name), plain), crm.evaluate(_intraday(name), noisy)
        assert (a.approved, a.final_quantity, a.blocking_gate) == (
            b.approved,
            b.final_quantity,
            b.blocking_gate,
        )


def test_refresh_without_flag_adds_nothing() -> None:
    from src.orchestrator import _refresh_options_risk_budget

    budget_module.set_current_equity(2_000_000.0)
    ctx = SimpleNamespace()
    _refresh_options_risk_budget(ctx)
    assert not hasattr(ctx, "mode_rupee_caps")


# ── flag on: labels, caps, floor, staircase ──────────────────────────────────


def test_flag_on_mode_of_names_the_signals_broker() -> None:
    ctx = _on(_live_ctx())
    assert mode_of(_intraday("ANG"), ctx) == "live:angel_one"
    assert mode_of(_intraday("KIT"), ctx) == "live:kite"
    assert mode_of(_intraday("OTHER"), ctx) == "live:kite"  # unrouted: the legacy broker
    ctx.strategy_execution_mode = {"ANG": "paper"}
    assert mode_of(_intraday("ANG"), ctx) == "paper"
    ctx.strategy_routes["BAD"] = BrokerRoute(None, "no broker")
    assert mode_of(_intraday("BAD"), ctx) == "live:"  # unusable route: matches no equity


def test_two_brokers_get_caps_from_their_own_equity() -> None:
    ctx = _on(_live_ctx())
    assert intraday_risk_cap(broker_view(ctx, "live:kite")) == pytest.approx(500.0)
    assert intraday_risk_cap(broker_view(ctx, "live:angel_one")) == pytest.approx(100.0)
    # G8 sizes each broker's trade on its own cap: risk/share = 20 -> 25 vs 5 shares
    k, a = crm.evaluate(_intraday("KIT"), ctx), crm.evaluate(_intraday("ANG"), ctx)
    assert k.approved and k.final_quantity == 25
    assert a.approved and a.final_quantity == 5
    assert ctx.current_equity == 2_000_000.0  # the shared context itself is never mutated


def test_refresh_with_flag_computes_caps_per_key() -> None:
    from src.orchestrator import _refresh_options_risk_budget

    budget_module.set_current_equity(2_000_000.0)
    ctx = _on(_live_ctx())
    ctx.mode_rupee_caps = {}
    _refresh_options_risk_budget(ctx)
    assert ctx.max_intraday_loss_per_trade == pytest.approx(1_000.0)  # global, unchanged
    caps = ctx.mode_rupee_caps
    assert caps["live:kite"]["max_intraday_loss_per_trade"] == pytest.approx(500.0)
    assert caps["live:angel_one"]["max_intraday_loss_per_trade"] == pytest.approx(100.0)
    assert caps["live:angel_one"]["max_intraday_daily_loss"] == pytest.approx(300.0)
    assert caps["live:angel_one"]["max_options_daily_loss"] == pytest.approx(1_000.0)
    assert ctx.mode_drawdown_pct == {"live:kite": 0.0, "live:angel_one": 0.0}


def test_floor_uses_the_brokers_own_baseline_not_operator_capital() -> None:
    # Rs 2L account vs operator total_capital Rs 20L: 10% of operator capital, but 100% of
    # its own baseline -- the 25% floor must not block a small second account.
    ctx = _on(_live_ctx())
    assert crm.evaluate(_intraday("ANG"), ctx).approved
    ctx.mode_current_equity["live:angel_one"] = 40_000.0  # 20% of its own 2L baseline
    d = crm.evaluate(_intraday("ANG"), ctx)
    assert d.blocking_gate == "G1" and "floor" in (d.blocking_reason or "")
    assert crm.evaluate(_intraday("KIT"), ctx).approved


def test_staircase_uses_the_brokers_own_drawdown() -> None:
    ctx = _on(_live_ctx(), baseline={"live:kite": 1_000_000.0, "live:angel_one": 200_000.0})
    ctx.mode_current_equity["live:angel_one"] = 180_000.0  # -10% from its own baseline
    d = crm.evaluate(_intraday("ANG"), ctx)
    assert d.blocking_gate == "G2" and "Drawdown" in (d.blocking_reason or "")
    assert crm.evaluate(_intraday("KIT"), ctx).approved


# ── flag on: entry blocks ────────────────────────────────────────────────────


def test_breaker_block_stops_only_that_broker_until_the_next_day() -> None:
    ctx = _on(_live_ctx())
    ctx.entries_blocked_brokers = {"angel_one": _PINNED_DAY}
    d = crm.evaluate(_intraday("ANG"), ctx)
    assert d.blocking_gate == "G1" and "blocked" in (d.blocking_reason or "")
    assert crm.evaluate(_intraday("KIT"), ctx).approved
    ctx.entries_blocked_brokers = {"angel_one": "2026-01-14"}  # yesterday's block expired
    assert crm.evaluate(_intraday("ANG"), ctx).approved


def test_missing_equity_blocks_only_that_broker() -> None:
    ctx = _on(_live_ctx(), equity={"live:kite": 1_000_000.0})
    d = crm.evaluate(_intraday("ANG"), ctx)
    assert d.blocking_gate == "G1" and "equity unavailable" in (d.blocking_reason or "")
    assert crm.evaluate(_intraday("KIT"), ctx).approved
    ctx2 = _on(_live_ctx())
    ctx2.strategy_routes["BAD"] = BrokerRoute(None, "no broker")
    bad = crm.evaluate(_intraday("BAD"), ctx2)  # no usable route: blocked too
    assert bad.blocking_gate == "G1"


def test_min_viable_equity_blocks_an_undersized_account() -> None:
    ctx = _on(_live_ctx())
    ctx.min_viable_equity_rupees = 300_000.0
    d = crm.evaluate(_intraday("ANG"), ctx)
    assert d.blocking_gate == "G1" and "minimum viable" in (d.blocking_reason or "")
    assert crm.evaluate(_intraday("KIT"), ctx).approved
    ctx.min_viable_equity_rupees = 0.0  # 0 = disabled
    assert crm.evaluate(_intraday("ANG"), ctx).approved


def test_paper_signal_is_never_affected_by_the_flag() -> None:
    ctx = _on(_live_ctx(), equity={})
    ctx.strategy_execution_mode = {"ANG": "paper"}
    ctx.entries_blocked_brokers = {"angel_one": _PINNED_DAY}
    assert crm.evaluate(_intraday("ANG"), ctx).approved


def test_absent_per_broker_counters_fall_back_to_the_live_bucket() -> None:
    ctx = _on(_live_ctx(max_open_positions=1))
    ctx.mode_open_position_count = {"live": 1}  # combined live book is full
    assert crm.evaluate(_intraday("ANG"), ctx).blocking_gate == "G7"
    ctx.mode_open_position_count = {"live": 1, "live:angel_one": 0}  # own count wins
    assert crm.evaluate(_intraday("ANG"), ctx).approved


# ── budget.get_current_equity(key) ───────────────────────────────────────────


def test_budget_equity_per_key_and_global_unchanged() -> None:
    fallback = budget_module.get_current_equity()
    budget_module.set_current_equity(500_000.0, key="live:angel_one")
    assert budget_module.get_current_equity("live:angel_one") == 500_000.0
    assert budget_module.get_current_equity() == fallback  # None key: untouched
    assert budget_module.get_current_equity("live:flattrade") == 0.0  # unknown: no fallback
    budget_module.clear_current_equity("live:angel_one")
    assert budget_module.get_current_equity("live:angel_one") == 0.0


def test_min_viable_equity_config_defaults_to_disabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json

    raw = json.loads(budget_module._PATH.read_text(encoding="utf-8"))
    assert budget_module.load_risk_budget().account.min_viable_equity_rupees == 0.0
    raw["account"]["min_viable_equity_rupees"] = "₹50,000"
    p = tmp_path / "capital.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert budget_module.load_risk_budget(p).account.min_viable_equity_rupees == 50_000.0
    raw["account"].pop("min_viable_equity_rupees")
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert budget_module.load_risk_budget(p).account.min_viable_equity_rupees == 0.0


def test_flag_is_false_in_working_and_example_config() -> None:
    import json
    from pathlib import Path

    cfg = Path(budget_module._PATH).parent
    for p in (cfg / "global.json", cfg / "examples" / "global.json"):
        g = json.loads(p.read_text(encoding="utf-8"))
        assert g["system"]["per_broker_equity"] is False
        assert "system.per_broker_equity" in g["_options"]


# ── orchestrator: per-stack equity fetch ─────────────────────────────────────


class _Broker:
    def __init__(self, *equities: float, fail: bool = False) -> None:
        self._eq = list(equities)
        self._fail = fail

    async def get_margins(self) -> Any:
        if self._fail:
            raise RuntimeError("broker down")
        e = self._eq.pop(0)
        return SimpleNamespace(equity=e, available_cash=e / 2, used_margin=0.0)


def _alerts(monkeypatch) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    out: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.infrastructure.operator_alerts.raise_alert",
        lambda msg, **kw: out.append({"msg": msg, **kw}) or True,
    )
    return out


async def test_fetch_sets_equity_cash_and_a_first_fetch_baseline(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.orchestrator import _broker_funds, _fetch_live_stack_equity

    _alerts(monkeypatch)
    shared: dict[str, Any] = {}
    b = _Broker(200_000.0, 150_000.0)
    assert await _fetch_live_stack_equity(shared, "live:angel_one", b)
    assert await _fetch_live_stack_equity(shared, "live:angel_one", b)
    assert shared["equity_by_key"] == {"live:angel_one": 150_000.0}
    assert shared["equity_baseline_by_key"] == {"live:angel_one": 200_000.0}  # first fetch
    assert _broker_funds(shared, "live:angel_one") == (150_000.0, 75_000.0)
    assert budget_module.get_current_equity("live:angel_one") == 150_000.0
    assert budget_module.get_current_equity() == budget_module.get_current_equity(None)


async def test_failed_fetch_blocks_only_that_broker_and_alerts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.orchestrator import _broker_funds, _fetch_live_stack_equity

    alerts = _alerts(monkeypatch)
    shared: dict[str, Any] = {}
    await _fetch_live_stack_equity(shared, "live:kite", _Broker(1_000_000.0))
    await _fetch_live_stack_equity(shared, "live:angel_one", _Broker(200_000.0))
    assert not await _fetch_live_stack_equity(shared, "live:angel_one", _Broker(fail=True))
    assert "live:angel_one" not in shared["equity_by_key"]
    assert _broker_funds(shared, "live:angel_one") is None
    assert _broker_funds(shared, "live:kite") == (1_000_000.0, 500_000.0)
    assert len(alerts) == 1 and alerts[0]["tier"] == "CRITICAL"
    assert "angel_one" in alerts[0]["msg"]
    # the CRM context aliases the same dicts at boot: angel is now blocked, kite is not
    ctx = _on(_live_ctx())
    ctx.mode_current_equity = shared["equity_by_key"]
    assert crm.evaluate(_intraday("ANG"), ctx).blocking_gate == "G1"
    assert crm.evaluate(_intraday("KIT"), ctx).approved
    # a zero/negative equity is a failure too (never a baseline of 0)
    assert not await _fetch_live_stack_equity(shared, "live:flattrade", _Broker(0.0))
    assert "live:flattrade" not in shared.get("equity_baseline_by_key", {})


# ── orchestrator: live daily-loss breaker blocks its broker ──────────────────


async def test_live_breaker_alerts_and_blocks_only_its_broker(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.orchestrator import _enforce_intraday_daily_loss_breaker

    alerts = _alerts(monkeypatch)
    ctx = _on(_live_ctx())
    ctx.mode_intraday_daily_loss_so_far = {"live:angel_one": 400.0, "live:kite": 400.0}
    shared: dict[str, Any] = {"per_broker_equity": True, "entries_blocked_brokers": {}}
    pe = SimpleNamespace(square_off_open=AsyncMock(return_value=0))
    await _enforce_intraday_daily_loss_breaker(shared, ctx, "live:angel_one", pe)  # cap 300
    await _enforce_intraday_daily_loss_breaker(shared, ctx, "live:kite", pe)  # cap 1500
    today = datetime.now(_IST).date().isoformat()
    assert shared["entries_blocked_brokers"] == {"angel_one": today}
    pe.square_off_open.assert_not_awaited()  # never auto-flattens real positions
    assert len(alerts) == 1 and alerts[0]["tier"] == "CRITICAL"
    assert "angel_one" in alerts[0]["msg"]


async def test_flag_off_live_breaker_unchanged(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.orchestrator import _enforce_intraday_daily_loss_breaker

    alerts = _alerts(monkeypatch)
    ctx = _live_ctx()
    ctx.max_intraday_daily_loss = 100.0
    ctx.mode_intraday_daily_loss_so_far = {"live": 400.0}
    shared: dict[str, Any] = {}
    pe = SimpleNamespace(square_off_open=AsyncMock(return_value=0))
    await _enforce_intraday_daily_loss_breaker(shared, ctx, "live", pe)
    assert "entries_blocked_brokers" not in shared
    pe.square_off_open.assert_not_awaited()
    assert len(alerts) == 1 and alerts[0]["source"] == "intraday_daily_loss_live"


# ── orchestrator: per-broker counters ────────────────────────────────────────


class _PE:
    """daily_risk_counters double: per (table, product, exec_broker)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def daily_risk_counters(self, table="paper_trades", *, product=None, exec_broker=None):  # type: ignore[no-untyped-def]
        self.calls.append((table, product, exec_broker))
        n = {None: 3, "kite": 1, "angel_one": 2}[exec_broker] if table == "trades" else 7
        return {
            "new_trades_today": n,
            "open_position_count": n,
            "daily_loss_so_far": 10.0 * n,
            "consecutive_losses": n,
            "per_strategy": {},
            "open_per_strategy": {},
        }


def _stack() -> dict[str, Any]:
    return {"order_monitor": SimpleNamespace(get_monitored=lambda: [])}


async def test_refresh_daily_counters_adds_per_broker_keys_only_with_the_flag(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.orchestrator import _refresh_daily_counters

    _alerts(monkeypatch)
    stacks = {"paper": _stack(), "live:kite": _stack(), "live:angel_one": _stack()}
    for flag in (False, True):
        pe = _PE()
        ctx = _live_ctx()
        if flag:
            _on(ctx)
        shared: dict[str, Any] = {"stacks": stacks, "paper_ledger": pe, "per_broker_equity": flag}
        await _refresh_daily_counters(shared, ctx)
        assert ctx.mode_new_trades_today["live"] == 3 and ctx.mode_new_trades_today["paper"] == 7
        if not flag:
            assert all(c[2] is None for c in pe.calls)
            assert "live:angel_one" not in ctx.mode_new_trades_today
        else:
            assert ctx.mode_new_trades_today["live:angel_one"] == 2
            assert ctx.mode_new_trades_today["live:kite"] == 1
            assert ctx.mode_intraday_daily_loss_so_far["live:angel_one"] == 20.0
            assert ctx.mode_engine_open_positions["live:angel_one:intraday"] == 0


async def test_daily_risk_counters_filters_by_exec_broker() -> None:
    from src.paper_trading.paper_engine import PaperTradingEngine

    seen: list[tuple[str, tuple[Any, ...]]] = []

    class _Conn:
        async def fetchrow(self, sql, *args):  # type: ignore[no-untyped-def]
            seen.append((sql, args))
            return {"placed": 0, "net_pnl": 0}

        async def fetch(self, sql, *args):  # type: ignore[no-untyped-def]
            seen.append((sql, args))
            return []

    class _Pool:
        def acquire(self) -> Any:
            class _C:
                async def __aenter__(self) -> Any:
                    return _Conn()

                async def __aexit__(self, *a: Any) -> bool:
                    return False

            return _C()

    pe = PaperTradingEngine(SimpleNamespace(), _Pool())
    await pe.daily_risk_counters("trades", product="MIS", exec_broker="angel_one")
    assert len(seen) == 4
    for sql, args in seen:
        assert "COALESCE(exec_broker, 'kite') = $1" in sql and args == ("angel_one",)
    seen.clear()
    await pe.daily_risk_counters("trades")
    assert all("exec_broker" not in sql and args == () for sql, args in seen)
    with pytest.raises(ValueError):
        await pe.daily_risk_counters("paper_trades", exec_broker="kite")


_DB = os.environ.get("DB_URL")


@pytest.mark.skipif(not _DB or "scratch" not in (_DB or ""), reason="scratch DB_URL not set")
async def test_daily_risk_counters_exec_broker_sql_runs_on_postgres() -> None:
    import asyncpg
    from src.paper_trading.paper_engine import PaperTradingEngine

    pool = await asyncpg.create_pool(_DB, min_size=1, max_size=1)
    try:
        pe = PaperTradingEngine(SimpleNamespace(), pool)
        c = await pe.daily_risk_counters("trades", product="MIS", exec_broker="angel_one")
        assert c["new_trades_today"] >= 0
    finally:
        await pool.close()


# ── orchestrator: routed live sizing inputs ──────────────────────────────────


def test_broker_funds_fail_closed_when_missing() -> None:
    from src.orchestrator import _broker_funds

    shared: dict[str, Any] = {"equity_by_key": {"live:kite": 5.0}, "cash_by_key": {}}
    assert _broker_funds(shared, "live:kite") is None  # no cash figure: unknown
    assert _broker_funds({}, "live:kite") is None


def test_live_key_follows_the_route_or_the_legacy_broker() -> None:
    from src.orchestrator import _live_key_for

    shared: dict[str, Any] = {
        "strategy_routes": {"ANG": BrokerRoute("angel_one", None)},
        "legacy_live_broker": "kite",
    }
    assert _live_key_for(shared, "ANG") == "live:angel_one"
    assert _live_key_for(shared, "OTHER") == "live:kite"


# ── I1: the per-broker drawdown baseline survives a restart (migration 092) ──


class _BaselinePool:
    """Stands in for broker_equity_baseline: INSERT ... ON CONFLICT DO NOTHING, then read."""

    def __init__(self, fail: bool = False) -> None:
        self.rows: dict[str, float] = {}
        self.fail = fail

    async def fetchval(self, sql: str, key: str, equity: float) -> float:
        if self.fail:
            raise RuntimeError("db down")
        assert "broker_equity_baseline" in sql
        return self.rows.setdefault(key, equity)


async def test_baseline_is_persisted_and_a_restart_does_not_reset_it(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.orchestrator import _fetch_live_stack_equity

    _alerts(monkeypatch)
    pool = _BaselinePool()
    first: dict[str, Any] = {"db_pool": pool}
    assert await _fetch_live_stack_equity(first, "live:angel_one", _Broker(200_000.0))
    assert pool.rows == {"live:angel_one": 200_000.0}
    # "Restart": a new process (fresh shared state) after the broker lost 9%.
    restarted: dict[str, Any] = {"db_pool": pool}
    assert await _fetch_live_stack_equity(restarted, "live:angel_one", _Broker(182_000.0))
    assert restarted["equity_baseline_by_key"] == {"live:angel_one": 200_000.0}
    ctx = _on(_live_ctx())
    ctx.mode_current_equity = restarted["equity_by_key"]
    ctx.mode_total_capital = restarted["equity_baseline_by_key"]
    assert broker_view(ctx, "live:angel_one").drawdown_pct == pytest.approx(9.0)


async def test_baseline_db_failure_blocks_that_broker_and_retries(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.orchestrator import _fetch_live_stack_equity

    alerts = _alerts(monkeypatch)
    pool = _BaselinePool(fail=True)
    shared: dict[str, Any] = {"db_pool": pool}
    assert await _fetch_live_stack_equity(shared, "live:angel_one", _Broker(182_000.0))
    assert "live:angel_one" not in shared["equity_baseline_by_key"]  # never the current equity
    assert any("baseline" in a["msg"] and a["tier"] == "CRITICAL" for a in alerts)
    ctx = _on(_live_ctx())
    ctx.mode_current_equity = shared["equity_by_key"]
    ctx.mode_total_capital = shared["equity_baseline_by_key"]
    assert crm.evaluate(_intraday("ANG"), ctx).blocking_gate == "G1"
    pool.rows["live:angel_one"] = 200_000.0
    pool.fail = False
    assert await _fetch_live_stack_equity(shared, "live:angel_one", _Broker(182_000.0))
    assert shared["equity_baseline_by_key"] == {"live:angel_one": 200_000.0}


@pytest.mark.skipif(not _DB or "scratch" not in (_DB or ""), reason="scratch DB_URL not set")
async def test_baseline_sql_inserts_once_then_reads_back_on_postgres() -> None:
    import asyncpg
    from src.orchestrator import _persisted_equity_baseline

    pool = await asyncpg.create_pool(_DB, min_size=1, max_size=1)
    key = "live:test_i1_baseline"
    try:
        await pool.execute("DELETE FROM broker_equity_baseline WHERE stack_key = $1", key)
        assert await _persisted_equity_baseline(pool, key, 200_000.0) == 200_000.0
        assert await _persisted_equity_baseline(pool, key, 182_000.0) == 200_000.0
        n = await pool.fetchval(
            "SELECT count(*) FROM broker_equity_baseline WHERE stack_key = $1", key
        )
        assert n == 1
    finally:
        await pool.execute("DELETE FROM broker_equity_baseline WHERE stack_key = $1", key)
        await pool.close()


# ── I4: a live broker with nothing open has 0 open risk, not the combined bucket ──


@pytest.mark.parametrize("flag", [True, False])
async def test_broker_without_positions_has_zero_open_risk(flag: bool) -> None:
    from src.orchestrator import _refresh_daily_counters

    pos = SimpleNamespace(
        symbol="NSE:INFY",
        quantity=100,
        fill_price=1000.0,
        current_sl=600.0,  # Rs 40,000 at risk on kite
        direction="BUY",
        product="MIS",
        sector="IT",
    )

    def _mon(ps: list[Any]) -> Any:
        return SimpleNamespace(get_monitored=lambda: ps)

    shared: dict[str, Any] = {
        "per_broker_equity": flag,
        "stacks": {
            "live:kite": {"order_monitor": _mon([pos])},
            "live:angel_one": {"order_monitor": _mon([])},
        },
    }
    ctx = _on(_live_ctx()) if flag else _live_ctx()
    await _refresh_daily_counters(shared, ctx)
    mor = ctx.mode_open_risk_rupees
    assert mor["live"] == 40_000.0
    if flag:
        assert mor["live:kite"] == 40_000.0 and mor["live:angel_one"] == 0.0
        view = broker_view(ctx, "live:angel_one")
        assert view.mode_open_risk_rupees["live:angel_one"] == 0.0
        assert view.mode_open_risk_rupees["live:angel_one:intraday"] == 0.0
    else:  # legacy: one combined bucket only
        assert "live:angel_one" not in mor and "live:kite" not in mor
