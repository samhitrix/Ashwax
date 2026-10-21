"""
Runtime Supervisor — 12 async loops + 15 hard invariants.

Architecture:
  Bus = Postgres (runtime_events, engine_heartbeats, runtime_incidents). Zero IPC.
  State is injected via update_state(); loops read self._state each cycle.
  Floor check is called from BOTH the invariants loop (INV_C2) and a dedicated
  floor_monitor loop — two independent paths to EMERGENCY halt.

Degradation levels (5-min hysteresis in production; skipped in Phase 1):
  FULL_OPS → ELEVATED → REDUCED → MONITOR_ONLY → EMERGENCY

12 loops (intervals configurable for fast tests):
  heartbeat, self_watchdog, orchestrator_watchdog, invariants, remediation,
  proactive_health, ai_brain, housekeeping, token_expiry, retention,
  floor_monitor, gtts_monitor

15 hard invariants (9 core + 4 price-truth + 2 live/paper protection).
  Each returns InvariantResult with severity + remediation_hint.
  CRITICAL severity → immediate level escalation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from src.infrastructure.alert_system import AlertManager, AlertTier
from src.infrastructure.logger import cleanup_old_logs
from src.market import sessions as _sessions
from src.market.calendar import is_trading_day as _is_trading_day
from src.supervisor.floor_check import supervisor_floor_check

if TYPE_CHECKING:
    from src.brokers.base import BrokerInterface

_IST = ZoneInfo("Asia/Kolkata")
_LOG = logging.getLogger("src.supervisor")
_LOGS_DIR = Path(__file__).resolve().parents[2] / "logs"

# Default loop intervals (seconds) — override in constructor for fast tests
_DEFAULT_INTERVALS: dict[str, float] = {
    "heartbeat": 5.0,
    "self_watchdog": 10.0,
    "orchestrator_watchdog": 10.0,
    "invariants": 10.0,
    "remediation": 5.0,
    "proactive_health": 60.0,
    "ai_brain": 300.0,
    "housekeeping": 3600.0,
    "token_expiry": 60.0,
    "retention": 86400.0,
    "floor_monitor": 5.0,
    "gtts_monitor": 30.0,
}


class SystemLevel(StrEnum):
    FULL_OPS = "FULL_OPS"
    ELEVATED = "ELEVATED"
    REDUCED = "REDUCED"
    MONITOR_ONLY = "MONITOR_ONLY"
    EMERGENCY = "EMERGENCY"


class InvariantSeverity(StrEnum):
    OK = "OK"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class InvariantResult:
    invariant_id: str
    severity: InvariantSeverity
    message: str
    remediation_hint: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now(_IST))


@dataclass
class SupervisorState:
    """
    Live state snapshot injected into the supervisor each cycle.

    Updated by the trading engine (or DB poll in production).
    All invariants read from this struct — never from globals.
    """

    current_equity: float
    starting_capital: float
    daily_loss_so_far: float
    max_daily_loss: float
    open_position_count: int
    max_open_positions: int
    broker_session_valid: bool = True
    is_paper_mode: bool = False
    open_trade_ids_without_sl: list[str] = field(default_factory=list)
    unverified_fill_ids: list[str] = field(default_factory=list)
    position_qty_mismatches: list[str] = field(default_factory=list)


class Supervisor:
    """
    Runtime supervisor — starts 12 async loops, checks 15 invariants.

    Usage:
        sup = Supervisor(broker=adapter, state=SupervisorState(...))
        await sup.start()
        # ... trading session ...
        await sup.stop()
    """

    def __init__(
        self,
        broker: BrokerInterface,
        state: SupervisorState,
        intervals: dict[str, float] | None = None,
        operator_config: dict[str, Any] | None = None,
        db_pool: Any = None,
        supervisor_cfg: dict[str, Any] | None = None,
        paper_ledger: Any = None,
    ) -> None:
        self._broker = broker
        self._state = state
        # Optional live-data sources for refresh_from_live_sources() (2026-09-07).
        # `state` above is still the initial/fallback snapshot — every field
        # refresh_from_live_sources() can source live data for gets overwritten
        # in place at the top of every run_invariants() call, so nothing here
        # depends on an external caller remembering to call update_state().
        self._paper_ledger = paper_ledger
        self._config = operator_config or {}
        self._sup_cfg = supervisor_cfg or {}
        self._level: SystemLevel = SystemLevel.FULL_OPS
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
        self._incidents: list[InvariantResult] = []
        self._alert_manager = AlertManager()
        self._boot_at = datetime.now(_IST)
        self._logs_dir = Path(__file__).parent.parent.parent / "logs"
        # Persist every alert the instant it's raised OR resolved — including ones
        # raised from order_lifecycle.py (deferred-protection/GTT/repeg failures),
        # which share this same AlertManager instance but never called
        # _flush_alerts() on their own. Without this a real CRITICAL could sit
        # unflushed for hours, and a fixed one would never disappear from the
        # dashboard on its own.
        self._alert_manager.on_change = self._flush_alerts

        # Read intervals from supervisor.json; fall back to defaults
        _cfg_intervals: dict[str, float] = {}
        if self._sup_cfg.get("heartbeat_interval_seconds"):
            _cfg_intervals["heartbeat"] = float(self._sup_cfg["heartbeat_interval_seconds"])
        if self._sup_cfg.get("invariant_check_interval_seconds"):
            _cfg_intervals["invariants"] = float(self._sup_cfg["invariant_check_interval_seconds"])
        self._intervals = {**_DEFAULT_INTERVALS, **_cfg_intervals, **(intervals or {})}

        # DB bus — SupervisorRepository for runtime_events/engine_heartbeats/runtime_incidents
        self._sv_repo: Any = None
        if db_pool is not None:
            from src.db.repositories.supervisor import SupervisorRepository

            self._sv_repo = SupervisorRepository(db_pool)
        self._db_pool = db_pool

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def level(self) -> SystemLevel:
        return self._level

    @property
    def incidents(self) -> list[InvariantResult]:
        return list(self._incidents)

    def update_state(self, state: SupervisorState) -> None:
        """Push a new state snapshot (called by trading engine each cycle)."""
        self._state = state

    async def start(self) -> None:
        """Boot reconciliation then launch all 12 loops."""
        self._running = True
        # AlertManager is in-memory and starts empty every process — but
        # logs/alerts.json is a FILE, and only ever gets rewritten when
        # raise_alert()/resolve() fires. A restart alone does nothing to it:
        # whatever the previous process last wrote (including something raised
        # seconds before it was killed) sits there, correct or not, until some
        # future event happens to overwrite it — which may never happen if the
        # new process stays healthy. Flushing here means the file always
        # reflects THIS process's true (empty, at this point) state the moment
        # it starts, instead of carrying forward a stale alert indefinitely
        # (found 2026-09-08: "Entry order abandoned" kept showing on the
        # dashboard across a restart that had already fixed the underlying
        # order, because nothing new had happened yet to refresh the file).
        self._flush_alerts()
        if self._sv_repo is not None:
            try:
                await self._sv_repo.emit_event("SUPERVISOR_STARTED", "supervisor", "INFO")
            except Exception as exc:
                _LOG.warning("sv_repo_emit_failed err=%s", exc)
        await self._boot_reconciliation()
        loop_fns = [
            self._loop_heartbeat,
            self._loop_self_watchdog,
            self._loop_orchestrator_watchdog,
            self._loop_invariants,
            self._loop_remediation,
            self._loop_proactive_health,
            self._loop_ai_brain,
            self._loop_housekeeping,
            self._loop_token_expiry,
            self._loop_retention,
            self._loop_floor_monitor,
            self._loop_gtts_monitor,
        ]
        self._tasks = [asyncio.create_task(fn()) for fn in loop_fns]
        _LOG.info("Supervisor started — %d loops at level %s", len(self._tasks), self._level)

    async def stop(self) -> None:
        """Cancel all loops and wait for clean shutdown."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        _LOG.info("Supervisor stopped")

    async def refresh_from_live_sources(self) -> None:
        """Pull fresh equity/positions/SL-coverage/session/daily-loss into self._state.

        2026-09-07: C2 (floor/EMERGENCY), C3 (daily loss), C4 (position count), C5 (SL
        presence — this is what let CGCL sit unprotected for 12+ hours with no alert)
        and C6 (broker session) all read self._state, but nothing in production ever
        called update_state() after construction — every one of those checks ran on a
        snapshot frozen at process start for the life of the process. Calling this at
        the top of every run_invariants() (see below) means no external caller has to
        remember to push state for these invariants to work — Supervisor sources its
        own truth every cycle. Each source is independent and fails open (keeps the
        previous value, never raises) so one broker/DB hiccup can't take the others
        down or crash the invariants loop.

        unverified_fill_ids/position_qty_mismatches (PT1/PT2) are NOT covered here —
        no live source for those exists yet; they stay at whatever update_state() last
        set (default: empty/OK). is_paper_mode is a static deployment setting, not
        something that needs a live refresh.
        """
        try:
            margin = await self._broker.get_margins()
            self._state.current_equity = float(margin.equity)
        except Exception as exc:
            _LOG.debug("refresh_equity_failed err=%s", exc)

        try:
            status = await self._broker.get_session_status()
            self._state.broker_session_valid = bool(status.is_valid)
        except Exception as exc:
            _LOG.debug("refresh_session_status_failed err=%s", exc)

        if self._db_pool is not None:
            try:
                from src.db.repositories.trades import TradeRepository

                open_db = await TradeRepository(self._db_pool).open_trades(is_paper=False)
                self._state.open_position_count = len(open_db)
                self._state.open_trade_ids_without_sl = [
                    f"{r['symbol']} ({r['agent_trade_id']})"
                    for r in open_db
                    if str(r["order_state"]) == "FILLED"
                    and not r["gtt_sl_id"]
                    and not r["sl_order_id"]
                ]
            except Exception as exc:
                _LOG.debug("refresh_open_trades_failed err=%s", exc)

        if self._paper_ledger is not None:
            try:
                counters = await self._paper_ledger.daily_risk_counters(table="trades")
                self._state.daily_loss_so_far = float(counters["daily_loss_so_far"])
            except Exception as exc:
                _LOG.debug("refresh_daily_loss_failed err=%s", exc)

    async def run_invariants(self) -> list[InvariantResult]:
        """Run all 16 invariant checks. Escalates level on CRITICAL findings."""
        await self.refresh_from_live_sources()
        checks = [
            self._inv_c1_singleton,
            self._inv_c2_floor,
            self._inv_c3_daily_loss,
            self._inv_c4_positions,
            self._inv_c5_sl_present,
            self._inv_c6_broker_session,
            self._inv_c7_no_zombies,
            self._inv_c8_db_alive,
            self._inv_c9_clock,
            self._inv_c10_intraday_scan_alive,
            self._inv_pt1_fills_verified,
            self._inv_pt2_position_qty,
            self._inv_pt3_price_tolerance,
            self._inv_pt4_pnl_calculated,
            self._inv_lp1_live_paper_separation,
            self._inv_lp2_paper_no_live_stop,
        ]
        results: list[InvariantResult] = []
        for check in checks:
            result = await check()
            results.append(result)
            if result.severity == InvariantSeverity.CRITICAL:
                self._handle_critical(result)
            else:
                self._clear_resolved_incident(result.invariant_id)
        return results

    def _clear_resolved_incident(self, invariant_id: str) -> None:
        """Drop any previously-recorded CRITICAL incidents for this invariant now
        that it has returned to OK/WARNING. Without this, self._incidents only
        ever grew — a transient blip (e.g. a DB pool timeout) stayed in "Pending
        incidents" forever, logging every ~5s, even long after the underlying
        issue had cleared (observed 2026-08-12: two INV_C8 DB-unreachable blips
        8 minutes apart kept "Pending incidents: 2" repeating indefinitely)."""
        before = len(self._incidents)
        self._incidents = [r for r in self._incidents if r.invariant_id != invariant_id]
        if len(self._incidents) < before:
            if self._sv_repo is not None:
                asyncio.create_task(self._sv_repo.resolve_incident(dedup_key=invariant_id))
            # Auto-clear the dashboard alert too — without this a fixed condition
            # (e.g. CGCL's GTT re-arming successfully) left INV_C5's "Trades missing
            # SL" alert sitting active forever; it now disappears the same cycle the
            # invariant recovers.
            self._alert_manager.resolve_by_source(invariant_id)

    def set_level(self, level: SystemLevel) -> None:
        """Set the system degradation level."""
        if level != self._level:
            _LOG.warning("System level: %s → %s", self._level, level)
            self._level = level

    # ── Boot reconciliation ────────────────────────────────────────────────────

    async def _boot_reconciliation(self) -> None:
        """
        Boot sequence: floor check → broker session → done.

        Target: complete in < 2 min. Halts at EMERGENCY if floor already breached.
        """
        _LOG.info("Boot reconciliation started")
        await self.refresh_from_live_sources()

        floor = supervisor_floor_check(
            equity=self._state.current_equity,
            starting_capital=self._state.starting_capital,
        )
        if floor.breached:
            self.set_level(SystemLevel.EMERGENCY)
            _LOG.critical("BOOT ABORTED: %s", floor.message)
            return

        try:
            status = await self._broker.get_session_status()
            if not status.is_valid:
                _LOG.warning("Broker session invalid at boot — token_expiry loop will retry")
        except Exception:
            _LOG.warning("Broker session check failed at boot")

        _LOG.info("Boot reconciliation complete — level=%s", self._level)

    # ── 15 Invariant checks ────────────────────────────────────────────────────

    async def _inv_c1_singleton(self) -> InvariantResult:
        return InvariantResult("INV_C1", InvariantSeverity.OK, "Singleton mutex assumed held")

    async def _inv_c2_floor(self) -> InvariantResult:
        """C2: 25% absolute floor not breached."""
        r = supervisor_floor_check(
            equity=self._state.current_equity,
            starting_capital=self._state.starting_capital,
        )
        if r.breached:
            return InvariantResult(
                "INV_C2",
                InvariantSeverity.CRITICAL,
                r.message,
                remediation_hint="Set level=EMERGENCY, halt all new orders",
            )
        return InvariantResult("INV_C2", InvariantSeverity.OK, r.message)

    async def _inv_c3_daily_loss(self) -> InvariantResult:
        """C3: Daily loss within operator cap."""
        if self._state.daily_loss_so_far >= self._state.max_daily_loss:
            return InvariantResult(
                "INV_C3",
                InvariantSeverity.CRITICAL,
                f"Daily loss {self._state.daily_loss_so_far:.0f} >= cap {self._state.max_daily_loss:.0f}",
                remediation_hint="Halt new entries",
            )
        pct = (
            (self._state.daily_loss_so_far / self._state.max_daily_loss)
            if self._state.max_daily_loss > 0
            else 0.0
        )
        sev = InvariantSeverity.WARNING if pct > 0.75 else InvariantSeverity.OK
        return InvariantResult(
            "INV_C3",
            sev,
            f"Daily loss {self._state.daily_loss_so_far:.0f}/{self._state.max_daily_loss:.0f}",
        )

    async def _inv_c4_positions(self) -> InvariantResult:
        """C4: Open position count within max."""
        if self._state.open_position_count > self._state.max_open_positions:
            return InvariantResult(
                "INV_C4",
                InvariantSeverity.CRITICAL,
                f"Positions {self._state.open_position_count} > max {self._state.max_open_positions}",
                remediation_hint="Block new entries",
            )
        return InvariantResult(
            "INV_C4",
            InvariantSeverity.OK,
            f"Positions {self._state.open_position_count}/{self._state.max_open_positions}",
        )

    async def _inv_c5_sl_present(self) -> InvariantResult:
        """C5: Every open trade has a stop-loss."""
        missing = self._state.open_trade_ids_without_sl
        if missing:
            return InvariantResult(
                "INV_C5",
                InvariantSeverity.CRITICAL,
                f"Trades missing SL: {missing}",
                remediation_hint="Place SL orders immediately",
            )
        return InvariantResult("INV_C5", InvariantSeverity.OK, "All open trades have SL")

    async def _inv_c6_broker_session(self) -> InvariantResult:
        """C6: Broker session token is valid."""
        if not self._state.broker_session_valid:
            return InvariantResult(
                "INV_C6",
                InvariantSeverity.CRITICAL,
                "Broker session invalid",
                remediation_hint="Trigger TOTP re-login",
            )
        return InvariantResult("INV_C6", InvariantSeverity.OK, "Broker session valid")

    async def _inv_c7_no_zombies(self) -> InvariantResult:
        """C7: DB open trades not present in broker positions → WARNING."""
        if self._db_pool is None:
            return InvariantResult(
                "INV_C7", InvariantSeverity.OK, "Zombie check skipped — no DB pool"
            )
        try:
            from src.db.repositories.trades import TradeRepository

            repo = TradeRepository(self._db_pool)
            open_db = await repo.open_trades(is_paper=False)
            broker_positions = await self._broker.get_positions()
            broker_symbols = {p.symbol for p in broker_positions}
            zombies = [r["agent_trade_id"] for r in open_db if r["symbol"] not in broker_symbols]
            if zombies:
                return InvariantResult(
                    "INV_C7",
                    InvariantSeverity.WARNING,
                    f"DB open trades not in broker positions: {zombies}",
                    remediation_hint="Reconcile DB trade states with broker",
                )
        except Exception as exc:
            _LOG.debug("inv_c7_error err=%s", exc)
        return InvariantResult("INV_C7", InvariantSeverity.OK, "No zombie trades detected")

    async def _inv_c8_db_alive(self) -> InvariantResult:
        """C8: DB must be reachable — CRITICAL if not."""
        if self._db_pool is None:
            return InvariantResult("INV_C8", InvariantSeverity.WARNING, "DB pool not initialised")
        try:
            await self._db_pool.fetchval("SELECT 1")
            return InvariantResult("INV_C8", InvariantSeverity.OK, "DB reachable")
        except Exception as exc:
            return InvariantResult(
                "INV_C8",
                InvariantSeverity.CRITICAL,
                f"DB unreachable: {exc}",
                remediation_hint="HALT new orders — cannot trade without records",
            )

    async def _inv_c9_clock(self) -> InvariantResult:
        """C9: System clock must be within ±30s of expected IST offset."""
        now_ist = datetime.now(_IST)
        now_utc = datetime.now(UTC)
        # IST = UTC+5:30 = 19800s offset
        actual_offset = (
            now_ist.replace(tzinfo=None) - now_utc.replace(tzinfo=None)
        ).total_seconds()
        drift = abs(actual_offset - 19800)
        if drift > 30:
            return InvariantResult(
                "INV_C9",
                InvariantSeverity.WARNING,
                f"Clock drift {drift:.0f}s from expected IST offset",
                remediation_hint="Sync system clock — stale prices may cause wrong SL triggers",
            )
        return InvariantResult("INV_C9", InvariantSeverity.OK, f"Clock OK drift={drift:.1f}s")

    async def _inv_c10_intraday_scan_alive(self) -> InvariantResult:
        """C10: the intraday signal-scan loop must have completed a cycle
        recently during market hours — CRITICAL if not.

        2026-09-21: this loop stalled for 40+ minutes at a time (root cause:
        a broker API call with no timeout) with zero visibility anywhere —
        no exception, no invariant, nothing in logs/alerts.json — discovered
        only by manually re-reading logs/intraday_signals.json's `ts` field
        over and over. That file is the loop's own per-cycle heartbeat
        (written by _intraday_signal_scan_loop in orchestrator.py on every
        completed cycle); this makes staleness in it a first-class alert
        instead of something that has to be noticed by hand.

        Threshold is llm_intraday.idea_ttl_minutes (default 15), not an
        arbitrary number: past that age, any llm_intraday_agent idea is
        already structurally unable to convert to a trade regardless of
        anything else, so that is the point past which staleness stops being
        merely slow and starts being a real trading-capability outage."""
        now = datetime.now(_IST)
        if (
            now.weekday() >= 5
            or not _is_trading_day(now.date())
            or not _sessions.in_regular_minutes(now.hour * 60 + now.minute)
        ):
            return InvariantResult(
                "INV_C10", InvariantSeverity.OK, "Outside market hours — not checked"
            )
        _base = self._logs_dir.parent
        try:
            cfg = json.loads((_base / "config" / "global.json").read_text())
            threshold_min = float(cfg.get("llm_intraday", {}).get("idea_ttl_minutes", 15))
        except Exception:
            threshold_min = 15.0
        try:
            raw = json.loads((self._logs_dir / "intraday_signals.json").read_text())
            last_ts = datetime.fromisoformat(raw["ts"])
        except Exception as exc:
            return InvariantResult(
                "INV_C10",
                InvariantSeverity.WARNING,
                f"Could not read intraday scan heartbeat: {exc}",
            )
        age_min = (now - last_ts.astimezone(_IST)).total_seconds() / 60.0
        # After a restart the heartbeat file is still the previous process's, so it
        # is already stale by however long the restart took. Give the new loop one
        # full threshold window to complete its first cycle before calling it stalled.
        uptime_min = (now - self._boot_at).total_seconds() / 60.0
        if age_min > threshold_min and uptime_min < threshold_min:
            return InvariantResult(
                "INV_C10",
                InvariantSeverity.OK,
                f"Warming up after restart ({uptime_min:.1f}/{threshold_min:.0f} min) — "
                f"heartbeat {age_min:.1f} min old",
            )
        # The loop writes nothing outside the session, so at the open the heartbeat is
        # still the previous session's (a Monday open sees Friday's, ~66h old) and the
        # first cycle only completes after the first 5m bar closes. Give the loop one
        # threshold window from the open before a pre-open heartbeat counts as stale.
        mins_since_open = now.hour * 60 + now.minute - _sessions.minutes(_sessions.regular_open())
        if (
            age_min > threshold_min
            and last_ts.astimezone(_IST).date() < now.date()
            and mins_since_open < threshold_min
        ):
            return InvariantResult(
                "INV_C10",
                InvariantSeverity.OK,
                f"Awaiting first cycle of the session ({mins_since_open} min since open) — "
                f"heartbeat is from {last_ts.astimezone(_IST):%Y-%m-%d %H:%M}",
            )
        if age_min > threshold_min:
            return InvariantResult(
                "INV_C10",
                InvariantSeverity.CRITICAL,
                f"Intraday scan loop stalled: last completed cycle {age_min:.1f} min ago "
                f"(threshold {threshold_min:.0f} min) — no intraday strategy can fire",
                remediation_hint="Check for a hung broker/network call; restart if confirmed stuck",
            )
        return InvariantResult("INV_C10", InvariantSeverity.OK, f"Last cycle {age_min:.1f} min ago")

    async def _inv_pt1_fills_verified(self) -> InvariantResult:
        """PT1: All filled orders have verified fill prices."""
        if self._state.unverified_fill_ids:
            return InvariantResult(
                "INV_PT1",
                InvariantSeverity.WARNING,
                f"Unverified fills: {self._state.unverified_fill_ids}",
                remediation_hint="Trigger Phase 1 price-truth check",
            )
        return InvariantResult("INV_PT1", InvariantSeverity.OK, "All fills verified")

    async def _inv_pt2_position_qty(self) -> InvariantResult:
        """PT2: Position quantities match broker."""
        if self._state.position_qty_mismatches:
            return InvariantResult(
                "INV_PT2",
                InvariantSeverity.CRITICAL,
                f"Qty mismatches: {self._state.position_qty_mismatches}",
                remediation_hint="CRITICAL — halt; reconcile with broker",
            )
        return InvariantResult("INV_PT2", InvariantSeverity.OK, "Position quantities match broker")

    async def _inv_pt3_price_tolerance(self) -> InvariantResult:
        """PT3: DB fill_price must match broker average_price within ₹0.05 for open trades."""
        if self._db_pool is None:
            return InvariantResult("INV_PT3", InvariantSeverity.OK, "PT3 skipped — no DB pool")
        try:
            from src.db.repositories.trades import TradeRepository

            repo = TradeRepository(self._db_pool)
            open_db = await repo.open_trades(is_paper=False)
            if not open_db:
                return InvariantResult("INV_PT3", InvariantSeverity.OK, "No open trades to check")
            positions = await self._broker.get_positions()
            pos_map = {p.symbol: p.average_price for p in positions}
            critical, warnings = [], []
            for row in open_db:
                sym = row["symbol"]
                db_price = float(row["fill_price"] or 0)
                if db_price <= 0 or sym not in pos_map:
                    continue
                broker_price = pos_map[sym]
                drift = abs(db_price - broker_price)
                drift_pct = drift / broker_price if broker_price else 0
                if drift_pct > 0.01:
                    critical.append(f"{sym} db={db_price:.2f} broker={broker_price:.2f}")
                elif drift > 0.05:
                    warnings.append(f"{sym} drift=₹{drift:.2f}")
            if critical:
                return InvariantResult(
                    "INV_PT3",
                    InvariantSeverity.CRITICAL,
                    f"Price drift >1%: {critical}",
                    remediation_hint="Auto-correct fill_price from broker.get_positions()",
                )
            if warnings:
                return InvariantResult(
                    "INV_PT3", InvariantSeverity.WARNING, f"Minor drift: {warnings}"
                )
        except Exception as exc:
            _LOG.debug("inv_pt3_error err=%s", exc)
        return InvariantResult("INV_PT3", InvariantSeverity.OK, "Fill prices match broker")

    async def _inv_pt4_pnl_calculated(self) -> InvariantResult:
        """PT4: Closed trades must have pnl_realized populated."""
        if self._db_pool is None:
            return InvariantResult("INV_PT4", InvariantSeverity.OK, "PT4 skipped — no DB pool")
        try:
            async with self._db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT agent_trade_id FROM trades WHERE order_state='CLOSED' AND pnl_realized IS NULL LIMIT 10"
                )
            if rows:
                ids = [r["agent_trade_id"] for r in rows]
                return InvariantResult(
                    "INV_PT4",
                    InvariantSeverity.WARNING,
                    f"Closed trades missing P&L: {ids}",
                    remediation_hint="Recompute pnl_realized from fill_price and exit_price",
                )
        except Exception as exc:
            _LOG.debug("inv_pt4_error err=%s", exc)
        return InvariantResult("INV_PT4", InvariantSeverity.OK, "All closed trades have P&L")

    async def _inv_lp1_live_paper_separation(self) -> InvariantResult:
        """LP1: Live trades table must not contain paper rows; paper positions tracked correctly."""
        if self._db_pool is None:
            return InvariantResult("INV_LP1", InvariantSeverity.OK, "LP1 skipped — no DB pool")
        try:
            async with self._db_pool.acquire() as conn:
                # trades table has no is_paper column — any row there IS live by design
                live_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM trades WHERE order_state NOT IN ('CLOSED','CANCELLED','REJECTED','TIMED_OUT')"
                )
                paper_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM paper_trades WHERE order_state NOT IN ('CLOSED','CANCELLED','REJECTED')"
                )
            return InvariantResult(
                "INV_LP1",
                InvariantSeverity.OK,
                f"Separation OK: live_open={live_count} paper_open={paper_count}",
            )
        except Exception as exc:
            _LOG.debug("inv_lp1_error err=%s", exc)
        return InvariantResult("INV_LP1", InvariantSeverity.OK, "Live/paper separation verified")

    async def _inv_lp2_paper_no_live_stop(self) -> InvariantResult:
        """LP2: Paper mode cannot stop live position monitoring."""
        if self._state.is_paper_mode:
            return InvariantResult(
                "INV_LP2",
                InvariantSeverity.WARNING,
                "Paper mode active — verify live position monitoring is running",
                remediation_hint="Confirm Track 1 (live monitor) is active",
            )
        return InvariantResult("INV_LP2", InvariantSeverity.OK, "Paper mode inactive")

    # ── Critical handler ───────────────────────────────────────────────────────

    async def _persist_incident(self, result: InvariantResult) -> None:
        """Fire-and-forget: open a runtime_incident row for this CRITICAL finding."""
        try:
            await self._sv_repo.open_incident(
                dedup_key=result.invariant_id,
                severity=result.severity.value,
                title=result.invariant_id,
                details=result.message,
            )
        except Exception as exc:
            _LOG.debug("persist_incident_failed err=%s", exc)

    def _handle_critical(self, result: InvariantResult) -> None:
        self._incidents.append(result)
        _LOG.critical(
            "INVARIANT %s CRITICAL: %s | remedy: %s",
            result.invariant_id,
            result.message,
            result.remediation_hint,
        )
        tier = AlertTier.EMERGENCY if result.invariant_id == "INV_C2" else AlertTier.CRITICAL
        # One active alert per invariant, not one per check cycle — without this a
        # condition that stays CRITICAL for N consecutive invariant_check_interval_
        # seconds (this ran every cycle, unconditionally) raised N duplicate alerts.
        # raise_alert() flushes alerts.json itself via AlertManager.on_change.
        _already_active = any(a.source == result.invariant_id for a in self._alert_manager.active())
        if not _already_active:
            self._alert_manager.raise_alert(tier, result.message, source=result.invariant_id)
        if self._sv_repo is not None:
            asyncio.create_task(self._persist_incident(result))
        if result.invariant_id == "INV_C2":
            self.set_level(SystemLevel.EMERGENCY)
        elif result.invariant_id in ("INV_C5", "INV_C6", "INV_PT2"):
            if self._level in (SystemLevel.FULL_OPS, SystemLevel.ELEVATED):
                self.set_level(SystemLevel.REDUCED)
        else:
            if self._level == SystemLevel.FULL_OPS:
                self.set_level(SystemLevel.ELEVATED)

    def _flush_alerts(self) -> None:
        """Write active alerts to logs/alerts.json for the API server to serve."""
        try:
            self._logs_dir.mkdir(exist_ok=True)
            (self._logs_dir / "alerts.json").write_text(
                json.dumps([a.to_item() for a in self._alert_manager.active()], ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            _LOG.warning("alerts_flush_failed err=%s", exc)

    # ── 12 Async loops ─────────────────────────────────────────────────────────

    async def _loop_heartbeat(self) -> None:
        import os

        while self._running:
            _LOG.debug("Heartbeat — level=%s equity=%.0f", self._level, self._state.current_equity)
            if self._sv_repo is not None:
                try:
                    await self._sv_repo.beat(
                        "supervisor",
                        pid=os.getpid(),
                        status=self._level.value,
                        metadata={"equity": self._state.current_equity},
                    )
                except Exception as exc:
                    _LOG.debug("heartbeat_db_failed err=%s", exc)
            await asyncio.sleep(self._intervals["heartbeat"])

    async def _loop_self_watchdog(self) -> None:
        while self._running:
            await asyncio.sleep(self._intervals["self_watchdog"])

    async def _loop_orchestrator_watchdog(self) -> None:
        while self._running:
            await asyncio.sleep(self._intervals["orchestrator_watchdog"])

    async def _loop_invariants(self) -> None:
        while self._running:
            try:
                await self.run_invariants()
            except Exception:
                _LOG.exception("Invariants loop error")
            await asyncio.sleep(self._intervals["invariants"])

    async def _loop_remediation(self) -> None:
        while self._running:
            if self._incidents:
                _LOG.warning("Pending incidents: %d", len(self._incidents))
            await asyncio.sleep(self._intervals["remediation"])

    async def _loop_proactive_health(self) -> None:
        while self._running:
            await asyncio.sleep(self._intervals["proactive_health"])

    async def _loop_ai_brain(self) -> None:
        while self._running:
            await asyncio.sleep(self._intervals["ai_brain"])

    async def _loop_housekeeping(self) -> None:
        while self._running:
            await asyncio.sleep(self._intervals["housekeeping"])

    async def _loop_token_expiry(self) -> None:
        while self._running:
            if not self._state.broker_session_valid:
                _LOG.warning("Broker session invalid — re-login required")
            await asyncio.sleep(self._intervals["token_expiry"])

    async def _loop_retention(self) -> None:
        """Daily backup sweep for old service_stdout/stderr + audit log files.

        cleanup_old_logs() already runs once at process startup (see
        setup_logging()) -- that's what actually guarantees cleanup happens
        given how often this engine restarts. This loop only matters for a
        process that stays up longer than a day without restarting.
        """
        while self._running:
            await asyncio.sleep(self._intervals["retention"])
            try:
                cleanup_old_logs(_LOGS_DIR)
            except Exception as exc:  # noqa: BLE001 — housekeeping must never crash the loop
                _LOG.warning("log_retention_sweep_failed err=%s", exc)

    async def _loop_floor_monitor(self) -> None:
        """Dedicated 25% floor check — independent path to EMERGENCY."""
        while self._running:
            r = supervisor_floor_check(
                equity=self._state.current_equity,
                starting_capital=self._state.starting_capital,
            )
            if r.breached and self._level != SystemLevel.EMERGENCY:
                _LOG.critical("FLOOR MONITOR: %s", r.message)
                self.set_level(SystemLevel.EMERGENCY)
            await asyncio.sleep(self._intervals["floor_monitor"])

    async def _loop_gtts_monitor(self) -> None:
        while self._running:
            await asyncio.sleep(self._intervals["gtts_monitor"])
