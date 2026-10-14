"""Unit tests for src/supervisor/supervisor.py."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from src.brokers.base import BrokerInterface, Margin, SessionStatus
from src.supervisor.supervisor import (
    InvariantResult,
    InvariantSeverity,
    Supervisor,
    SupervisorState,
    SystemLevel,
)

# Fast intervals — all loops sleep 0.01s so tests don't hang
_FAST = {
    k: 0.01
    for k in (
        "heartbeat",
        "self_watchdog",
        "orchestrator_watchdog",
        "invariants",
        "remediation",
        "proactive_health",
        "ai_brain",
        "housekeeping",
        "token_expiry",
        "retention",
        "floor_monitor",
        "gtts_monitor",
    )
}


def _broker(session_valid: bool = True, equity: float = 200_000.0) -> BrokerInterface:
    b = MagicMock(spec=BrokerInterface)
    b.get_session_status = AsyncMock(
        return_value=SessionStatus(is_valid=session_valid, user_id="AB1234")
    )
    # run_invariants() calls refresh_from_live_sources(), which reads real equity via
    # get_margins() — without this, MagicMock's auto-stubbed __float__ (returns 1.0)
    # silently corrupted current_equity in every run_invariants()-based test to 1.0,
    # tripping a false floor breach regardless of what update_state() had set.
    b.get_margins = AsyncMock(
        return_value=Margin(equity=equity, available_cash=equity, used_margin=0.0)
    )
    return b


def _state(**kw: object) -> SupervisorState:
    base: dict[str, object] = dict(
        current_equity=200_000.0,
        starting_capital=200_000.0,
        daily_loss_so_far=0.0,
        max_daily_loss=2_000.0,
        open_position_count=0,
        max_open_positions=5,
        broker_session_valid=True,
    )
    base.update(kw)
    return SupervisorState(**base)  # type: ignore[arg-type]


def _sup(broker: BrokerInterface | None = None, **kw: object) -> Supervisor:
    return Supervisor(broker=broker or _broker(), state=_state(), intervals=_FAST, **kw)  # type: ignore[arg-type]


# ── Initial state ──────────────────────────────────────────────────────────────


def test_initial_level_is_full_ops() -> None:
    assert _sup().level == SystemLevel.FULL_OPS


def test_initial_incidents_empty() -> None:
    assert _sup().incidents == []


# ── set_level ──────────────────────────────────────────────────────────────────


def test_set_level_changes_level() -> None:
    sup = _sup()
    sup.set_level(SystemLevel.ELEVATED)
    assert sup.level == SystemLevel.ELEVATED


def test_set_level_to_same_is_noop() -> None:
    sup = _sup()
    sup.set_level(SystemLevel.FULL_OPS)
    assert sup.level == SystemLevel.FULL_OPS


def test_set_level_to_emergency() -> None:
    sup = _sup()
    sup.set_level(SystemLevel.EMERGENCY)
    assert sup.level == SystemLevel.EMERGENCY


# ── update_state ───────────────────────────────────────────────────────────────


def test_update_state_replaces_state() -> None:
    sup = _sup()
    sup.update_state(_state(current_equity=150_000.0))
    assert sup._state.current_equity == 150_000.0


# ── run_invariants — happy path ────────────────────────────────────────────────


def _fresh_intraday_heartbeat(tmp_path: Any) -> None:
    """INV_C10 reads logs/intraday_signals.json against real wall-clock time —
    give it a just-written, always-fresh heartbeat so "healthy" tests stay
    deterministic regardless of when/whether they run during market hours."""
    ts = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
    (tmp_path / "intraday_signals.json").write_text(json.dumps({"ts": ts}))


async def test_run_invariants_returns_16_results(tmp_path: Any) -> None:
    sup = _sup()
    sup._logs_dir = tmp_path
    _fresh_intraday_heartbeat(tmp_path)
    results = await sup.run_invariants()
    assert len(results) == 16


async def test_run_invariants_all_ok_when_healthy(tmp_path: Any) -> None:
    sup = _sup()
    sup._logs_dir = tmp_path
    _fresh_intraday_heartbeat(tmp_path)
    results = await sup.run_invariants()
    assert all(r.severity != InvariantSeverity.CRITICAL for r in results)


async def test_run_invariants_all_have_ids(tmp_path: Any) -> None:
    sup = _sup()
    sup._logs_dir = tmp_path
    _fresh_intraday_heartbeat(tmp_path)
    results = await sup.run_invariants()
    ids = {r.invariant_id for r in results}
    assert "INV_C1" in ids
    assert "INV_C2" in ids
    assert "INV_C10" in ids
    assert "INV_LP2" in ids
    assert len(ids) == 16


# ── INV_C10 stale-heartbeat / restart grace ───────────────────────────────────


def _pin_market_hours(monkeypatch: Any) -> datetime:
    """Freeze the supervisor's clock at a Monday 11:00 IST so INV_C10 is in-window."""
    import src.supervisor.supervisor as sv

    fixed = datetime(2026, 9, 21, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return fixed

    monkeypatch.setattr(sv, "datetime", _FrozenDT)
    return fixed


async def test_inv_c10_stale_heartbeat_ok_while_warming_up_after_restart(
    tmp_path: Any, monkeypatch: Any
) -> None:
    fixed = _pin_market_hours(monkeypatch)
    sup = _sup()
    sup._logs_dir = tmp_path
    sup._boot_at = fixed  # process just started
    stale = (fixed.replace(hour=10, minute=30)).isoformat()  # 30 min old
    (tmp_path / "intraday_signals.json").write_text(json.dumps({"ts": stale}))
    r = await sup._inv_c10_intraday_scan_alive()
    assert r.severity == InvariantSeverity.OK
    assert "Warming up" in r.message


async def test_inv_c10_stale_heartbeat_critical_after_grace(
    tmp_path: Any, monkeypatch: Any
) -> None:
    fixed = _pin_market_hours(monkeypatch)
    sup = _sup()
    sup._logs_dir = tmp_path
    sup._boot_at = fixed.replace(hour=10, minute=0)  # up 60 min, well past grace
    stale = (fixed.replace(hour=10, minute=30)).isoformat()
    (tmp_path / "intraday_signals.json").write_text(json.dumps({"ts": stale}))
    r = await sup._inv_c10_intraday_scan_alive()
    assert r.severity == InvariantSeverity.CRITICAL


# ── INV_C2 floor ──────────────────────────────────────────────────────────────


async def test_inv_c2_ok_above_floor() -> None:
    sup = _sup()
    sup.update_state(_state(current_equity=100_000.0))
    r = await sup._inv_c2_floor()
    assert r.severity == InvariantSeverity.OK


async def test_inv_c2_critical_below_floor() -> None:
    sup = _sup()
    sup.update_state(_state(current_equity=40_000.0))
    r = await sup._inv_c2_floor()
    assert r.severity == InvariantSeverity.CRITICAL
    assert "BREACHED" in r.message


async def test_floor_breach_sets_emergency_via_run_invariants(tmp_path: Any) -> None:
    # run_invariants() pulls current_equity from the broker (refresh_from_live_sources),
    # not from update_state() — the scenario is set on the broker mock, not the state.
    sup = _sup(broker=_broker(equity=40_000.0))
    sup._logs_dir = tmp_path  # run_invariants -> _handle_critical flushes alerts.json
    await sup.run_invariants()
    assert sup.level == SystemLevel.EMERGENCY


async def test_incident_clears_once_invariant_recovers(tmp_path: Any) -> None:
    # Regression (2026-08-12): self._incidents only ever grew — a transient
    # CRITICAL that later resolved stayed in "Pending incidents" forever.
    broker = _broker(equity=40_000.0)
    sup = _sup(broker=broker)
    sup._logs_dir = tmp_path  # run_invariants -> _handle_critical flushes alerts.json
    await sup.run_invariants()
    assert len(sup.incidents) == 1

    broker.get_margins = AsyncMock(
        return_value=Margin(equity=200_000.0, available_cash=200_000.0, used_margin=0.0)
    )
    await sup.run_invariants()
    assert sup.incidents == []


# ── INV_C3 daily loss ─────────────────────────────────────────────────────────


async def test_inv_c3_ok_below_cap() -> None:
    sup = _sup()
    sup.update_state(_state(daily_loss_so_far=500.0))
    r = await sup._inv_c3_daily_loss()
    assert r.severity == InvariantSeverity.OK


async def test_inv_c3_warning_at_75_pct() -> None:
    sup = _sup()
    sup.update_state(_state(daily_loss_so_far=1_600.0, max_daily_loss=2_000.0))
    r = await sup._inv_c3_daily_loss()
    assert r.severity == InvariantSeverity.WARNING


async def test_inv_c3_critical_at_cap() -> None:
    sup = _sup()
    sup.update_state(_state(daily_loss_so_far=2_000.0, max_daily_loss=2_000.0))
    r = await sup._inv_c3_daily_loss()
    assert r.severity == InvariantSeverity.CRITICAL


# ── INV_C4 positions ──────────────────────────────────────────────────────────


async def test_inv_c4_ok_within_limit() -> None:
    sup = _sup()
    sup.update_state(_state(open_position_count=3, max_open_positions=5))
    r = await sup._inv_c4_positions()
    assert r.severity == InvariantSeverity.OK


async def test_inv_c4_critical_over_limit() -> None:
    sup = _sup()
    sup.update_state(_state(open_position_count=6, max_open_positions=5))
    r = await sup._inv_c4_positions()
    assert r.severity == InvariantSeverity.CRITICAL


# ── INV_C5 SL present ─────────────────────────────────────────────────────────


async def test_inv_c5_ok_no_missing_sl() -> None:
    r = await _sup()._inv_c5_sl_present()
    assert r.severity == InvariantSeverity.OK


async def test_inv_c5_critical_with_missing_sl() -> None:
    sup = _sup()
    sup.update_state(_state(open_trade_ids_without_sl=["AT001", "AT002"]))
    r = await sup._inv_c5_sl_present()
    assert r.severity == InvariantSeverity.CRITICAL
    assert "AT001" in r.message


# ── INV_C6 broker session ─────────────────────────────────────────────────────


async def test_inv_c6_ok_when_valid() -> None:
    r = await _sup()._inv_c6_broker_session()
    assert r.severity == InvariantSeverity.OK


async def test_inv_c6_critical_when_invalid() -> None:
    sup = _sup()
    sup.update_state(_state(broker_session_valid=False))
    r = await sup._inv_c6_broker_session()
    assert r.severity == InvariantSeverity.CRITICAL


# ── INV_PT2 position qty ──────────────────────────────────────────────────────


async def test_inv_pt2_ok_no_mismatches() -> None:
    r = await _sup()._inv_pt2_position_qty()
    assert r.severity == InvariantSeverity.OK


async def test_inv_pt2_critical_with_mismatches() -> None:
    sup = _sup()
    sup.update_state(_state(position_qty_mismatches=["INFY"]))
    r = await sup._inv_pt2_position_qty()
    assert r.severity == InvariantSeverity.CRITICAL


# ── INV_LP2 paper mode ────────────────────────────────────────────────────────


async def test_inv_lp2_ok_in_live_mode() -> None:
    r = await _sup()._inv_lp2_paper_no_live_stop()
    assert r.severity == InvariantSeverity.OK


async def test_inv_lp2_warning_in_paper_mode() -> None:
    sup = _sup()
    sup.update_state(_state(is_paper_mode=True))
    r = await sup._inv_lp2_paper_no_live_stop()
    assert r.severity == InvariantSeverity.WARNING


# ── _handle_critical ──────────────────────────────────────────────────────────


def test_critical_c2_sets_emergency(tmp_path: Any) -> None:
    sup = _sup()
    sup._logs_dir = tmp_path  # _handle_critical flushes alerts.json — don't touch the real logs/
    sup._handle_critical(InvariantResult("INV_C2", InvariantSeverity.CRITICAL, "floor breached"))
    assert sup.level == SystemLevel.EMERGENCY
    assert len(sup.incidents) == 1


def test_critical_c5_sets_reduced(tmp_path: Any) -> None:
    sup = _sup()
    sup._logs_dir = tmp_path  # _handle_critical flushes alerts.json — don't touch the real logs/
    sup._handle_critical(InvariantResult("INV_C5", InvariantSeverity.CRITICAL, "no SL"))
    assert sup.level == SystemLevel.REDUCED


def test_handle_critical_does_not_duplicate_alert_for_same_invariant(tmp_path: Any) -> None:
    """A condition that stays CRITICAL across consecutive invariant_check_interval_
    seconds cycles must raise ONE alert, not one per cycle."""
    sup = _sup()
    sup._logs_dir = tmp_path
    sup._handle_critical(
        InvariantResult("INV_C5", InvariantSeverity.CRITICAL, "Trades missing SL: [x]")
    )
    sup._handle_critical(
        InvariantResult("INV_C5", InvariantSeverity.CRITICAL, "Trades missing SL: [x]")
    )
    sup._handle_critical(
        InvariantResult("INV_C5", InvariantSeverity.CRITICAL, "Trades missing SL: [x]")
    )
    assert len(sup._alert_manager.active()) == 1


def test_clearing_incident_auto_resolves_its_alert(tmp_path: Any) -> None:
    """The alert must disappear off the dashboard the same cycle the underlying
    invariant recovers — not sit active forever once the real issue is fixed."""
    sup = _sup()
    sup._logs_dir = tmp_path
    sup._handle_critical(
        InvariantResult("INV_C5", InvariantSeverity.CRITICAL, "Trades missing SL: [x]")
    )
    assert len(sup._alert_manager.active()) == 1

    sup._clear_resolved_incident("INV_C5")

    assert sup._alert_manager.active() == []


def test_critical_other_sets_elevated(tmp_path: Any) -> None:
    sup = _sup()
    sup._logs_dir = tmp_path  # _handle_critical flushes alerts.json — don't touch the real logs/
    sup._handle_critical(InvariantResult("INV_C3", InvariantSeverity.CRITICAL, "daily loss"))
    assert sup.level == SystemLevel.ELEVATED


def test_critical_does_not_downgrade_level(tmp_path: Any) -> None:
    """Once at EMERGENCY, a C5 critical should not downgrade to REDUCED."""
    sup = _sup()
    sup._logs_dir = tmp_path  # _handle_critical flushes alerts.json — don't touch the real logs/
    sup.set_level(SystemLevel.EMERGENCY)
    sup._handle_critical(InvariantResult("INV_C5", InvariantSeverity.CRITICAL, "no SL"))
    assert sup.level == SystemLevel.EMERGENCY


# ── start / stop lifecycle ────────────────────────────────────────────────────


async def test_start_creates_12_tasks(tmp_path: Any) -> None:
    sup = _sup()
    sup._logs_dir = tmp_path  # start() flushes alerts.json — don't touch the real logs/
    await sup.start()
    assert len(sup._tasks) == 12
    await sup.stop()


async def test_stop_clears_all_tasks(tmp_path: Any) -> None:
    sup = _sup()
    sup._logs_dir = tmp_path  # start() flushes alerts.json — don't touch the real logs/
    await sup.start()
    await sup.stop()
    assert sup._tasks == []


async def test_stop_sets_running_false(tmp_path: Any) -> None:
    sup = _sup()
    sup._logs_dir = tmp_path  # start() flushes alerts.json — don't touch the real logs/
    await sup.start()
    await sup.stop()
    assert sup._running is False


async def test_start_flushes_a_fresh_alerts_file_clearing_stale_content(tmp_path: Any) -> None:
    """2026-09-08: an alert raised seconds before the process was killed sat in
    logs/alerts.json across a restart that had already fixed the underlying
    problem — a restart alone does nothing to the file, since it's only ever
    rewritten on the NEXT raise/resolve, which may not happen for a while on a
    healthy process. start() must flush immediately so the file always
    reflects this process's true (empty, freshly booted) state."""
    import json as _json

    stale = tmp_path / "alerts.json"
    stale.write_text(
        _json.dumps(
            [
                {
                    "id": "old",
                    "ts": "x",
                    "tier": "CRITICAL",
                    "message": "stale",
                    "acknowledged": False,
                }
            ]
        )
    )
    sup = _sup()
    sup._logs_dir = tmp_path

    await sup.start()
    await sup.stop()

    assert _json.loads(stale.read_text()) == []


# ── boot reconciliation ───────────────────────────────────────────────────────


async def test_boot_healthy_state_keeps_full_ops() -> None:
    sup = _sup()
    await sup._boot_reconciliation()
    assert sup.level == SystemLevel.FULL_OPS


async def test_boot_floor_breach_sets_emergency() -> None:
    # _boot_reconciliation now refreshes equity from the broker too (2026-09-07) —
    # the scenario is set on the broker mock, not the initial state.
    sup = Supervisor(broker=_broker(equity=40_000.0), state=_state(), intervals=_FAST)
    await sup._boot_reconciliation()
    assert sup.level == SystemLevel.EMERGENCY


async def test_boot_invalid_session_stays_full_ops() -> None:
    """Session invalidity at boot is a warning only — handled by token_expiry loop."""
    sup = Supervisor(broker=_broker(session_valid=False), state=_state(), intervals=_FAST)
    await sup._boot_reconciliation()
    assert sup.level == SystemLevel.FULL_OPS


# ── AlertManager wiring ───────────────────────────────────────────────────────


def test_handle_critical_raises_alert(tmp_path: Any) -> None:
    from src.infrastructure.alert_system import AlertTier

    sup = _sup()
    sup._logs_dir = tmp_path
    sup._handle_critical(InvariantResult("INV_C3", InvariantSeverity.CRITICAL, "daily loss cap"))
    active = sup._alert_manager.active()
    assert len(active) == 1
    assert active[0].tier == AlertTier.CRITICAL
    assert "daily loss cap" in active[0].message


def test_handle_critical_inv_c2_raises_emergency_tier(tmp_path: Any) -> None:
    from src.infrastructure.alert_system import AlertTier

    sup = _sup()
    sup._logs_dir = tmp_path
    sup._handle_critical(InvariantResult("INV_C2", InvariantSeverity.CRITICAL, "floor breached"))
    active = sup._alert_manager.active()
    assert len(active) == 1
    assert active[0].tier == AlertTier.EMERGENCY


def test_handle_critical_flushes_alerts_json(tmp_path: Any) -> None:
    import json as _json

    sup = _sup()
    sup._logs_dir = tmp_path
    sup._handle_critical(InvariantResult("INV_C5", InvariantSeverity.CRITICAL, "no SL"))
    alerts_file = tmp_path / "alerts.json"
    assert alerts_file.exists()
    data = _json.loads(alerts_file.read_text())
    assert len(data) == 1
    assert data[0]["tier"] == "CRITICAL"


# ── refresh_from_live_sources (2026-09-07) ─────────────────────────────────────
# Nothing in production ever called update_state() after construction, so C2/C3/
# C4/C5/C6 all ran on a snapshot frozen at process start for the life of the
# process — a real CGCL position could sit unprotected with no working invariant
# to catch it. run_invariants() (and _boot_reconciliation()) now self-refresh
# from the broker/DB/paper_ledger every time, so no external caller has to
# remember to push state for these to work.


def _db_pool_with_rows(rows: list[dict[str, Any]]) -> Any:
    """A minimal asyncpg-pool-shaped mock for TradeRepository.open_trades()'s
    `async with self._pool.acquire() as conn: await conn.fetch(...)` pattern."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def _open_trade_row(
    agent_trade_id: str,
    order_state: str = "FILLED",
    gtt_sl_id: str = "",
    sl_order_id: str = "",
    symbol: str = "CGCL",
) -> dict[str, Any]:
    return {
        "agent_trade_id": agent_trade_id,
        "order_state": order_state,
        "gtt_sl_id": gtt_sl_id,
        "sl_order_id": sl_order_id,
        "symbol": symbol,
    }


async def test_refresh_from_live_sources_updates_equity_and_session() -> None:
    sup = _sup(broker=_broker(session_valid=False, equity=123_456.0))
    await sup.refresh_from_live_sources()
    assert sup._state.current_equity == 123_456.0
    assert sup._state.broker_session_valid is False


async def test_refresh_from_live_sources_derives_open_trade_ids_without_sl() -> None:
    """Reproduces the exact CGCL shape: a FILLED trade with no gtt_sl_id and no
    sl_order_id must show up as missing SL. A protected FILLED trade and a
    still-resting SUBMITTED entry (no SL yet by design) must not."""
    rows = [
        _open_trade_row("TRD-CGCL", order_state="FILLED"),  # naked — like CGCL
        _open_trade_row("TRD-PROTECTED", order_state="FILLED", gtt_sl_id="GTT-1"),
        _open_trade_row("TRD-PENDING", order_state="SUBMITTED"),
    ]
    sup = _sup()
    sup._db_pool = _db_pool_with_rows(rows)

    await sup.refresh_from_live_sources()

    assert sup._state.open_trade_ids_without_sl == ["CGCL (TRD-CGCL)"]
    assert sup._state.open_position_count == 3


async def test_refresh_from_live_sources_updates_daily_loss_from_paper_ledger() -> None:
    ledger = MagicMock()
    ledger.daily_risk_counters = AsyncMock(return_value={"daily_loss_so_far": 1234.5})
    sup = _sup(paper_ledger=ledger)

    await sup.refresh_from_live_sources()

    assert sup._state.daily_loss_so_far == 1234.5
    ledger.daily_risk_counters.assert_awaited_once_with(table="trades")


async def test_refresh_from_live_sources_fails_open_on_broker_error() -> None:
    """A transient broker error must never crash the invariants loop or corrupt
    state — the previous value is kept and the other sources still run."""
    broker = _broker()
    broker.get_margins = AsyncMock(side_effect=Exception("rate limited"))
    sup = _sup(broker=broker)
    sup._state.current_equity = 999_999.0

    await sup.refresh_from_live_sources()  # must not raise

    assert sup._state.current_equity == 999_999.0
    assert sup._state.broker_session_valid is True  # the other source still ran


async def test_run_invariants_pulls_open_trade_ids_without_sl_into_inv_c5(tmp_path: Any) -> None:
    """End-to-end: a naked FILLED trade in the DB reaches INV_C5 without anyone
    calling update_state() — this is what should have caught CGCL."""
    sup = _sup()
    sup._logs_dir = tmp_path  # run_invariants -> _handle_critical flushes alerts.json
    sup._db_pool = _db_pool_with_rows([_open_trade_row("TRD-CGCL")])

    results = await sup.run_invariants()

    c5 = next(r for r in results if r.invariant_id == "INV_C5")
    assert c5.severity == InvariantSeverity.CRITICAL
    assert "TRD-CGCL" in c5.message
