"""
src/orchestrator.py — MAIN ENTRY POINT for the ashwax trading engine.

Per docs/09-multiprocess-engine.md boot sequence:
  1. Singleton lock
  2. Kill zombies, clear SHM
  3. Connect broker
  4. Reconcile broker vs DB
  5. Verify GTTs
  6. Load strategies
  7. Start TickIngester + WorkerPool + Supervisor
  8. Block until shutdown

Usage:
    python src/orchestrator.py
    python src/orchestrator.py --paper
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import signal
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from src.brokers.stack_keys import (
    LEGACY_LIVE_BROKER,
    live_stacks,
    primary_live_key,
    stack_broker,
    stack_is_paper,
    stack_key,
)
from src.infrastructure.logger import get_logger, setup_logging, strategy_logged
from src.infrastructure.singleton import acquire_singleton_lock
from src.market import sessions as _sessions
from src.market.calendar import is_trading_day, load_holidays
from src.risk.gate_log import log_gate_decisions

_BASE = Path(__file__).parent.parent
_CONFIG = _BASE / "config"
_LOGS = _BASE / "logs"

# Single source for the autonomy_mode fallback so the canonical resolution
# (global.json system.autonomy_mode) and every shared.get("autonomy_mode", ...)
# read-site default can't drift apart from each other.
_DEFAULT_AUTONOMY_MODE = "assisted"

# Position-size multiplier applied per health tier before order placement (see
# usage below). Module-level (not a local in the placement function) so the
# read-only dashboard endpoint that displays "what the learning loop is
# currently doing to this strategy's size" (src/api/server.py) imports this
# exact mapping instead of hand-copying it — the two can never drift apart.
HEALTH_TIER_SIZE_MULTIPLIER: dict[str, float] = {
    "HEALTHY": 1.0,
    "CAUTIOUS": 0.75,
    "DEGRADED": 0.50,
    "STRESSED": 0.25,
    "SUSPENDED": 0.00,
}


def _resolve_is_paper(
    shared: dict[str, Any],
    crm_ctx: Any,
    segment: str,
    strategy_name: str,
    paper_mode: bool,
) -> bool:
    """Paper/live resolution precedence for one signal:

        1. operator.json account.paper_mode_locked — hard, fail-safe to paper,
           overrides everything below (same fail-safe direction as the boot
           --paper/--live flag override).
        2. strategies_registry.json per-strategy "execution_mode" override
           ("paper" | "live"), when not "inherit"/absent.
        3. global.json system.segments.<segment>.paper_mode (existing
           per-segment default), falling back to the boot flag.

    Both call sites (_execute_intraday_signal, _execute_swing_signal) must
    resolve this identically — a single shared function instead of two
    independent inline expressions, so they cannot drift apart from each
    other the way two hand-copied literals could."""
    if shared.get("paper_mode_locked", False):
        return True
    _override = shared.get("strategy_execution_mode", {}).get(strategy_name, "inherit")
    if _override == "paper":
        return True
    if _override == "live":
        return False
    return bool(getattr(crm_ctx, "segment_paper_mode", {}).get(segment, paper_mode))


def _resolve_broker(
    shared: Mapping[str, Any], strategy_name: str, is_paper: bool
) -> tuple[str | None, str | None]:
    """(broker, reject_reason) for one signal whose paper/live mode _resolve_is_paper decided
    (a paper_mode_locked account never gets here as live). Spec rule R1, owner decision:

        paper                                    -> (None, None); routes are never consulted
        live, route ok (shared["strategy_routes"]) -> (route.broker, None)
        live, route not ok                       -> (None, reason): REJECT -- no default
                                                    broker, no failover, no demotion to paper
        live, no route entry (not in registry or -> (shared["legacy_live_broker"], None):
          "inherit": the segment decides)           the legacy global.json broker, unchanged
    """
    if is_paper:
        return None, None
    from src.brokers.routing import route_rejection

    routes = shared.get("strategy_routes") or {}
    reason = route_rejection(routes, strategy_name)
    if reason is not None:
        return None, reason
    route = routes.get(strategy_name)
    if route is not None:
        return route.broker, None
    return shared.get("legacy_live_broker"), None


def _held_symbols_unknown_for(
    shared: Mapping[str, Any], is_paper: bool, stack: Mapping[str, Any] | None
) -> bool:
    """True when an entry must fail closed because the held-symbol guard cannot see what it
    needs: before the first reconciliation (every entry, as before), or -- live only -- while
    THIS live stack's broker books have never loaded (held_symbols_unloaded_keys). Paper and
    other live brokers are not blocked by another broker that cannot be read."""
    if not shared.get("held_symbols_loaded"):
        return True
    if is_paper or stack is None:
        return False
    stacks = shared.get("stacks") or {}
    return any(stacks.get(k) is stack for k in shared.get("held_symbols_unloaded_keys") or ())


def _unloaded_brokers_tracked_symbols(shared: Mapping[str, Any]) -> set[str]:
    """Bare symbols the engine itself tracks (OrderMonitor) on live brokers whose books have
    never loaded -- R3 (one live position per symbol across brokers) still sees them while
    that broker cannot be read. Empty when every broker has loaded (the usual case)."""
    stacks = shared.get("stacks") or {}
    out: set[str] = set()
    for k in shared.get("held_symbols_unloaded_keys") or ():
        mon = (stacks.get(k) or {}).get("order_monitor")
        if mon is not None:
            out |= {p.symbol.split(":")[-1] for p in mon.get_monitored()}
    return out


def _live_stack_or_reject(
    shared: dict[str, Any], strategy_name: str, segment: str
) -> tuple[dict[str, Any] | None, str | None]:
    """The live stack a LIVE-resolved signal must use, or (None, reason) -- logged as
    broker_route_rejected -- when its route is unusable or its broker has no stack (e.g. the
    broker is configured but its stack failed to build). Never another broker's stack."""
    broker, reason = _resolve_broker(shared, strategy_name, False)
    stack = None
    if reason is None:
        stack = _pick_stack(shared, False, broker)
        if stack is None:
            reason = f"no live stack for broker {broker or '<primary>'}"
    if reason is not None:
        _log.warning(
            "broker_route_rejected strategy=%s segment=%s reason=%s",
            strategy_name,
            segment,
            reason,
        )
        return None, reason
    return stack, None


def _is_stack_key(key: Any) -> bool:
    return isinstance(key, str) and (key in ("paper", "live") or key.startswith("live:"))


def _pick_stack(
    shared_or_stacks: dict[str, Any], is_paper: bool, broker: str | None = None
) -> dict[str, Any] | None:
    """The execution stack for paper, for `broker`'s live stack, or (live, broker=None)
    the primary live stack -- the legacy (global.json) broker's, `shared["legacy_live_broker"]`,
    when it exists. Accepts `shared` or the stacks dict itself (no legacy broker known then);
    None when the stack does not exist (callers fall back to the legacy shared aliases)."""
    d = shared_or_stacks or {}
    is_stacks = all(_is_stack_key(k) for k in d)
    stacks = d if is_stacks else (d.get("stacks") or {})
    if is_paper:
        key: str | None = stack_key(True)
    elif broker is None:
        key = primary_live_key(stacks, None if is_stacks else d.get("legacy_live_broker"))
    else:
        key = stack_key(False, broker)
    return stacks.get(key) if key is not None else None


def _known_live_brokers() -> list[str]:
    """Registered non-paper broker names; any other exec_broker value is "unknown"."""
    from src.brokers.registry import list_brokers

    return sorted(set(list_brokers()) - {"paper"})


def _reseed_scope(
    key: str, stacks: Mapping[str, Any], legacy_broker: str | None = None
) -> dict[str, Any]:
    """Broker filter kwargs for _reseed_order_monitor / _reseed_pending_entries of stack `key`.

    Paper: {} (paper_trades, unfiltered as before). A live stack reseeds only the `trades` rows
    its own broker holds, so no real position is ever tracked -- and exited -- by two stacks or
    by the wrong broker. Rows without a usable stamp belong to the KITE stack, never to
    whichever stack is primary, because NULL means "kite" everywhere (every row written before
    routing existed was placed through Kite):
      * NULL and unknown values (not a registered broker -- consistent with
        _ensure_live_broker_for_open_positions, which never builds a stack for those);
      * on a DB without the exec_broker column (migration 089 not applied), every row: the
        kite stack falls back to the unfiltered query, any other live stack reseeds nothing.
    With no kite stack those rows are reseeded by NO stack (_report_orphaned_live_rows raises
    a CRITICAL alert). `stacks` is unused except to keep call sites uniform; the legacy bare
    "live" key (old fixtures) is the legacy broker's stack.
    """
    if stack_is_paper(key):
        return {}
    name = stack_broker(key) or legacy_broker or LEGACY_LIVE_BROKER
    if name != LEGACY_LIVE_BROKER:
        return {"broker_name": name}
    return {
        "broker_name": name,
        "known_brokers": _known_live_brokers(),
        "unfiltered_if_no_column": True,
    }


def _monitor_tracking(shared: Mapping[str, Any], tid: str) -> Any:
    """The OrderMonitor (of any stack) that tracks `tid`, or None. Falls back to the legacy
    primary alias shared["order_monitor"] when there are no stacks (callers/tests that are
    not stack-aware). Never assume the primary monitor covers every open position."""
    monitors = [
        s.get("order_monitor")
        for s in (shared.get("stacks") or {}).values()
        if s.get("order_monitor") is not None
    ] or [shared.get("order_monitor")]
    return next((m for m in monitors if m is not None and m.get(tid) is not None), None)


def mode_label(key: str) -> str:
    """CRM `mode_*` dict key for a stack: "paper" or "live" (every live broker shares
    the legacy "live" counters until per-broker CRM keys exist)."""
    return "paper" if stack_is_paper(key) else "live"


def needed_live_brokers(
    routes: Mapping[str, Any],
    seg_paper_mode: Mapping[str, bool],
    legacy_name: str,
    locked: bool,
    any_seg_live: bool,
) -> list[str]:
    """Live brokers to build at boot: none when paper_mode_locked; the legacy (global.json)
    broker when a segment is live or `any_seg_live` (the legacy path needs it); then every
    usable routed broker in registry order. Deduped, legacy first."""
    if locked:
        return []
    out = [legacy_name] if any_seg_live or not all(seg_paper_mode.values()) else []
    for r in routes.values():
        if r.ok and r.broker not in out:
            out.append(r.broker)
    return out


setup_logging("orchestrator", log_dir=str(_LOGS))
_log = get_logger("orchestrator")
_swing_log = get_logger("swing")
_opt_log = get_logger("options")
# All intraday engine activity — the DSL 5-min scan (_intraday_signal_scan_loop),
# the llm_intraday_agent's own signal-side decisions (_evaluate_llm_intraday_signal,
# _llm_intraday_signal_tick, _run_llm_intraday_pass, _llm_intraday_agent_loop) —
# shares this one logger/file (intraday.log), same pattern as _swing_log above.
# Before 2026-09-23 this was split across orchestrator.log (signal/TTL/cap
# decisions, via the generic _log) and strategies.log (the LLM reasoning trail).
_intraday_log = get_logger("intraday")


def _cfg(name: str) -> dict[str, Any]:
    p = _CONFIG / f"{name}.json"
    result: dict[str, Any] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return result


def _signal_ranker_mode(segment: str) -> str:
    """config/learning.json signal_ranker.mode_{segment}, falling back to the
    segment-agnostic signal_ranker.mode, falling back to "off". See
    prompt.md (Learning section) and the learning-engine wiring plan."""
    try:
        _sr = _cfg("learning").get("signal_ranker", {})
        return str(_sr.get(f"mode_{segment}") or _sr.get("mode") or "off")
    except Exception:
        return "off"


def _learning_mode(key: str) -> str:
    """config/learning.json {key}.mode, or "off" if absent/unreadable. Used by
    every single-mode (non-segmented) learning-engine gate — promotion,
    apply_promoted_overrides, expectancy_resume, failure_scorer, strategy_selector,
    universe_adapter, causal_memory."""
    try:
        return str(_cfg("learning").get(key, {}).get("mode") or "off")
    except Exception:
        return "off"


# Index universe → cached constituent-list file (data/*.json), written by
# src/market_data/ohlcv_downloader.py's _fetch_index_symbols(). NIFTY50 has no
# cache file — it's a small hardcoded list (ohlcv_downloader.NIFTY50).
_INDEX_UNIVERSE_FILES: dict[str, str] = {
    "NIFTY100": "nifty100_symbols.json",
    "NIFTY200": "nifty200_symbols.json",
    "NIFTY500": "nifty500_symbols.json",
}


_INR_PER_CRORE = 1e7


def _apply_market_cap_limit(
    symbols: list[str], caps_inr: dict[str, float], max_cap_crores: float
) -> tuple[list[str], int]:
    """Drop symbols whose market cap exceeds the limit; symbols with no known cap
    are kept (fail-open — fail-closed would silently trade nothing whenever the
    fundamentals feed is off). Returns (kept, unknown_cap_count)."""
    kept: list[str] = []
    unknown = 0
    for s in symbols:
        cap = caps_inr.get(s)
        if cap is None:
            unknown += 1
            kept.append(s)
        elif cap / _INR_PER_CRORE <= max_cap_crores:
            kept.append(s)
    return kept, unknown


def _sync_scan_alert(shared: dict[str, Any], source: str, problem: tuple[str, str] | None) -> None:
    """Make the dashboard Alerts tab reflect one scan condition: raise (tier, message)
    when `problem` is set — once per unchanged condition, not once per scan — and
    resolve it once `problem` is None. Never raises: a failure to alert is logged."""
    mgr = shared.get("alert_manager")
    if mgr is None:
        if problem is not None:
            _swing_log.error(
                "scan_alert_unavailable source=%s msg=%s — alert_manager not wired",
                source,
                problem[1],
            )
        return
    try:
        from src.infrastructure.alert_system import AlertTier

        active = [a for a in mgr.active() if a.source == source]
        if problem is None:
            if active:
                mgr.resolve_by_source(source)
            return
        tier = AlertTier(problem[0])
        if any(a.tier == tier and a.message == problem[1] for a in active):
            return
        if active:  # condition changed (tier/wording/counts) — replace, don't stack
            mgr.resolve_by_source(source)
        mgr.raise_alert(tier, problem[1], source=source)
    except Exception as exc:
        _swing_log.error("scan_alert_failed source=%s err=%s: %s", source, type(exc).__name__, exc)


def _new_scan_funnel() -> dict[str, Any]:
    return {
        "evaluated": 0,
        "no_data": 0,
        "eval_err": 0,
        "explain_err": 0,
        "first_err": None,
        "order": [],
        "alone": {},
        "cumulative": {},
        "all_pass": 0,
    }


def _funnel_record(fun: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """Fold one symbol's per-condition results into the funnel: how many symbols each
    condition passes on its own, and how many survive all conditions up to it (spec order)."""
    alive = True
    for r in rows:
        n = r["name"]
        if n not in fun["alone"]:
            fun["order"].append(n)
            fun["alone"][n] = 0
            fun["cumulative"][n] = 0
        fun["alone"][n] += int(r["passed"])
        alive = alive and r["passed"]
        fun["cumulative"][n] += int(alive)
    if rows and alive:
        fun["all_pass"] += 1


def _fmt_conditions(rows: list[dict[str, Any]]) -> str:
    parts = []
    for r in rows:
        v = r["value"]
        found = f"{v:.2f}" if isinstance(v, int | float) else ("-" if v is None else str(v))
        rule = ",".join(f"{k}={val}" for k, val in r["rule"].items())
        parts.append(f"{r['name']}={'PASS' if r['passed'] else 'FAIL'}[found={found} rule={rule}]")
    return " ".join(parts)


def _log_scan_funnel(
    shared: dict[str, Any], strategy: str, universe_n: int, fun: dict[str, Any]
) -> None:
    """One INFO line per strategy per scan (what was checked, what passed), and the
    Alerts-tab entries for conditions that mean the strategy was blind or degraded."""
    ev = fun["evaluated"]
    stages = " ".join(
        f"{n}:{fun['alone'][n]}/{ev}(cum={fun['cumulative'][n]})" for n in fun["order"]
    )
    _swing_log.info(
        "swing_scan_funnel strategy=%s universe=%d evaluated=%d no_data=%d eval_err=%d "
        "all_conditions_pass=%d | %s",
        strategy,
        universe_n,
        ev,
        fun["no_data"],
        fun["eval_err"],
        fun["all_pass"],
        stages or "no conditions evaluated",
    )

    src = f"swing_scan:{strategy}"
    err_problem = None
    if fun["eval_err"]:
        tier = "CRITICAL" if ev == 0 or fun["eval_err"] >= 0.10 * max(universe_n, 1) else "WARNING"
        err_problem = (
            tier,
            f"{strategy}: {fun['eval_err']}/{universe_n} symbols failed evaluation "
            f"(first: {fun['first_err']})",
        )
    _sync_scan_alert(shared, f"{src}:eval_err", err_problem)

    nodata_problem = None
    if universe_n and fun["no_data"] >= 0.20 * universe_n:
        tier = "CRITICAL" if ev == 0 else "WARNING"
        nodata_problem = (
            tier,
            f"{strategy}: no usable daily data for {fun['no_data']}/{universe_n} symbols "
            f"({ev} evaluated)",
        )
    _sync_scan_alert(shared, f"{src}:no_data", nodata_problem)

    explain_problem = None
    if fun["explain_err"]:
        explain_problem = (
            "WARNING",
            f"{strategy}: condition diagnostics failed for {fun['explain_err']} symbols "
            "(signals unaffected; see swing_explain_err in the log)",
        )
    _sync_scan_alert(shared, f"{src}:explain_err", explain_problem)


def _resolve_index_universe(index_name: str | None) -> list[str]:
    """Resolve an index name (e.g. "NIFTY500") to its bare-symbol list.

    Falls back to NIFTY50 when the name is unset, unrecognized, or its cache
    file hasn't been fetched yet (run ohlcv_downloader.py --universe to seed
    nifty100/200/500 caches) — a strategy should degrade to a smaller universe
    rather than silently trade zero symbols.
    """
    from src.market_data.ohlcv_downloader import NIFTY50 as _N50

    name = (index_name or "NIFTY50").upper()
    fname = _INDEX_UNIVERSE_FILES.get(name)
    if fname is None:
        return _N50
    path = _BASE / "data" / fname
    if path.exists():
        try:
            syms: list[str] = json.loads(path.read_text(encoding="utf-8"))
            if syms:
                return syms
        except (OSError, json.JSONDecodeError):
            pass
    _log.warning(
        "index_universe_cache_missing index=%s file=%s — falling back to NIFTY50", name, fname
    )
    return _N50


_NSE_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities"
_BSE_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"

_SECTOR_STOCKS: dict[str, list[str]] = {
    "BANKING": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "INDUSINDBK"],
    "IT": ["TCS", "INFY", "WIPRO", "TECHM", "HCLTECH"],
    "PHARMA": ["SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB"],
    "AUTO": ["MARUTI", "TATAMOTORS", "M&M", "BAJAJ-AUTO", "EICHERMOT"],
    "FMCG": ["HINDUNILVR", "ITC", "NESTLEIND"],
    "METAL": ["TATASTEEL", "JSWSTEEL", "HINDALCO"],
    "OIL_GAS": ["RELIANCE", "ONGC", "BPCL"],
}

# symbol → sector, reversed from _SECTOR_STOCKS for O(1) lookup.
_SYMBOL_TO_SECTOR: dict[str, str] = {
    sym: sector for sector, syms in _SECTOR_STOCKS.items() for sym in syms
}

# Caches the sector_gate_enabled flag from operator.json so _resolve_sector_declining
# doesn't re-read + re-parse it on every signal. Cleared by tests via
# orchestrator._sector_gate_cache.clear().
_sector_gate_cache: dict[str, object] = {}


def _resolve_sector_declining(symbol: str) -> bool:
    """True only when `symbol`'s sector is currently classified DECLINING by
    SectorRotationTracker (src/intelligence/sector_rotation.py), read from
    logs/sector_rotation.json.

    Fail-open: an unmapped symbol, a disabled gate (operator.json
    sector_gate_enabled=false), or a missing/unreadable log file all resolve
    to False. This gate only ever blocks on a POSITIVE, current DECLINING
    read — never on the absence of data (matches the fail-open philosophy
    RS_PERCENTILE already uses in compiler.py).
    """
    try:
        if "gate_enabled" not in _sector_gate_cache:
            _sector_gate_cache["gate_enabled"] = bool(
                _cfg("operator").get("sector_gate_enabled", True)
            )
        if not _sector_gate_cache["gate_enabled"]:
            return False

        sym_clean = symbol.split(":")[-1]
        sector = _SYMBOL_TO_SECTOR.get(sym_clean)
        if sector is None:
            return False

        log_path = _LOGS / "sector_rotation.json"
        if not log_path.exists():
            return False
        payload = json.loads(log_path.read_text(encoding="utf-8"))
        for record in payload.get("sectors", []):
            if record.get("name") == sector:
                return bool(record.get("status") == "DECLINING")
        return False
    except Exception:
        return False


def _resolve_sector(symbol: str) -> str:
    """Sector name for `symbol` via _SYMBOL_TO_SECTOR, "" if unmapped — feeds
    CRMSignal.sector for CRM's sector exposure cap (G7)."""
    return _SYMBOL_TO_SECTOR.get(symbol.split(":")[-1], "")


def _position_open_risk(p: Any) -> float:
    """Rupees still at risk on an open position: stop-to-fill distance on the LOSING
    side only. A stop already moved past entry (breakeven/profit lock/trail) risks
    nothing, so it counts 0 — abs() would count locked profit as risk."""
    fill, sl, qty = float(p.fill_price), float(p.current_sl), float(p.quantity)
    is_short = str(p.direction).upper() in ("SELL", "SHORT")
    per_share = (sl - fill) if is_short else (fill - sl)
    return max(0.0, per_share) * qty


def _update_open_risk_and_sector_exposure(crm_ctx: Any, positions: list[Any]) -> None:
    """Recompute CRMContext.open_risk_rupees / sector_open_exposure_rupees from
    the currently-monitored open positions (spec §6.3/§6.4 portfolio-level caps).
    Runs every cycle alongside open_exposure_rupees — same data, same cadence.
    """
    try:
        crm_ctx.open_risk_rupees = sum(_position_open_risk(p) for p in positions)
        sector_totals: dict[str, float] = {}
        for p in positions:
            sector = _resolve_sector(p.symbol)
            if not sector:
                continue
            sector_totals[sector] = sector_totals.get(sector, 0.0) + float(p.quantity) * float(
                p.fill_price
            )
        crm_ctx.sector_open_exposure_rupees = sector_totals
    except Exception:
        pass


async def _update_regime_phase(
    db_pool: Any, strategy_name: str, symbol: str, regime_ok: bool
) -> None:
    """Upsert strategy_regime_phase (spec §8.2) — sets a fresh phase_started_at
    whenever regime_ok flips False→True (a new in-regime phase begins)."""
    if db_pool is None:
        return
    try:
        await db_pool.execute(
            """
            INSERT INTO strategy_regime_phase
                   (strategy_name, symbol, regime_ok, phase_started_at, updated_at)
            VALUES ($1, $2, $3, CASE WHEN $3 THEN now() ELSE NULL END, now())
            ON CONFLICT (strategy_name, symbol) DO UPDATE SET
                regime_ok = EXCLUDED.regime_ok,
                phase_started_at = CASE
                    WHEN EXCLUDED.regime_ok AND NOT strategy_regime_phase.regime_ok THEN now()
                    WHEN EXCLUDED.regime_ok THEN strategy_regime_phase.phase_started_at
                    ELSE NULL
                END,
                updated_at = now()
            """,
            strategy_name,
            symbol,
            regime_ok,
        )
    except Exception as exc:
        _log.warning(
            "regime_phase_upsert_failed strategy=%s sym=%s err=%s",
            strategy_name,
            symbol,
            str(exc)[:160],
        )


async def _regime_phase_attempts_for(
    db_pool: Any, strategy_name: str, symbol: str
) -> tuple[int, bool]:
    """(attempts, regime_ok) for strategy+symbol — attempts = count of losing
    closed trades (any strategy-mode's paper_trades/trades, Pine's `lastPnl<0`
    rule) since the current in-regime phase began. Always 0 while not in-regime."""
    if db_pool is None:
        return 0, True
    try:
        row = await db_pool.fetchrow(
            "SELECT regime_ok, phase_started_at FROM strategy_regime_phase "
            "WHERE strategy_name = $1 AND symbol = $2",
            strategy_name,
            symbol,
        )
        if row is None:
            return 0, True
        if not row["regime_ok"] or row["phase_started_at"] is None:
            return 0, bool(row["regime_ok"])
        count = await db_pool.fetchval(
            """
            SELECT COUNT(*) FROM (
                SELECT pnl_realized FROM paper_trades
                 WHERE strategy_name = $1 AND symbol = $2 AND order_state = 'CLOSED'
                   AND exit_timestamp >= $3 AND pnl_realized < 0
                UNION ALL
                SELECT pnl_realized FROM trades
                 WHERE strategy_name = $1 AND symbol = $2 AND order_state = 'CLOSED'
                   AND exit_timestamp >= $3 AND pnl_realized < 0
            ) losses
            """,
            strategy_name,
            symbol,
            row["phase_started_at"],
        )
        return int(count or 0), True
    except Exception as exc:
        _log.warning(
            "regime_phase_attempts_failed strategy=%s sym=%s err=%s",
            strategy_name,
            symbol,
            str(exc)[:160],
        )
        return 0, True


class _InMemoryRedis:
    """Fallback when redis.asyncio is unavailable — satisfies Deduplicator + ImpactRouter Protocols."""

    def __init__(self) -> None:
        import time as _time

        self._data: dict[str, tuple[str, float]] = {}
        self._time = _time

    async def exists(self, key: str) -> bool:
        entry = self._data.get(key)
        if entry is None:
            return False
        if self._time.time() > entry[1]:
            del self._data[key]
            return False
        return True

    async def set(self, key: str, value: str, ex: int) -> None:
        self._data[key] = (value, self._time.time() + ex)

    async def get(self, key: str) -> str | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if self._time.time() > entry[1]:
            del self._data[key]
            return None
        return entry[0]

    async def setex(self, key: str, ttl: int, value: str) -> None:
        await self.set(key, value, ex=ttl)


class _NullLLM:
    """Always raises so NewsClassifier falls back to keyword classification."""

    async def complete(self, prompt: str) -> str:
        raise RuntimeError("no LLM configured")


class _NullNewsRepo:
    """No-op DB sink — JSON log is the real output."""

    async def insert(self, record: dict[str, Any]) -> None:
        pass


class _RealNewsRepo:
    """Adapter: bridges NewsStateStore dict protocol → NewsRepository named params."""

    def __init__(self, repo: Any) -> None:
        self._r = repo

    async def insert(self, record: dict[str, Any]) -> None:
        from decimal import Decimal as _D
        from decimal import InvalidOperation

        _sent_raw = str(record.get("sentiment", "NEUTRAL")).upper()
        _sent_db = {"BULLISH": "POSITIVE", "BEARISH": "NEGATIVE"}.get(_sent_raw, _sent_raw)
        if _sent_db not in ("POSITIVE", "NEGATIVE", "NEUTRAL"):
            _sent_db = "NEUTRAL"
        # magnitude is "HIGH"/"MEDIUM"/"LOW" from classifier — map to numeric
        _MAG_MAP = {"HIGH": "0.80", "MEDIUM": "0.50", "LOW": "0.20"}
        _mag_raw = str(record.get("magnitude", "") or "")
        _mag_str = _MAG_MAP.get(_mag_raw.upper(), _mag_raw)
        try:
            _mag = _D(_mag_str) if _mag_str else None
        except InvalidOperation:
            _mag = None
        try:
            _conf = _D(str(record["confidence"])) if record.get("confidence") else None
        except InvalidOperation:
            _conf = None
        await self._r.insert(
            headline=record.get("headline", ""),
            source=record.get("source", ""),
            event_type=str(record.get("event_type", "UNKNOWN")),
            sentiment=_sent_db,
            symbol=record.get("symbol"),
            magnitude=_mag,
            trading_action=record.get("trading_action"),
            avoid_duration_minutes=record.get("avoid_duration_minutes"),
            confidence=_conf,
            reasoning=record.get("reasoning"),
            affected_symbols=record.get("affected_symbols"),
        )


_REDIS_CLIENT: Any = None
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def _make_redis_client() -> Any:
    """Process-wide Redis cache client (one shared instance), or an in-memory stand-in.

    URL comes from the REDIS_URL env var (default redis://localhost:6379/0). Redis is an
    OPTIONAL cache (news dedup/impact/token budget, options chain cache, regime key): if the
    ``redis`` package is missing the in-memory stand-in is used, and if the server is
    unreachable at call time each operation degrades to the same in-memory stand-in
    (one warning) instead of raising into the news/options loops.
    """
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    _REDIS_CLIENT = _build_redis_client()
    return _REDIS_CLIENT


def _build_redis_client() -> Any:
    try:
        import os as _os

        import redis.asyncio as _aioredis  # type: ignore[import-untyped]

        url = _os.environ.get("REDIS_URL") or _DEFAULT_REDIS_URL
        raw = _aioredis.Redis.from_url(url, decode_responses=True)
    except Exception:
        return _InMemoryRedis()

    fallback = _InMemoryRedis()
    warned: list[bool] = [False]

    def _degrade(exc: Exception) -> None:
        if not warned[0]:
            warned[0] = True
            _log.warning("redis_unavailable err=%s -- using in-memory cache fallback", exc)

    class _Adapter:
        def __init__(self, r: Any) -> None:
            self._r = r

        async def exists(self, key: str) -> bool:
            try:
                return bool(await self._r.exists(key))
            except Exception as exc:  # noqa: BLE001 — cache must never break callers
                _degrade(exc)
                return await fallback.exists(key)

        async def set(self, key: str, value: str, ex: int) -> None:
            try:
                await self._r.set(key, value, ex=ex)
            except Exception as exc:  # noqa: BLE001
                _degrade(exc)
                await fallback.set(key, value, ex=ex)

        async def get(self, key: str) -> str | None:
            try:
                return await self._r.get(key)  # type: ignore[no-any-return]
            except Exception as exc:  # noqa: BLE001
                _degrade(exc)
                return await fallback.get(key)

        async def setex(self, key: str, ttl: int, value: str) -> None:
            try:
                await self._r.setex(key, ttl, value)
            except Exception as exc:  # noqa: BLE001
                _degrade(exc)
                await fallback.setex(key, ttl, value)

    return _Adapter(raw)


def _load_ohlcv_prices() -> tuple[Any | None, Any | None]:
    """Load day OHLCV parquet files → (prices_df, nifty_series). Returns (None, None) if empty."""
    try:
        import pandas as _pd

        ohlcv_dir = _BASE / "data" / "ohlcv" / "day"
        if not ohlcv_dir.exists():
            return None, None
        frames: dict[str, _pd.Series[float]] = {}
        for pq in ohlcv_dir.glob("*.parquet"):
            try:
                df = _pd.read_parquet(pq)
                if "close" in df.columns and len(df) >= 20:
                    frames[pq.stem] = df["close"].astype(float)
            except Exception:
                continue
        if not frames:
            return None, None
        prices_df = _pd.DataFrame(frames)
        prices_df.sort_index(inplace=True)
        if "NIFTY50" in prices_df.columns:
            nifty = prices_df["NIFTY50"]
        elif "NIFTY 50" in prices_df.columns:
            nifty = prices_df["NIFTY 50"]
        else:
            nifty = prices_df.mean(axis=1)
        return prices_df, nifty
    except Exception:
        return None, None


async def _load_ohlcv_prices_db(shared: dict[str, Any]) -> tuple[Any | None, Any | None]:
    """DB-first OHLCV loader. Falls back to Parquet if DB unavailable or empty."""
    import pandas as _pd

    _repo = shared.get("ohlcv_repo")
    if _repo is not None:
        try:
            watched: list[str] = list(shared.get("watchlist", []) or [])
            if not watched:
                from src.market_data.ohlcv_downloader import NIFTY50

                watched = NIFTY50
            frames: dict[str, _pd.Series[float]] = {}
            for sym in watched:
                try:
                    df = await _repo.fetch_dataframe(sym, "1d", 400)
                    if not df.empty and "close" in df.columns and len(df) >= 20:
                        frames[sym] = df["close"].astype(float)
                except Exception:
                    continue
            if frames:
                prices_df = _pd.DataFrame(frames)
                prices_df.sort_index(inplace=True)
                # Fetch the benchmark explicitly rather than hoping it's in
                # `watched` — it wasn't: `watched` is the stock watchlist, and
                # this used to look for a "NIFTY50" column (no space) that
                # never existed (the real symbol is "NIFTY 50"), so `.get()`
                # silently fell through to prices_df.mean(axis=1) — a
                # mean-of-watchlist pseudo-index — on every call, never real
                # NIFTY 50. That's what was actually feeding the RS ranking.
                try:
                    _nifty_df = await _repo.fetch_dataframe("NIFTY 50", "1d", 400)
                    nifty = (
                        _nifty_df["close"].astype(float)
                        if _nifty_df is not None
                        and not _nifty_df.empty
                        and "close" in _nifty_df.columns
                        else prices_df.mean(axis=1)
                    )
                except Exception:
                    nifty = prices_df.mean(axis=1)
                return prices_df, nifty
        except Exception:
            pass
    # Parquet fallback
    return _load_ohlcv_prices()


def _intraday_benchmark_symbols(strategy_reg: Any) -> set[str]:
    """Benchmark index symbols (e.g. "NIFTY 50") needed by enabled intraday
    strategies' market-direction gate. Their 5m candles must be ingested into the
    OHLCV DB so both the backtest and the live scan can read them via ohlcv_repo.
    """
    syms: set[str] = set()
    if strategy_reg is None:
        return syms
    try:
        for c in strategy_reg.all_enabled():
            tf = (getattr(c.spec.meta, "timeframe", "") or "").upper()
            if tf not in ("5MIN", "15MIN", "5M", "15M"):
                continue
            mf = getattr(c.spec, "market_filter", None)
            if mf is not None and getattr(mf, "enabled", False) and getattr(mf, "symbol", None):
                syms.add(mf.symbol)
    except Exception:
        pass
    return syms


# Intraday OHLCV timeframes. 5m is owned by the signal scan (it fetches the
# just-closed 5m bar itself at each candle boundary, so an ORB entry fires seconds
# after the breakout bar closes); this ingest loop maintains only 10m/15m. Splitting
# ownership keeps total broker calls flat — no double 5m fetch, no cross-loop race.
# Live 10m/15m ingest is OFF (2026-09-21): it cost 994 Kite historical calls per pass
# (21 min at 0.8 calls/s) against a 3 calls/s cap, starving the 5m scan that actually
# drives entries. Nothing live reads 10m or 15m; the EOD download
# (_TIMEFRAMES) already refreshes both.
_INGEST_TFS: list[tuple[str, str]] = []
_SCAN_TFS: list[tuple[str, str]] = [("5minute", "5m")]
# Seconds to wait AFTER a bar boundary before fetching, so the broker has finalized
# the just-closed candle. Ingest leads (short settle); the scan follows.
_INGEST_SETTLE_S = 2.0
_SCAN_SETTLE_S = 8.0
# Max time one pre-scan stage may take: one 5m bar. A stage that outlasts the bar
# it was meant to serve has already missed it, so abandon the cycle instead.
_INTRADAY_STAGE_TIMEOUT_S = 300


def _seconds_to_next_bar(now: Any, bar_min: int = 5, settle_s: float = _SCAN_SETTLE_S) -> float:
    """Seconds from `now` until the next `bar_min` boundary, plus `settle_s`.

    Aligning the intraday loops to candle-close boundaries (…09:25:00, 09:30:00…)
    is what makes an ORB entry fire seconds after the breakout bar closes instead
    of up to two unsynchronized 5-minute polls later.
    """
    from datetime import timedelta

    base = now.replace(second=0, microsecond=0)
    delta_min = bar_min - (now.minute % bar_min)
    nb = base + timedelta(minutes=delta_min)
    return float(max(1.0, (nb - now).total_seconds() + settle_s))


async def _refresh_intraday_ohlcv(
    broker: Any,
    shared: dict[str, Any],
    timeframes: list[tuple[str, str]],
    now: Any,
) -> None:
    """Delta-append the given intraday timeframes for the watchlist (+ benchmark)
    into the OHLCV DB. Idempotent (upsert); called from both the ingest loop and
    the scan loop's cycle-start freshness step.

    Fetches concurrently, bounded by equity.screener.max_concurrent_fetches (the
    same limit already used by the bulk historical downloader for Kite calls —
    scripts/../ohlcv_downloader.py — not a new number invented for this). Was
    fully serial before 2026-09-21: one broker.get_ohlcv() round-trip at a time
    across the whole watchlist, which measured as the actual cause of the
    intraday scan loop taking 10-44 min per cycle instead of the intended ~5.
    Worst right after a restart: the DB has no fresh candle for anyone yet, so
    every symbol needs a live Kite fetch on the very first cycle, and serial
    awaiting turns ~500 round-trips into ~500x single-request latency instead
    of ~500/concurrency.

    The concurrency semaphore is cached on `shared`, not created fresh here,
    because this function is called from two independently-scheduled loops —
    _ohlcv_intraday_loop (10m/15m) and _intraday_signal_scan_loop (5m) — that
    run concurrently with no mutual gating. A fresh Semaphore(9) per call let
    both loops each run 9 fetches at once (up to 18 combined) against one
    shared KiteAdapter rate limiter; caching the instance here caps the real
    combined budget at 9 (2026-09-21: this alone didn't fully explain a
    measured 19.4-min cycle — see the sem_wait_s/kite_call_s fields logged
    below, added to gather direct evidence rather than guess further)."""
    import time as _time_mod
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from src.market_data.ohlcv_downloader import NIFTY50, _df_to_rows

    _IST = ZoneInfo("Asia/Kolkata")
    _repo = shared.get("ohlcv_repo")
    if _repo is None:
        return
    # Bulk-resolve instrument tokens once per broker instance (idempotent, see
    # KiteAdapter.load_instrument_tokens) so the per-symbol get_ohlcv() calls
    # below skip Kite's 1 req/sec quote() lookup for a token that never changes
    # intraday — this loop's own serial per-symbol quote() calls were the whole
    # cost of a ~34-minute cycle over ~500 watchlist symbols (2026-09-15).
    # Not every broker double used in tests implements this — optional,
    # never required for correctness.
    _load_tokens = getattr(broker, "load_instrument_tokens", None)
    if _load_tokens is not None:
        try:
            await _load_tokens()
        except Exception:
            pass
    symbols: list[str] = list(shared.get("watchlist", []) or NIFTY50)
    _bench = _intraday_benchmark_symbols(shared.get("strategy_registry"))
    _concurrency = int(
        _cfg("global").get("equity", {}).get("screener", {}).get("max_concurrent_fetches", 9)
    )
    _sem = shared.get("_ohlcv_fetch_sem")
    if _sem is None:
        _sem = asyncio.Semaphore(_concurrency)
        shared["_ohlcv_fetch_sem"] = _sem
    _timing = {"sem_wait_s": 0.0, "kite_call_s": 0.0, "calls": 0}

    async def _refresh_one(sym: str, kite_tf: str, tf_key: str) -> None:
        _wait_t0 = _time_mod.time()
        async with _sem:
            _timing["sem_wait_s"] += _time_mod.time() - _wait_t0
            try:
                last_ts = await _repo.latest_ts(sym, tf_key)
                # Skip the Kite fetch only when the newest stored candle is younger
                # than a bar — otherwise always refresh so the just-closed bar lands.
                if last_ts is not None:
                    _lts = last_ts if last_ts.tzinfo else last_ts.replace(tzinfo=_IST)
                    if _lts >= now - timedelta(minutes=4):
                        return
                _call_t0 = _time_mod.time()
                _fetch = getattr(broker, "get_ohlcv_with_source", None)
                if _fetch is not None:
                    df, _src = await _fetch(f"NSE:{sym}", kite_tf, 2)
                else:
                    df, _src = await broker.get_ohlcv(f"NSE:{sym}", kite_tf, 2), None
                _timing["kite_call_s"] += _time_mod.time() - _call_t0
                _timing["calls"] += 1
                if df is None or df.empty:
                    return
                df.columns = [c.lower() for c in df.columns]
                rows = _df_to_rows(df)
                # Kite's historical_data(to_date=now()) includes the still-forming
                # current candle as the last row during market hours — its OHLC keeps
                # changing until the bar closes. Upserting it let evaluate_candle()
                # score breakouts/entry_candle stops off a live, not-yet-final print
                # (2026-08-17: ITI/ABCAPITAL both entered on an intrabar spike that
                # reverted before the bar closed, never a confirmed breakout). Only
                # trust bars whose full duration has elapsed.
                _bar_min = int("".join(c for c in kite_tf if c.isdigit()) or 0)
                if _bar_min:
                    rows = [r for r in rows if r["ts"] + timedelta(minutes=_bar_min) <= now]
                if last_ts is not None:
                    rows = [r for r in rows if r["ts"] > last_ts]
                if rows:
                    await _repo.upsert_candles(sym, tf_key, rows, source=_src)
            except Exception as exc:
                # 2026-09-21: was a bare silent skip -- with per-symbol calls now
                # timeout-bounded (see KiteAdapter.get_ohlcv), a failure here is
                # rare enough in normal operation that logging every one is not
                # noise, and a systemic failure (e.g. broker session expired)
                # will show up as a flood here instead of only as a slow/stuck
                # cycle discovered by hand.
                _log.warning("intraday_ohlcv_refresh_failed sym=%s tf=%s err=%s", sym, tf_key, exc)
                return

    _all_syms = list(symbols) + [b for b in _bench if b not in symbols]
    _cycle_t0 = _time_mod.time()
    await asyncio.gather(
        *[_refresh_one(sym, kite_tf, tf_key) for sym in _all_syms for kite_tf, tf_key in timeframes]
    )
    _log.info(
        "intraday_ohlcv_refresh_timing",
        extra={
            "ctx": {
                "timeframes": [tf_key for _, tf_key in timeframes],
                "symbols": len(_all_syms),
                "total_s": round(_time_mod.time() - _cycle_t0, 1),
                "kite_calls": _timing["calls"],
                "sem_wait_s_sum": round(_timing["sem_wait_s"], 1),
                "kite_call_s_sum": round(_timing["kite_call_s"], 1),
                "kite_call_s_avg": round(_timing["kite_call_s"] / _timing["calls"], 2)
                if _timing["calls"]
                else 0.0,
            }
        },
    )


async def _ohlcv_intraday_loop(
    broker: Any,
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Append intraday 10m/15m OHLCV deltas to the DB at each candle boundary during
    market hours. 5m is owned by the signal scan (see _intraday_signal_scan_loop)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")

    while not stop.is_set():
        now = datetime.now(_IST)
        if (
            _INGEST_TFS
            and is_trading_day(now.date())
            and _sessions.in_regular_minutes(now.hour * 60 + now.minute)
        ):
            await _refresh_intraday_ohlcv(broker, shared, _INGEST_TFS, now)
        await asyncio.sleep(_seconds_to_next_bar(datetime.now(_IST), settle_s=_INGEST_SETTLE_S))


async def _startup_ohlcv_backfill(broker: Any, shared: dict[str, Any]) -> None:
    """On every launch, delta-update each watchlist symbol's OHLCV into the DB
    BEFORE the engine starts — any time of day, today's completed session
    included. Reuses the engine's already-authenticated broker + repo. Fails
    LOUDLY (never silently) so stale data cannot pass unnoticed.

    This is the missing piece behind 'I ran the launcher but the DB is still on
    yesterday': the live ingest loop only runs 09:15–15:30, so an after-hours
    launch previously ingested nothing and no startup backfill existed.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.market_data.ohlcv_downloader import (
        _TIMEFRAMES,
        _download_symbol_timeframe,
        _fetch_nifty500_symbols,
    )

    _IST = ZoneInfo("Asia/Kolkata")

    # Downloads run ONLY after the market close. On a weekday, skip any launch
    # before 15:30 — this covers both pre-market (start of the day) and the open
    # session. Pre-market data is already current from yesterday's EOD download;
    # downloading here wastes Kite quota and delays engine startup. During the
    # open session live ticks handle intraday data. Genuine after-close launches
    # (>15:30) still backfill in case the 16:00 EOD loop didn't run.
    _now = datetime.now(_IST)
    _mins = _now.hour * 60 + _now.minute
    _MARKET_CLOSE = _sessions.minutes(_sessions.regular_close())
    if _now.weekday() < 5 and _mins < _MARKET_CLOSE:
        _log.info(
            "startup_ohlcv_backfill: before the 15:30 close (pre-market/open) — "
            "skipping historical download. Downloads run only after market hours "
            "(EOD loop at 16:00; live ticks handle intraday)."
        )
        return

    # Skip if a successful backfill ran within the last 20 hours (same-day restarts).
    # EOD loop at 16:00 and the intraday loop handle incremental updates independently.
    _DONE_FLAG = _BASE / "data" / ".backfill_done"
    if _DONE_FLAG.exists():
        try:
            _age_h = (
                datetime.now(_IST) - datetime.fromtimestamp(_DONE_FLAG.stat().st_mtime, _IST)
            ).total_seconds() / 3600
            if _age_h < 20:
                _log.info(
                    "startup_ohlcv_backfill: last backfill was %.1fh ago — skipping (data is current)",
                    _age_h,
                )
                return
        except Exception:
            pass  # can't stat flag → fall through and re-download

    repo = shared.get("ohlcv_repo")
    if repo is None:
        _log.error(
            "startup_ohlcv_backfill: DB/ohlcv_repo unavailable — SKIPPING. "
            "Strategies will run on stale or no data."
        )
        return

    # Outside market hours — download full Nifty500, not just the watchlist
    symbols: list[str] = list(shared.get("watchlist", []))
    nifty500 = _fetch_nifty500_symbols()
    # Nifty500 first, then any watchlist extras; deduped preserving order
    _seen: set[str] = set()
    backfill_syms: list[str] = []
    for _s in nifty500 + symbols:
        if _s not in _seen:
            _seen.add(_s)
            backfill_syms.append(_s)

    # Benchmark index(es) for the market-direction gate
    _bench = _intraday_benchmark_symbols(shared.get("strategy_registry"))
    for _b in _bench:
        if _b not in _seen:
            _seen.add(_b)
            backfill_syms.append(_b)
    sem = asyncio.Semaphore(5)
    # Outside market hours: treat data as current if updated within the last 23 h.
    # This means only one real download per day regardless of how many times
    # the launcher is restarted (data already in DB = skip).
    _STALE_THRESHOLD = 23 * 60
    tasks = [
        _download_symbol_timeframe(
            broker,
            repo,
            sym,
            kite_interval,
            tf_key,
            bootstrap,
            is_intraday,
            sem,
            stale_threshold_minutes=_STALE_THRESHOLD,
        )
        for sym in backfill_syms
        for kite_interval, tf_key, bootstrap, is_intraday in _TIMEFRAMES
    ]
    _log.info(
        "startup_ohlcv_backfill: delta-updating %d symbols × %d timeframes…",
        len(backfill_syms),
        len(_TIMEFRAMES),
    )
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if r is True)
    failed = len(results) - ok

    # Coverage proof: how stale is the 5m data after backfill?
    stale_min = None
    try:
        latest = await repo.latest_ts(symbols[0], "5m")
        if latest is not None:
            latest_ist = latest.astimezone(_IST) if latest.tzinfo else latest.replace(tzinfo=_IST)
            stale_min = (datetime.now(_IST) - latest_ist).total_seconds() / 60.0
            _log.info(
                "startup_ohlcv_backfill: %d ok / %d failed · %s 5m latest=%s (%.0f min old)",
                ok,
                failed,
                symbols[0],
                latest_ist.strftime("%Y-%m-%d %H:%M"),
                stale_min,
            )
    except Exception:
        _log.info("startup_ohlcv_backfill: %d ok / %d failed", ok, failed)

    if failed > ok:
        _log.error(
            "startup_ohlcv_backfill: MAJORITY FAILED (%d/%d) — likely expired Kite "
            "token or API outage. Intraday strategies will trade on STALE data. "
            "Re-auth and restart.",
            failed,
            len(results),
        )
    else:
        # Mark backfill complete so the next restart skips re-downloading.
        try:
            _DONE_FLAG.parent.mkdir(parents=True, exist_ok=True)
            _DONE_FLAG.touch()
        except Exception:
            pass
    if stale_min is not None and stale_min > 90:
        _log.warning(
            "startup_ohlcv_backfill: 5m data still %.0f min stale after backfill — "
            "check Kite session / market calendar.",
            stale_min,
        )


async def _eod_ohlcv_download_loop(
    broker: Any,
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Once per weekday shortly after the 15:30 close, delta-download the FULL
    Nifty500 universe × all timeframes into the DB — so the entire 500-symbol
    database stays current, not just the trading watchlist. Fails loudly."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.market_data.ohlcv_downloader import (
        _TIMEFRAMES,
        _download_symbol_timeframe,
        _fetch_nifty500_symbols,
    )

    _IST = ZoneInfo("Asia/Kolkata")
    _last_run_date = None
    while not stop.is_set():
        now = datetime.now(_IST)
        if (
            now.weekday() < 5
            and now.hour == _sessions.schedule_time("daily_ohlcv_download").hour
            and _last_run_date != now.date()
        ):
            repo = shared.get("ohlcv_repo")
            if repo is not None:
                try:
                    symbols = _fetch_nifty500_symbols()
                    sem = asyncio.Semaphore(5)
                    tasks = [
                        _download_symbol_timeframe(broker, repo, sym, ki, tk, bs, intr, sem)
                        for sym in symbols
                        for ki, tk, bs, intr in _TIMEFRAMES
                    ]
                    _log.info(
                        "eod_ohlcv_download: %d symbols × %d timeframes…",
                        len(symbols),
                        len(_TIMEFRAMES),
                    )
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    ok = sum(1 for r in results if r is True)
                    _log.info("eod_ohlcv_download: %d/%d ok", ok, len(results))
                    if ok < len(results) // 2:
                        _log.error(
                            "eod_ohlcv_download: MAJORITY FAILED (%d/%d) — likely "
                            "expired Kite token or API outage.",
                            len(results) - ok,
                            len(results),
                        )
                except Exception as exc:
                    _log.error("eod_ohlcv_download failed: %s", exc)
            _last_run_date = now.date()
        await asyncio.sleep(300.0)


@strategy_logged(fixed="news_catalyst_momentum")
def _evaluate_news_catalyst_signal(
    compiled: Any,
    shared: dict[str, Any],
    sym: str,
    df: Any,
    now: Any,
) -> Any | None:
    """Catalyst-confirmed momentum entry — structurally different from ORB/mean-
    reversion (both already proven to lose to costs across 25+ variants). The
    PRIMARY gate is a live NewsOpportunity: OpportunityScanner has already
    filtered for HIGH magnitude + confidence>=0.65 BULLISH/BOOST (long) or
    BEARISH/AVOID|HALT (short) news from real RSS/BSE feeds
    (src/news/opportunity_scanner.py) before it ever lands in
    shared["news_opportunities"] — this is a real corporate event, not a
    price-shape pattern. Confirmed by price vs session-VWAP (above for longs,
    below for shorts) + 2x volume surge so we're not entering an
    already-priced-in move.

    Reuses the module-level _compute_sl_value/_compute_target_value (ATR-based
    stop, RR=3 target from news_catalyst_momentum.json) with an explicit
    Direction — NOT compiled.compute_sl/compute_target, which hardcode LONG for
    any non-SHORT spec.meta.direction and would compute the stop on the wrong
    side for a short. The resulting Signal rides the exact same SmartExitTracker
    exit management (partial/BE/trail/reversal) as every other intraday
    strategy — this function only decides entry, never execution.
    """
    import time as _t

    from src.strategies.uss.compiler import Signal, _compute_sl_value, _compute_target_value
    from src.strategies.uss.schema import Direction

    opp = shared.get("news_opportunities", {}).get(sym.split(":")[-1])
    if opp is None:
        return None
    # Freshness: only act within 20 minutes of the news firing. Older than that
    # it's either already priced in or was never going to move the stock —
    # entering hours later would be chasing, not catching the catalyst.
    if _t.time() - float(opp.get("ts", 0)) > 1200:
        return None
    # Portfolio-wide cap: max 2 catalyst trades/day regardless of how many
    # qualifying opportunities fire — quality over frequency, per design.
    _today = now.date().isoformat()
    _counts = shared.setdefault("news_catalyst_traded", {})
    if _counts.get(_today, 0) >= 2:
        return None
    if len(df) < 3:
        return None

    is_short = str(opp.get("direction", "LONG")).upper() == "SHORT"
    direction = Direction.SHORT if is_short else Direction.LONG

    from src.strategies.indicators.library import compute

    # Session VWAP only — compute() runs on whatever df it's given, and the
    # scan's df spans several days of 5m bars, so slice to today's IST bars
    # first or the "VWAP" would blend in prior sessions' prices.
    _today_date = now.date()
    _day_df = df[df.index.tz_convert("Asia/Kolkata").date == _today_date]
    if len(_day_df) < 3:
        return None  # not enough of today's session yet to trust VWAP

    vwap_val = float(compute(_day_df, "VWAP").iloc[-1])
    vol_ratio_val = float(compute(df, "VOLUME_RATIO", period=20).iloc[-1])
    last_close = float(df["close"].iloc[-1])

    if is_short:
        if not (last_close < vwap_val):
            return None
    else:
        if not (last_close > vwap_val):
            return None
    if not (vol_ratio_val >= 2.0):
        return None

    entry = last_close
    idx = len(df) - 1
    sl = _compute_sl_value(compiled.spec.stop_loss, entry, df, idx, direction, None)
    target = _compute_target_value(compiled.spec.target, entry, sl, direction)
    if is_short:
        if not (target < entry < sl):
            return None
    else:
        if not (sl < entry < target):
            return None
    return Signal(
        compiled.spec.meta.name,
        direction,
        entry,
        sl,
        target,
        confidence=float(opp.get("confidence_boost", 0.65)),
    )


def _intraday_refusal(
    shared: dict[str, Any],
    sym: str,
    reason: str,
    detail: str = "",
    *,
    persistent: bool = False,
    now: Any = None,
) -> None:
    """Record WHY _execute_intraday_signal is about to return None for `sym`, so the caller
    can log/alert the exact reason instead of the idea just vanishing (2026-09-24: four of
    its refusal paths returned silently).

    `persistent=True` (with `now`) additionally marks the symbol as unable to fill again
    TODAY, for either direction, so llm_intraday_agent's menu (_intraday_menu) stops
    re-offering it and burning idea slots on a dead end (2026-10-08: JYOTICNC's
    counter_trend_short_blocked refusal fired 7x across 3 separate LLM passes before this
    existed). Only set this for refusal reasons that are structural and cannot resolve
    itself within the session (e.g. a stock's own DMA position, which does not move
    intraday) -- symmetric for whichever direction such a reason applies to. A
    price/time-dependent or transient reason (chase_guard, no_funds, news_halt_avoid, ...)
    must stay False, or a real later opportunity on that symbol would be silently thrown
    away for the rest of the day."""
    bare = sym.split(":")[-1]
    shared.setdefault("_intraday_refusals", {})[bare] = (reason, detail)
    if persistent and now is not None:
        shared.setdefault("_intraday_blocked_today", {}).setdefault(
            now.date().isoformat(), set()
        ).add(bare)


@strategy_logged(fixed="llm_intraday_agent")
def _llm_idea_funnel(
    shared: dict[str, Any],
    now: Any,
    stage: str,
    outcome: str,
    sym: str,
    reason: str = "",
    detail: str = "",
    level: int = 20,
    **kv: Any,
) -> None:
    """One canonical llm_funnel log line for an llm_intraday_agent idea + a deduped
    terminal alert (see src/strategies/llm_intraday/funnel.py)."""
    from src.strategies.llm_intraday.funnel import funnel_alert, funnel_log

    bare = sym.split(":")[-1]
    funnel_log(
        _intraday_log,
        agent="llm_intraday_agent",
        stage=stage,
        outcome=outcome,
        symbol=bare,
        reason=reason,
        level=level,
        **({"detail": detail} if detail else {}),
        **kv,
    )
    _idea = (shared.get("llm_intraday_ideas") or {}).get(bare)
    if _idea is not None and outcome not in ("placed", "expired"):
        # The idea's own trail: what happened at its most recent step, so an expiry can
        # say exactly what the last thing was (it used to claim 'never reached a price
        # check' for an idea that had been checked three times).
        _idea["_last_check"] = f"{now:%H:%M:%S} {stage}/{outcome}: {reason}" + (
            f" - {detail[:110]}" if detail else ""
        )
    funnel_alert(
        shared,
        day=now.date().isoformat(),
        agent="llm_intraday_agent",
        stage=stage,
        outcome=outcome,
        symbol=bare,
        reason=reason,
        detail=detail,
    )


@strategy_logged(fixed="llm_intraday_agent")
def _evaluate_llm_intraday_signal(
    compiled: Any,
    shared: dict[str, Any],
    sym: str,
    df: Any,
    now: Any,
) -> Any | None:
    """LLM intraday agent entry: turn a still-fresh idea published by
    _llm_intraday_agent_loop (shared["llm_intraday_ideas"]) into a Signal. Same
    role as _evaluate_news_catalyst_signal — this only decides entry; the Signal
    then rides the regime/breadth gates, ranked placement, _execute_intraday_signal
    (CRM G0-G10 + the fail-closed llm.intraday_g9 review) and SmartExitTracker.

    The LLM reasoned from a snapshot up to idea_ttl_minutes old, so the idea is
    re-checked against the latest close: it must still be within
    entry_tolerance_pct of the proposed entry AND pass the full validation rules
    at that price (stop floor, reward:risk), or the trade that gets placed is not
    the one that was validated.

    `now` must be freshly read by the caller for each symbol, not reused from
    the top of a per-cycle symbol loop -- on a slow cycle (497 symbols
    evaluated sequentially) a stale, loop-level `now` would drift for a
    late-ordered symbol (2026-09-18 postmortem: a POONAWALLA idea 20 min old
    was still treated as within the 15-min TTL this way, though it was then
    correctly rejected downstream by still_valid_at's reward:risk check
    anyway). Fixed 2026-09-21 at the call site, not here, so this function
    stays deterministic/testable on whatever `now` it's given."""
    from src.strategies.llm_intraday.pipeline import (
        ValidationLimits,
        minutes_to_flat,
        still_valid_at,
    )
    from src.strategies.uss.compiler import Signal
    from src.strategies.uss.schema import Direction

    _bare_sym = sym.split(":")[-1]
    idea = shared.get("llm_intraday_ideas", {}).get(_bare_sym)
    if idea is None or df is None or len(df) == 0:
        return None
    _now = now
    cfg = _cfg("global").get("llm_intraday", {})
    _age_min = (_now.timestamp() - float(idea["ts"])) / 60.0
    _ttl_min = float(cfg.get("idea_ttl_minutes", 15))
    if _age_min > _ttl_min:
        # Evict, don't just decline. An expired idea can never come back -- age only
        # increases -- but it used to sit in the store until a new idea for the same
        # symbol replaced it, so every 5-minute scan re-checked and re-logged the same
        # corpse: 162 ttl_expired lines on 2026-09-22 from 16 ideas, at ages up to
        # 87 minutes against a 15-minute TTL. That noise buried the rejections that
        # actually mattered and made the funnel unreadable.
        shared.get("llm_intraday_ideas", {}).pop(_bare_sym, None)
        _intraday_log.info(
            "llm_intraday_signal_rejected sym=%s reason=ttl_expired age_min=%.1f ttl_min=%.1f "
            "action=evicted",
            sym,
            _age_min,
            _ttl_min,
        )
        _n = int(idea.get("_checks", 0))
        _last = idea.get("_last_check") or (
            "no price check happened" if _n == 0 else f"{_n} check(s), no outcome recorded"
        )
        _llm_idea_funnel(
            shared,
            _now,
            "entry_check",
            "expired",
            sym,
            "ttl_expired",
            f"idea aged {_age_min:.0f} min (ttl {_ttl_min:.0f}) without an entry; "
            f"{_n} price check(s) done; last step: {_last}",
        )
        return None
    idea.setdefault("_checked_at", _now.timestamp())  # first price check happened
    idea["_checks"] = int(idea.get("_checks", 0)) + 1
    _traded_count = shared.get("llm_intraday_traded", {}).get(_now.date().isoformat(), 0)
    _max_trades = int(cfg.get("max_trades_per_day", 2))
    if _traded_count >= _max_trades:
        _intraday_log.info(
            "llm_intraday_signal_rejected sym=%s reason=max_trades_reached count=%d cap=%d",
            sym,
            _traded_count,
            _max_trades,
        )
        _llm_idea_funnel(
            shared,
            _now,
            "entry_check",
            "blocked",
            sym,
            "max_trades_reached",
            f"{_traded_count} trades already taken today, cap {_max_trades} (llm_intraday.max_trades_per_day)",
        )
        return None
    _cut_h, _cut_m = (int(x) for x in f"{_sessions.last_entry_by():%H:%M}".split(":"))
    if (_now.hour, _now.minute) >= (_cut_h, _cut_m):
        _intraday_log.info(
            "llm_intraday_signal_rejected sym=%s reason=past_cutoff now=%02d:%02d cutoff=%02d:%02d",
            sym,
            _now.hour,
            _now.minute,
            _cut_h,
            _cut_m,
        )
        _llm_idea_funnel(
            shared,
            _now,
            "entry_check",
            "expired",
            sym,
            "past_last_entry_cutoff",
            f"now {_now:%H:%M} is past the last-entry cutoff {_cut_h:02d}:{_cut_m:02d}",
        )
        return None

    last_close = float(df["close"].iloc[-1])
    entry = float(idea["entry_price"])
    _tolerance = float(cfg.get("entry_tolerance_pct", 0.4))
    _drift_pct = abs(last_close - entry) / entry * 100.0 if entry > 0 else float("inf")
    if entry <= 0 or _drift_pct > _tolerance:
        _intraday_log.info(
            "llm_intraday_signal_rejected sym=%s reason=entry_drift drift_pct=%.2f tolerance_pct=%.2f",
            sym,
            _drift_pct,
            _tolerance,
        )
        idea["_last_check"] = (
            f"entry_drift {_drift_pct:.2f}% > {_tolerance:.2f}% (close {last_close:.2f} vs idea {entry:.2f})"
        )
        _llm_idea_funnel(
            shared,
            _now,
            "entry_check",
            "waiting",
            sym,
            "entry_drift",
            idea["_last_check"],
        )
        return None
    _revalidated = still_valid_at(
        idea, last_close, ValidationLimits.from_cfg(cfg), minutes_to_flat(_now)
    )
    if _revalidated is None:
        # still_valid_at() -> _validate_idea() already logs the specific failing
        # check (intraday_idea_rejected reason=...) via its own _reject() helper;
        # this line just makes the outcome greppable alongside the other
        # llm_intraday_signal_rejected reasons above.
        _intraday_log.info("llm_intraday_signal_rejected sym=%s reason=still_valid_at_failed", sym)
        idea["_last_check"] = (
            f"revalidation failed at close {last_close:.2f} (stop/target/R:R no longer valid)"
        )
        _llm_idea_funnel(
            shared,
            _now,
            "entry_check",
            "waiting",
            sym,
            "revalidation_failed",
            idea["_last_check"],
        )
        return None
    _new_sl, _new_target = _revalidated
    _llm_idea_funnel(
        shared,
        _now,
        "entry_check",
        "signal",
        sym,
        "price_within_tolerance",
        f"close {last_close:.2f} within {_drift_pct:.2f}% of idea entry {entry:.2f}; "
        f"sl={_new_sl:.2f} target={_new_target:.2f}",
    )
    return Signal(
        compiled.spec.meta.name,
        Direction.SHORT if idea["direction"] == "SHORT" else Direction.LONG,
        last_close,
        _new_sl,
        _new_target,
        confidence=float(idea.get("confidence", 0.5)),
    )


@strategy_logged(param="strategy_name")
async def _execute_intraday_signal(
    shared: dict[str, Any],
    strategy_name: str,
    sym: str,
    sig: Any,
    now: Any,
    ref_ltp: float | None = None,
) -> tuple[str, float, float, float] | None:
    """Route a live intraday signal (e.g. ORB) into the REAL execution path.

    paper_mode → paper_engine (writes paper_trades, is_paper=TRUE);
    live        → lifecycle.place_entry_and_protection (writes trades, real broker).
    Enforces ORB's 1-trade-per-day, skips symbols already holding a position,
    honours the news HALT/AVOID gate, and sizes via the same CRM risk gates as
    the daily strategies (never bypasses risk controls).

    Returns (trade_id, fill_price, sl_price, target_price) on success — all values
    are POST-REANCHOR (actual fill, not signal entry), so callers must use these
    and not sig.entry_price/sl/target for exit tracking.
    """
    import time as _t

    from src.risk.crm import CRMSignal

    # 1-trade-per-day gate (ORB max_trades_per_day=1)
    _today = now.date().isoformat()
    _today_set: set[str] = shared.setdefault("intraday_traded", {}).setdefault(_today, set())
    _key = sym  # one trade per symbol per day regardless of which strategy signals
    if _key in _today_set:
        _intraday_refusal(shared, sym, "already_traded_today")
        _log.info("intraday_blocked_already_traded symbol=%s strategy=%s", sym, strategy_name)
        return None

    paper_mode = bool(shared.get("paper_mode", True))
    crm = shared.get("crm")
    crm_ctx = shared.get("crm_ctx")
    # This function only ever handles the "intraday" segment, so its paper/live
    # mode is fixed for the whole call (unlike the combined scan loop, which
    # handles multiple segments and must resolve mode per-signal).
    _is_paper = _resolve_is_paper(shared, crm_ctx, "intraday", strategy_name, paper_mode)
    if _is_paper:
        _stack = _pick_stack(shared, True)
        # Fall back to the legacy singular shared["lifecycle"]/["order_monitor"] when
        # shared["stacks"] isn't populated (e.g. callers/tests not yet stack-aware).
        lifecycle = _stack["lifecycle"] if _stack else shared.get("lifecycle")
        order_monitor = _stack["order_monitor"] if _stack else shared.get("order_monitor")
    else:
        # Live: only the strategy's own broker's stack (R1) -- never the primary alias.
        _stack, _route_reject = _live_stack_or_reject(shared, strategy_name, "intraday")
        if _stack is None:
            _intraday_refusal(shared, sym, "broker_route_rejected", str(_route_reject))
            return None
        lifecycle = _stack["lifecycle"]
        order_monitor = _stack["order_monitor"]

    # Fail CLOSED: never place an entry until the held-position set has been loaded
    # from the DB (paper) / broker (live) at least once. Right after a launcher restart
    # both order_monitor and held_symbols are empty, so we cannot tell whether this
    # symbol is already open — refuse rather than risk a duplicate (the restart-window
    # re-entry bug). The flag is set by the reconciliation loop on its first pass.
    if _held_symbols_unknown_for(shared, _is_paper, _stack):
        _log.warning("intraday_blocked_unreconciled sym=%s strategy=%s", sym, strategy_name)
        _intraday_refusal(shared, sym, "held_positions_not_loaded", "fail-closed after restart")
        return None
    # Never trade a symbol already in a position — no matter what. Block on EITHER
    # an engine-tracked position (order_monitor, both paper & live) OR any broker /
    # external holding (cached each cycle by the reconciliation loop). Symbols are
    # compared bare (exchange prefix stripped) so "NSE:BPCL" and "BPCL" match.
    _norm_sym = sym.split(":")[-1]
    # Phase 2: dedup is scoped to THIS call's own resolved mode only — a symbol
    # already held in paper must never block a live entry, and vice versa
    # (confirmed with user: fully separate). Falls back to the legacy combined
    # shared["held_symbols"] when the mode-specific set isn't populated.
    _om_syms = (
        {p.symbol.split(":")[-1] for p in order_monitor.get_monitored()}
        if order_monitor is not None
        else set()
    )
    _held_key = "held_symbols_paper" if _is_paper else "held_symbols_live"
    _held_syms = shared.get(_held_key)
    if _held_syms is None:
        _held_syms = shared.get("held_symbols") or set()
    if not _is_paper:
        _held_syms = _held_syms | _unloaded_brokers_tracked_symbols(shared)
    if _norm_sym in _om_syms or _norm_sym in _held_syms:
        _intraday_refusal(shared, sym, "already_held_or_open_order")
        _log.info("intraday_blocked_already_held symbol=%s strategy=%s", sym, strategy_name)
        return None

    # news HALT/AVOID gate
    _avoid = shared.get("news_avoid_symbols", {})
    if sym in _avoid and _avoid[sym] > _t.time():
        _log.info("intraday_blocked_news symbol=%s", sym)
        _intraday_refusal(shared, sym, "news_halt_avoid")
        return None

    entry = float(sig.entry_price)
    sl = float(sig.sl)
    target = float(sig.target)
    is_long = sig.direction.value == "LONG"
    direction = "BUY" if is_long else "SELL"
    sl_pct = abs(entry - sl) / entry if entry else 0.0
    tgt_pct = abs(target - entry) / entry if entry else 0.0
    if sl_pct <= 0 or entry <= 0:
        _intraday_refusal(shared, sym, "invalid_levels", f"entry={entry} sl={sl}")
        _log.warning(
            "intraday_blocked_invalid_levels symbol=%s entry=%s sl=%s target=%s",
            sym,
            entry,
            sl,
            target,
        )
        return None

    # Pre-order "don't chase" guard: if price has already run too far past the
    # breakout (or is beyond target / through SL) we skip the entry entirely —
    # no order, no slippage loss. Avoids the extended-entry whipsaw.
    # Use a FRESH LTP fetch (not the stale 5-min candle close) so the guard
    # sees where price actually is right now, before the order hits the broker.
    _live_ltp = ref_ltp  # fallback to candle close if broker fetch fails
    try:
        _broker_obj = lifecycle._broker if lifecycle is not None else None
        if _broker_obj is not None:
            _ltp_map = await _broker_obj.get_ltp([sym])
            if _ltp_map and sym in _ltp_map and _ltp_map[sym]:
                _live_ltp = float(_ltp_map[sym])
    except Exception:
        pass  # stale candle close is the safe fallback

    if _live_ltp is not None:
        from src.execution.entry_guard import should_skip_chase

        _skip, _reason = should_skip_chase(
            entry=entry,
            sl=sl,
            target=target,
            ltp=_live_ltp,
            direction=direction,
            max_chase_r=float(shared.get("max_entry_chase_r", 0.75)),
        )
        if _skip:
            _log.info(
                "intraday_skip_chase symbol=%s strategy=%s reason=%s entry=%.2f ltp=%.2f",
                sym,
                strategy_name,
                _reason,
                entry,
                _live_ltp,
            )
            _intraday_refusal(shared, sym, "chase_guard", str(_reason)[:120])
            return None

    # size + risk-gate through CRM (identical path to daily strategies).
    # MANDATORY: a signal may only become an order via a passing CRM decision.
    # If the risk engine is unavailable we FAIL CLOSED — never trade unguarded.
    if crm is None or crm_ctx is None:
        _log.warning(
            "intraday_blocked_no_risk_engine symbol=%s strategy=%s — risk engine unavailable, "
            "no order placed (fail-closed)",
            sym,
            strategy_name,
        )
        _intraday_refusal(shared, sym, "no_risk_engine", "fail-closed")
        return None
    qty = 0
    if crm is not None and crm_ctx is not None:
        # Smart intraday sizing: risk-capped (₹ max_risk_per_trade / SL distance) and
        # constrained by affordable funds (live available cash × MIS leverage). Replaces
        # the old hardcoded qty=1. CRM G8 still applies daily-budget/cash ceilings after.
        from src.risk.crm import intraday_risk_cap
        from src.risk.position_sizing import intraday_position_size

        # Same per-trade cap G8 applies (capital.json intraday.max_loss_per_trade, else
        # the legacy %-of-equity cap), so sizing and G8 cannot disagree. Refresh first:
        # the pre-CRM refresh below runs after sizing, too late for the first trade.
        crm_ctx.current_equity = float(shared.get("current_equity", crm_ctx.current_equity))
        _refresh_options_risk_budget(crm_ctx)
        _risk_cap = intraday_risk_cap(crm_ctx)
        _avail_cash = float(shared.get("available_cash", crm_ctx.current_equity))
        if not _is_paper and shared.get("per_broker_equity"):
            # Per-broker risk: size on the routed broker's OWN cap and cash; unknown => no trade.
            from src.risk.crm import broker_view

            _bkey = _live_key_for(shared, strategy_name)
            _funds = _broker_funds(shared, _bkey)
            if _funds is None:
                _log.warning(
                    "intraday_blocked_broker_equity_unknown sym=%s strategy=%s key=%s",
                    sym,
                    strategy_name,
                    _bkey,
                )
                _intraday_refusal(shared, sym, "broker_equity_unavailable", _bkey)
                return None
            _risk_cap = intraday_risk_cap(broker_view(crm_ctx, _bkey))
            _avail_cash = _funds[1]
        _intended = intraday_position_size(entry, sl, _risk_cap, _avail_cash * crm_ctx.mis_leverage)
        # Per-stock daily-trend caution: shrink size when this intraday breakout fights
        # the stock's own 50/200 DMA. Soft (size cut), fail-open on missing daily data.
        _dma_mult = 1.0
        _setup_q: float | None = None
        _ohlcv_repo = shared.get("ohlcv_repo")
        if _ohlcv_repo is not None and _intended > 0:
            try:
                from src.risk.trend_filter import daily_trend_size_multiplier

                _ddf = await _ohlcv_repo.fetch_dataframe(sym, "1d", 220)
                if _ddf is not None and len(_ddf) >= 200:
                    # Setup-quality score (0-100) from the same daily df — trend +
                    # low-volatility + volume-consistency. Powers the dashboard
                    # "Setup" column. Best-effort; leaves None on any error.
                    try:
                        from src.strategies.indicators.library import quality_score as _qscore

                        _setup_q = round(float(_qscore(_ddf).iloc[-1]), 1)
                    except Exception:
                        _setup_q = None
                    # LIVE price vs the DMAs (the DMAs come from daily closes, the price does
                    # not). This used the last DAILY close = yesterday's: PINELABS had fallen
                    # 189.10 -> 179.65 through its 200-DMA (183.08) and TATAINVEST 674.00 ->
                    # 660.60 through its 200-DMA (665.22), yet both were refused as "shorts
                    # above their 50/200-DMA" (2026-09-24).
                    _c = float(entry)
                    _s50 = float(_ddf["close"].rolling(50).mean().iloc[-1])
                    _s200 = float(_ddf["close"].rolling(200).mean().iloc[-1])
                    _dma_mult = daily_trend_size_multiplier(_c, _s50, _s200, direction)
                    # Shorting a stock above its own 50 AND 200 DMA is blocked, not halved,
                    # for llm_intraday_agent: 67 closed MIS counter-trend shorts ran
                    # -1.27R/trade at 10% win rate, PF 0.03 (worst bucket in the book;
                    # counter-trend LONGS were the best, -0.38R, so they stay halved only).
                    if (
                        _dma_mult < 1.0
                        and direction in ("SELL", "SHORT")
                        and strategy_name == "llm_intraday_agent"
                        and bool(
                            _cfg("global").get("llm_intraday", {}).get("block_counter_trend_shorts")
                        )
                    ):
                        _log.info(
                            "intraday_blocked_counter_trend_short symbol=%s strategy=%s "
                            "close=%.2f sma50=%.2f sma200=%.2f",
                            sym,
                            strategy_name,
                            _c,
                            _s50,
                            _s200,
                        )
                        _intraday_refusal(
                            shared,
                            sym,
                            "counter_trend_short_blocked",
                            f"SHORT while close {_c:.2f} is above its 50-DMA {_s50:.2f} and 200-DMA "
                            f"{_s200:.2f} (llm_intraday.block_counter_trend_shorts; PF 0.03 historically)",
                            # DMA position does not move within a session, so this symbol
                            # cannot fill SHORT again today -- persistent=True stops
                            # llm_intraday_agent's menu re-offering it (see _intraday_menu).
                            persistent=strategy_name == "llm_intraday_agent",
                            now=now,
                        )
                        return None
                    if _dma_mult < 1.0:
                        _intended = max(1, int(_intended * _dma_mult))
                        _log.info(
                            "intraday_dma_caution symbol=%s dir=%s mult=%.2f qty=%d",
                            sym,
                            direction,
                            _dma_mult,
                            _intended,
                        )
                    # Liquidity cap: never be more than N% of the stock's average daily
                    # volume, or our own order moves price and the stop fills badly.
                    if crm_ctx.max_adv_participation_pct > 0 and "volume" in _ddf.columns:
                        _adv = float(_ddf["volume"].iloc[-20:].mean())
                        _adv_cap = int(_adv * crm_ctx.max_adv_participation_pct / 100.0)
                        if _adv > 0 and _intended > _adv_cap:
                            _log.info(
                                "intraday_adv_cap symbol=%s qty=%d -> %d adv20=%.0f pct=%.2f",
                                sym,
                                _intended,
                                _adv_cap,
                                _adv,
                                crm_ctx.max_adv_participation_pct,
                            )
                            _intended = _adv_cap
            except Exception as _dexc:
                _log.debug("intraday_dma_caution_skipped symbol=%s err=%s", sym, _dexc)
        if _intended <= 0:
            _log.info("intraday_blocked_no_funds symbol=%s avail_cash=%.0f", sym, _avail_cash)
            _intraday_refusal(shared, sym, "no_funds", f"avail_cash={_avail_cash:.0f}")
            return None
        from src.execution.trade_meta import build_trade_meta

        # "Stocks to watch" priority aligned with the signal direction → G5/G9 +0.10.
        _pri = shared.get("news_priority_symbols", {}).get(sym)
        _news_priority_aligned = bool(
            _pri
            and (
                (_pri["bias"] == "BULLISH" and direction in ("BUY", "LONG"))
                or (_pri["bias"] == "BEARISH" and direction in ("SELL", "SHORT"))
            )
        )

        crm_signal = CRMSignal(
            signal_id=f"ORB-{sym}-{now.strftime('%H%M%S')}",
            symbol=sym,
            sector_declining=_resolve_sector_declining(sym),
            sector=_resolve_sector(sym),
            direction=direction,
            strategy_name=strategy_name,
            intended_quantity=_intended,
            stop_loss_pct=sl_pct,
            target_pct=tgt_pct,
            risk_rupees=abs(entry - sl) * _intended,  # TOTAL rupees (G7 open-risk cap)
            entry_price=entry,
            regime=shared.get("regime", "BULL"),
            strategy_health=shared.get("health_scores", {}).get(strategy_name, "HEALTHY"),
            # Intraday news (halt-only policy): hard halts already blocked earlier via
            # news_avoid_symbols; earnings is left non-blocking (positions square off EOD).
            # HIGH-magnitude news still applies G5's −0.30 LLM-confidence penalty.
            earnings_proximity_days=999,
            high_magnitude_news=(sym in shared.get("news_high_magnitude", {})),
            vix=float(shared.get("vix", 15.0)),
            engine="intraday",
            meta=build_trade_meta(
                autonomy_mode=shared.get("autonomy_mode", _DEFAULT_AUTONOMY_MODE),
                strategy_name=strategy_name,
                # Intraday trades record the DAY-scoped regime the gate actually
                # used (2026-07-09: dashboard showed structural BEAR while the
                # gate saw BULL — the mismatch made the gate look broken).
                regime_at_entry=(
                    str(shared["intraday_regime"])
                    if shared.get("intraday_regime") not in (None, "", "UNKNOWN")
                    else shared.get("regime", "UNKNOWN")
                ),
            ),
        )
        crm_signal.meta["news_priority_aligned"] = _news_priority_aligned
        crm_signal.meta["max_risk_rupees"] = _risk_cap
        if _setup_q is not None:
            crm_signal.meta["setup_quality_score"] = _setup_q
        crm_signal.meta["backtest_passed"] = (
            shared.get("backtest_summary", {}).get(strategy_name, {}).get("passed", True)
        )
        # G9 LLM confirmation at signal generation — populate meta so the BLOCKING G9
        # gate inside crm.evaluate() can reject a low-quality setup. Verdict is logged
        # human-readably to both llm.log (g9_llm_verdict) and here.
        #
        # strategies_registry.json `llm_gate_required` (via crm_ctx.llm_gate_required)
        # decides the policy: required -> a genuine verdict must come back or the trade
        # is skipped (paper or live); not required -> the old fail-open trade_gate path
        # (an outage never halts it). LLM-originated strategies (`llm_originated`, e.g.
        # llm_intraday_agent) use the gemini-free llm.intraday_g9 reviewer, since the idea
        # was itself produced on the gemini-first chain; other required strategies use
        # the standard trade_gate chain.
        _required = crm_ctx.llm_gate_required.get(strategy_name, False)
        _originated = strategy_name in shared.get("llm_originated_strategies", set())
        if _originated and "intraday_g9_llm" not in shared:
            from src.llm.providers import make_intraday_gate_llm_client

            shared["intraday_g9_llm"] = make_intraday_gate_llm_client()
        _g9 = shared.get("intraday_g9_llm") if _originated else shared.get("g9_llm")
        if _g9 is not None and (_required or not crm_ctx.llm_bypass):
            from src.llm.providers import g9_chain_timeout
            from src.llm.trade_evaluator import evaluate_trade as _eval_trade
            from src.llm.trade_evaluator import evaluate_trade_gated as _eval_trade_gated
            from src.news.context import build_news_context

            _g9_sig = {
                "symbol": sym,
                "direction": direction,
                "strategy_name": strategy_name,
                "regime": shared.get("regime", "BULL"),
                "entry_price": entry,
                "stop_loss_pct": sl_pct,
                "target_pct": tgt_pct,
                "strategy_health": shared.get("health_scores", {}).get(strategy_name, "HEALTHY"),
                "vix": float(shared.get("vix", 15.0)),
                "news_context": build_news_context(sym, shared),
                "news_priority_aligned": crm_signal.meta.get("news_priority_aligned"),
            }
            if _required:
                _v = await _eval_trade_gated(
                    _g9,
                    _g9_sig,
                    int(shared.get("g9_malformed_intraday", 0)),
                    chain_timeout_secs=g9_chain_timeout("intraday_g9", 12.0),
                )
            else:
                _v = await _eval_trade(_g9, _g9_sig, int(shared.get("g9_malformed_intraday", 0)))
            shared["g9_malformed_intraday"] = _v["malformed"]
            crm_signal.meta["llm_confidence"] = _v["llm_confidence"]
            crm_signal.meta["llm_flag"] = _v["llm_flag"]
            crm_signal.meta["llm_reason"] = _v["llm_reason"]
            crm_signal.meta["llm_evaluated"] = _v["evaluated"]
            crm_signal.meta["llm_provider"] = _v.get("provider")
            crm_signal.meta["llm_latency_ms"] = _v.get("latency_ms")

            # sig.confidence is only a genuine origination-model confidence when
            # _originated is True -- non-LLM intraday strategies never set it, so
            # it silently holds Signal's dataclass default of 1.0. Phase 1: observe
            # the origination-vs-reviewer gap only, never gates on it.
            if _originated and _v["evaluated"]:
                _gap = abs(float(sig.confidence) - float(_v["llm_confidence"]))
                crm_signal.meta["llm_origination_confidence"] = sig.confidence
                crm_signal.meta["llm_confidence_gap"] = _gap
                _log.info(
                    "g9_disagreement symbol=%s strategy=%s origination_confidence=%.2f "
                    "reviewer_confidence=%.2f gap=%.2f reviewer_flag=%s",
                    sym,
                    strategy_name,
                    sig.confidence,
                    _v["llm_confidence"],
                    _gap,
                    _v["llm_flag"],
                )
            _log.info(
                "intraday_llm_check strategy=%s symbol=%s dir=%s flag=%s confidence=%.2f "
                "evaluated=%s provider=%s latency_ms=%d reason=%s",
                strategy_name,
                sym,
                direction,
                _v["llm_flag"],
                _v["llm_confidence"],
                _v["evaluated"],
                _v.get("provider"),
                _v["latency_ms"],
                _v["llm_reason"][:100],
            )
        if _required and not crm_signal.meta.get("llm_evaluated", False):
            _log.warning(
                "intraday_g9_no_verdict sym=%s strategy=%s — LLM gate required, trade skipped (%s)",
                sym,
                strategy_name,
                "no provider configured" if _g9 is None else "no valid verdict",
            )
            # A trade the system was about to take is being skipped because the
            # reviewer never answered -- a real decision reversal, previously only a
            # log line (2026-09-23).
            try:
                from src.notifications import EventKind, notify
                from src.notifications.events import Segment

                notify(
                    EventKind.STRATEGY_ERROR,
                    is_paper=True,
                    segment=Segment.INTRADAY,
                    strategy=strategy_name,
                )
            except Exception:  # notify() must never break signal evaluation
                pass
            _intraday_refusal(
                shared,
                sym,
                "g9_no_verdict",
                "no provider configured" if _g9 is None else "reviewer gave no valid verdict",
            )
            return None
        crm_ctx.current_equity = float(shared.get("current_equity", crm_ctx.current_equity))
        _refresh_options_risk_budget(crm_ctx)
        try:
            decision = crm.evaluate(crm_signal, crm_ctx)
        except Exception as exc:
            _log.error("intraday_crm_error symbol=%s err=%s", sym, exc)
            _intraday_refusal(shared, sym, "crm_error", str(exc)[:120])
            return None
        log_gate_decisions(crm_signal, decision)
        # WS7 — one structured "why" line per evaluated signal: inputs + every gate
        # result + news/llm/dma + final action. This is the per-decision audit record.
        _gate_str = " ".join(
            f"{g.gate_id}={g.decision.value}"
            for g in (getattr(decision, "gate_results", None) or [])
        )
        _action = (
            f"PLACED qty={decision.final_quantity}"
            if (decision.approved and decision.final_quantity > 0)
            else f"BLOCKED {decision.blocking_gate}:{decision.blocking_reason}"
        )
        _log.info(
            "intraday_decision symbol=%s dir=%s entry=%.2f sl=%.2f tgt=%.2f intended_qty=%d "
            "regime=%s news_halt=%s high_mag_news=%s llm=%s/%.2f(eval=%s) dma_mult=%.2f "
            "gates=[%s] action=%s",
            sym,
            direction,
            entry,
            sl,
            target,
            _intended,
            crm_signal.regime,
            crm_signal.news_halt,
            crm_signal.high_magnitude_news,
            crm_signal.meta.get("llm_flag", "-"),
            float(crm_signal.meta.get("llm_confidence", 0.0)),
            crm_signal.meta.get("llm_evaluated", False),
            _dma_mult,
            _gate_str,
            _action,
        )
        if not decision.approved or decision.final_quantity <= 0:
            _log.info(
                "intraday_crm_blocked symbol=%s gate=%s reason=%s",
                sym,
                decision.blocking_gate,
                decision.blocking_reason,
            )
            _intraday_refusal(
                shared,
                sym,
                f"crm_{decision.blocking_gate}",
                str(decision.blocking_reason)[:160],
            )
            return None
        qty = int(decision.final_quantity)

    from src.risk.crm import is_shadow as _is_shadow

    if _is_shadow(crm_signal, crm_ctx):
        _log.info(
            "intraday_shadow_skip symbol=%s dir=%s qty=%d — segment shadow_mode: not placed",
            sym,
            direction,
            qty,
        )
        _intraday_refusal(shared, sym, "shadow_mode", "segment shadow_mode: not placed")
        return None

    # place — ONE path for paper and live. The injected broker adapter is the only
    # difference: paper simulates the fill (never hits the broker), live sends it to
    # Kite. place_entry_protection_and_poll places entry+SL+target, realizes the fill,
    # and re-anchors SL/target to the actual fill. Returns the agent_trade_id so the
    # scan loop can seed the SmartExitTracker.
    placed_id: str | None = None
    if lifecycle is None or order_monitor is None:
        _log.error("intraday_blocked_no_lifecycle symbol=%s strategy=%s", sym, strategy_name)
        _intraday_refusal(shared, sym, "no_order_lifecycle", "lifecycle/order_monitor missing")
        return None
    # Lock the symbol for today BEFORE attempting placement so a failed placement
    # (LTP unavailable, SL order rejected, internet blip) cannot generate duplicates
    # on the next scan cycle. The 1-trade-per-day gate fires on attempt, not fill.
    _today_set.add(_key)
    try:
        signal_ref = f"ORB-{sym}-{now.strftime('%H%M%S')}"
        record = await lifecycle.place_entry_protection_and_poll(
            signal_id=signal_ref,
            symbol=sym,
            direction=direction,
            qty=qty,
            entry_price=entry,
            sl_price=sl,
            target_price=target,
            product="MIS",
            order_types=shared.get("order_types", {}),
            meta=crm_signal.meta,
        )
    except Exception as exc:
        _log.error("intraday_placement_failed symbol=%s err=%s", sym, exc)
        _intraday_refusal(shared, sym, "placement_failed", str(exc)[:160])
        try:
            from src.notifications import EventKind, notify
            from src.notifications.events import Segment

            notify(
                EventKind.ENTRY_REJECTED,
                is_paper=_is_paper,
                symbol=sym,
                segment=Segment.INTRADAY,
                error=str(exc)[:200],
            )
        except Exception:  # notify() must never mask the original failure
            pass
        return None

    from src.execution.order_lifecycle import MonitoredPosition, OrderState

    # Register in OrderMonitor's pending registry immediately after submission.
    _tp = getattr(order_monitor, "track_pending", None)
    if _tp is not None:
        _tp(record.agent_trade_id, now)

    if record.state != OrderState.FILLED or not record.fill_price:
        _log.warning(
            "intraday_fill_unconfirmed symbol=%s state=%s — resting, monitor will re-check",
            sym,
            record.state,
        )
        import asyncio as _asyncio

        # Hand the resting entry to the monitor loop, which re-polls it against live LTP
        # every cycle (~2s) and re-pegs it once inside the risk-based chase allowance.
        # Before 2026-08-25 nothing revisited an unfilled entry at all: the fill was
        # decided by a single poll here and the row was stranded SUBMITTED for good.
        # Carry everything OrderMonitor needs so a late fill is tracked immediately,
        # instead of becoming an unmonitored live position.
        record.monitor_template = MonitoredPosition(
            agent_trade_id=record.agent_trade_id,
            symbol=sym,
            direction=direction,
            quantity=qty,
            fill_price=0.0,
            initial_sl=float(sl),
            current_sl=float(sl),
            initial_target=float(target),
            current_target=float(target),
            sl_order_id=record.sl_order_id or "",
            target_order_id=record.target_order_id or "",
            product="MIS",
            entry_ts=now.isoformat(),
            strategy_name=strategy_name,
        )
        lifecycle.track_pending_entry(record.agent_trade_id)
        # Backstop if it never becomes marketable. 20s, not the old 8s: the re-check
        # now has a real chance to fill it, so cutting it at 8s would abandon entries
        # that the monitor was about to complete. Cancels entry + SL + target together.
        _asyncio.create_task(lifecycle.cancel_entry_if_unfilled(record.agent_trade_id, 20.0))
        _intraday_refusal(
            shared, sym, "entry_resting_unfilled", "monitor re-checks; cancelled if unfilled in 20s"
        )
        return None

    # Entry confirmed filled — clear from pending registry.
    _rp = getattr(order_monitor, "resolve_pending", None)
    if _rp is not None:
        _rp(record.agent_trade_id)

    # Post-fill chase check: flag when the actual fill drifted more than
    # max_chase_r×risk beyond the signal entry (e.g. paper fill at current LTP >>
    # ORB high) — informational only. This used to call lifecycle.cancel_protection()
    # to reject the trade, but the order is ALREADY FILLED at this point (checked
    # above at line ~1099) — cancel_protection tries transition(..., REJECTED),
    # which the state machine correctly refuses for a FILLED trade (you can't
    # un-fill a fill), the exception was swallowed, and `return None` here skipped
    # order_monitor.track() below entirely. Net effect: a real, fully-filled,
    # money-at-risk position was silently orphaned from ALL SL/target/trailing
    # monitoring for its whole life, only ever closed by the unrelated EOD
    # exit-window sweep at whatever price happened to be then (see EMMVEE
    # 2026-08-13: filled, chase-rejected-and-abandoned at 09:45, target 319.73
    # never checked, closed by EOD sweep at 323.50 instead). A high-drift fill is
    # still a real, live position — the correct response is to monitor it
    # normally (record.sl_price/target_price already reflect the post-fill
    # reanchor), not abandon it. Falls through to order_monitor.track() below.
    _initial_risk = abs(entry - sl)
    _fill_drift = (
        float(record.fill_price) - entry
    )  # positive = filled above signal entry (bad for both LONG chase and SHORT bounce); negative = filled below entry (normal for SHORT)
    _max_chase_r = float(shared.get("max_entry_chase_r", 0.75))
    if _initial_risk > 0 and _fill_drift > _max_chase_r * _initial_risk:
        _log.warning(
            "fill_high_chase_drift sym=%s fill=%.2f entry=%.2f drift_r=%.2f max_r=%.2f — "
            "accepting and tracking normally (fill is already committed)",
            sym,
            float(record.fill_price),
            entry,
            _fill_drift / _initial_risk,
            _max_chase_r,
        )

    # setup_quality_score (0-100) is written at INSERT time by _persist_entry from
    # signal meta — see _execute_intraday_signal / order_lifecycle._persist_entry.

    placed_id = record.agent_trade_id
    # Register the filled position with the shared OrderMonitor so SL/target/exit
    # (and the smart-exit ratchet) run identically for paper and live.
    order_monitor.track(
        MonitoredPosition(
            agent_trade_id=record.agent_trade_id,
            symbol=sym,
            direction=direction,
            quantity=int(record.filled_quantity or qty),
            fill_price=float(record.fill_price),
            initial_sl=float(record.sl_price),
            current_sl=float(record.sl_price),
            initial_target=float(record.target_price),
            current_target=float(record.target_price),
            sl_order_id=record.sl_order_id or "",
            target_order_id=record.target_order_id or "",
            product="MIS",
            entry_ts=now.isoformat(),
            strategy_name=strategy_name,
        )
    )

    # Count this placement against the daily cap so CRM G7 (max_new_trades_per_day)
    # actually enforces. open_position_count is re-derived from the live tracker each
    # scan cycle (see _intraday_signal_scan_loop), so it is not touched here.
    if crm_ctx is not None:
        crm_ctx.new_trades_today = int(getattr(crm_ctx, "new_trades_today", 0)) + 1
        crm_ctx.intraday_trades_today = int(getattr(crm_ctx, "intraday_trades_today", 0)) + 1
        _pst = getattr(crm_ctx, "per_strategy_trades_today", {})
        _pst[strategy_name] = int(_pst.get(strategy_name, 0)) + 1
        crm_ctx.per_strategy_trades_today = _pst
    _log.info(
        "intraday_trade_placed strategy=%s symbol=%s dir=%s qty=%d entry=%.2f sl=%.2f tgt=%.2f paper=%s",
        strategy_name,
        sym,
        direction,
        qty,
        entry,
        sl,
        target,
        paper_mode,
    )
    # placed_id is set at fill above; cast narrows str | None -> str for the typed return.
    return (
        cast("str", placed_id),
        float(record.fill_price),
        float(record.sl_price),
        float(record.target_price),
    )


def _intraday_segment_is_paper(shared: dict[str, Any]) -> bool:
    """Whether the intraday (MIS) segment is currently paper — gates the paper-only
    exit-window/EOD bulk sweeps below. A live intraday segment relies entirely on
    OrderMonitor's own per-position MIS-flat-by enforcer (order_lifecycle.py
    cycle()), which already works identically paper/live — no live bulk sweep
    needed here."""
    crm_ctx = shared.get("crm_ctx")
    if crm_ctx is None:
        return bool(shared.get("paper_mode", True))
    return bool(crm_ctx.segment_paper_mode.get("intraday", bool(shared.get("paper_mode", True))))


def _exit_window_start_min(shared: dict[str, Any]) -> int:
    """Earliest enabled intraday strategy's exit-window START, in minutes since IST
    midnight. Drives the hard paper flat-by so positions are flattened from the
    window start (e.g. 1510-1520 → from 15:10), well before the 15:20 deadline.
    Falls back to 15:10 (910)."""
    from src.strategies.uss.compiler import _parse_window

    default = _sessions.minutes(_sessions.mis_flat_by())
    reg = shared.get("strategy_registry")
    if reg is None:
        return default
    starts: list[int] = []
    try:
        for c in reg.all_enabled():
            tf = (getattr(c.spec.meta, "timeframe", "") or "").upper()
            if tf not in ("5MIN", "15MIN", "5M", "15M"):
                continue
            sess = getattr(c.spec, "intraday", None)
            if sess is None:
                continue
            xs, _xe = _parse_window(sess.exit_window)
            starts.append(xs.hour * 60 + xs.minute)
    except Exception:
        return default
    return min(starts) if starts else default


async def _refresh_daily_counters(shared: dict[str, Any], crm_ctx: Any) -> None:
    """Refresh day-scoped CRM counters from the DB on every scan cycle.

    new_trades_today / open_position_count / daily_loss_so_far / consecutive_losses
    gate trading via CRM. They are held in-memory, so a mid-day launcher restart
    would reset them to 0 and silently bypass the caps (root cause of 6-before +
    6-after = 12 trades on 2026-06-18). Sourcing them from paper_trades (the source
    of truth) every cycle keeps every counter correct for the whole trading day and
    across any restart — there is no per-restart reset. The within-cycle placement
    increment of new_trades_today still applies on top so multiple fills in one
    cycle accumulate before the next DB refresh. Live mode (no paper engine) falls
    back to the order monitor for the open-position count.
    """
    _stacks = shared.get("stacks") or {}
    if _stacks:
        # Union across every active stack — this field isn't mode-split (only the
        # 4 counters explicitly confirmed with the user are), it's total deployed
        # capital across whatever's actually open.
        _monitored_all = [p for s in _stacks.values() for p in s["order_monitor"].get_monitored()]
        crm_ctx.open_exposure_rupees = sum(
            float(p.quantity) * float(p.fill_price) for p in _monitored_all
        )
        _update_open_risk_and_sector_exposure(crm_ctx, _monitored_all)
        # Open risk is a risk budget, so it IS mode-split (paper never uses live's).
        try:
            _mor: dict[str, float] = {}
            # Keyed by CRM mode label: every live stack sums into the legacy "live"
            # budget (identical to today with one live stack; per-broker keys come later).
            _pbe = bool(shared.get("per_broker_equity"))
            for _k, s in _stacks.items():
                _m = mode_label(_k)
                # Per-broker risk also keeps each live stack's own bucket ("live:<broker>").
                _labels = (_m, _k) if _pbe and _k.startswith("live:") else (_m,)
                if _pbe and _k.startswith("live:"):
                    # A broker with nothing open has 0 open risk -- never absent, which
                    # broker_view would read as the COMBINED live bucket.
                    for _key0 in (_k, f"{_k}:intraday", f"{_k}:swing"):
                        _mor.setdefault(_key0, 0.0)
                for p in s["order_monitor"].get_monitored():
                    _r = _position_open_risk(p)
                    _eng = "intraday" if str(p.product).upper() == "MIS" else "swing"
                    for _lb in _labels:
                        _mor[_lb] = _mor.get(_lb, 0.0) + _r
                        _mor[f"{_lb}:{_eng}"] = _mor.get(f"{_lb}:{_eng}", 0.0) + _r
            crm_ctx.mode_open_risk_rupees = _mor
        except Exception:
            pass
    else:
        om = shared.get("order_monitor")
        if om is not None:
            _monitored_all = om.get_monitored()
            crm_ctx.open_exposure_rupees = sum(
                float(p.quantity) * float(p.fill_price) for p in _monitored_all
            )
            _update_open_risk_and_sector_exposure(crm_ctx, _monitored_all)

    pe = shared.get("paper_ledger")
    if pe is not None:
        try:
            from src.risk.crm import engine_of, resolve_engine_counts

            _reg = shared.get("strategy_registry")
            _name_to_engine: dict[str, str] = {}
            if _reg is not None:
                for _cc in _reg.all_enabled():
                    _meta = getattr(_cc.spec, "meta", None)
                    _tf = getattr(_meta, "timeframe", None) or getattr(_cc.spec, "timeframe", "")
                    _nm = getattr(_meta, "name", None) or getattr(_cc.spec, "name", "")
                    if _nm:
                        _name_to_engine[str(_nm)] = engine_of(_tf)

            # "paper" table always (matches today's exact behavior when populating
            # the legacy unscoped fields below); "trades" (live) additionally, but
            # only when some segment is actually live — never scan `trades` for an
            # all-paper deployment. Fully independent counters per mode (confirmed
            # with user): a paper loss must never trip a real-money circuit breaker.
            # Mode labels, not stack keys: the `trades` table holds every live broker's rows.
            _modes_to_fetch = ["paper"] + (
                ["live"] if any(not stack_is_paper(k) for k in _stacks) else []
            )
            _llm_intraday_db_count = 0
            for _mode in _modes_to_fetch:
                _table = "paper_trades" if _mode == "paper" else "trades"
                c = await pe.daily_risk_counters(table=_table)
                # Intraday's OWN daily-loss figure (config/capital.json
                # intraday.max_loss_per_day), completely separate from swing's P&L
                # (swing has no daily limit by design) -- 2026-10-07.
                try:
                    _ic = await pe.daily_risk_counters(table=_table, product="MIS")
                    crm_ctx.mode_intraday_daily_loss_so_far[_mode] = float(_ic["daily_loss_so_far"])
                except Exception as _ice:
                    _log.warning("intraday_daily_loss_refresh_failed mode=%s err=%s", _mode, _ice)
                else:
                    await _enforce_intraday_daily_loss_breaker(shared, crm_ctx, _mode, pe)
                crm_ctx.mode_new_trades_today[_mode] = int(c["new_trades_today"])
                crm_ctx.mode_open_position_count[_mode] = int(c["open_position_count"])
                crm_ctx.mode_daily_loss_so_far[_mode] = float(c["daily_loss_so_far"])
                crm_ctx.mode_consecutive_losses[_mode] = int(c["consecutive_losses"])
                _ps_counts = {str(k): int(v) for k, v in c.get("per_strategy", {}).items()}
                _llm_intraday_db_count += _ps_counts.get("llm_intraday_agent", 0)
                _open_ps_counts = {
                    str(k): int(v) for k, v in c.get("open_per_strategy", {}).items()
                }
                _intraday_today, _swing_today = resolve_engine_counts(_ps_counts, _name_to_engine)
                _intraday_open, _swing_open = resolve_engine_counts(
                    _open_ps_counts, _name_to_engine
                )
                crm_ctx.mode_engine_trades_today[f"{_mode}:intraday"] = _intraday_today
                crm_ctx.mode_engine_trades_today[f"{_mode}:swing"] = _swing_today
                crm_ctx.mode_engine_open_positions[f"{_mode}:intraday"] = _intraday_open
                crm_ctx.mode_engine_open_positions[f"{_mode}:swing"] = _swing_open
                if _mode == "paper":
                    # Legacy unscoped fields = paper counters — preserves today's
                    # exact behavior for any reader not yet mode-aware (matches the
                    # G1/G2/G7 fallback-to-legacy-field pattern in src/risk/crm.py).
                    crm_ctx.new_trades_today = int(c["new_trades_today"])
                    crm_ctx.open_position_count = int(c["open_position_count"])
                    crm_ctx.daily_loss_so_far = float(c["daily_loss_so_far"])
                    crm_ctx.consecutive_losses = int(c["consecutive_losses"])
                    crm_ctx.per_strategy_trades_today = _ps_counts
                    crm_ctx.intraday_trades_today = _intraday_today
                    crm_ctx.swing_trades_today = _swing_today
                    crm_ctx.intraday_open_positions = _intraday_open
                    crm_ctx.swing_open_positions = _swing_open
                    # NOT _enforce_daily_loss_breaker: that trigger is the COMBINED
                    # swing+intraday pot, so it could force-flatten intraday purely
                    # because SWING lost money -- wrong under the 2026-10-07 design
                    # (swing has no daily limit; intraday's fate depends only on its
                    # own P&L). _enforce_intraday_daily_loss_breaker above replaces it
                    # for the main loop; the old function is kept for its own tests
                    # and as the legacy/"other"-segment mechanism.
            if shared.get("per_broker_equity"):
                await _refresh_per_broker_counters(shared, crm_ctx, pe, _stacks, _name_to_engine)
            # llm_intraday_agent's own max_trades_per_day counter was in-memory only,
            # so a mid-day restart reset it to 0 and the cap silently reopened -- the
            # identical failure this function exists to prevent for the CRM counters.
            # 2026-09-22: the service restarted repeatedly and the agent took 3 trades
            # against a cap of 2 (TATACHEM, HOMEFIRST, AEGISLOG). Re-seed from the DB,
            # but never BELOW the in-memory value: a fill placed earlier in this cycle
            # has not reached the DB yet, and losing it would reopen the cap again.
            from datetime import datetime as _li_dt
            from zoneinfo import ZoneInfo as _li_tz

            _li_day = _li_dt.now(_li_tz("Asia/Kolkata")).date().isoformat()
            _li_counts = shared.setdefault("llm_intraday_traded", {})
            _li_counts[_li_day] = max(int(_li_counts.get(_li_day, 0)), _llm_intraday_db_count)
            return
        except Exception:
            pass
    _om_fallback = shared.get("order_monitor")
    if _om_fallback is not None:
        crm_ctx.open_position_count = len(_om_fallback.get_monitored())


async def _refresh_per_broker_counters(
    shared: dict[str, Any],
    crm_ctx: Any,
    pe: Any,
    stacks: Mapping[str, Any],
    name_to_engine: dict[str, str],
) -> None:
    """Per-broker risk (system.per_broker_equity): the live counters of each live stack,
    from the `trades` rows its broker holds, under "live:<broker>" (+ ":<engine>") keys, and
    that broker's own intraday daily-loss breaker. A failed read drops the broker's keys, so
    CRM falls back to the combined "live" counters (never to zero)."""
    from src.risk.crm import resolve_engine_counts

    for _key in live_stacks(stacks):
        _broker = stack_broker(_key)
        if not _broker:
            continue
        try:
            c = await pe.daily_risk_counters(table="trades", exec_broker=_broker)
            _ic = await pe.daily_risk_counters(table="trades", product="MIS", exec_broker=_broker)
        except Exception as exc:  # noqa: BLE001
            _log.warning("per_broker_counters_refresh_failed key=%s err=%s", _key, exc)
            for _d in (
                crm_ctx.mode_new_trades_today,
                crm_ctx.mode_open_position_count,
                crm_ctx.mode_daily_loss_so_far,
                crm_ctx.mode_consecutive_losses,
                crm_ctx.mode_intraday_daily_loss_so_far,
            ):
                _d.pop(_key, None)
            for _eng in ("intraday", "swing"):
                crm_ctx.mode_engine_trades_today.pop(f"{_key}:{_eng}", None)
                crm_ctx.mode_engine_open_positions.pop(f"{_key}:{_eng}", None)
            continue
        crm_ctx.mode_new_trades_today[_key] = int(c["new_trades_today"])
        crm_ctx.mode_open_position_count[_key] = int(c["open_position_count"])
        crm_ctx.mode_daily_loss_so_far[_key] = float(c["daily_loss_so_far"])
        crm_ctx.mode_consecutive_losses[_key] = int(c["consecutive_losses"])
        crm_ctx.mode_intraday_daily_loss_so_far[_key] = float(_ic["daily_loss_so_far"])
        _ps = {str(k): int(v) for k, v in c.get("per_strategy", {}).items()}
        _ops = {str(k): int(v) for k, v in c.get("open_per_strategy", {}).items()}
        _it, _st = resolve_engine_counts(_ps, name_to_engine)
        _io, _so = resolve_engine_counts(_ops, name_to_engine)
        crm_ctx.mode_engine_trades_today[f"{_key}:intraday"] = _it
        crm_ctx.mode_engine_trades_today[f"{_key}:swing"] = _st
        crm_ctx.mode_engine_open_positions[f"{_key}:intraday"] = _io
        crm_ctx.mode_engine_open_positions[f"{_key}:swing"] = _so
        await _enforce_intraday_daily_loss_breaker(shared, crm_ctx, _key, pe)


def _held_symbols_from_holdings(holdings: Any) -> set[str]:
    """Bare symbols the broker currently holds as delivery stock.

    Kite's positions() is the DAY book: a CNC swing position bought on a previous
    trading day is only in holdings(), so a duplicate-entry guard built from
    positions() alone cannot see it. `t1_quantity` (bought T+1, not yet moved into
    `quantity`) is real held stock too — the same rule the reconciler applies."""
    out: set[str] = set()
    for h in holdings or []:
        sym = getattr(h, "symbol", None)
        qty = int(getattr(h, "quantity", 0) or 0) + int(getattr(h, "t1_quantity", 0) or 0)
        if sym and qty != 0:
            out.add(str(sym).split(":")[-1])
    return out


def _order_monitors_from_shared(shared: dict[str, Any]) -> list[Any]:
    """Every active stack's OrderMonitor — pass to PaperTradingEngine.
    square_off_open() so a force-close also untracks the in-memory position,
    not just the DB row (see square_off_open's order_monitors docstring)."""
    return [
        s.get("order_monitor")
        for s in (shared.get("stacks") or {}).values()
        if s.get("order_monitor") is not None
    ]


async def _enforce_intraday_daily_loss_breaker(
    shared: dict[str, Any], crm_ctx: Any, mode: str, pe: Any
) -> None:
    """config/capital.json intraday.max_loss_per_day (2026-10-07), checked PER MODE and
    scoped to INTRADAY (MIS) positions only -- completely independent of swing, which
    has no daily limit. Supersedes _enforce_daily_loss_breaker's combined-pot trigger
    for the main loop (see that call site's comment).

    PAPER: force-flattens every open intraday position at LTP, exactly as the older
    breaker did (pe.square_off_open is already MIS-only) -- safe, no real money.

    LIVE: deliberately does NOT auto-close real positions here -- there is no existing,
    tested live equivalent of square_off_open (it writes to paper_trades), and building
    one is a real-money action this change does not take lightly. G1's own per-mode
    intraday daily-loss check (src/risk/crm.py) already blocks every NEW live intraday
    entry once this cap is breached, which is the critical half (stop making it worse).
    This instead raises a loud, undismissable CRITICAL alert telling the operator the
    cap is breached and live intraday positions need a manual close.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    _per_broker = mode.startswith("live:")  # only with system.per_broker_equity on
    if _per_broker:
        # That broker's own cap (its equity x capital.json %); none => equity unknown, and
        # CRM G1 already blocks the broker's entries.
        cap = float(
            (getattr(crm_ctx, "mode_rupee_caps", {}) or {})
            .get(mode, {})
            .get("max_intraday_daily_loss", 0.0)
        )
    else:
        cap = float(getattr(crm_ctx, "max_intraday_daily_loss", 0.0) or 0.0)
    loss = float(getattr(crm_ctx, "mode_intraday_daily_loss_so_far", {}).get(mode, 0.0))
    if cap <= 0 or loss < cap:
        return
    today = _dt.now(_ZI("Asia/Kolkata")).date().isoformat()
    _flag = f"intraday_daily_loss_flat_date:{mode}"
    if shared.get(_flag) == today:
        return  # already handled today — do not re-fire every cycle
    shared[_flag] = today
    _log = get_logger("orchestrator")
    if _per_broker:
        # Ruling (d): alert as before AND block new entries for THIS broker until the next
        # trading day (CRM G1 reads the same dict). Never auto-flatten real positions here.
        _broker = mode[len("live:") :]
        shared.setdefault("entries_blocked_brokers", {})[_broker] = today
        _log.error(
            "intraday_daily_loss_cap_breached_LIVE broker=%s loss=%.0f cap=%.0f "
            "-- new entries on this broker blocked until the next trading day",
            _broker,
            loss,
            cap,
        )
        try:
            from src.infrastructure.operator_alerts import raise_alert as _op_alert

            _op_alert(
                f"LIVE intraday daily-loss cap breached on broker {_broker}: loss "
                f"Rs{loss:.0f} >= cap Rs{cap:.0f}. ALL new entries on {_broker} are blocked "
                "until the next trading day (CRM G1), but its open LIVE positions were NOT "
                "auto-closed -- review and close manually.",
                source=f"intraday_daily_loss_live:{_broker}",
                tier="CRITICAL",
                dedupe_seconds=4 * 3600,
            )
        except Exception as exc:  # noqa: BLE001 — the ERROR log above already fired
            _log.error("intraday_daily_loss_live_alert_failed err=%s", exc)
        return
    if mode == "live":
        _log.error("intraday_daily_loss_cap_breached_LIVE loss=%.0f cap=%.0f", loss, cap)
        try:
            from src.infrastructure.operator_alerts import raise_alert as _op_alert

            _op_alert(
                f"LIVE intraday daily-loss cap breached: loss Rs{loss:.0f} >= cap "
                f"Rs{cap:.0f}. New LIVE intraday entries are now blocked (CRM G1), but "
                "open LIVE intraday positions were NOT auto-closed -- review and close "
                "manually.",
                source="intraday_daily_loss_live",
                tier="CRITICAL",
                dedupe_seconds=4 * 3600,
            )
        except Exception as exc:  # noqa: BLE001 — the ERROR log above already fired
            _log.error("intraday_daily_loss_live_alert_failed err=%s", exc)
        return
    if mode != "paper":
        return  # never let an unexpected label reach the paper flatten below
    if pe is None:
        return
    try:
        n = await pe.square_off_open(
            shared.get("ohlcv_repo"),
            reason="INTRADAY_DAILY_LOSS_BREAKER",
            price_mode="ltp",
            order_monitors=_order_monitors_from_shared(shared),
        )
        _log.error(
            "intraday_daily_loss_breaker_flattened mode=%s loss=%.0f cap=%.0f closed=%d",
            mode,
            loss,
            cap,
            n,
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("intraday_daily_loss_breaker_err mode=%s err=%s", mode, exc)


async def _enforce_daily_loss_breaker(shared: dict[str, Any], crm_ctx: Any, pe: Any) -> None:
    """Force-flatten ALL open paper positions once the day's combined realized +
    unrealized loss reaches the configured cap.

    The CRM G1 gate only REFUSES new entries at the cap; it cannot stop losses on
    positions already open, and it counted realized loss only — so a burst of
    simultaneous entries defeated it (they all opened before any booked a loss).
    This breaker acts on the mark-to-market loss (daily_risk_counters now folds in
    unrealized P&L) and squares off at LTP, once per day. After flattening, the
    booked loss is >= cap so the entry gate keeps new trades out for the day.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    cap = float(getattr(crm_ctx, "max_daily_loss", 0.0) or 0.0)
    if pe is None or cap <= 0 or float(crm_ctx.daily_loss_so_far) < cap:
        return
    today = _dt.now(_ZI("Asia/Kolkata")).date().isoformat()
    if shared.get("daily_loss_flat_date") == today:
        return  # already flattened today — do not re-fire every cycle
    shared["daily_loss_flat_date"] = today
    _log = get_logger("orchestrator")
    try:
        n = await pe.square_off_open(
            shared.get("ohlcv_repo"),
            reason="DAILY_LOSS_BREAKER",
            price_mode="ltp",
            order_monitors=_order_monitors_from_shared(shared),
        )
        _log.error(
            "daily_loss_breaker_flattened loss=%.0f cap=%.0f closed=%d",
            float(crm_ctx.daily_loss_so_far),
            cap,
            n,
        )
    except Exception as _fe:
        _log.error("daily_loss_breaker_err err=%s", _fe)


async def _intraday_signal_scan_loop(
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Scan 5MIN/15MIN strategies (e.g. ORB) every 5 min during market hours (9:30–15:15)."""
    import time as _cycle_time_mod
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")
    _log_i = _intraday_log  # module-level; every 5MIN/15MIN DSL strategy's activity

    # Seed the day-scoped intraday regime from the last persisted scan so a launcher
    # restart mid-session keeps showing the previously computed trend instead of
    # "UNKNOWN" until the first fresh classification lands. Only today's value is
    # reused — a new trading day legitimately starts UNKNOWN until the first scan.
    try:
        import json as _json_seed

        _seed_path = _LOGS / "intraday_signals.json"
        if _seed_path.exists():
            _seed = _json_seed.loads(_seed_path.read_text(encoding="utf-8"))
            _seed_regime = str(_seed.get("intraday_regime", "") or "")
            _seed_ts = str(_seed.get("ts", ""))
            if _seed_regime and _seed_regime != "UNKNOWN" and _seed_ts:
                _seed_date = datetime.fromisoformat(_seed_ts).astimezone(_IST).date()
                if _seed_date == datetime.now(_IST).date():
                    shared["intraday_regime"] = _seed_regime
                    _log_i.info("intraday_regime_seeded regime=%s from=%s", _seed_regime, _seed_ts)
    except Exception as _se:
        _log_i.debug("intraday_regime_seed_err %s", str(_se)[:80])

    # G9 LLM client — built once, shared with _execute_intraday_signal so intraday
    # signals get the same mandatory blocking LLM check as the daily strategies.
    from src.llm.providers import make_trade_llm_client as _make_trade_llm_client

    # Trade gate provider order: GEMINI → GROQ → OLLAMA (local qwen, last resort).
    shared["g9_llm"] = _make_trade_llm_client()
    if shared["g9_llm"] is None:
        _log_i.warning("intraday_llm_disabled reason=no_provider (GEMINI/GROQ/OLLAMA)")

    while not stop.is_set():
        now = datetime.now(_IST)
        mins = now.hour * 60 + now.minute
        # 2026-09-21: the per-symbol scan body measured fast (duration_s ~8s
        # for 497 symbols) while completed cycles were 10-44 min apart -- the
        # gap was somewhere BEFORE that measured section. _iter_t0 times the
        # whole iteration so the stages below (daily counters, broker OHLCV
        # refresh) can log their own elapsed-since-start and pinpoint it.
        _iter_t0 = _cycle_time_mod.time()
        _log_i.info("intraday_cycle_stage", extra={"ctx": {"stage": "iteration_start"}})
        # Hard exit-window flat-by (paper): from the strategies' exit-window START
        # (~15:10) force-close ALL open paper positions priced at the current LTP and
        # stamped at the flatten moment — independent of per-symbol smart-exit trackers
        # (so restart-orphaned rows are covered too) and well before the 15:20 deadline.
        # The 15:35 EOD sweep below remains only as a last resort.
        if (
            now.weekday() < 5
            and mins >= _exit_window_start_min(shared)
            and _intraday_segment_is_paper(shared)
        ):
            _fb_day = now.date().isoformat()
            if shared.get("paper_flatby_date") != _fb_day:
                shared["paper_flatby_date"] = _fb_day
                _pe_fb = shared.get("paper_ledger")
                if _pe_fb is not None:
                    try:
                        _n_fb = await _pe_fb.square_off_open(
                            shared.get("ohlcv_repo"),
                            reason="EXIT_WINDOW_FLAT",
                            price_mode="ltp",
                            order_monitors=_order_monitors_from_shared(shared),
                        )
                        if _n_fb:
                            shared.get("intraday_exit_state", {}).clear()
                            _log_i.info("paper_exit_window_flat closed=%d at_min=%d", _n_fb, mins)
                    except Exception as _fb_exc:
                        _log_i.error("paper_exit_window_flat_err err=%s", _fb_exc)
        # EOD square-off backstop — once per day at/after 15:35 IST, force-close
        # any paper positions still open today (the 15:10–15:15 smart-exit window
        # only fires for positions with a seeded tracker and a live scan). Runs
        # outside the 09:30–15:15 trading gate below.
        if (
            now.weekday() < 5
            and mins >= (_sessions.minutes(_sessions.regular_close()) + 5)
            and _intraday_segment_is_paper(shared)
        ):
            _eod_day = now.date().isoformat()
            if shared.get("paper_eod_squared_date") != _eod_day:
                shared["paper_eod_squared_date"] = _eod_day
                _pe_eod = shared.get("paper_ledger")
                if _pe_eod is not None:
                    try:
                        _n_eod = await _pe_eod.square_off_open(
                            shared.get("ohlcv_repo"),
                            reason="EOD_SQUAREOFF",
                            order_monitors=_order_monitors_from_shared(shared),
                        )
                        if _n_eod:
                            _log_i.info("paper_eod_squareoff closed=%d", _n_eod)
                    except Exception as _eod_exc:
                        _log_i.error("paper_eod_squareoff_err err=%s", _eod_exc)
        # Keep the CRM caps live across restarts: refresh all day-scoped counters
        # from the DB (source of truth) every cycle so G3/G5/G7 enforce correctly
        # for the whole day — a launcher restart never resets them. Unconditional
        # (not gated to the 09:15-15:15 intraday window below) because swing/CNC
        # strategies (e.g. oneil_canslim) place trades via _crm_loop in the evening
        # too; when this refresh was market-hours-only, an evening restart booted
        # crm_ctx.per_strategy_trades_today back to a stale/zero count with no path
        # to correct it, silently defeating the per-strategy daily cap after hours
        # (observed: 11 oneil_canslim trades in a day against a cap of 2).
        if is_trading_day(now.date()):
            _crm_ctx = shared.get("crm_ctx")
            if _crm_ctx is not None:
                # 2026-09-21: after a restart this loop went 14+ min with no log line
                # at all and no way to tell which unbounded await it sat in. Bound to
                # one bar (a stage that outlasts the 5m bar has already missed it).
                # On timeout SKIP the cycle: CRM caps would be unverified, and the
                # system must fail safe rather than trade on stale risk counters.
                try:
                    await asyncio.wait_for(
                        _refresh_daily_counters(shared, _crm_ctx), timeout=_INTRADAY_STAGE_TIMEOUT_S
                    )
                except TimeoutError:
                    _log_i.error(
                        "intraday_stage_timeout stage=daily_counters timeout_s=%d — "
                        "skipping cycle (risk counters unverified)",
                        _INTRADAY_STAGE_TIMEOUT_S,
                    )
                    await asyncio.sleep(_seconds_to_next_bar(datetime.now(_IST)))
                    continue
                _log_i.info(
                    "intraday_cycle_stage",
                    extra={
                        "ctx": {
                            "stage": "daily_counters",
                            "elapsed_s": round(_cycle_time_mod.time() - _iter_t0, 1),
                        }
                    },
                )
        # Runs from market open — NOT from the entry window start. The compiled
        # entry_window mask (derived from range_window, see compute_session_levels)
        # is the single source of truth for when entries are allowed; gating the
        # scan itself to a later clock time risked skipping the first post-range
        # candle before the scan ever looked at it (2026-07-30: 09:30 start missed
        # the 09:20-09:25 breakout candle for BHARTIARTL/M&M).
        if is_trading_day(now.date()) and _sessions.minutes(
            _sessions.regular_open()
        ) <= mins <= _sessions.minutes(_sessions.exit_only_start()):
            strategy_reg = shared.get("strategy_registry")
            ohlcv_repo = shared.get("ohlcv_repo")
            if strategy_reg is not None and ohlcv_repo is not None:
                intraday_compiled = [
                    c
                    for c in strategy_reg.all_enabled()
                    if (getattr(c.spec.meta, "timeframe", "") or "").upper()
                    in ("5MIN", "15MIN", "5M", "15M")
                ]
                if not intraday_compiled:
                    await asyncio.sleep(_seconds_to_next_bar(datetime.now(_IST)))
                    continue
                # Fetch the just-closed 5m bar for the watchlist FIRST, so every
                # strategy below evaluates on the bar that actually just closed. This
                # is what removes the multi-minute entry lag vs TradingView: the scan
                # no longer waits on the separate ingest loop's unsynchronized poll.
                _scan_broker = getattr(shared.get("lifecycle"), "_broker", None)
                if _scan_broker is not None:
                    # Bound = llm_intraday.idea_ttl_minutes: a refresh slower than that
                    # can no longer yield a tradeable idea. Upserts are incremental, so
                    # a cancelled refresh keeps its progress for the next cycle. Scanning
                    # on stale bars is worse than skipping, so skip on timeout.
                    _refresh_timeout_s = (
                        float(_cfg("global").get("llm_intraday", {}).get("idea_ttl_minutes", 15))
                        * 60.0
                    )
                    try:
                        await asyncio.wait_for(
                            _refresh_intraday_ohlcv(_scan_broker, shared, _SCAN_TFS, now),
                            timeout=_refresh_timeout_s,
                        )
                    except TimeoutError:
                        _log_i.error(
                            "intraday_stage_timeout stage=broker_ohlcv_refresh timeout_s=%d — "
                            "skipping cycle (bars stale)",
                            int(_refresh_timeout_s),
                        )
                        await asyncio.sleep(_seconds_to_next_bar(datetime.now(_IST)))
                        continue
                    _log_i.info(
                        "intraday_cycle_stage",
                        extra={
                            "ctx": {
                                "stage": "broker_ohlcv_refresh",
                                "elapsed_s": round(_cycle_time_mod.time() - _iter_t0, 1),
                            }
                        },
                    )
                # Load the benchmark (e.g. NIFTY 50) once for the market-direction
                # gate, then attach to each intraday strategy before scanning.
                _bench_cache: dict[str, Any] = {}
                for compiled in intraday_compiled:
                    _mf = getattr(compiled.spec, "market_filter", None)
                    if _mf is not None and getattr(_mf, "enabled", False):
                        _bsym = _mf.symbol
                        if _bsym not in _bench_cache:
                            try:
                                _bench_cache[_bsym] = await ohlcv_repo.fetch_dataframe(
                                    _bsym, "5m", 120
                                )
                            except Exception:
                                _bench_cache[_bsym] = None
                        compiled.set_market_context(_bench_cache.get(_bsym))
                symbols: list[str] = list(shared.get("watchlist", []))
                # "Stocks to watch" names scan first (stable sort keeps the rest in order).
                _pri_syms = shared.get("news_priority_symbols", {})
                symbols = sorted(symbols, key=lambda s: 0 if s in _pri_syms else 1)
                _cycle_signals: list[dict[str, Any]] = []
                # Every gate-eligible, scored candidate this cycle. Placement picks
                # the best-ranked entry out of this pool once the whole watchlist
                # has been scanned — see the ranked-placement block below the loop.
                _cycle_candidates: list[dict[str, Any]] = []
                # (symbol, strategy) -> the live compiled/sig objects placement needs.
                # Kept out of _cycle_candidates so that list stays JSON-serialisable
                # for the dashboard status file.
                _cand_payload: dict[tuple[str, str], dict[str, Any]] = {}
                _cycle_placed: list[dict[str, Any]] = []
                _scanned = 0
                # Cycle-duration split (2026-09-21: the "5-min" scan below was
                # empirically running every ~10 min every sampled day) -- fetch_s
                # isolates time spent awaiting the per-symbol OHLCV DB fetch from
                # the rest (indicator/strategy compute), so a slow cycle can be
                # attributed to DB/pool contention vs CPU-bound work instead of
                # guessed at.
                import time as _time_mod

                _cycle_start_t = _time_mod.time()
                _fetch_s = 0.0
                # At most ONE new intraday entry is PLACED per scan cycle so two
                # trades are never fired at the same time. Without this, one cycle's
                # symbol loop placed an order for every matching symbol back-to-back
                # (2026-07-20: 12 fills in 4 minutes, blowing past the daily cap).
                _entered_this_cycle = False
                # news_catalyst_momentum gets its own reserved slot, separate from the
                # structural strategies' one-per-cycle cap — a real catalyst must never
                # lose its (20-min-fresh) window because ORB already filled this cycle.
                _news_entered_this_cycle = False
                _bear_sym_count = 0  # symbols with close < EMA20 this cycle
                _bear_sym_total = 0
                # Day-scoped intraday regime (operator rule): benchmark day-move +
                # watchlist breadth, refreshed EVERY scan cycle — the structural
                # (daily-candle) regime is too slow to catch an intraday U-turn.
                # Thresholds from operator.json hard_limits.intraday.regime.
                try:
                    from src.execution.entry_guard import classify_intraday_regime

                    _rg_cfg = shared.get("intraday_regime_cfg", {}) or {}
                    _idx_sym = str(_rg_cfg.get("index_symbol", "NIFTY 50"))
                    _idx_chg: float | None = None
                    # Prefer the LIVE index day-change published by _regime_loop
                    # (broker LTP, every 5 min). The DB's index 5m candles only
                    # refresh at boot — 2026-07-09 they froze idx_chg all day and
                    # the gate blocked the wrong direction. Staleness cap from
                    # config (default 900s) → fall back to candles, then UNKNOWN.
                    import time as _time_mod

                    _pub_ts = float(shared.get("index_day_change_ts", 0.0))
                    _max_age = float(_rg_cfg.get("index_ltp_max_age_s", 900))
                    if _pub_ts > 0 and (_time_mod.time() - _pub_ts) <= _max_age:
                        _idx_chg = float(shared["index_day_change_pct"])
                    else:
                        _idx_df = await ohlcv_repo.fetch_dataframe(_idx_sym, "5m", 100)
                        if _idx_df is not None and len(_idx_df) > 1:
                            _idx_dates = _idx_df.index.tz_convert("Asia/Kolkata").date
                            _prev_closes = _idx_df["close"][_idx_dates != _idx_dates[-1]]
                            if len(_prev_closes) > 0:
                                _prev_c = float(_prev_closes.iloc[-1])
                                _last_c = float(_idx_df["close"].iloc[-1])
                                # candles must include TODAY's bars to be usable
                                _today_ist = now.astimezone(ZoneInfo("Asia/Kolkata")).date()
                                if _idx_dates[-1] == _today_ist:
                                    _idx_chg = (_last_c - _prev_c) / _prev_c * 100
                    _new_regime = classify_intraday_regime(
                        _idx_chg,
                        shared.get("_bearish_pct"),
                        index_move_min_pct=float(_rg_cfg.get("index_move_min_pct", 0.10)),
                        breadth_confirm=float(_rg_cfg.get("breadth_confirm", 0.50)),
                    )
                    _prev_regime = shared.get("intraday_regime")
                    # Sticky: a transient UNKNOWN (index LTP not yet published right
                    # after a launcher restart, or a data gap) must NOT wipe the last
                    # good day-scoped trend — keep showing it until a real regime is
                    # computed. Seeded from today's persisted value on boot (above).
                    if _new_regime == "UNKNOWN" and _prev_regime not in (None, "", "UNKNOWN"):
                        _new_regime = str(_prev_regime)
                    if _new_regime != _prev_regime:
                        _log_i.info(
                            "intraday_regime_change regime=%s idx_chg=%s bearish_pct=%.2f",
                            _new_regime,
                            f"{_idx_chg:+.2f}%" if _idx_chg is not None else "n/a",
                            float(shared.get("_bearish_pct", 0.5)),
                        )
                    shared["intraday_regime"] = _new_regime
                except Exception as _rg_err:
                    # Keep the last good value across transient errors; UNKNOWN only if
                    # we never computed one this session.
                    shared.setdefault("intraday_regime", "UNKNOWN")
                    _log_i.debug("intraday_regime_err %s", str(_rg_err)[:80])
                # ---- LLM idea audit: what ideas are live going into this cycle ------------
                _llm_seen: set[str] = set()  # ideas the entry check actually ran for
                _live_ideas = shared.get("llm_intraday_ideas", {})
                if _live_ideas:
                    from src.strategies.llm_intraday.funnel import funnel_log as _funnel_log

                    _funnel_log(
                        _log_i,
                        agent="llm_intraday_agent",
                        stage="cycle",
                        outcome="live_ideas",
                        ideas=",".join(
                            f"{k}({(now.timestamp() - float(v.get('ts', 0))) / 60:.0f}m)"
                            for k, v in _live_ideas.items()
                        ),
                        watchlist=len(symbols),
                    )
                _sym_errors: list[str] = []
                for sym in symbols:
                    try:
                        _fetch_t0 = _time_mod.time()
                        df = await ohlcv_repo.fetch_dataframe(sym, "5m", 100)
                        _fetch_s += _time_mod.time() - _fetch_t0
                        if df is None or len(df) < 20:
                            continue
                        _scanned += 1
                        # Setup score for llm_intraday's signal trigger, from the df already
                        # in hand (no extra fetch): |chg%| x vol_ratio20 (pipeline.quick_setup_score).
                        try:
                            from src.strategies.llm_intraday.pipeline import quick_setup_score

                            _qs = quick_setup_score(df, now)
                            if _qs is not None:
                                shared.setdefault("intraday_setup_scores", {})[
                                    sym.split(":")[-1]
                                ] = (_qs, _time_mod.time())
                        except Exception:
                            pass
                        # Accumulate per-symbol bearish indicator (close < EMA20)
                        # for the BEAR gate; used on the NEXT cycle (5-min lag OK).
                        try:
                            from src.strategies.uss.compiler import (  # type: ignore[attr-defined]
                                compute as _compute_ind,
                            )

                            _ema20_val = float(_compute_ind(df, "EMA", period=20).iloc[-1])
                            _bear_sym_total += 1
                            if float(df["close"].iloc[-1]) < _ema20_val:
                                _bear_sym_count += 1
                        except Exception:
                            pass
                        # Smart-exit management for an open paper position on this
                        # symbol (5-min cadence). Uses the SAME stepper as the
                        # backtest → live ride matches Pine (partial/BE/trail/reversal).
                        _ex = shared.get("intraday_exit_state", {}).get(sym)
                        if _ex is not None:
                            # The stack whose monitor tracks the trade (any broker), not
                            # the primary alias -- else a live position looks "closed".
                            _om = _monitor_tracking(shared, _ex["tid"])
                            _tid2 = _ex["tid"]
                            if _om is None or _om.get(_tid2) is None:
                                # Position already closed (SL/target via 2s monitor) — drop it.
                                shared.get("intraday_exit_state", {}).pop(sym, None)
                            else:
                                _mins2 = now.hour * 60 + now.minute
                                _in_exit_win = _mins2 >= _exit_window_start_min(shared)
                                _be = _ex["tracker"].step(df, _in_exit_win)
                                _lastc = float(df["close"].iloc[-1])
                                if _be.booked_partial:
                                    await _om.book_partial(_tid2, 0.5, _lastc)
                                if _be.exit:
                                    await _om.force_exit(_tid2, _lastc, _be.reason)
                                    shared.get("intraday_exit_state", {}).pop(sym, None)
                                    _log_i.info(
                                        "intraday_smart_exit symbol=%s reason=%s price=%.2f",
                                        sym,
                                        _be.reason,
                                        _lastc,
                                    )
                                elif _be.sl_changed:
                                    await _om.update_sl(_tid2, _ex["tracker"].current_sl)
                                    _log_i.info(
                                        "intraday_sl_ratchet symbol=%s new_sl=%.2f partial=%s",
                                        sym,
                                        _ex["tracker"].current_sl,
                                        _be.booked_partial,
                                    )
                        for compiled in intraday_compiled:
                            _is_news_strategy = compiled.spec.meta.name == "news_catalyst_momentum"
                            # COLLECT ONLY — no order is placed inside this loop.
                            # This pass gates and scores every candidate into
                            # _cycle_candidates; ranked placement happens once, after
                            # the whole watchlist has been seen (see the "ranked
                            # placement" block below the symbol loop). Placing inline
                            # here meant the first symbol in watchlist order (which is
                            # alphabetical) consumed the cycle's only slot — the shadow
                            # ranker disagreed with that pick in 16 of 16 logged cycles
                            # (2026-08-31 → 09-08), so ordering, not setup quality, was
                            # choosing the trade. Mirrors the swing path, which has
                            # pooled-then-ranked via select_top_candidates all along.
                            _wl = compiled.spec.universe_filter.symbols
                            if _wl and sym not in _wl:
                                continue
                            try:
                                if _is_news_strategy:
                                    sig = _evaluate_news_catalyst_signal(
                                        compiled, shared, sym, df, now
                                    )
                                elif compiled.spec.meta.name == "llm_intraday_agent":
                                    # Idea published by _llm_intraday_agent_loop; uses the
                                    # structural slot and stays subject to the regime and
                                    # breadth gates below (unlike news).
                                    #
                                    # `now` is read fresh here rather than reusing the
                                    # cycle-level `now` captured at loop entry: on a slow
                                    # cycle (497 symbols evaluated sequentially) the TTL/
                                    # cutoff checks inside would otherwise drift stale for
                                    # a late-ordered symbol (2026-09-18: a 20-min-old idea
                                    # was still treated as within its 15-min TTL this way).
                                    _llm_seen.add(sym.split(":")[-1])
                                    sig = _evaluate_llm_intraday_signal(
                                        compiled, shared, sym, df, datetime.now(_IST)
                                    )
                                else:
                                    sig = compiled.evaluate_candle(df, len(df) - 1)
                                if sig is not None:
                                    _log_i.info(
                                        "intraday_signal",
                                        extra={
                                            "ctx": {
                                                "strategy": compiled.spec.meta.name,
                                                "symbol": sym,
                                                "entry": sig.entry_price,
                                                "sl": sig.sl,
                                                "target": sig.target,
                                                # per-trade news audit: was this symbol
                                                # news-prioritized this cycle?
                                                "news_pri": bool(
                                                    shared.get("news_priority_symbols", {}).get(sym)
                                                ),
                                                "regime": shared.get("intraday_regime", "UNKNOWN"),
                                            }
                                        },
                                    )
                                    _rec = {
                                        "strategy": compiled.spec.meta.name,
                                        "symbol": sym,
                                        "entry": sig.entry_price,
                                        "sl": sig.sl,
                                        "target": sig.target,
                                        "ts": now.isoformat(),
                                    }
                                    sigs: list[dict[str, Any]] = shared.setdefault(
                                        "intraday_signals", []
                                    )
                                    sigs.append(_rec)
                                    _cycle_signals.append(_rec)
                                    # Regime/breadth gates exist to block counter-trend
                                    # price-shape entries (ORB etc.) — they don't apply to
                                    # news_catalyst_momentum, which trades a real, already-
                                    # confirmed corporate event (VWAP+volume-gated in
                                    # _evaluate_news_catalyst_signal), not a chart pattern
                                    # riding the day's index trend.
                                    # 2026-10-06 (operator decision, paper AND live): the
                                    # regime + breadth direction gates below are off for
                                    # llm_intraday_agent when llm_intraday.direction_gates_enabled
                                    # is false. 2-week replay: 6 gate-blocked ideas +2.8R vs 3
                                    # taken -3.0R (tiny n). The counter-trend-short block
                                    # (execution stage, 409-trade evidence) still applies.
                                    _gates_off = (
                                        compiled.spec.meta.name == "llm_intraday_agent"
                                        and not bool(
                                            _cfg("global")
                                            .get("llm_intraday", {})
                                            .get("direction_gates_enabled", True)
                                        )
                                    )
                                    if not _is_news_strategy and not _gates_off:
                                        # HARD structural-regime gate (operator rule): the
                                        # displayed regime (shared["regime"], same value stamped
                                        # on trades as regime_at_entry) blocks counter-trend
                                        # entries outright — BEAR* → no LONG, BULL* → no SHORT.
                                        # 2026-07-08 postmortem: BEAR-tagged longs (SBIN, M&M)
                                        # passed because only the slower breadth gate below ran.
                                        from src.execution.entry_guard import (
                                            regime_blocks_direction as _regime_blocks,
                                        )

                                        # Day-scoped regime first (refreshed every cycle).
                                        # When it is UNKNOWN (index feed down), behavior is
                                        # config-driven — 2026-07-10 the structural fallback
                                        # said BEAR on a +1% NIFTY day and wrongly blocked
                                        # longs, so "structural" is no longer the default:
                                        #   block_all  (default): no data → no trades
                                        #   structural: fall back to the slow daily regime
                                        #   allow_all : gate stands aside
                                        _regime_now = str(
                                            shared.get("intraday_regime") or "UNKNOWN"
                                        )
                                        if _regime_now == "UNKNOWN":
                                            _fb = str(
                                                (shared.get("intraday_regime_cfg", {}) or {}).get(
                                                    "fallback", "block_all"
                                                )
                                            )
                                            if _fb == "structural":
                                                _regime_now = str(shared.get("regime", "UNKNOWN"))
                                            elif _fb == "block_all" and bool(
                                                shared.get("structural_regime_gate", True)
                                            ):
                                                _log_i.warning(
                                                    "intraday_regime_gate_blocked sym=%s dir=%s "
                                                    "regime=UNKNOWN fallback=block_all",
                                                    sym,
                                                    sig.direction.value,
                                                )
                                                continue
                                        if bool(
                                            shared.get("structural_regime_gate", True)
                                        ) and _regime_blocks(_regime_now, sig.direction.value):
                                            _log_i.warning(
                                                "intraday_regime_gate_blocked sym=%s dir=%s regime=%s",
                                                sym,
                                                sig.direction.value,
                                                _regime_now,
                                            )
                                            if compiled.spec.meta.name == "llm_intraday_agent":
                                                _llm_idea_funnel(
                                                    shared,
                                                    now,
                                                    "gate",
                                                    "blocked",
                                                    sym,
                                                    "regime_gate",
                                                    f"{sig.direction.value} not allowed in {_regime_now} "
                                                    "regime (BEAR blocks LONG, BULL blocks SHORT)",
                                                )
                                            continue
                                        # Regime gate: BEAR blocks LONGs; BULL blocks SHORTs.
                                        # Fires when ≥80% of watchlist is on the same side as
                                        # the broad regime — confirms the move is universe-wide,
                                        # not just the index. Neutral regime → both directions open.
                                        # Intraday breadth gate — uses 5-min EMA20 breadth, NOT
                                        # structural regime (MA50/150/200 is for swing trades).
                                        # Default 0.5 = unknown (first cycle); neither gate fires.
                                        _bearish_pct = shared.get("_bearish_pct", 0.5)
                                        _gate_threshold = float(
                                            shared.get("bear_gate_threshold", 0.60)
                                        )
                                        # HIGH-magnitude MACRO news tightens the counter-trend
                                        # block only (lowers the matching threshold); it never
                                        # loosens the opposite side. Adds caution, never removes it.
                                        from src.news.context import macro_gate_thresholds

                                        _bear_thr, _bull_thr = macro_gate_thresholds(
                                            _gate_threshold,
                                            shared.get("news_macro_bias"),
                                            now.timestamp(),
                                        )
                                        _bear_gate = _bearish_pct >= _bear_thr
                                        _bull_gate = (1.0 - _bearish_pct) >= _bull_thr
                                        _sig_dir = sig.direction.value
                                        if (_bear_gate and _sig_dir == "LONG") or (
                                            _bull_gate and _sig_dir == "SHORT"
                                        ):
                                            _log_i.warning(
                                                "intraday_breadth_gate_blocked sym=%s dir=%s bear_pct=%.0f%%",
                                                sym,
                                                _sig_dir,
                                                _bearish_pct * 100,
                                            )
                                            if compiled.spec.meta.name == "llm_intraday_agent":
                                                _llm_idea_funnel(
                                                    shared,
                                                    now,
                                                    "gate",
                                                    "blocked",
                                                    sym,
                                                    "breadth_gate",
                                                    f"{_sig_dir} blocked: {_bearish_pct * 100:.0f}% of "
                                                    "watchlist is below its 5-min EMA20 (counter-trend to breadth)",
                                                )
                                            continue
                                    # ELIGIBLE — score it into the cycle pool. Scoring sits
                                    # BELOW the regime/breadth gates deliberately: while it
                                    # ran above them the shadow pool contained candidates
                                    # the gates would have refused, so the ranker's "best"
                                    # could be a symbol the system was never allowed to
                                    # trade. Everything reaching here has already cleared
                                    # both gates, so the pool is genuinely placeable.
                                    # Wrapped broadly — a scoring failure must not drop an
                                    # otherwise valid signal, so it falls back to a neutral
                                    # 0.5 and still competes.
                                    try:
                                        from src.execution.signal_ranker import (
                                            score_candidate as _score_cand,
                                        )

                                        _orb_w = None
                                        _orb_h = getattr(compiled, "_orb_high", None)
                                        _orb_l = getattr(compiled, "_orb_low", None)
                                        if _orb_h is not None and _orb_l is not None:
                                            _hi = float(_orb_h.iloc[-1])
                                            _lo = float(_orb_l.iloc[-1])
                                            _px = float(df["close"].iloc[-1])
                                            if _px > 0 and _hi == _hi and _lo == _lo:
                                                _orb_w = (_hi - _lo) / _px * 100.0
                                        _vol = float(df["volume"].iloc[-1])
                                        _avg_vol = float(df["volume"].iloc[-21:-1].mean())
                                        _cs = _score_cand(
                                            bar_high=float(df["high"].iloc[-1]),
                                            bar_low=float(df["low"].iloc[-1]),
                                            bar_close=float(df["close"].iloc[-1]),
                                            is_short=(sig.direction.value == "SHORT"),
                                            breakout_volume=_vol,
                                            avg_volume=_avg_vol,
                                            or_width_pct=_orb_w,
                                            nifty_aligned=_nifty_aligned_for_cycle(
                                                shared, sig.direction.value
                                            ),
                                        )
                                        _score, _components = _cs.score, _cs.components
                                    except Exception as _rk:
                                        _score, _components = 0.5, {}
                                        _log_i.debug(
                                            "signal_rank_err",
                                            extra={"ctx": {"sym": sym, "err": str(_rk)[:80]}},
                                        )
                                    # Signal Quality Score (spec §10) — shadow-only, gated
                                    # by config/learning.json signal_ranker.mode_intraday.
                                    # Logged next to the live score for later validation;
                                    # never affects ranking/placement in this mode.
                                    _sqs = None
                                    if _signal_ranker_mode("intraday") in ("shadow", "live"):
                                        try:
                                            from src.execution.signal_quality_adapter import (
                                                build_signal_quality_input,
                                            )
                                            from src.intelligence.signal_quality_score import (
                                                compute as _sqs_compute,
                                            )

                                            _sec = _resolve_sector(sym)
                                            _bare = sym.split(":")[-1]
                                            _bt = strategy_reg.backtest_result(
                                                compiled.spec.meta.name
                                            )
                                            _bt_at = strategy_reg.backtest_computed_at(
                                                compiled.spec.meta.name
                                            )
                                            _sqs_res = _sqs_compute(
                                                build_signal_quality_input(
                                                    bar_high=float(df["high"].iloc[-1]),
                                                    bar_low=float(df["low"].iloc[-1]),
                                                    bar_close=float(df["close"].iloc[-1]),
                                                    is_short=(sig.direction.value == "SHORT"),
                                                    breakout_volume=_vol,
                                                    avg_volume=_avg_vol,
                                                    sector=_sec,
                                                    sector_status=shared.get("sector_status", {}),
                                                    sector_records=shared.get("sector_records", {}),
                                                    regime=str(shared.get("regime", "NEUTRAL")),
                                                    news_sentiment=(
                                                        shared.get("news_recent", {}).get(_bare, {})
                                                        or {}
                                                    ).get("sentiment"),
                                                    backtest_win_rate=getattr(
                                                        _bt, "win_rate", None
                                                    ),
                                                    backtest_avg_r=getattr(
                                                        _bt, "expectancy_r", None
                                                    ),
                                                    backtest_computed_at=_bt_at,
                                                )
                                            )
                                            _sqs = {
                                                "total": _sqs_res.total,
                                                "setup": _sqs_res.setup_quality,
                                                "hist": _sqs_res.historical_edge,
                                                "ctx": _sqs_res.context_quality,
                                                "llm": _sqs_res.llm_assessment,
                                            }
                                        except Exception as _sqe:
                                            _log_i.debug(
                                                "sqs_shadow_err",
                                                extra={"ctx": {"sym": sym, "err": str(_sqe)[:80]}},
                                            )
                                    _cand_key = (sym, compiled.spec.meta.name)
                                    _cand_entry: dict[str, Any] = {
                                        "symbol": sym,
                                        "strategy": compiled.spec.meta.name,
                                        "direction": sig.direction.value,
                                        "score": _score,
                                        "components": _components,
                                        "sqs": _sqs,
                                        "placeable": True,
                                    }
                                    _pw_intraday = _strategy_priority_weight(
                                        shared,
                                        compiled.spec.meta.name,
                                        data_mode=_segment_mode(shared, "intraday"),
                                    )
                                    if (
                                        _learning_mode("strategy_selector") == "live"
                                        and _pw_intraday is not None
                                    ):
                                        _cand_entry["priority_weight"] = _pw_intraday
                                    # JSON-safe record for the log/status file...
                                    _cycle_candidates.append(_cand_entry)
                                    # ...and the live objects placement needs, held apart
                                    # so the status-file dump stays serialisable.
                                    _cand_payload[_cand_key] = {
                                        "compiled": compiled,
                                        "sig": sig,
                                        "ref_ltp": float(df["close"].iloc[-1]),
                                        "is_news": _is_news_strategy,
                                    }
                            except Exception as _se:
                                _log_i.debug(
                                    "intraday_signal_err",
                                    extra={
                                        "ctx": {
                                            "strategy": compiled.spec.meta.name,
                                            "symbol": sym,
                                            "err": str(_se)[:80],
                                        }
                                    },
                                )
                    except Exception as _sym_exc:
                        # Was a bare `continue` -- any failure in this symbol's fetch/exit/
                        # entry processing vanished with no trace. Collected and reported
                        # once per cycle below (a systematic fault would otherwise print
                        # one line per symbol).
                        _sym_errors.append(f"{sym.split(':')[-1]}:{str(_sym_exc)[:70]}")
                        continue

                if _sym_errors:
                    _log_i.warning(
                        "intraday_symbol_loop_errors count=%d of %d first=%s",
                        len(_sym_errors),
                        len(symbols),
                        _sym_errors[:3],
                    )
                # Ideas whose symbol was never reached this cycle (not in the watchlist, or
                # no/short 5-min data) used to sit unevaluated with no trace until replaced --
                # the TTL check lives inside the evaluator they never reached.
                _ttl_min_c = float(
                    _cfg("global").get("llm_intraday", {}).get("idea_ttl_minutes", 15)
                )
                for _isym, _idea in list(shared.get("llm_intraday_ideas", {}).items()):
                    if _isym in _llm_seen:
                        continue
                    _age_c = (now.timestamp() - float(_idea.get("ts", 0))) / 60.0
                    if _age_c > _ttl_min_c:
                        shared.get("llm_intraday_ideas", {}).pop(_isym, None)
                        _llm_idea_funnel(
                            shared,
                            now,
                            "entry_check",
                            "expired",
                            _isym,
                            "ttl_expired_never_evaluated",
                            f"idea aged {_age_c:.0f} min without ever being price-checked: symbol not "
                            "scanned this cycle (not in watchlist, or no/short 5-min data)",
                        )
                    else:
                        _llm_idea_funnel(
                            shared,
                            now,
                            "entry_check",
                            "failed",
                            _isym,
                            "symbol_not_scanned",
                            "idea cannot be price-checked: symbol not scanned this cycle "
                            "(not in watchlist, or no/short 5-min data)",
                            level=30,
                        )

                # ---- RANKED PLACEMENT -------------------------------------------
                # The whole watchlist has now been scanned and every gate-eligible
                # candidate scored. Place the BEST one rather than the one that
                # happened to sort first. Slot caps are unchanged: one structural
                # entry per cycle plus the reserved news slot.
                #
                # Falls through to the next-best candidate when the top pick is
                # refused downstream (CRM G0-G10, including the G9 LLM veto), so a
                # single rejection no longer costs the cycle its trade — the old
                # inline loop got that behaviour for free and it is preserved here.
                # Attempts are capped because each one can carry an LLM round-trip;
                # a cycle must not spend its whole 5-minute bar retrying a pool that
                # is blocked for a portfolio-level reason (daily cap, loss breaker).
                _MAX_PLACE_ATTEMPTS = 5
                if _cycle_candidates:
                    if _signal_ranker_mode("intraday") == "live":
                        from src.execution.signal_ranker import (
                            rank_candidates_by_sqs as _rank_cands,
                        )
                    else:
                        from src.execution.signal_ranker import rank_candidates as _rank_cands

                    _ranked_pool = _rank_cands(_cycle_candidates)
                    _attempts = 0
                    _placed_syms: set[str] = set()
                    _attempted_syms: set[str] = set()
                    for _rank_i, _cand in enumerate(_ranked_pool, 1):
                        if _entered_this_cycle and _news_entered_this_cycle:
                            break
                        if _attempts >= _MAX_PLACE_ATTEMPTS:
                            _log_i.info(
                                "intraday_place_attempts_exhausted attempts=%d pool=%d",
                                _attempts,
                                len(_ranked_pool),
                            )
                            break
                        _c_sym = str(_cand["symbol"])
                        _c_strat = str(_cand["strategy"])
                        _pay = _cand_payload.get((_c_sym, _c_strat))
                        if _pay is None:
                            continue
                        # One symbol, one trade — two strategies can both signal the
                        # same name in a cycle and the structural/news slots are
                        # independent, so the cap has to hold here explicitly.
                        if _c_sym in _placed_syms:
                            continue
                        _is_news_cand = bool(_pay["is_news"])
                        if _is_news_cand and _news_entered_this_cycle:
                            continue
                        if not _is_news_cand and _entered_this_cycle:
                            continue
                        _attempts += 1
                        _attempted_syms.add(_c_sym)
                        _sig_c = _pay["sig"]
                        _compiled_c = _pay["compiled"]
                        _log_i.info(
                            "intraday_rank_pick symbol=%s strategy=%s score=%.3f rank=%d/%d "
                            "attempt=%d",
                            _c_sym,
                            _c_strat,
                            float(_cand.get("score", 0.0)),
                            _rank_i,
                            len(_ranked_pool),
                            _attempts,
                        )
                        try:
                            _result = await _execute_intraday_signal(
                                shared,
                                _c_strat,
                                _c_sym,
                                _sig_c,
                                now,
                                ref_ltp=float(_pay["ref_ltp"]),
                            )
                        except Exception as _pe:
                            _log_i.error(
                                "intraday_place_err symbol=%s strategy=%s err=%s",
                                _c_sym,
                                _c_strat,
                                str(_pe)[:120],
                            )
                            if _c_strat == "llm_intraday_agent":
                                _llm_idea_funnel(
                                    shared,
                                    now,
                                    "execution",
                                    "failed",
                                    _c_sym,
                                    "exception",
                                    str(_pe)[:160],
                                    level=40,
                                )
                            continue
                        if not _result and _c_strat == "llm_intraday_agent":
                            _why, _why_detail = shared.get("_intraday_refusals", {}).pop(
                                _c_sym.split(":")[-1], ("unknown", "no reason recorded")
                            )
                            _llm_idea_funnel(
                                shared,
                                now,
                                "execution",
                                "resting" if _why == "entry_resting_unfilled" else "refused",
                                _c_sym,
                                _why,
                                _why_detail,
                            )
                        # Seed a SmartExitTracker so the position rides the Pine
                        # smart-exit (partial/BE/trail/reversal) via the 5-min exit
                        # pass above. Shared for paper + live (the ratchet applies
                        # through order_monitor either way).
                        # CRITICAL: use POST-REANCHOR fill values, not sig.* so that
                        # 1R partial/BE targets reflect the actual fill.
                        if _result:
                            _tid, _fill_price, _fill_sl, _fill_tgt = _result
                            if _c_strat == "llm_intraday_agent":
                                from src.strategies.llm_intraday.funnel import (
                                    funnel_log as _funnel_log,
                                )

                                _funnel_log(
                                    _log_i,
                                    agent="llm_intraday_agent",
                                    stage="execution",
                                    outcome="placed",
                                    symbol=_c_sym.split(":")[-1],
                                    reason="filled",
                                    tid=_tid,
                                    fill=f"{_fill_price:.2f}",
                                    sl=f"{_fill_sl:.2f}",
                                    target=f"{_fill_tgt:.2f}",
                                )
                            if _c_strat == "news_catalyst_momentum":
                                _nc_counts = shared.setdefault("news_catalyst_traded", {})
                                _nc_day = now.date().isoformat()
                                _nc_counts[_nc_day] = _nc_counts.get(_nc_day, 0) + 1
                            elif _c_strat == "llm_intraday_agent":
                                _li_counts = shared.setdefault("llm_intraday_traded", {})
                                _li_day = now.date().isoformat()
                                _li_counts[_li_day] = _li_counts.get(_li_day, 0) + 1
                                # One idea, one trade — consume it so it can't re-fire.
                                shared.get("llm_intraday_ideas", {}).pop(
                                    _c_sym.split(":")[-1], None
                                )
                            from src.strategies.uss.compiler import SmartExitTracker as _SET

                            shared.setdefault("intraday_exit_state", {})[_c_sym] = {
                                "tracker": _SET(
                                    _compiled_c.spec,
                                    _fill_price,
                                    _fill_sl,
                                    _fill_tgt,
                                    _sig_c.direction,
                                ),
                                "tid": _tid,
                            }
                            _placed_syms.add(_c_sym)
                            if _is_news_cand:
                                _news_entered_this_cycle = True
                            else:
                                _entered_this_cycle = True
                            _cycle_placed.append(
                                {
                                    "symbol": _c_sym,
                                    "strategy": _c_strat,
                                    "direction": _sig_c.direction.value,
                                    "score": float(_cand.get("score", 0.0)),
                                }
                            )

                    # A candidate that passed its price check but was never attempted (the
                    # cycle's single entry slot went to a better-ranked one) used to vanish
                    # from the logs. It stays live until its TTL and is re-checked next scan.
                    for _rank_i, _cd in enumerate(_ranked_pool, 1):
                        _dsym = str(_cd["symbol"])
                        if (
                            str(_cd["strategy"]) == "llm_intraday_agent"
                            and _dsym not in _attempted_syms
                            and _dsym not in _placed_syms
                        ):
                            _llm_idea_funnel(
                                shared,
                                now,
                                "execution",
                                "deferred",
                                _dsym,
                                "one_entry_per_cycle",
                                f"passed its price check, ranked {_rank_i}/{len(_ranked_pool)}; this "
                                f"cycle's single entry slot went to {', '.join(sorted(_placed_syms)) or 'another idea'}. "
                                "Still valid: re-checked at the next 5-min scan until its TTL.",
                            )

                # Update bearish_pct for the BEAR gate (used next cycle).
                if _bear_sym_total > 0:
                    shared["_bearish_pct"] = _bear_sym_count / _bear_sym_total

                # Persist the scan to logs/ so it is observable, not just in memory:
                # a per-cycle log line plus a status file the dashboard reads.
                _log_i.info(
                    "intraday_scan",
                    extra={
                        "ctx": {
                            "scanned": _scanned,
                            "strategies": len(intraday_compiled),
                            "strategy_names": [c.spec.meta.name for c in intraday_compiled],
                            "signals": len(_cycle_signals),
                            "signal_detail": [
                                f"{s.get('strategy')}:{s.get('symbol')}" for s in _cycle_signals
                            ],
                            "next_scan": (
                                datetime.now(_IST)
                                + timedelta(seconds=_seconds_to_next_bar(datetime.now(_IST)))
                            ).strftime("%H:%M:%S"),
                            "duration_s": round(_time_mod.time() - _cycle_start_t, 1),
                            "fetch_s": round(_fetch_s, 1),
                            "total_cycle_s": round(_cycle_time_mod.time() - _iter_t0, 1),
                        }
                    },
                )
                shared["intraday_scan_timing"] = {
                    "done_ts": _time_mod.time(),
                    "cycle_s": round(_cycle_time_mod.time() - _iter_t0, 1),
                }
                try:
                    import json as _json

                    _scan_status = {
                        # Completion time, not cycle start: this file is the loop's
                        # liveness heartbeat (supervisor INV_C10), and a start stamp
                        # made a healthy 6-min cycle look 6 min staler than it was.
                        "ts": datetime.now(_IST).isoformat(),
                        "scanned": _scanned,
                        "intraday_regime": shared.get("intraday_regime", "UNKNOWN"),
                        "index_day_change_pct": shared.get("index_day_change_pct"),
                        "strategies": [c.spec.meta.name for c in intraday_compiled],
                        "signals": _cycle_signals,
                        "candidates": _cycle_candidates,
                        "placed": _cycle_placed,
                    }
                    # RANKING audit: the pool's top pick vs what was actually placed.
                    # No longer shadow — ranking now drives placement, so `agree=False`
                    # here means the top pick was refused downstream (CRM/G9) and a
                    # lower-ranked candidate took the slot, which is the intended
                    # fall-through, not a disagreement. `agree=None` = nothing placed.
                    if _cycle_candidates:
                        try:
                            if _signal_ranker_mode("intraday") == "live":
                                from src.execution.signal_ranker import (
                                    rank_candidates_by_sqs as _rank_cands,
                                )
                            else:
                                from src.execution.signal_ranker import (
                                    rank_candidates as _rank_cands,
                                )

                            _ranked = _rank_cands(_cycle_candidates)
                            _best = _ranked[0]
                            _actual = _cycle_placed[0]["symbol"] if _cycle_placed else None
                            if len(_ranked) > 1 or _actual:
                                _log_i.info(
                                    "shadow_rank n=%d best=%s(%.3f) actual=%s agree=%s ranked=%s",
                                    len(_ranked),
                                    _best["symbol"],
                                    _best["score"],
                                    _actual,
                                    (_actual == _best["symbol"]) if _actual else None,
                                    [(c["symbol"], c["score"]) for c in _ranked[:5]],
                                )
                        except Exception as _re:
                            _log_i.debug("shadow_rank_err", extra={"ctx": {"err": str(_re)[:80]}})
                    _scan_path = _LOGS / "intraday_signals.json"
                    _tmp = _scan_path.with_suffix(".tmp")
                    _tmp.write_text(_json.dumps(_scan_status, indent=2), encoding="utf-8")
                    _tmp.replace(_scan_path)
                except Exception as _we:
                    _log_i.debug("intraday_scan_write_err", extra={"ctx": {"err": str(_we)[:80]}})
        await asyncio.sleep(_seconds_to_next_bar(datetime.now(_IST)))


def _nifty_aligned_for_cycle(shared: dict[str, Any], direction: str) -> bool | None:
    """Is this signal's direction with the day's index move?

    Returns None when the index read is unavailable so the ranker stays neutral
    rather than punishing the candidate for missing data.
    """
    regime = str(shared.get("intraday_regime") or "UNKNOWN")
    if regime == "UNKNOWN":
        return None
    if regime.startswith("BULL"):
        return direction == "LONG"
    if regime.startswith("BEAR"):
        return direction == "SHORT"
    return None


async def _news_loop(
    shared: dict[str, Any],
    news_cfg: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Poll 5+ free news sources, dedup, classify, route, persist to logs/news_status.json."""
    import time as _time
    from datetime import datetime as _news_dt
    from zoneinfo import ZoneInfo as _news_ZI

    _IST = _news_ZI("Asia/Kolkata")
    # "Stocks to watch / in focus" editorial roundups → scan-first + aligned boost.
    _WATCH_PATTERNS = (
        "stocks to watch",
        "stocks in focus",
        "stocks to buy",
        "buzzing stocks",
        "f&o ban",
        "stocks in news",
    )

    from src.news.classifier import NewsClassifier
    from src.news.deduplicator import Deduplicator
    from src.news.impact_router import ImpactRouter, TradingAction
    from src.news.poller import NewsPoller
    from src.news.state_store import NewsStateStore

    src_cfg = news_cfg.get("sources", {})
    poll_interval = int(src_cfg.get("poll_interval_seconds", 300))
    impact_rules = news_cfg.get("impact_rules", {})
    classifier_cfg = news_cfg.get("classifier", {})
    blackouts_cfg = news_cfg.get("blackouts", {})

    redis_client = _make_redis_client()
    dedup = Deduplicator(redis_client)
    router = ImpactRouter(
        redis_client,
        avoid_duration_minutes=int(impact_rules.get("avoid_duration_minutes", 120)),
    )
    # Background loops use GROQ → OLLAMA only (never Gemini), reserving the Gemini
    # free-tier quota for the live G9 trade gate. None → NullLLM (keyword classify).
    from src.llm.providers import make_background_llm_client as _make_bg_llm

    _llm_client: Any = _make_bg_llm(purpose="news_classify") or _NullLLM()
    # News classification's share of the shared provider quota is the `news_classify` entry of
    # config/global.json llm.token_budgets (enforced inside LLMClient — see
    # src/llm/token_ledger.py), so it can never again starve the trade-critical G9 reviews
    # (2026-09-15: 658 Groq calls in 90 minutes burned the whole 200k/day cap). News already
    # has a keyword fallback for any LLM failure — an exhausted budget just routes there.
    classifier = NewsClassifier(
        _llm_client,
        timeout_seconds=int(classifier_cfg.get("timeout_seconds", 5)),
        fallback_to_keyword=bool(classifier_cfg.get("fallback_to_keyword", True)),
    )
    _nr = shared.get("news_repo")
    _news_sink: Any = _RealNewsRepo(_nr) if _nr is not None else _NullNewsRepo()
    store = NewsStateStore(
        _news_sink,
        status_path=_LOGS / "news_status.json",
        earnings_pre_hours=int(blackouts_cfg.get("earnings_pre_hours", 24)),
        earnings_post_hours=int(blackouts_cfg.get("earnings_post_hours", 4)),
        rbi_policy_blackout=bool(blackouts_cfg.get("rbi_policy_blackout", True)),
        budget_blackout=bool(blackouts_cfg.get("budget_blackout", True)),
    )

    rss_feeds: list[dict[str, str]] = []
    for f in src_cfg.get("rss_feeds", []):
        if isinstance(f, dict):
            rss_feeds.append(f)
        elif isinstance(f, str):
            rss_feeds.append({"name": "RSS", "url": f})

    nse_enabled = bool(src_cfg.get("nse_enabled", False))
    bse_enabled = bool(src_cfg.get("bse_enabled", False))

    while not stop.is_set():
        # Weekend / NSE holiday: nothing to trade on, so no polling and no LLM classification.
        # Unseen items are still in the feeds when the next trading day's first poll runs.
        if not is_trading_day(_news_dt.now(_IST).date()):
            await asyncio.sleep(float(poll_interval))
            continue
        try:
            import aiohttp as _aiohttp

            all_items = []
            timeout = _aiohttp.ClientTimeout(total=10)
            ua = {"User-Agent": "Mozilla/5.0 (compatible; AshwaX/3.0)"}

            async with _aiohttp.ClientSession(headers=ua, timeout=timeout) as session:

                class _Http:
                    def __init__(self, s: Any) -> None:
                        self._s = s

                    async def get_text(self, url: str) -> str:
                        async with self._s.get(url) as r:
                            text: str = await r.text()
                            return text

                poller = NewsPoller(_Http(session), poll_interval_seconds=poll_interval)

                for src in rss_feeds:
                    items = await poller.poll_rss(src["name"], src["url"])
                    all_items.extend(items)

                if nse_enabled:
                    try:
                        nse_hdrs = {**ua, "Referer": "https://www.nseindia.com/"}
                        async with session.get(_NSE_URL, headers=nse_hdrs) as r:
                            all_items.extend(await poller.poll_nse(await r.text()))
                    except Exception as exc:
                        _log.debug("nse_poll_failed err=%s", exc)

                if bse_enabled:
                    try:
                        async with session.get(_BSE_URL) as r:
                            all_items.extend(await poller.poll_bse(await r.text()))
                    except Exception as exc:
                        _log.debug("bse_poll_failed err=%s", exc)

            _NOISE_TITLES = {
                "shareholders meeting",
                "resignation",
                "copy of newspaper publication",
                "change in management",
                "analysts/institutional investor meet",
                "acquisition",
                "updates",
                "resignation of director",
                "resignation of director/kmp/smp",
                "change in directorate",
                "outcome of board meeting",
                "board meeting",
                "intimation",
            }

            now_ts = _time.time()
            _now_ist = _news_dt.now(_IST)
            _today_iso = _now_ist.date().isoformat()
            import datetime as _datetime_mod

            _yesterday_iso = (_now_ist.date() - _datetime_mod.timedelta(days=1)).isoformat()
            # Keep today's AND yesterday's "stocks to watch" priorities active.
            # "Stocks to Watch for July 7" is published on July 6 (date="2026-07-06");
            # without keeping yesterday's, it gets dropped on July 7 before it can trade.
            _pri_map = shared.setdefault("news_priority_symbols", {})
            for _ps in [
                s for s, v in _pri_map.items() if v.get("date") not in (_today_iso, _yesterday_iso)
            ]:
                _pri_map.pop(_ps, None)
            processed = 0
            # Real tradeable-ticker set (cached daily by the trade-universe boot
            # path) — the news classifier is LLM-driven and its symbol/
            # affected_symbols fields are free text (index names like "NIFTY50",
            # or raw company names like "Jindal Poly Films" instead of a
            # tradingsymbol). Anything that isn't a real ticker can never match a
            # watchlist symbol downstream, so filter it out here rather than let
            # it silently waste an opportunity/priority slot. None (no cache yet)
            # fails open — no filtering, unchanged behavior.
            from src.market_data.instrument_master import cached_valid_symbols as _cached_valid

            _valid_nse_syms = _cached_valid()

            def _is_real_ticker(sym: str) -> bool:
                return _valid_nse_syms is None or sym.split(":")[-1] in _valid_nse_syms

            for item in all_items:
                if await dedup.is_seen(item.dedup_hash):
                    continue
                h_lower = item.headline.lower().strip()
                if h_lower in _NOISE_TITLES or len(item.headline.split()) <= 3:
                    await dedup.mark_seen(item.dedup_hash)
                    continue
                await dedup.mark_seen(item.dedup_hash)
                classification = await classifier.classify(item)
                result = await router.route(classification)
                await store.persist(classification, item.source, headline=item.headline)
                processed += 1

                if result.action in (TradingAction.HALT, TradingAction.AVOID):
                    ttl = result.redis_ttl_seconds
                    for sym in [classification.symbol] + classification.affected_symbols:
                        shared["news_avoid_symbols"][sym.split(":")[-1]] = now_ts + ttl
                # HIGH-magnitude news (not a halt) → 2h window; CRM G5 applies a
                # −0.30 LLM-confidence penalty to intraday entries on these names.
                if classification.magnitude == "HIGH":
                    _hm = shared.setdefault("news_high_magnitude", {})
                    for sym in [classification.symbol] + classification.affected_symbols:
                        _hm[sym.split(":")[-1]] = now_ts + 7200

                # Per-symbol hot cache → one-line news context for the G9 LLM prompt.
                _nr = shared.setdefault("news_recent", {})
                for _s in [classification.symbol] + classification.affected_symbols:
                    _nr[_s.split(":")[-1]] = {
                        "sentiment": classification.sentiment,
                        "action": classification.trading_action,
                        "magnitude": classification.magnitude,
                        "headline": item.headline[:120],
                        "reasoning": classification.reasoning[:160],
                        "ts": now_ts,
                    }

                # "Stocks to watch" roundups → scan-first priority for the named names.
                if any(p in h_lower for p in _WATCH_PATTERNS) and classification.affected_symbols:
                    _pri = shared.setdefault("news_priority_symbols", {})
                    for _s in classification.affected_symbols:
                        if not _is_real_ticker(_s):
                            continue
                        _pri[_s.split(":")[-1]] = {
                            "bias": classification.sentiment,
                            "source": item.source,
                            "date": _today_iso,
                        }

                # Opportunity scanner — BOOST+HIGH+BULLISH news → store in shared for
                # G5 confidence boost during intraday scan. Does NOT bypass CRM.
                from src.news.opportunity_scanner import OpportunityScanner as _OppScanner

                for _opp in _OppScanner().scan(classification):
                    if not _is_real_ticker(_opp.symbol):
                        _log.debug("news_opportunity_dropped_bad_symbol sym=%s", _opp.symbol)
                        continue
                    _sym_bare = _opp.symbol.split(":")[-1]
                    shared.setdefault("news_opportunities", {})[_sym_bare] = {
                        "confidence_boost": _opp.confidence_boost,
                        "event_type": _opp.event_type,
                        "reasoning": _opp.reasoning,
                        "direction": _opp.direction,
                        "ts": now_ts,
                    }
                    _log.info(
                        "news_opportunity_queued sym=%s event=%s confidence=%.2f direction=%s source=%s",
                        _sym_bare,
                        _opp.event_type,
                        _opp.confidence_boost,
                        _opp.direction,
                        item.source,
                    )

                # HIGH-magnitude MACRO news → directional bias for the breadth gate.
                if (
                    classification.scope == "MACRO"
                    and classification.magnitude == "HIGH"
                    and classification.sentiment in ("BULLISH", "BEARISH")
                ):
                    shared["news_macro_bias"] = {
                        "bias": classification.sentiment,
                        "ts": now_ts,
                    }

            shared["news_avoid_symbols"] = {
                s: exp for s, exp in shared["news_avoid_symbols"].items() if exp > now_ts
            }
            shared["news_high_magnitude"] = {
                s: exp for s, exp in shared.get("news_high_magnitude", {}).items() if exp > now_ts
            }
            shared["news_recent"] = {
                s: v for s, v in shared.get("news_recent", {}).items() if now_ts - v["ts"] < 14400
            }
            shared["news_opportunities"] = {
                s: v
                for s, v in shared.get("news_opportunities", {}).items()
                if now_ts - v["ts"] < 14400  # 4-hour TTL; intraday use only
            }
            _macro = shared.get("news_macro_bias")
            if _macro and now_ts - _macro["ts"] >= 14400:
                shared["news_macro_bias"] = None

            if processed:
                _log.info("news_processed fresh=%d total_polled=%d", processed, len(all_items))

        except Exception as exc:
            _log.warning("news_loop_error err=%s", exc)

        await asyncio.sleep(float(poll_interval))


async def _rs_ranking_loop(
    shared: dict[str, Any],
    rs_cfg: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Rank symbols by Mansfield RS every compute_interval_minutes. Writes logs/rs_ranking.json."""
    from src.screener.rs_ranking import RSRankingEngine  # spec §8.2 path

    interval_s = float(rs_cfg.get("compute_interval_minutes", 30)) * 60.0
    _rs_thresholds = _cfg("universe").get("rs_thresholds", {})
    engine = RSRankingEngine(
        log_path=_LOGS / "rs_ranking.json",
        lookback_days=int(rs_cfg.get("lookback_days", 252)),
        top_n=int(rs_cfg.get("top_n", 20)),
        blacklist_bottom_pct=float(rs_cfg.get("blacklist_bottom_pct", 20.0)),
        min_rs_to_trade=float(_rs_thresholds["min_rs_to_trade"])
        if "min_rs_to_trade" in _rs_thresholds
        else None,
        blacklist_below=float(_rs_thresholds["blacklist_below"])
        if "blacklist_below" in _rs_thresholds
        else None,
    )

    while not stop.is_set():
        try:
            prices_df, nifty = await _load_ohlcv_prices_db(shared)
            if prices_df is None or prices_df.empty:
                _log.warning("rs_ranking_skipped: no OHLCV data in data/ohlcv/day/")
            else:
                result = engine.rank(prices_df, nifty)
                shared["rs_top20"] = {r.symbol for r in result.leaders}
                shared["rs_blacklist"] = {r.symbol for r in result.blacklist}
                # rs_flags: symbol → rs_new_high (§8.2: RS new high → +0.10 LLM confidence boost)
                shared["rs_flags"] = {
                    r.symbol: r.rs_new_high
                    for r in result.leaders + result.improvers + result.blacklist
                }
                # The FULL records, not just the names. rs_score is 12-month excess return
                # vs NIFTY — the single number that says whether a name is actually beating
                # the index — and until now it was computed every 30 min and thrown away,
                # leaving the swing LLM to judge "is this sector tradeable" from a bare
                # ticker list. improvers (top-20 by RS momentum) was never consumed at all.
                shared["rs_records"] = {
                    r.symbol: {
                        "rs_percentile": r.rs_percentile,
                        "rs_score": r.rs_score,
                        "rs_momentum": r.rs_momentum,
                        "rs_new_high": r.rs_new_high,
                    }
                    for r in result.leaders + result.improvers + result.blacklist
                }
                shared["rs_improvers"] = {r.symbol for r in result.improvers}
                # DB write (truth store)
                _mdr = shared.get("market_data_repo")
                if _mdr is not None:
                    from decimal import Decimal as _D

                    for _r in result.leaders + result.improvers + result.blacklist:
                        try:
                            await _mdr.upsert_rs(
                                _r.symbol, _D(str(_r.rs_score)), int(_r.rs_percentile)
                            )
                        except Exception:
                            pass
                _log.info(
                    "rs_ranking_done leaders=%d blacklist=%d",
                    len(result.leaders),
                    len(result.blacklist),
                )
        except Exception as exc:
            _log.warning("rs_ranking_loop_error err=%s", exc)
        await asyncio.sleep(interval_s)


async def _sector_rotation_loop(
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Classify sectors every 30 min using composite prices from NIFTY50 stocks."""
    import pandas as _pd

    from src.intelligence.sector_rotation import SectorRotationTracker

    tracker = SectorRotationTracker(log_path=_LOGS / "sector_rotation.json")

    while not stop.is_set():
        # universe.json sectors.rotation_tracking — when off, do not classify/publish.
        if not _cfg("universe").get("sectors", {}).get("rotation_tracking", True):
            await asyncio.sleep(1800.0)
            continue
        try:
            prices_df, nifty = await _load_ohlcv_prices_db(shared)
            if prices_df is None or prices_df.empty:
                _log.warning("sector_rotation_skipped: no OHLCV data")
                await asyncio.sleep(1800.0)
                continue

            sector_series: dict[str, _pd.Series[float]] = {}
            for sector_name, stocks in _SECTOR_STOCKS.items():
                available = [s for s in stocks if s in prices_df.columns]
                if len(available) >= 2:
                    sector_series[sector_name] = prices_df[available].mean(axis=1)

            if not sector_series:
                _log.warning("sector_rotation_skipped: no sector stock data available yet")
                await asyncio.sleep(1800.0)
                continue

            sec_df = _pd.DataFrame(sector_series)
            result = tracker.update(sec_df, nifty)
            shared["sector_status"] = {r.name: r.status.value for r in result.sectors}
            # Full records (incl. rs_4w) — signal_quality_adapter's sector_strength
            # component needs the underlying RS number, not just the LEADING/
            # EMERGING/LAGGING/DECLINING bucket.
            shared["sector_records"] = {r.name: r for r in result.sectors}
            # DB write — sector_rotation_log (migration 038); JSON is cache only
            _pool = shared.get("db_pool")
            if _pool is not None:
                try:
                    async with _pool.acquire() as _conn:
                        await _conn.executemany(
                            "INSERT INTO sector_rotation_log (sector_name, status, rs_score) VALUES ($1, $2, $3)",
                            [(r.name, r.status.value, float(r.rs_4w)) for r in result.sectors],
                        )
                except Exception as _db_exc:
                    _log.warning("sector_rotation_db_write_failed err=%s", _db_exc)
            _log.info("sector_rotation_done sectors=%d", len(result.sectors))
        except Exception as exc:
            _log.warning("sector_rotation_loop_error err=%s", exc)
        await asyncio.sleep(1800.0)


async def _attribution_loop(
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Nightly at 20:00 IST: attribute P&L across 7 dimensions. Writes logs/attribution.json."""
    import json as _json
    from dataclasses import asdict
    from datetime import date as _date
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    from src.monitoring.performance_attribution import PerformanceAttributor
    from src.monitoring.performance_attribution import TradeRecord as AttrRecord

    _IST = _ZI("Asia/Kolkata")
    _attr_log = _LOGS / "attribution.json"
    attributor = PerformanceAttributor()
    last_run_date: _date | None = None

    while not stop.is_set():
        now = _dt.now(_IST)
        today = now.date()
        if last_run_date != today and now.hour >= 20:
            try:
                attr_trades: list[AttrRecord] = []
                _db_closed_trades = await _fetch_closed_trades_from_db(shared.get("db_pool"))
                for t in _db_closed_trades:
                    try:
                        entry_ts = str(t.get("entry_ts", "") or t.get("exit_ts", ""))
                        entry_time = _dt.fromisoformat(entry_ts) if entry_ts else _dt.now(_IST)
                        exit_ts = str(t.get("exit_ts", ""))
                        exit_time = _dt.fromisoformat(exit_ts) if exit_ts else entry_time
                        hold_hours = max(0.0, (exit_time - entry_time).total_seconds() / 3600.0)
                    except Exception:
                        hold_hours, entry_time = 0.0, _dt.now(_IST)
                    attr_trades.append(
                        AttrRecord(
                            strategy=str(t.get("strategy_name", t.get("strategy", "unknown"))),
                            pnl_r=float(t.get("pnl_r", 0.0)),
                            entry_time=entry_time,
                            regime_at_entry=str(
                                t.get("regime_at_entry", shared.get("regime", "UNKNOWN"))
                            ),
                            sector=str(t.get("sector", "UNKNOWN")),
                            llm_confidence=float(t.get("llm_confidence", 0.5)),
                            hold_hours=hold_hours,
                            volume_at_entry=str(t.get("volume_at_entry", "AVG")),
                        )
                    )

                result = attributor.attribute(attr_trades)
                payload = {
                    "by_strategy": {k: asdict(v) for k, v in result.by_strategy.items()},
                    "by_time_window": {k: asdict(v) for k, v in result.by_time_window.items()},
                    "by_regime": {k: asdict(v) for k, v in result.by_regime.items()},
                    "by_sector": {k: asdict(v) for k, v in result.by_sector.items()},
                    "by_confidence_bucket": {
                        k: asdict(v) for k, v in result.by_confidence_bucket.items()
                    },
                    "by_hold_duration": {k: asdict(v) for k, v in result.by_hold_duration.items()},
                    "by_volume": {k: asdict(v) for k, v in result.by_volume.items()},
                    "computed_at": result.computed_at,
                    "trade_count": len(attr_trades),
                }
                tmp = _attr_log.with_suffix(".tmp")
                tmp.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
                tmp.replace(_attr_log)
                last_run_date = today
                # DB write — performance_attribution (migration 024); JSON is cache only
                _pool = shared.get("db_pool")
                if _pool is not None:
                    try:
                        rows = []
                        for strat, stats in result.by_strategy.items():
                            rows.append(
                                (
                                    today,
                                    strat,
                                    shared.get("regime", "UNKNOWN"),
                                    None,  # timing_bucket — aggregated, not per-trade
                                    None,  # signal_quality
                                    float(stats.total_r),
                                    float(stats.total_r),
                                    float(stats.avg_r),
                                )
                            )
                        if rows:
                            async with _pool.acquire() as _conn:
                                await _conn.executemany(
                                    """INSERT INTO performance_attribution
                                       (attribution_date, strategy_name, regime, timing_bucket,
                                        signal_quality, gross_pnl, net_pnl, r_multiple)
                                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                                    rows,
                                )
                    except Exception as _db_exc:
                        _log.warning("attribution_db_write_failed err=%s", _db_exc)
                _sup = shared.get("supervisor_repo")
                if _sup is not None:
                    try:
                        await _sup.emit_event(
                            "ATTRIBUTION_COMPUTED",
                            "attribution_loop",
                            severity="INFO",
                            payload={
                                "trade_count": len(attr_trades),
                                "computed_at": result.computed_at,
                            },
                        )
                    except Exception:
                        pass
                _log.info("attribution_computed trades=%d", len(attr_trades))
            except Exception as exc:
                _log.warning("attribution_loop_error err=%s", exc)
        await asyncio.sleep(300.0)


async def _slippage_loop(
    lifecycle: Any,
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Write slippage stats to logs/slippage_stats.json every 5 min."""
    import json as _json
    from dataclasses import asdict

    _slip_log = _LOGS / "slippage_stats.json"

    while not stop.is_set():
        try:
            stats = lifecycle.slippage_tracker.all_stats()
            if stats:
                payload = [asdict(s) for s in stats]
                tmp = _slip_log.with_suffix(".tmp")
                tmp.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
                tmp.replace(_slip_log)
                shared["slippage_stats"] = payload
                # DB write — slippage_stats_log (migration 039); JSON is cache only
                _pool = shared.get("db_pool")
                if _pool is not None:
                    try:
                        rows = [
                            (
                                s.order_type,
                                s.time_of_day,
                                s.adv_bucket,
                                float(s.avg_slippage_pct),
                                int(s.count),
                            )
                            for s in stats
                            if s.count > 0
                        ]
                        if rows:
                            async with _pool.acquire() as _conn:
                                await _conn.executemany(
                                    """INSERT INTO slippage_stats_log
                                       (order_type, time_of_day, adv_bucket, avg_slippage, count)
                                       VALUES ($1,$2,$3,$4,$5)""",
                                    rows,
                                )
                    except Exception as _db_exc:
                        _log.warning("slippage_db_write_failed err=%s", _db_exc)
        except Exception as exc:
            _log.warning("slippage_loop_error err=%s", exc)
        await asyncio.sleep(300.0)


async def _expectancy_loop(
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Every 5 min: record pnl_r per strategy, update paused set. Writes logs/expectancy_status.json.

    Also feeds Layer 4A (StrategySelector, regime-bucketed expectancy — spec §9,
    config/learning.json strategy_selector.mode) and Layer 5A (UniverseAdapter,
    win-sector concentration — universe_adapter.mode, shadow-log only per the
    learning-engine wiring plan) from the same closed-trade sweep, so both
    orphaned modules get real data instead of never being called.
    """
    import json as _json
    from dataclasses import asdict

    from src.learning.strategy_selector import StrategySelector
    from src.learning.universe_adapter import UniverseAdapter
    from src.monitoring.expectancy_engine import ExpectancyEngine

    _exp_log = _LOGS / "expectancy_status.json"
    engine = ExpectancyEngine()
    _seen_ids: set[str] = set()
    _seen_ids_paper: set[str] = set()
    _regime_trades: dict[str, dict[tuple[str, str], list[float]]] = {"paper": {}, "live": {}}
    _strategy_selector: StrategySelector = shared.setdefault(
        "strategy_selector", StrategySelector()
    )
    _universe_adapter: UniverseAdapter = shared.setdefault("universe_adapter", UniverseAdapter())
    _universe_outcomes: dict[str, dict[str, list[dict[str, Any]]]] = {"paper": {}, "live": {}}

    while not stop.is_set():
        # DB truth (trades only — live), not shared['closed_trades'] — see
        # _fetch_closed_trades_from_db docstring. Also fixes a dedup bug: the old
        # in-memory closed-trade dicts never had a 'trade_id'/'signal_id' key, so
        # every entry's tid was '' and got skipped by `if not tid: continue` —
        # this loop never actually recorded anything.
        # live-only: should_pause here hard-blocks a strategy's LIVE trading at
        # CRM G6 (STRATEGY_DECAY) — a run of simulated paper losses must never
        # pause real trading, and vice versa. This ExpectancyEngine instance
        # (the pause/resume gate) stays live-only, unchanged — but Layers 4A/5A
        # below now ALSO track a separate paper stream, since ranking priority
        # for a paper-mode segment (e.g. today's intraday) must be learned from
        # paper outcomes, not starved of data because it isn't "live".
        _new_trades: list[dict[str, Any]] = []
        for t in await _fetch_closed_trades_from_db(shared.get("db_pool"), mode="live"):
            tid = str(t.get("agent_trade_id", ""))
            if not tid or tid in _seen_ids:
                continue
            _seen_ids.add(tid)
            strat = str(t.get("strategy_name", "unknown"))
            engine.record(strat, float(t.get("pnl_r", 0.0)))
            _new_trades.append(t)
            if _learning_mode("strategy_selector") in ("shadow", "live"):
                regime = str(t.get("regime_at_entry", "UNKNOWN"))
                _regime_trades["live"].setdefault((strat, regime), []).append(
                    float(t.get("pnl_r", 0.0))
                )
            if _learning_mode("universe_adapter") in ("shadow", "live"):
                _universe_outcomes["live"].setdefault(strat, []).append(t)

        _new_trades_paper: list[dict[str, Any]] = []
        if _learning_mode("strategy_selector") in ("shadow", "live") or _learning_mode(
            "universe_adapter"
        ) in ("shadow", "live"):
            for t in await _fetch_closed_trades_from_db(shared.get("db_pool"), mode="paper"):
                tid = str(t.get("agent_trade_id", ""))
                if not tid or tid in _seen_ids_paper:
                    continue
                _seen_ids_paper.add(tid)
                strat = str(t.get("strategy_name", "unknown"))
                _new_trades_paper.append(t)
                if _learning_mode("strategy_selector") in ("shadow", "live"):
                    regime = str(t.get("regime_at_entry", "UNKNOWN"))
                    _regime_trades["paper"].setdefault((strat, regime), []).append(
                        float(t.get("pnl_r", 0.0))
                    )
                if _learning_mode("universe_adapter") in ("shadow", "live"):
                    _universe_outcomes["paper"].setdefault(strat, []).append(t)

        if _learning_mode("strategy_selector") in ("shadow", "live"):
            for mode_key in ("live", "paper"):
                if mode_key == "live" and not _new_trades:
                    continue
                if mode_key == "paper" and not _new_trades_paper:
                    continue
                for (strat, regime), pnls in _regime_trades[mode_key].items():
                    window = pnls[-20:]
                    wr = len([p for p in window if p > 0]) / len(window)
                    wins = [p for p in window if p > 0]
                    losses = [abs(p) for p in window if p <= 0]
                    avg_win = sum(wins) / len(wins) if wins else 0.0
                    avg_loss = sum(losses) / len(losses) if losses else 0.0
                    expectancy_r = (wr * avg_win) - ((1 - wr) * avg_loss)
                    _strategy_selector.update(strat, regime, expectancy_r, mode=mode_key)

        if _learning_mode("universe_adapter") in ("shadow", "live"):
            for mode_key in ("live", "paper"):
                if mode_key == "live" and not _new_trades:
                    continue
                if mode_key == "paper" and not _new_trades_paper:
                    continue
                for strat, outcomes in _universe_outcomes[mode_key].items():
                    _universe_adapter.update(strat, outcomes, mode=mode_key)

        payload: dict[str, Any] = {}
        # Mutate the shared set in place, per-strategy — NEVER wholesale-replace it.
        # The health-score loop (below) also adds/discards its own SUSPENDED entries
        # into this same set; replacing the whole set here used to silently undo
        # every health-loop pause on each 300s tick.
        _paused_set: set[str] = shared.setdefault("paused_strategies", set())
        _sup_repo = shared.get("supervisor_repo")
        _resume_mode = _learning_mode("expectancy_resume")
        for strat in list(engine._trades.keys()):
            result = engine.evaluate(strat)
            payload[strat] = asdict(result)

            # §8.4's spec'd gate — "resume only if backtest E>0" — previously
            # never implemented: shared["backtest_queue"] was write-only, nothing
            # ever read it, so a paused strategy always resumed on live win-rate
            # recovery alone. Only runs the (real, walk-forward) check when the
            # strategy is actually queued and the mode allows it — a HEALTHY
            # strategy that was never paused skips this entirely, unchanged.
            _gate_passed = True
            _needs_gate = (not result.should_pause) and strat in shared.get("backtest_queue", set())
            if _needs_gate and _resume_mode in ("shadow", "live"):
                _gate_passed = await _expectancy_resume_backtest_passed(shared, strat)
                _log.info(
                    "expectancy_resume_gate strategy=%s passed=%s mode=%s",
                    strat,
                    _gate_passed,
                    _resume_mode,
                )
            _blocked_by_gate = _needs_gate and _resume_mode == "live" and not _gate_passed

            if not result.should_pause and not _blocked_by_gate:
                _paused_set.discard(strat)
            if result.should_pause:
                _paused_set.add(strat)
                # §8.4: raise STRATEGY_DECAY incident + queue walk-forward backtest
                if _sup_repo is not None:
                    try:
                        await _sup_repo.open_incident(
                            dedup_key=f"strategy_decay_{strat}",
                            invariant_id="STRATEGY_DECAY",
                            severity="CRITICAL",
                            message=f"Strategy {strat} E negative {result.consecutive_negative} consecutive — auto-paused",
                            remediation_hint="walk-forward backtest required before resume",
                        )
                    except Exception:
                        pass
                shared.setdefault("backtest_queue", set()).add(strat)
            elif result.status == "WARNING" and _sup_repo is not None:
                # §8.4: 20-trade E < 50% of all-time E
                try:
                    await _sup_repo.emit_event(
                        "STRATEGY_DECAY",
                        "expectancy_loop",
                        severity="WARNING",
                        payload={
                            "strategy": strat,
                            "current_e": result.current_e,
                            "all_time_e": result.all_time_e,
                        },
                    )
                except Exception:
                    pass
            elif _blocked_by_gate:
                _log.warning(
                    "expectancy_resume_still_blocked strategy=%s backtest_queue_still_failing",
                    strat,
                )
            else:
                # Resolve STRATEGY_DECAY incident when healthy again
                if _sup_repo is not None:
                    try:
                        await _sup_repo.resolve_incident(f"strategy_decay_{strat}")
                    except Exception:
                        pass
                shared.get("backtest_queue", set()).discard(strat)
        shared["expectancy_status"] = payload
        if payload:
            tmp = _exp_log.with_suffix(".tmp")
            tmp.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(_exp_log)
        await asyncio.sleep(300.0)


async def _calibration_loop(
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Per closed trade: record llm_confidence. Weekly Sunday 20:00 evaluate.
    Writes logs/calibration_report.json (live) + logs/calibration_report_paper.json.

    Tracks paper and live calibration COMPLETELY SEPARATELY (two independent
    CalibrationEngine instances) — previously this fetched ALL closed trades
    (mode="all"), mixing simulated paper outcomes into the same calibration
    that raises/lowers the REAL G9 confidence floor (shared["calibration_thresholds"]),
    exactly the class of bug this file's own _fetch_closed_trades_from_db
    docstring warns about for the sibling expectancy/health loops. Only the
    LIVE engine feeds shared["calibration_thresholds"] (the actual G9 gate
    input) — unchanged behavior/consumer, just correctly live-only sourced
    now. The PAPER engine is observational only (its own log + DB rows,
    distinguished via a "::paper" model_used suffix so no schema change is
    needed) — useful for judging a paper-trial strategy's calibration before
    it ever goes live, matching this project's existing paper-trial philosophy
    (see e.g. options_settings.json's paper_trial_days_before_live).
    """
    import json as _json
    from dataclasses import asdict
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    from src.llm.calibration import CalibrationEngine

    _IST = _ZI("Asia/Kolkata")
    _cal_log = _LOGS / "calibration_report.json"
    _cal_log_paper = _LOGS / "calibration_report_paper.json"
    engines = {"live": CalibrationEngine(), "paper": CalibrationEngine()}
    _seen_ids: dict[str, set[str]] = {"live": set(), "paper": set()}
    _last_eval_week: int | None = None

    while not stop.is_set():
        for _fetch_mode, engine in engines.items():
            for t in await _fetch_closed_trades_from_db(shared.get("db_pool"), mode=_fetch_mode):
                tid = str(t.get("agent_trade_id", ""))
                if not tid or tid in _seen_ids[_fetch_mode]:
                    continue
                _seen_ids[_fetch_mode].add(tid)
                engine.add(
                    str(t.get("strategy_name", "unknown")),
                    llm_confidence=float(t.get("llm_confidence", 0.5)),
                    was_win=float(t.get("pnl_r", 0.0)) > 0,
                )
        now = _dt.now(_IST)
        iso_week = now.isocalendar()[1]
        if now.weekday() == 6 and now.hour >= 20 and _last_eval_week != iso_week:
            try:
                _thresholds: dict[str, float] = dict(shared.get("calibration_thresholds", {}))
                for _fetch_mode, engine in engines.items():
                    report: dict[str, Any] = {}
                    for strat, records in engine._records.items():
                        if len(records) >= 5:
                            report[strat] = asdict(engine.evaluate(strat))
                    if report:
                        _log_path = _cal_log if _fetch_mode == "live" else _cal_log_paper
                        tmp = _log_path.with_suffix(".tmp")
                        tmp.write_text(_json.dumps(report, indent=2), encoding="utf-8")
                        tmp.replace(_log_path)
                        # DB write — one row per bucket per strategy, mode-tagged
                        _lr = shared.get("learning_repo")
                        if _lr is not None:
                            from datetime import date as _dt_date
                            from decimal import Decimal as _D

                            _period_end = _dt_date.today()
                            _period_start = _dt_date.fromordinal(_period_end.toordinal() - 7)
                            for _strat, _cal_result in engine._records.items():
                                if len(_cal_result) < 5:
                                    continue
                                _ev = engine.evaluate(_strat)
                                _model_tag = _strat if _fetch_mode == "live" else f"{_strat}::paper"
                                for _b in _ev.buckets:
                                    try:
                                        await _lr.insert_calibration(
                                            period_start=_period_start,
                                            period_end=_period_end,
                                            model_used=_model_tag,
                                            confidence_bucket=_b.bucket_label,
                                            predicted_wr=_D(str(round(_b.predicted_confidence, 4))),
                                            actual_wr=_D(str(round(_b.actual_win_rate, 4))),
                                            trade_count=_b.count,
                                            brier_score=_D(str(round(_ev.brier_score, 4))),
                                        )
                                    except Exception:
                                        pass

                    if _fetch_mode != "live":
                        # Paper is observational only — never touches the real
                        # G9 confidence floor.
                        continue

                    # Phase 4: raise min confidence thresholds for miscalibrated
                    # strategies — LIVE data only, feeds the real gate.
                    for _strat2, _r in report.items():
                        if _r.get("overall_miscalibrated"):
                            # Raise threshold by 0.05, capped at 0.90
                            _cur = _thresholds.get(_strat2, 0.5)
                            _thresholds[_strat2] = min(0.90, _cur + 0.05)
                            _log.warning(
                                "calibration_threshold_raised strategy=%s new_min=%.2f",
                                _strat2,
                                _thresholds[_strat2],
                            )
                        else:
                            if _strat2 in _thresholds:
                                _thresholds[_strat2] = max(0.5, _thresholds[_strat2] - 0.02)
                        # Persist to DB — source of truth, survives restart. Routed
                        # through PromotionEngine per spec §8.9 ("adjust minimum
                        # confidence thresholds via promotion engine") — a static
                        # helper, not a shared instance with _evolution_loop's
                        # PromotionEngine, since this is a separate loop with its
                        # own learning_repo reference; the DB write itself is
                        # unchanged (same upsert_calibration_threshold call).
                        _lr2 = shared.get("learning_repo")
                        if _lr2 is not None and _strat2 in _thresholds:
                            try:
                                from src.learning.promotion import PromotionEngine as _PromoE

                                await _PromoE.adjust_confidence_threshold(
                                    _lr2, _strat2, _thresholds[_strat2]
                                )
                            except Exception:
                                pass
                    _log.info("calibration_evaluated mode=live strategies=%d", len(report))
                shared["calibration_thresholds"] = _thresholds
                _last_eval_week = iso_week
            except Exception as exc:
                _log.warning("calibration_loop_error err=%s", exc)
        await asyncio.sleep(300.0)


async def _nightly_scanner_loop(
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """21:00–23:00 IST Mon–Fri: score symbols, update shared nightly_setups. Scanner writes the log."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    from src.screener.nightly_scanner import NightlyScanner

    _IST = _ZI("Asia/Kolkata")
    scanner = NightlyScanner(log_path=_LOGS / "nightly_setups.json")
    _last_scan_date = None

    while not stop.is_set():
        now = _dt.now(_IST)
        today = now.date()
        if _last_scan_date != today and 21 <= now.hour < 23 and today.weekday() < 5:
            try:
                import pandas as _pd

                prices_df, _ = await _load_ohlcv_prices_db(shared)
                if prices_df is None or prices_df.empty:
                    _log.warning("nightly_scanner_skipped: no OHLCV data")
                else:
                    _rs_top20 = shared.get("rs_top20")
                    rs_ranks: dict[str, float] = _rs_top20 if isinstance(_rs_top20, dict) else {}
                    result = scanner.scan(prices_df, _pd.DataFrame(), rs_ranks)
                    shared["nightly_setups"] = {s.symbol: s.score for s in result.top_setups}
                    # DB write
                    _lr = shared.get("learning_repo")
                    if _lr is not None:
                        for _s in result.top_setups:
                            try:
                                await _lr.insert_setup(
                                    _s.symbol,
                                    _s.stage,
                                    _s.score,
                                    0.0,
                                    0.0,
                                    0.0,
                                    f"rs={_s.rs_rank:.1f} vcp={_s.vcp_tightness:.2f}",
                                )
                            except Exception:
                                pass
                    _last_scan_date = today
                    _log.info("nightly_scan_complete setups=%d", len(result.top_setups))
            except Exception as exc:
                _log.warning("nightly_scanner_loop_error err=%s", exc)
        await asyncio.sleep(300.0)


def _persist_swing_scan_date(path: Any, today: Any) -> None:
    """Best-effort write of the swing scan's last-run date so a launcher restart
    later the same day (after 15:45) doesn't replay the whole scan."""
    import json as _json_scan_persist

    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            _json_scan_persist.dumps({"last_scan_date": today.isoformat()}), encoding="utf-8"
        )
        tmp.replace(path)
    except Exception:
        pass


# A candidate that passed CRM and ranking but was blocked only by insufficient_cash
# gets this many EXTRA scan attempts (beyond the original) before it's dropped as
# stale — the entry/SL/target are computed off the candle that produced the original
# signal, and a VCP/CANSLIM breakout level goes stale fast. ranked_below_cap losses
# are NOT carried forward: that candidate lost fairly to a better setup that day.
_SWING_CASH_BACKLOG_MAX_RETRIES = 2

# Zerodha's actual AMO-acceptance cutoff is later than the 15:45 IST the code
# assumed (see KiteAdapter._current_order_variety() docstring) — confirmed live
# 2026-09-17: ACUTAAS/GRANULES were rejected with "AMOs can only be placed
# after trading hours" at 15:46:39, one minute past the assumed window open.
# Rather than guess a new fixed cutoff or block the scan loop in place for
# hours, a rejected candidate is parked in swing_amo_retry_backlog.json and
# retried by the loop's own 2-min heartbeat (see the bottom of
# _swing_signal_scan_loop's while loop) every _SWING_AMO_RETRY_INTERVAL_SEC,
# until it fills or Zerodha's documented AMO window actually closes at 08:57
# IST the next session (_swing_amo_retry_cutoff_ts). This NEVER re-enters
# candidate selection/ranking — only replays the exact already-decided
# signal — so it cannot cause the 2026-07-30 duplicate-order replay
# (_last_scan_date's guard, above) the way a second full scan would.
_SWING_AMO_RETRY_INTERVAL_SEC = 300.0


def _swing_amo_retry_cutoff_ts(first_failed_ts: float) -> float:
    """First AMO-close time (market_hours.json amo.close) strictly after `first_failed_ts`. Wall-clock, not elapsed-time, so a candidate that first
    failed at 07:30 IST doesn't get an extra almost-24h before its cutoff."""
    from datetime import datetime as _dt2
    from datetime import timedelta as _timedelta2
    from zoneinfo import ZoneInfo as _ZI2

    dt0 = _dt2.fromtimestamp(first_failed_ts, _ZI2("Asia/Kolkata"))
    _amo_close = _sessions.amo_close()
    cutoff = dt0.replace(hour=_amo_close.hour, minute=_amo_close.minute, second=0, microsecond=0)
    if cutoff <= dt0:
        cutoff += _timedelta2(days=1)
    return cutoff.timestamp()


def _load_swing_cash_backlog(path: Any) -> list[dict[str, Any]]:
    import json as _json_backlog

    try:
        if path.exists():
            return list(_json_backlog.loads(path.read_text(encoding="utf-8")))
    except Exception:
        pass
    return []


def _save_swing_cash_backlog(path: Any, entries: list[dict[str, Any]]) -> None:
    import json as _json_backlog

    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(_json_backlog.dumps(entries), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _swing_amo_backlog_entry(
    bare_sym: str,
    full_sym: str,
    strategy_name: str,
    sig: Any,
    first_failed_ts: float,
    last_attempt_ts: float,
    attempts: int,
) -> dict[str, Any]:
    return {
        "bare_sym": bare_sym,
        "full_sym": full_sym,
        "strategy_name": strategy_name,
        "direction": sig.direction.value,
        "entry_price": sig.entry_price,
        "sl": sig.sl,
        "target": sig.target,
        "first_failed_ts": first_failed_ts,
        "last_attempt_ts": last_attempt_ts,
        "attempts": attempts,
    }


async def _process_swing_amo_retry_backlog(shared: dict[str, Any], path: Any, now: Any) -> None:
    """Non-blocking companion to the once-daily scan above: retries candidates
    parked here by an `amo_window_not_open` result, without re-entering
    candidate selection/ranking (see _SWING_AMO_RETRY_INTERVAL_SEC's comment).
    Runs on every tick of the scan loop's own 2-min heartbeat, so a pending
    retry costs nothing extra and survives a launcher.py restart (state is on
    disk, not in this coroutine's stack).

    Known limitation: a fill via this path does NOT count against the day's
    swing_day_set/max_swing_per_day caps (those are local to the once-daily
    scan's own invocation) — acceptable because it only ever fires for a
    candidate that already failed to place at all on its original attempt,
    so it cannot inflate the day's placed-order count beyond that cap plus
    this rare, broker-rejection-triggered edge case.
    """
    entries = _load_swing_cash_backlog(path)
    if not entries:
        return
    strategy_reg = shared.get("strategy_registry")
    if strategy_reg is None:
        return
    compiled_by_strategy = {
        c.spec.meta.name: c
        for c in strategy_reg.all_enabled()
        if getattr(getattr(c, "spec", None), "meta", None) is not None
        and c.spec.meta.timeframe == "DAILY"
    }
    now_ts = now.timestamp()
    kept: list[dict[str, Any]] = []
    for e in entries:
        if now_ts - float(e.get("last_attempt_ts", 0.0)) < _SWING_AMO_RETRY_INTERVAL_SEC:
            kept.append(e)  # not due yet
            continue
        if now_ts >= _swing_amo_retry_cutoff_ts(float(e["first_failed_ts"])):
            _swing_log.warning(
                "swing_amo_retry_expired sym=%s strategy=%s attempts=%d — "
                "AMO window closed, giving up",
                e["bare_sym"],
                e["strategy_name"],
                int(e.get("attempts", 0)),
            )
            continue
        compiled = compiled_by_strategy.get(e["strategy_name"])
        if compiled is None:
            _swing_log.info(
                "swing_amo_retry_dropped sym=%s strategy=%s reason=strategy_no_longer_enabled",
                e["bare_sym"],
                e["strategy_name"],
            )
            continue
        try:
            from src.strategies.uss.compiler import Signal as _ARSig
            from src.strategies.uss.schema import Direction as _ARDir

            sig = _ARSig(
                e["strategy_name"],
                _ARDir(e["direction"]),
                float(e["entry_price"]),
                float(e["sl"]),
                float(e["target"]),
            )
        except Exception:
            continue
        _trade_id, _px, _sl, _tgt, _reason, _gates = await _execute_swing_signal(
            shared, e["strategy_name"], e["full_sym"], sig, now, compiled=compiled
        )
        if _trade_id is not None or _reason == "not_filled":
            _swing_log.info(
                "swing_amo_retry_succeeded sym=%s strategy=%s attempts=%d filled=%s",
                e["bare_sym"],
                e["strategy_name"],
                int(e.get("attempts", 0)) + 1,
                _trade_id is not None,
            )
            continue
        if _reason != "amo_window_not_open":
            _swing_log.info(
                "swing_amo_retry_dropped sym=%s strategy=%s reason=%s",
                e["bare_sym"],
                e["strategy_name"],
                _reason,
            )
            continue
        attempts = int(e.get("attempts", 0)) + 1
        _swing_log.info(
            "swing_amo_retry sym=%s strategy=%s attempt=%d — broker AMO window still "
            "not open, retrying again in %.0fs",
            e["bare_sym"],
            e["strategy_name"],
            attempts,
            _SWING_AMO_RETRY_INTERVAL_SEC,
        )
        kept.append(
            _swing_amo_backlog_entry(
                e["bare_sym"],
                e["full_sym"],
                e["strategy_name"],
                sig,
                float(e["first_failed_ts"]),
                now_ts,
                attempts,
            )
        )
    _save_swing_cash_backlog(path, kept)


def _swing_cash_backlog_entry(c: dict[str, Any], retries_used: int) -> dict[str, Any]:
    sig = c["sig"]
    return {
        "bare_sym": c["bare_sym"],
        "strategy_name": c["strategy_name"],
        "direction": sig.direction.value,
        "entry_price": sig.entry_price,
        "sl": sig.sl,
        "target": sig.target,
        "score": c.get("score", 0.0),
        "rr": c.get("rr", 0.0),
        "retries_used": retries_used,
    }


async def _swing_signal_scan_loop(
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Once the AMO window opens (market_hours.json amo.open, Mon–Fri): evaluate DAILY
    strategies on today's close, place CNC orders.

    Runs once per trading day after the intraday session closes. Each DAILY compiled
    strategy is evaluated against the last daily candle. Passing signals go through the
    full CRM pipeline (G1-G10) and are placed as DELIVERY (CNC) orders.

    Not at the 15:30 close: Zerodha rejects orders between the close and the AMO
    open under both REGULAR and AMO varieties (live 2026-09-17 rejection at 15:46;
    2026-09-21 paper order stranded from 15:47). The window opens at 16:00; the
    time lives ONLY in config/market_hours.json. Scanning any earlier would
    generate a signal the broker cannot accept.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    _IST = _ZI("Asia/Kolkata")
    _last_scan_date = None
    # Scan (and place) only once the broker's AMO window is open — config/market_hours.json
    # markets.nse_equity.amo.open. The scan is the queue: nothing goes out before then.
    _SCAN_HOUR, _SCAN_MIN = _sessions.amo_open().hour, _sessions.amo_open().minute

    # Seed from disk so a launcher restart after 15:45 doesn't think the scan
    # hasn't run yet today and replay the WHOLE thing — re-signaling every symbol,
    # including ones still SUBMITTED/unfilled from the earlier run (unfilled rows
    # don't count as "held", so the duplicate-guard doesn't block the re-entry).
    # Observed 2026-07-30: repeated restarts stacked 2-3x CRAFTSMAN/ALKEM/NESTLEIND
    # entries a few minutes apart, each stuck SUBMITTED.
    _scan_state_path = _LOGS / "swing_scan_state.json"
    try:
        import json as _json_scan_seed

        if _scan_state_path.exists():
            _saved_date = _json_scan_seed.loads(_scan_state_path.read_text(encoding="utf-8")).get(
                "last_scan_date"
            )
            if _saved_date == _dt.now(_IST).date().isoformat():
                _last_scan_date = _dt.now(_IST).date()
    except Exception:
        pass

    _cash_backlog_path = _LOGS / "swing_cash_backlog.json"
    _amo_backlog_path = _LOGS / "swing_amo_retry_backlog.json"

    while not stop.is_set():
        now = _dt.now(_IST)
        today = now.date()
        is_weekday = today.weekday() < 5
        past_scan_time = (now.hour, now.minute) >= (_SCAN_HOUR, _SCAN_MIN)

        # Non-blocking AMO-window retry check — runs every tick regardless of
        # whether it's today's scan time, so a candidate parked here (see
        # _SWING_AMO_RETRY_INTERVAL_SEC's comment) gets retried on its own
        # schedule without waiting for or blocking the once-daily scan below.
        try:
            await _process_swing_amo_retry_backlog(shared, _amo_backlog_path, now)
        except Exception as _are:
            _swing_log.warning("swing_amo_retry_loop_err err=%s", _are)

        if not (is_weekday and past_scan_time and _last_scan_date != today):
            _swing_log.debug(
                "swing_daily_trail_wait tracked=%d is_weekday=%s past_scan_time=%s "
                "(runs at/after %02d:%02d IST) last_scan_date=%s today=%s "
                "reason=%s — daily chandelier trail + daily partial/BE step run ONLY inside "
                "the once-a-day scan",
                len(shared.get("swing_exit_state") or {}),
                is_weekday,
                past_scan_time,
                _SCAN_HOUR,
                _SCAN_MIN,
                _last_scan_date,
                today,
                "already_ran_today"
                if _last_scan_date == today
                else ("weekend" if not is_weekday else "before_scan_time"),
            )
        if is_weekday and past_scan_time and _last_scan_date != today:
            try:
                strategy_reg = shared.get("strategy_registry")
                ohlcv_repo = shared.get("ohlcv_repo")
                if strategy_reg is None or ohlcv_repo is None:
                    _swing_log.warning(
                        "swing_scan_skipped: strategy_registry or ohlcv_repo unavailable"
                    )
                    await asyncio.sleep(300.0)
                    continue

                # Fetch daily OHLCV lazily, cached across strategies so symbols
                # shared between universes (e.g. NIFTY50 ⊂ NIFTY500) are fetched once.
                daily_frames: dict[str, Any] = {}

                async def _get_daily(bare_sym: str) -> Any:
                    if bare_sym in daily_frames:
                        return daily_frames[bare_sym]
                    try:
                        # 500 bars of daily history for long-period indicators.
                        df = await ohlcv_repo.fetch_dataframe(bare_sym, "1d", 500)
                    except Exception as _fe:
                        _swing_log.warning(
                            "swing_scan_fetch_err sym=%s err=%s: %s",
                            bare_sym,
                            type(_fe).__name__,
                            _fe,
                        )
                        return None
                    if df is not None and len(df) >= 50:
                        daily_frames[bare_sym] = df
                        return df
                    _swing_log.debug(
                        "swing_scan_insufficient_history sym=%s bars=%d",
                        bare_sym,
                        0 if df is None else len(df),
                    )
                    return None

                # Daily swing exit-management step: advance every open swing
                # position's SmartExitTracker on the just-closed daily candle —
                # trailing_stop/breakeven/partial_book/reversal_exit from the
                # strategy JSON's exit_conditions (previously wired only for
                # intraday, so swing SLs never moved once placed at entry).
                # Runs even when DAILY strategies are currently disabled, so an
                # existing open position keeps trailing. Any resulting SL move
                # is applied via order_monitor.update_sl(); the always-on 2s
                # OrderMonitor.cycle() then enforces it live during market
                # hours the next session — never deferred to this once-daily
                # AMO-window scan.
                _swing_exit_state: dict[str, Any] = shared.get("swing_exit_state", {})
                # All stacks' monitors, not just the single "primary" one (found
                # 2026-09-23, alongside the tid-keying fix above): a position opened
                # in the non-primary mode (e.g. a paper position while this process's
                # primary is live) otherwise looks "closed" here on EVERY iteration
                # and is dropped, permanently losing its daily trail. Same class of
                # bug as the tid-keying fix, same rule: never assume a single monitor
                # covers every open position.
                _swing_all_monitors = [
                    s.get("order_monitor")
                    for s in (shared.get("stacks") or {}).values()
                    if s.get("order_monitor") is not None
                ]
                for _setid in list(_swing_exit_state.keys()):
                    _sex = _swing_exit_state[_setid]
                    _sesym = _sex["symbol"]
                    _swing_om = next(
                        (m for m in _swing_all_monitors if m.get(_setid) is not None), None
                    )
                    if _swing_om is None:
                        # Position already closed (SL/target/manual) — drop it.
                        _swing_log.debug(
                            "swing_daily_trail_drop tid=%s symbol=%s "
                            "reason=not_found_in_any_monitor",
                            _setid,
                            _sesym,
                        )
                        _swing_exit_state.pop(_setid, None)
                        continue
                    _sebare = _sesym.split(":")[-1]
                    _sedf = await _get_daily(_sebare)
                    if _sedf is None:
                        # _get_daily already logged the specific reason
                        # (swing_scan_fetch_err / swing_scan_insufficient_history) —
                        # this line ties it back to the trail step that got skipped.
                        _swing_log.debug(
                            "swing_daily_trail_skip tid=%s symbol=%s reason=no_daily_df",
                            _setid,
                            _sesym,
                        )
                        continue
                    _sest = _sex["tracker"]._state
                    _sl_before = _sest.sl
                    _swing_log.debug(
                        "swing_daily_trail_step_begin tid=%s symbol=%s bar_ts=%s bar_high=%.2f "
                        "bar_low=%.2f bar_close=%.2f sl_before=%.2f be_moved=%s partial_booked=%s",
                        _setid,
                        _sesym,
                        _sedf.index[-1],
                        float(_sedf["high"].iloc[-1]),
                        float(_sedf["low"].iloc[-1]),
                        float(_sedf["close"].iloc[-1]),
                        _sl_before,
                        _sest.be_moved,
                        _sest.partial_booked,
                    )
                    _swing_trace(
                        _setid,
                        _sex,
                        float(_sedf["close"].iloc[-1]),
                        _swing_om.get(_setid),
                        -1,
                        "daily_step_input",
                    )
                    _sebe = _sex["tracker"].step(_sedf, in_exit=False)
                    _selastc = float(_sedf["close"].iloc[-1])
                    _trail_note = "no_trail_configured"
                    if _sest.trail is not None:
                        _d = -1.0 if _sest.is_short else 1.0
                        _pr = (
                            _d
                            * ((_sest.trough if _sest.is_short else _sest.peak) - _sest.entry)
                            / _sest.initial_risk
                            if _sest.initial_risk > 0
                            else 0.0
                        )
                        if _pr < _sest.activation_r:
                            _trail_note = (
                                f"trail_NOT_armed peak_r={_pr:.2f} < activation_r="
                                f"{_sest.activation_r:.2f}"
                            )
                        else:
                            try:
                                from src.strategies.uss.compiler import (
                                    _compute_trail_value as _ctv,
                                )
                                from src.strategies.uss.schema import Direction as _TD

                                _cand = _ctv(
                                    _sest.trail,
                                    _sl_before,
                                    _sest.entry,
                                    _sedf,
                                    len(_sedf) - 1,
                                    entry_bar=(len(_sedf) - 1 - _sex["tracker"]._bars),
                                    direction=_TD.SHORT if _sest.is_short else _TD.LONG,
                                )
                                _trail_note = (
                                    f"trail_ARMED peak_r={_pr:.2f} method="
                                    f"{getattr(_sest.trail.method, 'value', _sest.trail.method)} "
                                    f"candidate_sl(after ratchet-only)={_cand:.2f} "
                                    f"vs sl_before={_sl_before:.2f} -> "
                                    f"{'MOVES' if _cand != _sl_before else 'NO_CHANGE (candidate not tighter than current stop)'}"
                                )
                            except Exception as _te:
                                _trail_note = f"trail_ARMED candidate_calc_err={str(_te)[:80]}"
                    _swing_log.debug(
                        "swing_daily_trail_step_result tid=%s symbol=%s exit=%s exit_reason=%s "
                        "sl_changed=%s booked_partial=%s sl_before=%.2f sl_after=%.2f | %s",
                        _setid,
                        _sesym,
                        _sebe.exit,
                        _sebe.reason or "-",
                        _sebe.sl_changed,
                        _sebe.booked_partial,
                        _sl_before,
                        _sest.sl,
                        _trail_note,
                    )
                    if _sebe.booked_partial:
                        await _swing_om.book_partial(_setid, 0.5, _selastc)
                    if _sebe.exit:
                        await _swing_om.force_exit(_setid, _selastc, _sebe.reason)
                        _swing_exit_state.pop(_setid, None)
                        _swing_log.info(
                            "swing_smart_exit symbol=%s tid=%s reason=%s price=%.2f",
                            _sebare,
                            _setid,
                            _sebe.reason,
                            _selastc,
                        )
                    elif _sebe.sl_changed:
                        await _swing_om.update_sl(_setid, _sex["tracker"].current_sl)
                        _swing_log.info(
                            "swing_sl_ratchet symbol=%s tid=%s new_sl=%.2f partial=%s",
                            _sebare,
                            _setid,
                            _sex["tracker"].current_sl,
                            _sebe.booked_partial,
                        )

                compiled_all = strategy_reg.all_enabled()
                swing_strategies = [
                    c
                    for c in compiled_all
                    if getattr(getattr(c, "spec", None), "meta", None) is not None
                    and c.spec.meta.timeframe == "DAILY"
                ]
                if not swing_strategies:
                    _swing_log.info("swing_scan: no DAILY strategies enabled")
                    _last_scan_date = today
                    _persist_swing_scan_date(_scan_state_path, today)
                    await asyncio.sleep(300.0)
                    continue

                # Per-strategy universe: each DAILY strategy declares its own
                # universe_filter.index_filter (e.g. minervini_vcp/oneil_canslim
                # both declare NIFTY500) — swing strategies are not limited to
                # the global intraday watchlist. Falls back to the global
                # watchlist for any strategy that doesn't declare one.
                default_watchlist = shared.get("watchlist", [])
                strategy_universes: dict[str, list[str]] = {}
                for compiled in swing_strategies:
                    _idx = getattr(
                        getattr(compiled.spec, "universe_filter", None), "index_filter", None
                    )
                    strategy_universes[compiled.spec.meta.name] = (
                        _resolve_index_universe(_idx[0]) if _idx else default_watchlist
                    )
                    _cap_cr = getattr(
                        getattr(compiled.spec, "universe_filter", None),
                        "max_market_cap_crores",
                        None,
                    )
                    if _cap_cr is not None:
                        _sname = compiled.spec.meta.name
                        _mcap_src = f"swing_scan:{_sname}:mcap_filter"
                        _fund_repo = shared.get("fundamentals_repo")
                        _before = strategy_universes[_sname]
                        _mcap_problem: tuple[str, str] | None = None  # (tier, message)
                        if _fund_repo is None:
                            _mcap_problem = (
                                "CRITICAL",
                                f"{_sname}: market-cap filter (<= {_cap_cr:g} cr) is NOT applied — "
                                "no fundamentals repository (DB unavailable)",
                            )
                        else:
                            try:
                                _caps = await _fund_repo.latest_market_caps(_before)
                                _kept, _n_unknown_cap = _apply_market_cap_limit(
                                    _before, _caps, _cap_cr
                                )
                                _dropped = sorted(set(_before) - set(_kept))
                                strategy_universes[_sname] = _kept
                                _swing_log.info(
                                    "swing_scan_mcap strategy=%s limit_cr=%g universe=%d "
                                    "with_cap_data=%d unknown=%d dropped_too_large=%d kept=%d",
                                    _sname,
                                    _cap_cr,
                                    len(_before),
                                    len(_caps),
                                    _n_unknown_cap,
                                    len(_dropped),
                                    len(_kept),
                                )
                                _swing_log.debug(
                                    "swing_scan_mcap_dropped strategy=%s %s",
                                    _sname,
                                    {s: round(_caps[s] / _INR_PER_CRORE) for s in _dropped},
                                )
                                if _before and _n_unknown_cap == len(_before):
                                    _mcap_problem = (
                                        "CRITICAL",
                                        f"{_sname}: market-cap filter (<= {_cap_cr:g} cr) is INACTIVE — "
                                        f"no market-cap data for any of {len(_before)} symbols "
                                        "(fundamentals feed off/empty: set global.json "
                                        "fundamentals.enabled=true and let the daily fetch run)",
                                    )
                                elif _n_unknown_cap:
                                    _mcap_problem = (
                                        "WARNING",
                                        f"{_sname}: market-cap filter applied to "
                                        f"{len(_before) - _n_unknown_cap}/{len(_before)} symbols; "
                                        f"{_n_unknown_cap} have no cap data and were kept unfiltered",
                                    )
                            except Exception as _mce:
                                _swing_log.error(
                                    "swing_scan_mcap_err strategy=%s err=%s: %s — universe unfiltered",
                                    _sname,
                                    type(_mce).__name__,
                                    _mce,
                                )
                                _mcap_problem = (
                                    "CRITICAL",
                                    f"{_sname}: market-cap filter query failed "
                                    f"({type(_mce).__name__}: {str(_mce)[:100]}) — universe unfiltered",
                                )
                        if _mcap_problem is not None:
                            _swing_log.warning(
                                "swing_scan_mcap_problem tier=%s %s",
                                _mcap_problem[0],
                                _mcap_problem[1],
                            )
                        _sync_scan_alert(shared, _mcap_src, _mcap_problem)

                _swing_log.info(
                    "swing_scan_start strategies=%d universes=%s",
                    len(swing_strategies),
                    {name: len(syms) for name, syms in strategy_universes.items()},
                )

                # Benchmark + cross-sectional RS percentile, wired into every DAILY
                # strategy before evaluation. Without this, RS_PERCENTILE (used by
                # minervini_vcp's rs_rating>70 gate) falls back to ranking a stock
                # against its OWN trailing 252-day history — which only clears 70
                # once price is already near its own 1yr high, i.e. well after a
                # VCP base breakout has run (a base is a pause below the stock's
                # own recent RS peak, so the self-referential percentile dips during
                # it and only recovers post-breakout). rs_rankings is the real
                # cross-sectional table (this symbol vs its peers), refreshed by
                # _rs_ranking_loop every compute_interval_minutes — reuse it here
                # instead of recomputing, so the swing scan sees a stock as an RS
                # leader as soon as it actually is one, not days after the move.
                _nifty_df = await _get_daily("NIFTY 50")
                _rs_pct_map: dict[str, float] = {}
                _mdr = shared.get("market_data_repo")
                if _mdr is not None:
                    try:
                        _rs_snap = await _mdr.latest_rs_snapshot()
                        _rs_pct_map = {
                            str(_r["symbol"]): float(_r["percentile_rank"])
                            for _r in _rs_snap
                            if _r["percentile_rank"] is not None
                        }
                    except Exception as _rse:
                        _swing_log.debug("swing_rs_snapshot_err err=%s", str(_rse)[:80])
                _swing_log.info(
                    "swing_scan_rs_context nifty=%s rs_symbols=%d",
                    _nifty_df is not None,
                    len(_rs_pct_map),
                )

                # Layer 5A (Universe Adapter, spec §9) — shadow-log ONLY, per an
                # explicit decision: narrowing the tradeable universe removes
                # real-money opportunities, so this never actually filters
                # strategy_universes in this build regardless of mode value.
                # Enforcing it live is a deliberate future decision after
                # reviewing weeks of would-exclude logs for false positives.
                if _learning_mode("universe_adapter") in ("shadow", "live"):
                    try:
                        _ua = shared.get("universe_adapter")
                        if _ua is not None:
                            for _strat_name, _syms in strategy_universes.items():
                                _meta = {
                                    s: {
                                        "sector": _resolve_sector(s),
                                        "rs_rank": _rs_pct_map.get(s, 0.0),
                                    }
                                    for s in _syms
                                }
                                _would_keep = set(
                                    _ua.filter_universe(
                                        _strat_name,
                                        _syms,
                                        _meta,
                                        mode=_segment_mode(shared, "swing"),
                                    )
                                )
                                _would_exclude = [s for s in _syms if s not in _would_keep]
                                if _would_exclude:
                                    _swing_log.info(
                                        "universe_adapter_shadow strategy=%s would_exclude=%d/%d",
                                        _strat_name,
                                        len(_would_exclude),
                                        len(_syms),
                                    )
                    except Exception as _uae:
                        _swing_log.debug("universe_adapter_shadow_err err=%s", str(_uae)[:80])

                # Swing budget: config/capital.json is the single source of truth for
                # these three (2026-10-07), in place of operator.json hard_limits.swing.
                from src.risk.budget import get_budget as _rb_get_budget_swing

                _rb_swing = _rb_get_budget_swing().swing
                max_swing_per_day = _rb_swing.max_new_trades_per_day
                max_swing_per_strategy = _rb_swing.max_new_trades_per_day_per_strategy
                # Fraction of free cash held back from CNC delivery orders. Covers the
                # gap between the signal price and the actual next-open fill, plus
                # charges — without it a full-cash order rejects on a small gap up.
                shared["swing_cash_cushion_pct"] = _rb_swing.cash_buffer_pct / 100.0

                _today_iso = today.isoformat()
                swing_day_set = shared.setdefault("swing_traded", {}).setdefault(_today_iso, {})
                # DB-source the day's per-strategy counts too (defense in depth alongside
                # the persisted _last_scan_date above) — an in-memory-only counter reset
                # to 0 on every restart, silently bypassing max_new_trades_per_day[_per_strategy]
                # the same way the CRM G7 counters did before they were made DB-sourced.
                _pool = shared.get("db_pool")
                if _pool is not None:
                    try:
                        _db_rows = await _pool.fetch(
                            "SELECT strategy_name, count(*) AS n FROM ("
                            "  SELECT strategy_name FROM trades"
                            "  WHERE product = 'CNC' AND order_state NOT IN"
                            "        ('REJECTED','CANCELLED')"
                            "    AND (entry_timestamp AT TIME ZONE 'Asia/Kolkata')::date = $1"
                            "  UNION ALL"
                            "  SELECT strategy_name FROM paper_trades"
                            "  WHERE product = 'CNC' AND order_state NOT IN"
                            "        ('REJECTED','CANCELLED')"
                            "    AND (entry_timestamp AT TIME ZONE 'Asia/Kolkata')::date = $1"
                            ") x GROUP BY strategy_name",
                            today,
                        )
                        for _r in _db_rows:
                            _sn = str(_r["strategy_name"] or "")
                            swing_day_set[_sn] = max(swing_day_set.get(_sn, 0), int(_r["n"]))
                    except Exception as _dce:
                        _swing_log.debug("swing_day_count_db_err err=%s", str(_dce)[:80])
                swing_total = sum(swing_day_set.values())
                _signals_seen: list[dict[str, Any]] = []

                # Evaluate EVERY strategy's whole universe first, pool the candidates,
                # then rank once across the entire pool and place only the best few.
                # Ranking used to happen inside this per-strategy loop, so whichever
                # strategy was evaluated first consumed the daily cap and a better setup
                # from a later strategy never got a look (2026-08-27).
                _candidates: list[dict[str, Any]] = []
                _breadth_series = await _breadth_for(swing_strategies, _pool)
                for compiled in swing_strategies:
                    strategy_name = compiled.spec.meta.name
                    strat_count = swing_day_set.get(strategy_name, 0)
                    compiled.set_market_context(_nifty_df)
                    _set_breadth = getattr(compiled, "set_breadth_context", None)
                    if _set_breadth is not None:
                        _set_breadth(_breadth_series)

                    if strat_count >= max_swing_per_strategy:
                        _swing_log.info(
                            "swing_scan_strategy_skipped strategy=%s reason=daily_cap "
                            "placed_today=%d cap=%d",
                            strategy_name,
                            strat_count,
                            max_swing_per_strategy,
                        )
                        continue

                    # Per-strategy funnel: how many symbols each entry condition let
                    # through (alone, and cumulatively in spec order), plus every
                    # reason a symbol was not evaluated at all.
                    _fun = _new_scan_funnel()
                    _universe = strategy_universes[strategy_name]
                    for bare_sym in _universe:
                        df = await _get_daily(bare_sym)
                        if df is None:
                            _fun["no_data"] += 1
                            continue
                        full_sym = f"NSE:{bare_sym}" if ":" not in bare_sym else bare_sym

                        compiled.set_rs_percentile_override(_rs_pct_map.get(bare_sym))
                        try:
                            sig = compiled.evaluate_candle(df, -1)
                        except Exception as _se:
                            _fun["eval_err"] += 1
                            _fun["first_err"] = _fun["first_err"] or (
                                f"{bare_sym}: {type(_se).__name__}: {_se}"
                            )
                            if _fun["eval_err"] <= 5:  # cap noise; total is in the funnel line
                                _swing_log.warning(
                                    "swing_eval_err sym=%s strat=%s err=%s: %s",
                                    bare_sym,
                                    strategy_name,
                                    type(_se).__name__,
                                    _se,
                                )
                            continue
                        _fun["evaluated"] += 1
                        try:
                            _expl = compiled.explain_entry(df, -1)
                        except Exception as _xe:
                            _expl = []
                            _fun["explain_err"] += 1
                            if _fun["explain_err"] <= 3:
                                _swing_log.warning(
                                    "swing_explain_err sym=%s strat=%s err=%s: %s",
                                    bare_sym,
                                    strategy_name,
                                    type(_xe).__name__,
                                    _xe,
                                )
                        _funnel_record(_fun, _expl)
                        _all_pass = bool(_expl) and all(r["passed"] for r in _expl)
                        if _all_pass:
                            _swing_log.info(
                                "swing_scan_conditions strat=%s sym=%s all_conditions=PASS signal=%s | %s",
                                strategy_name,
                                bare_sym,
                                "yes" if sig is not None else "no(invalid_geometry)",
                                _fmt_conditions(_expl),
                            )
                        else:
                            _swing_log.debug(
                                "swing_scan_conditions strat=%s sym=%s failed=%s | %s",
                                strategy_name,
                                bare_sym,
                                [r["name"] for r in _expl if not r["passed"]],
                                _fmt_conditions(_expl),
                            )
                        if sig is None:
                            continue

                        _risk = abs(sig.entry_price - sig.sl)
                        _reward = abs(sig.target - sig.entry_price)
                        _rr = (_reward / _risk) if _risk > 0 else 0.0
                        _candidates.append(
                            {
                                "bare_sym": bare_sym,
                                "full_sym": full_sym,
                                "sig": sig,
                                "rr": _rr,
                                "symbol": bare_sym,  # rank_candidates tie-breaks on this
                                "strategy_name": strategy_name,
                                "compiled": compiled,
                                "score": _score_swing_candidate(df, sig),
                                "sqs": _swing_sqs_shadow(
                                    shared, strategy_reg, strategy_name, bare_sym, df, sig
                                ),
                            }
                        )
                        _pw = _strategy_priority_weight(
                            shared, strategy_name, data_mode=_segment_mode(shared, "swing")
                        )
                        if _learning_mode("strategy_selector") == "live" and _pw is not None:
                            _candidates[-1]["priority_weight"] = _pw
                        elif _pw is not None:
                            _swing_log.debug(
                                "strategy_selector_shadow strategy=%s regime=%s weight=%.3f",
                                strategy_name,
                                shared.get("regime", "NEUTRAL"),
                                _pw,
                            )

                    _log_scan_funnel(shared, strategy_name, len(_universe), _fun)

                # Cash-blocked retries: a signal that passed CRM/ranking on an earlier
                # scan but was stopped only by insufficient_cash gets folded back into
                # today's pool so it competes for a slot like any fresh signal — same
                # ranking, same live cash check, same dedup. Capped at
                # _SWING_CASH_BACKLOG_MAX_RETRIES extra scans (see that constant).
                _fresh_keys = {(c["bare_sym"], c["strategy_name"]) for c in _candidates}
                _compiled_by_strategy = {c.spec.meta.name: c for c in swing_strategies}
                _cash_backlog_in = _load_swing_cash_backlog(_cash_backlog_path)
                for _b in _cash_backlog_in:
                    _b_key = (_b.get("bare_sym"), _b.get("strategy_name"))
                    if _b_key in _fresh_keys:
                        continue  # the strategy re-fired on it today — fresh signal supersedes it
                    _b_compiled = _compiled_by_strategy.get(_b.get("strategy_name"))
                    if _b_compiled is None:
                        continue  # strategy no longer active — drop
                    try:
                        from src.strategies.uss.compiler import Signal as _BLSig
                        from src.strategies.uss.schema import Direction as _BLDir

                        _b_sig = _BLSig(
                            str(_b["strategy_name"]),
                            _BLDir(_b["direction"]),
                            float(_b["entry_price"]),
                            float(_b["sl"]),
                            float(_b["target"]),
                        )
                    except Exception:
                        continue
                    _candidates.append(
                        {
                            "bare_sym": _b["bare_sym"],
                            "full_sym": f"NSE:{_b['bare_sym']}",
                            "sig": _b_sig,
                            "rr": float(_b.get("rr", 0.0)),
                            "symbol": _b["bare_sym"],
                            "strategy_name": _b["strategy_name"],
                            "compiled": _b_compiled,
                            "score": float(_b.get("score", 0.0)),
                            "from_backlog": True,
                            "backlog_retries_used": int(_b.get("retries_used", 0)),
                        }
                    )
                _next_cash_backlog: list[dict[str, Any]] = []

                if True:  # scope guard — selection + execution below operate on the pool
                    _accepted, _rejected = _select_top_candidates(
                        _candidates,
                        max_total=max(0, max_swing_per_day - swing_total),
                        max_per_strategy=max_swing_per_strategy,
                        per_strategy_counts=dict(swing_day_set),
                    )
                    if _candidates:
                        _swing_log.info(
                            "swing_rank_pool n=%d accepted=%s",
                            len(_candidates),
                            [
                                f"{c['bare_sym']}/{c['strategy_name']}({c['score']:.3f})"
                                for c in _accepted
                            ],
                        )
                        _sqs_seen = [c for c in _candidates if c.get("sqs")]
                        if _sqs_seen:
                            _swing_log.info(
                                "swing_sqs_shadow n=%d pool=%s",
                                len(_sqs_seen),
                                [
                                    f"{c['bare_sym']}/{c['strategy_name']}"
                                    f"(score={c['score']:.3f} sqs={c['sqs']['total']:.1f})"
                                    for c in _sqs_seen
                                ],
                            )

                    for c in _rejected:
                        _swing_log.info(
                            "swing_signal_rejected strategy=%s sym=%s score=%.3f rr=%.2f "
                            "reason=ranked_below_cap",
                            c["strategy_name"],
                            c["bare_sym"],
                            c["score"],
                            c["rr"],
                        )
                        _signals_seen.append(
                            {
                                "symbol": c["bare_sym"],
                                "strategy_name": c["strategy_name"],
                                "direction": c["sig"].direction.value,
                                "entry": c["sig"].entry_price,
                                "sl": c["sig"].sl,
                                "target": c["sig"].target,
                                "decision": "blocked",
                                "gate": f"ranked_below_cap rr={c['rr']:.2f}",
                                "gate_results": [],
                                "ts": now.isoformat(),
                            }
                        )
                        if c.get("from_backlog"):
                            _retries = int(c.get("backlog_retries_used", 0)) + 1
                            if _retries <= _SWING_CASH_BACKLOG_MAX_RETRIES:
                                _next_cash_backlog.append(_swing_cash_backlog_entry(c, _retries))
                            else:
                                _swing_log.info(
                                    "swing_cash_backlog_expired sym=%s strategy=%s retries=%d",
                                    c["bare_sym"],
                                    c["strategy_name"],
                                    _retries,
                                )

                    # One at a time: _execute_swing_signal re-reads live cash before
                    # every single order, so each placement sees what the previous one
                    # actually consumed.
                    for _acc_idx, c in enumerate(_accepted):
                        bare_sym, full_sym, sig = c["bare_sym"], c["full_sym"], c["sig"]
                        _cand_strategy = c["strategy_name"]
                        if swing_total >= max_swing_per_day:
                            # None of the remaining candidates reached execution this
                            # cycle — the day's cap, not cash, stopped them. Carry any
                            # backlog-sourced ones forward unchanged: not a used retry.
                            for _c2 in _accepted[_acc_idx:]:
                                if _c2.get("from_backlog"):
                                    _next_cash_backlog.append(
                                        _swing_cash_backlog_entry(
                                            _c2, int(_c2.get("backlog_retries_used", 0))
                                        )
                                    )
                            break
                        # Execute through shared execution path with CNC product
                        try:
                            (
                                _trade_id,
                                _px,
                                _sl,
                                _tgt,
                                _reason,
                                _gates,
                            ) = await _execute_swing_signal(
                                shared,
                                _cand_strategy,
                                full_sym,
                                sig,
                                now,
                                compiled=c["compiled"],
                            )
                            if _reason == "amo_window_not_open":
                                # Broker's AMO window isn't actually open at 15:45
                                # the way _current_order_variety() assumes (see
                                # _SWING_AMO_RETRY_INTERVAL_SEC's comment) — hand off
                                # to the non-blocking backlog instead of retrying
                                # in place, so the next candidate isn't stuck behind
                                # this one and other scan-loop duties aren't delayed.
                                _amo_now_ts = _dt.now(_IST).timestamp()
                                _amo_backlog_in = _load_swing_cash_backlog(_amo_backlog_path)
                                _amo_backlog_in.append(
                                    _swing_amo_backlog_entry(
                                        bare_sym,
                                        full_sym,
                                        _cand_strategy,
                                        sig,
                                        _amo_now_ts,
                                        _amo_now_ts,
                                        0,
                                    )
                                )
                                _save_swing_cash_backlog(_amo_backlog_path, _amo_backlog_in)
                                _swing_log.info(
                                    "swing_amo_window_deferred sym=%s strategy=%s — "
                                    "broker AMO window not open yet, parked for retry "
                                    "every %.0fs until 08:57 IST",
                                    bare_sym,
                                    _cand_strategy,
                                    _SWING_AMO_RETRY_INTERVAL_SEC,
                                )
                            _signals_seen.append(
                                {
                                    "symbol": bare_sym,
                                    "strategy_name": _cand_strategy,
                                    "direction": sig.direction.value,
                                    "entry": _px,
                                    "sl": _sl,
                                    "target": _tgt,
                                    "decision": "executed" if _trade_id else "blocked",
                                    "gate": None if _trade_id else _reason,
                                    "gate_results": _gates,
                                    "ts": _dt.now(_IST).isoformat(),
                                }
                            )
                            # Count every order that REACHED THE BROKER, not just the
                            # ones that filled. _execute_swing_signal returns None for
                            # "not_filled", so counting on _trade_id let unfilled orders
                            # consume a broker slot for free: on 2026-08-27 three orders
                            # went out against max_new_trades_per_day=2 because
                            # JINDALSAW never filled and so never counted.
                            if _trade_id is not None or _reason == "not_filled":
                                swing_day_set[_cand_strategy] = (
                                    swing_day_set.get(_cand_strategy, 0) + 1
                                )
                                swing_total += 1
                                _swing_log.info(
                                    "swing_signal_executed strategy=%s sym=%s dir=%s "
                                    "score=%.3f rr=%.2f filled=%s",
                                    _cand_strategy,
                                    bare_sym,
                                    sig.direction.value,
                                    c["score"],
                                    c["rr"],
                                    _trade_id is not None,
                                )
                            elif _reason == "insufficient_cash":
                                _retries = int(c.get("backlog_retries_used", 0)) + (
                                    1 if c.get("from_backlog") else 0
                                )
                                if _retries <= _SWING_CASH_BACKLOG_MAX_RETRIES:
                                    _next_cash_backlog.append(
                                        _swing_cash_backlog_entry(c, _retries)
                                    )
                                else:
                                    _swing_log.info(
                                        "swing_cash_backlog_expired sym=%s strategy=%s retries=%d",
                                        bare_sym,
                                        _cand_strategy,
                                        _retries,
                                    )
                            # else (crm_blocked/lifecycle_err/crm_error/shadow_mode): drop —
                            # not a cash issue, blind-retrying it isn't in scope.
                            # (amo_window_not_open is handled above, before this block —
                            # parked in the retry backlog, not dropped.)
                        except Exception as _ee:
                            _swing_log.error(
                                "swing_execute_err sym=%s strat=%s err=%s",
                                bare_sym,
                                _cand_strategy,
                                _ee,
                            )

                _save_swing_cash_backlog(_cash_backlog_path, _next_cash_backlog)
                _last_scan_date = today
                _persist_swing_scan_date(_scan_state_path, today)
                _swing_log.info(
                    "swing_scan_complete placed=%d symbols_evaluated=%d",
                    swing_total,
                    len(daily_frames),
                )

                try:
                    import json as _json2

                    _swing_status = {
                        "ts": now.isoformat(),
                        "strategies_evaluated": len(swing_strategies),
                        "symbols_evaluated": len(daily_frames),
                        "signals": _signals_seen,
                    }
                    _swing_path = _LOGS / "swing_signals.json"
                    _tmp2 = _swing_path.with_suffix(".tmp")
                    _tmp2.write_text(_json2.dumps(_swing_status, indent=2), encoding="utf-8")
                    _tmp2.replace(_swing_path)
                except Exception as _we2:
                    _swing_log.debug("swing_scan_write_err err=%s", str(_we2)[:80])

            except Exception as exc:
                _swing_log.warning("swing_signal_scan_loop_error err=%s", exc)

        await asyncio.sleep(120.0)


def _score_swing_candidate(df: Any, sig: Any) -> float:
    """Composite 0-1 quality score for one swing candidate, via signal_ranker.

    Reuses the ranker that has been shadow-logging intraday picks since 2026-08-25 so
    swing and intraday judge "best setup" the same way. The daily signal bar supplies
    follow-through (close near the extreme = real demand, not a wick) and volume vs its
    own average. opening-range width is intraday-only, so it is passed as None and the
    ranker degrades that component to a neutral 0.5 rather than penalising the
    candidate for data that does not exist on a daily bar.
    """
    from src.execution.signal_ranker import score_candidate

    try:
        _bar = df.iloc[-1]
        _vol = float(_bar.get("volume", 0.0) or 0.0)
        _avg_vol = float(df["volume"].tail(50).mean()) if "volume" in df else 0.0
        return score_candidate(
            bar_high=float(_bar["high"]),
            bar_low=float(_bar["low"]),
            bar_close=float(_bar["close"]),
            is_short=str(getattr(sig.direction, "value", sig.direction)).upper() == "SHORT",
            breakout_volume=_vol or None,
            avg_volume=_avg_vol or None,
            or_width_pct=None,
        ).score
    except Exception:
        return 0.5  # never let a scoring failure drop a valid signal to the bottom


def _segment_for_strategy(strategy_reg: Any, name: str) -> str | None:
    """ "swing" | "intraday" from a strategy's own USSSpec.meta.timeframe
    (DAILY -> swing, 5MIN/15MIN -> intraday) — None if the strategy can't be
    found or its timeframe doesn't map cleanly, so callers can skip rather
    than guess which mode (paper/live) its trades actually belong to."""
    if strategy_reg is None:
        return None
    compiled = strategy_reg.get(name)
    if compiled is None:
        return None
    timeframe = str(getattr(getattr(compiled.spec, "meta", None), "timeframe", "") or "")
    if timeframe == "DAILY":
        return "swing"
    if timeframe in ("TICK", "1MIN", "5MIN", "15MIN", "30MIN", "1HOUR"):
        return "intraday"
    return None  # WEEKLY or unrecognized — not confidently mappable to a segment


def _segment_mode(shared: dict[str, Any], segment: str) -> str:
    """ "paper" or "live" for `segment` ("intraday"|"swing"|"options"), from
    crm_ctx.segment_paper_mode — defaults to "paper" (the safe default) if
    unavailable, never "live" on missing data."""
    crm_ctx = shared.get("crm_ctx")
    seg_map = getattr(crm_ctx, "segment_paper_mode", None) if crm_ctx is not None else None
    is_paper = True if seg_map is None else bool(seg_map.get(segment, True))
    return "paper" if is_paper else "live"


def _strategy_priority_weight(
    shared: dict[str, Any], strategy_name: str, data_mode: str = "live"
) -> float | None:
    """Layer 4A (StrategySelector) priority weight for `strategy_name` in the
    current regime, sourced from `data_mode`'s ("paper"|"live") own tracked
    expectancy data (paper and live are tracked completely separately — see
    src.learning.strategy_selector's module docstring; note this is a
    DIFFERENT axis from the learning-engine's off/shadow/live GATING mode
    returned by _learning_mode()) — None if strategy_selector.mode (the gate)
    is "off" or no data exists yet for this regime/data_mode. Callers attach
    the result to a candidate dict as "priority_weight" only when the GATING
    mode=="live" (so rank_candidates_by_sqs applies it); in "shadow" gating
    mode it's logged separately, never attached, so ranking is provably
    unaffected."""
    if _learning_mode("strategy_selector") not in ("shadow", "live"):
        return None
    selector = shared.get("strategy_selector")
    if selector is None:
        return None
    regime = str(shared.get("regime", "NEUTRAL"))
    priorities = selector.get_priorities(regime, [strategy_name], mode=data_mode)
    return priorities[0].weight if priorities else None


def _swing_sqs_shadow(
    shared: dict[str, Any],
    strategy_reg: Any,
    strategy_name: str,
    bare_sym: str,
    df: Any,
    sig: Any,
) -> dict[str, float] | None:
    """Signal Quality Score (spec §10) for one swing candidate — shadow-only,
    gated by config/learning.json signal_ranker.mode_swing. Mirrors the intraday
    inline computation in _intraday_signal_scan_loop; never affects placement here.
    """
    if _signal_ranker_mode("swing") not in ("shadow", "live"):
        return None
    try:
        from src.execution.signal_quality_adapter import build_signal_quality_input
        from src.intelligence.signal_quality_score import compute as _sqs_compute

        _bar = df.iloc[-1]
        _vol = float(_bar.get("volume", 0.0) or 0.0)
        _avg_vol = float(df["volume"].tail(50).mean()) if "volume" in df else 0.0
        _bt = strategy_reg.backtest_result(strategy_name)
        _bt_at = strategy_reg.backtest_computed_at(strategy_name)
        _res = _sqs_compute(
            build_signal_quality_input(
                bar_high=float(_bar["high"]),
                bar_low=float(_bar["low"]),
                bar_close=float(_bar["close"]),
                is_short=str(getattr(sig.direction, "value", sig.direction)).upper() == "SHORT",
                breakout_volume=_vol or None,
                avg_volume=_avg_vol or None,
                sector=_resolve_sector(bare_sym),
                sector_status=shared.get("sector_status", {}),
                sector_records=shared.get("sector_records", {}),
                regime=str(shared.get("regime", "NEUTRAL")),
                news_sentiment=(shared.get("news_recent", {}).get(bare_sym, {}) or {}).get(
                    "sentiment"
                ),
                backtest_win_rate=getattr(_bt, "win_rate", None),
                backtest_avg_r=getattr(_bt, "expectancy_r", None),
                backtest_computed_at=_bt_at,
            )
        )
        return {
            "total": _res.total,
            "setup": _res.setup_quality,
            "hist": _res.historical_edge,
            "ctx": _res.context_quality,
            "llm": _res.llm_assessment,
        }
    except Exception:
        return None


def _select_top_candidates(
    candidates: list[dict[str, Any]],
    *,
    max_total: int,
    max_per_strategy: int,
    per_strategy_counts: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from src.execution.signal_ranker import select_top_candidates

    return select_top_candidates(
        candidates,
        max_total=max_total,
        max_per_strategy=max_per_strategy,
        per_strategy_counts=per_strategy_counts,
        use_sqs=(_signal_ranker_mode("swing") == "live"),
    )


@strategy_logged(param="strategy_name")
async def _execute_swing_signal(
    shared: dict[str, Any],
    strategy_name: str,
    sym: str,
    sig: Any,
    now: Any,
    compiled: Any = None,
) -> tuple[str | None, float, float, float, str, list[dict[str, Any]]]:
    """Place a DAILY strategy signal as a CNC (DELIVERY) order through the full CRM pipeline.

    Returns (agent_trade_id_or_None, entry/fill_price, sl, target, reason, gate_results) —
    reason is "executed" on success, otherwise the block cause, so callers can log/display
    why a signal did not trade (Scanner tab wiring) without changing the success-path
    contract. gate_results is the full G0-G10 per-gate verdict list from CRM (empty if the
    signal never reached CRM — e.g. no_risk_engine/already_held/sizing_zero — since those
    are pre-CRM checks with no gate to report).
    """
    import time as _t

    from src.risk.crm import CRMSignal

    entry = float(sig.entry_price)
    sl = float(sig.sl)
    target = float(sig.target)

    crm = shared.get("crm")
    crm_ctx = shared.get("crm_ctx")
    # This function only ever handles the "swing" segment, so its paper/live mode
    # is fixed for the whole call.
    paper_mode = bool(shared.get("paper_mode", True))
    _is_paper = _resolve_is_paper(shared, crm_ctx, "swing", strategy_name, paper_mode)
    if _is_paper:
        _stack = _pick_stack(shared, True)
        # Fall back to the legacy singular shared["lifecycle"]/["order_monitor"] when
        # shared["stacks"] isn't populated (e.g. callers/tests not yet stack-aware).
        lifecycle = _stack["lifecycle"] if _stack else shared.get("lifecycle")
        order_monitor = _stack["order_monitor"] if _stack else shared.get("order_monitor")
    else:
        # Live: only the strategy's own broker's stack (R1) -- never the primary alias.
        _stack, _route_reject = _live_stack_or_reject(shared, strategy_name, "swing")
        if _stack is None:
            return None, entry, sl, target, "broker_route_rejected", []
        lifecycle = _stack["lifecycle"]
        order_monitor = _stack["order_monitor"]

    if crm is None or crm_ctx is None or lifecycle is None:
        _swing_log.warning("swing_blocked_no_risk_engine sym=%s strategy=%s", sym, strategy_name)
        return None, entry, sl, target, "no_risk_engine", []

    # Fail CLOSED until the held-position set has been loaded once (first reconciliation).
    # Prevents restart-window duplicates when order_monitor/held_symbols are still empty.
    if _held_symbols_unknown_for(shared, _is_paper, _stack):
        _swing_log.warning("swing_blocked_unreconciled sym=%s strategy=%s", sym, strategy_name)
        return None, entry, sl, target, "unreconciled", []
    # Skip if already holding this symbol
    _norm_sym = sym.split(":")[-1]
    # Phase 2: dedup is scoped to THIS call's own resolved mode only — a symbol
    # already held in paper must never block a live entry, and vice versa
    # (confirmed with user: fully separate). Falls back to the legacy combined
    # shared["held_symbols"] when the mode-specific set isn't populated.
    _om_syms = (
        {p.symbol.split(":")[-1] for p in order_monitor.get_monitored()}
        if order_monitor is not None
        else set()
    )
    _held_key = "held_symbols_paper" if _is_paper else "held_symbols_live"
    _held_syms = shared.get(_held_key)
    if _held_syms is None:
        _held_syms = shared.get("held_symbols") or set()
    if not _is_paper:
        _held_syms = _held_syms | _unloaded_brokers_tracked_symbols(shared)
    if _norm_sym in _om_syms or _norm_sym in _held_syms:
        return None, entry, sl, target, "already_held", []

    # News HALT/AVOID gate
    _avoid = shared.get("news_avoid_symbols", {})
    if sym in _avoid and _avoid[sym] > _t.time():
        _swing_log.info("swing_blocked_news sym=%s", sym)
        return None, entry, sl, target, "news_avoid", []

    is_long = sig.direction.value == "LONG"
    direction = "BUY" if is_long else "SELL"
    sl_pct = abs(entry - sl) / entry if entry else 0.0
    tgt_pct = abs(target - entry) / entry if entry else 0.0
    if sl_pct <= 0 or entry <= 0:
        return None, entry, sl, target, "invalid_sl", []

    from src.risk.crm import swing_risk_cap
    from src.risk.position_sizing import intraday_position_size

    # Must use the same risk cap G8 will apply: G8 ends in min(qty, cap_qty, budget_qty)
    # and can only size DOWN, so sizing here off a smaller cap would silently clamp the
    # trade below what config/capital.json swing.max_loss_per_trade allows. Refresh
    # first: the pre-CRM refresh below runs after sizing, too late for the first trade.
    _risk_pct = getattr(compiled.spec, "risk_pct", None) if compiled is not None else None
    crm_ctx.current_equity = float(shared.get("current_equity", crm_ctx.current_equity))
    _refresh_options_risk_budget(crm_ctx)
    _avail_cash = float(shared.get("available_cash", crm_ctx.current_equity))
    _sizing_ctx = crm_ctx
    _bkey: str | None = None
    if not _is_paper and shared.get("per_broker_equity"):
        # Per-broker risk: size on the routed broker's OWN cap and cash; unknown => no trade.
        from src.risk.crm import broker_view

        _bkey = _live_key_for(shared, strategy_name)
        _funds = _broker_funds(shared, _bkey)
        if _funds is None:
            _swing_log.warning(
                "swing_blocked_broker_equity_unknown sym=%s strategy=%s key=%s",
                sym,
                strategy_name,
                _bkey,
            )
            return None, entry, sl, target, "broker_equity_unavailable", []
        _sizing_ctx = broker_view(crm_ctx, _bkey)
        _avail_cash = _funds[1]
    _intended = intraday_position_size(
        entry, sl, swing_risk_cap(_risk_pct, _sizing_ctx), _avail_cash
    )
    if _intended <= 0:
        return None, entry, sl, target, "sizing_zero", []

    from src.execution.trade_meta import build_trade_meta

    # RegimeFilter.max_attempts_per_phase (spec §8.2) — only strategies that set
    # regime_filter pay this cost. (_risk_pct is resolved above, before sizing.)
    _regime_phase_attempts = 0
    _max_regime_phase_attempts = 0
    _rf = getattr(compiled.spec, "regime_filter", None) if compiled is not None else None
    if _rf is not None and _rf.enabled and _rf.max_attempts_per_phase > 0:
        _db_pool = shared.get("db_pool")
        _regime_ok = True
        _rlo = getattr(compiled, "_regime_long_ok", None)
        if _rlo is not None and len(_rlo):
            _regime_ok = bool(_rlo.iloc[-1])
        await _update_regime_phase(_db_pool, strategy_name, sym, _regime_ok)
        _regime_phase_attempts, _ = await _regime_phase_attempts_for(_db_pool, strategy_name, sym)
        _max_regime_phase_attempts = _rf.max_attempts_per_phase

    crm_signal = CRMSignal(
        signal_id=f"SWING-{sym}-{now.strftime('%Y%m%d')}",
        symbol=sym,
        sector_declining=_resolve_sector_declining(sym),
        sector=_resolve_sector(sym),
        regime_phase_attempts=_regime_phase_attempts,
        max_regime_phase_attempts=_max_regime_phase_attempts,
        risk_pct=_risk_pct,
        direction=direction,
        strategy_name=strategy_name,
        intended_quantity=_intended,
        stop_loss_pct=sl_pct,
        target_pct=tgt_pct,
        risk_rupees=abs(entry - sl) * _intended,  # TOTAL rupees (G7 open-risk cap)
        entry_price=entry,
        regime=shared.get("regime", "BULL"),
        strategy_health=shared.get("health_scores", {}).get(strategy_name, "HEALTHY"),
        earnings_proximity_days=999,
        high_magnitude_news=(sym in shared.get("news_high_magnitude", {})),
        vix=float(shared.get("vix", 15.0)),
        engine="swing",
        meta=build_trade_meta(
            autonomy_mode=shared.get("autonomy_mode", _DEFAULT_AUTONOMY_MODE),
            strategy_name=strategy_name,
            regime_at_entry=shared.get("regime", "UNKNOWN"),
        ),
    )
    crm_signal.meta["backtest_passed"] = (
        shared.get("backtest_summary", {}).get(strategy_name, {}).get("passed", True)
    )
    # Post-fill cap must equal the cap this trade was sized with (see TradeRecord).
    crm_signal.meta["max_risk_rupees"] = swing_risk_cap(_risk_pct, _sizing_ctx)
    crm_ctx.current_equity = float(shared.get("current_equity", crm_ctx.current_equity))
    _refresh_options_risk_budget(crm_ctx)
    if strategy_name == "llm_swing_agent":
        crm_ctx.blocked_regimes["llm_swing_agent"] = [
            str(r) for r in (_cfg("global").get("llm_swing", {}).get("block_regimes") or [])
        ]

    # ── G9 LLM confirmation ───────────────────────────────────────────────────
    # Driven by strategies_registry.json `llm_gate_required` (via crm_ctx.llm_gate_required;
    # llm_originated strategies are always required). Required -> a genuine verdict must
    # come back from the swing_g9 provider chain (global.json llm.swing_g9, gemini-free:
    # llm_swing_agent originates on the gemini-first chain, so gemini would grade its own
    # idea) or the trade is skipped — no DSL-only pass. crm.py G9 enforces the same rule
    # for every path, this early return just adds the alert. Not required -> no LLM call.
    if crm_ctx.llm_gate_required.get(strategy_name, False):
        if "swing_g9_llm" not in shared:
            from src.llm.providers import make_swing_gate_llm_client

            shared["swing_g9_llm"] = make_swing_gate_llm_client()
        _g9_client = shared.get("swing_g9_llm")
        _verdict: dict[str, Any] | None = None
        if _g9_client is not None:
            from src.llm.providers import g9_chain_timeout
            from src.llm.trade_evaluator import evaluate_trade_gated as _evaluate_trade
            from src.news.context import build_news_context

            _verdict = await _evaluate_trade(
                _g9_client,
                {
                    "symbol": sym,
                    "direction": getattr(sig.direction, "value", str(sig.direction)),
                    "strategy_name": strategy_name,
                    "regime": shared.get("regime", "UNKNOWN"),
                    "entry_price": entry,
                    "stop_loss_pct": abs(entry - sl) / entry * 100 if entry else 0.0,
                    "target_pct": abs(target - entry) / entry * 100 if entry else 0.0,
                    "strategy_health": shared.get("health_scores", {}).get(
                        strategy_name, "HEALTHY"
                    ),
                    "vix": float(shared.get("vix", 15.0)),
                    "news_context": build_news_context(sym, shared),
                },
                int(shared.get("g9_malformed_swing", 0)),
                chain_timeout_secs=g9_chain_timeout("swing_g9", 60.0),
            )
            shared["g9_malformed_swing"] = _verdict["malformed"]
            # order_lifecycle writes llm_flag/llm_confidence/llm_reason straight off
            # this meta dict, so setting it here is also the DB + dashboard wiring.
            crm_signal.meta["llm_confidence"] = _verdict["llm_confidence"]
            crm_signal.meta["llm_flag"] = _verdict["llm_flag"]
            crm_signal.meta["llm_reason"] = _verdict["llm_reason"]
            crm_signal.meta["llm_evaluated"] = _verdict["evaluated"]
            crm_signal.meta["llm_provider"] = _verdict.get("provider")
            crm_signal.meta["llm_latency_ms"] = _verdict.get("latency_ms")

            # sig.confidence is only a genuine origination-model confidence for
            # llm_swing_agent -- DSL swing strategies never set it, so it silently
            # holds Signal's dataclass default of 1.0, which would pollute this
            # comparison with a fake "gap". Phase 1: observe only, never gates.
            if strategy_name == "llm_swing_agent" and _verdict["evaluated"]:
                _gap = abs(float(sig.confidence) - float(_verdict["llm_confidence"]))
                crm_signal.meta["llm_origination_confidence"] = sig.confidence
                crm_signal.meta["llm_confidence_gap"] = _gap
                _swing_log.info(
                    "g9_disagreement symbol=%s strategy=%s origination_confidence=%.2f "
                    "reviewer_confidence=%.2f gap=%.2f reviewer_flag=%s",
                    sym,
                    strategy_name,
                    sig.confidence,
                    _verdict["llm_confidence"],
                    _gap,
                    _verdict["llm_flag"],
                )

        if _verdict is None or not _verdict["evaluated"]:
            _reason = (
                "no provider configured"
                if _g9_client is None
                else str((_verdict or {}).get("llm_reason", "no valid verdict"))
            )
            _swing_log.warning(
                "swing_g9_no_verdict sym=%s strategy=%s — LLM gate required, trade skipped (%s)",
                sym,
                strategy_name,
                _reason,
            )
            # A real trade being skipped, silently, with no alert, is exactly the kind
            # of thing a trader needs to know without digging through logs.
            try:
                from src.notifications import EventKind, notify
                from src.notifications.events import Segment

                notify(
                    EventKind.ENTRY_REJECTED,
                    is_paper=getattr(lifecycle, "_is_paper", True),
                    symbol=sym,
                    segment=Segment.SWING,
                    error=f"{strategy_name}: G9 LLM gate required, no verdict — trade skipped",
                )
            except Exception:
                pass
            return None, entry, sl, target, "g9_no_verdict", []

        _swing_log.info(
            "swing_g9_check sym=%s strategy=%s flag=%s confidence=%.2f provider=%s attempts=%s",
            sym,
            strategy_name,
            crm_signal.meta.get("llm_flag", "-"),
            float(crm_signal.meta.get("llm_confidence", 0.0)),
            (_verdict or {}).get("provider"),
            (_verdict or {}).get("attempts"),
        )

    try:
        decision = crm.evaluate(crm_signal, crm_ctx)
    except Exception as exc:
        _swing_log.error("swing_crm_error sym=%s err=%s", sym, exc)
        return None, entry, sl, target, "crm_error", []

    log_gate_decisions(crm_signal, decision)
    _gates = [
        {"gate_id": g.gate_id, "decision": g.decision.value, "reason": g.reason}
        for g in (decision.gate_results or [])
    ]

    if not decision.approved or decision.final_quantity <= 0:
        _blk_reason = next((g["reason"] for g in _gates if g["decision"] == "BLOCK"), "-")
        _swing_log.info(
            "swing_crm_blocked sym=%s strategy=%s gate=%s reason=%s",
            sym,
            strategy_name,
            decision.blocking_gate,
            _blk_reason,
        )
        if decision.blocking_gate == "G9":
            # G9 is the only gate that represents an independent LLM saying "no"
            # to an idea already past every other risk check -- worth a page.
            # The other gates (size/exposure/regime caps) are routine and fire
            # often enough that alerting on every one would just be noise.
            try:
                from src.notifications import EventKind, notify
                from src.notifications.events import Segment

                notify(
                    EventKind.ENTRY_REJECTED,
                    is_paper=getattr(lifecycle, "_is_paper", True),
                    symbol=sym,
                    segment=Segment.SWING,
                    error=f"{strategy_name}: G9 rejected the trade",
                )
            except Exception:
                pass
        return None, entry, sl, target, f"crm_blocked:{decision.blocking_gate}", _gates

    qty = decision.final_quantity or _intended

    from src.risk.crm import is_shadow as _is_shadow

    if _is_shadow(crm_signal, crm_ctx):
        _swing_log.info(
            "swing_shadow_skip sym=%s strategy=%s qty=%d — segment shadow_mode: not placed",
            sym,
            strategy_name,
            qty,
        )
        return None, entry, sl, target, "shadow_mode", _gates

    # ── Delivery cash gate ────────────────────────────────────────────────────
    # CNC is delivery: it must be paid for in FULL, in CASH. Pledged collateral funds
    # intraday/F&O margin but cannot buy delivery stock, so `net` (mostly collateral
    # here) is not spending power — available_cash is. Checked live immediately before
    # the order, and against cash already committed by earlier orders this session,
    # because each signal used to size against the same stale snapshot: on 2026-08-27
    # three orders totalling Rs 36,706 went out against Rs 29,785 of real cash.
    _cash_cushion = float(shared.get("swing_cash_cushion_pct", 0.0))  # fraction of cash held back
    _required = float(qty) * float(entry)
    _committed = float(shared.get("swing_cash_committed_today", 0.0))
    _free_cash = float(shared.get("available_cash", 0.0))
    if _bkey is not None:
        # Per-broker risk: the fallback (margin read fails below) is THIS broker's last cash,
        # never the primary's; unknown => 0 (cannot fund => never a real order).
        _free_cash = (_broker_funds(shared, _bkey) or (0.0, 0.0))[1]
    _broker_for_margin = getattr(lifecycle, "_broker", None)
    if _broker_for_margin is not None:
        try:
            _m = await asyncio.wait_for(_broker_for_margin.get_margins(), timeout=10.0)
            _free_cash = float(_m.available_cash)
        except Exception as _me:
            _swing_log.warning("swing_margin_fetch_failed sym=%s err=%s", sym, str(_me)[:80])
    # Every caller of this function places an AMO (after-market, 15:45+ IST) —
    # Zerodha's margin API does NOT reserve/debit cash for a pending AMO until it
    # actually converts and executes the next session, so a successful live read
    # here still shows the FULL cash even after an earlier order this same cycle
    # was placed. Always subtract the session accumulator on top of the live read
    # (bug found 2026-09-07: minervini_vcp and oneil_canslim both signalled CGCL in
    # the same 15:45 scan; the live read "succeeded" for order 2 and, skipping
    # _committed, wrongly saw the pre-order-1 cash again — two qty=45 AMOs went out
    # for the same symbol instead of one being cash-blocked).
    _spendable = _free_cash * (1.0 - _cash_cushion) - _committed
    if _required > _spendable and float(entry) > 0:
        # Size DOWN to what cash allows rather than skip: same SL, fewer shares means
        # strictly less rupee risk, so the trade stays inside every risk limit.
        _affordable = int(max(_spendable, 0.0) // float(entry))
        if _affordable >= 1:
            _swing_log.warning(
                "swing_qty_reduced_for_cash sym=%s qty=%d->%d spendable=%.2f entry=%.2f",
                sym,
                qty,
                _affordable,
                _spendable,
                float(entry),
            )
            qty = _affordable
            _required = float(qty) * float(entry)
    if _required > _spendable:
        # The live account cannot fund even one share. Discarding the idea here loses
        # more than the trade: it also destroys the outcome that would have told us
        # whether the idea was any good, which is why 3 of this agent's 7 lifetime
        # proposals left no trace at all. Run it in PAPER instead, explicitly marked.
        #
        # One-directional by construction: this only ever demotes live -> paper. There
        # is no path back, and a signal already resolved to paper just blocks as before.
        _paper_stack = _pick_stack(shared, True)
        _paper_lifecycle = _paper_stack.get("lifecycle") if _paper_stack else None
        if not _is_paper and _paper_lifecycle is not None:
            _swing_log.warning(
                "swing_cash_fallback_to_paper sym=%s strategy=%s qty=%d required=%.2f "
                "spendable=%.2f (cash=%.2f committed=%.2f) — live account underfunded, "
                "routing to PAPER; EXCLUDED from live performance",
                sym,
                strategy_name,
                qty,
                _required,
                _spendable,
                _free_cash,
                _committed,
            )
            lifecycle = _paper_lifecycle
            order_monitor = _paper_stack.get("order_monitor") if _paper_stack else None
            _is_paper = True
            # Persisted onto the trade row: any live-performance query must filter this
            # out, or unfunded ideas silently inflate/deflate the real track record.
            crm_signal.meta["cash_forced_paper"] = True
            crm_signal.meta["cash_forced_paper_shortfall"] = round(_required - _spendable, 2)
        else:
            _swing_log.warning(
                "swing_insufficient_cash sym=%s qty=%d required=%.2f spendable=%.2f "
                "(cash=%.2f committed=%.2f cushion=%.0f%%) — not placed",
                sym,
                qty,
                _required,
                _spendable,
                _free_cash,
                _committed,
                _cash_cushion * 100.0,
            )
            return None, entry, sl, target, "insufficient_cash", _gates

    try:
        from src.execution.order_lifecycle import MonitoredPosition, OrderState

        result = await lifecycle.place_entry_protection_and_poll(
            signal_id=crm_signal.signal_id,
            symbol=sym,
            direction=direction,
            qty=qty,
            entry_price=entry,
            sl_price=sl,
            target_price=target,
            product="CNC",
            order_types=shared.get("order_types", {}),
            meta=crm_signal.meta,
            # Delivery: arm the SL/target GTTs only once the entry actually fills.
            defer_protection=True,
        )
        if result.state == OrderState.FILLED and result.fill_price and order_monitor is not None:
            order_monitor.track(
                MonitoredPosition(
                    agent_trade_id=result.agent_trade_id,
                    symbol=sym,
                    direction=direction,
                    quantity=int(result.filled_quantity or qty),
                    fill_price=float(result.fill_price),
                    initial_sl=float(result.sl_price),
                    current_sl=float(result.sl_price),
                    initial_target=float(result.target_price),
                    current_target=float(result.target_price),
                    sl_order_id=result.sl_order_id or "",
                    target_order_id=result.target_order_id or "",
                    # One OCO trigger protects both legs; carrying it here is what
                    # lets update_sl trail the real GTT and the 30s integrity check
                    # notice if it disappears.
                    gtt_sl_id=result.gtt_sl_id or "",
                    gtt_target_id=result.gtt_sl_id or "",
                    product="CNC",
                    strategy_name=strategy_name,
                )
            )
            # Seed a SmartExitTracker so the position's trailing_stop/breakeven/
            # partial_book rules (from the strategy JSON's exit_conditions) get
            # advanced once/day off the fresh daily candle — see the swing exit
            # step at the top of _swing_signal_scan_loop. Mirrors the intraday
            # wiring; uses POST-FILL values, not sig.*, same reasoning as intraday.
            if compiled is not None:
                from src.strategies.uss.compiler import SmartExitTracker as _SET

                shared.setdefault("swing_exit_state", {})[result.agent_trade_id] = {
                    "tracker": _SET(
                        compiled.spec,
                        float(result.fill_price),
                        float(result.sl_price),
                        float(result.target_price),
                        sig.direction,
                    ),
                    "tid": result.agent_trade_id,
                    "symbol": sym,
                }
        # The order is at the broker now, so its cash is committed whether or not it
        # has filled yet — an unfilled resting order still blocks those funds.
        shared["swing_cash_committed_today"] = _committed + _required
        if result.fill_price:
            return result.agent_trade_id, result.fill_price, sl, target, "executed", _gates
        # Unfilled. A swing entry is placed after the close as an AMO and legitimately
        # rests until the next open, so it is NOT cancelled on a timer the way an
        # intraday entry is. Register it so the monitor re-polls it every cycle and
        # picks up the fill whenever it happens — before this the row was simply
        # abandoned at SUBMITTED forever (JINDALSAW, 2026-08-27).
        _tpe = getattr(lifecycle, "track_pending_entry", None)
        if _tpe is not None:
            result.monitor_template = MonitoredPosition(
                agent_trade_id=result.agent_trade_id,
                symbol=sym,
                direction=direction,
                quantity=qty,
                fill_price=0.0,
                initial_sl=float(sl),
                current_sl=float(sl),
                initial_target=float(target),
                current_target=float(target),
                product="CNC",
                entry_ts=now.isoformat(),
                strategy_name=strategy_name,
            )
            _tpe(result.agent_trade_id)
            _swing_log.info(
                "swing_entry_resting sym=%s tid=%s — AMO, monitor will pick up the fill",
                sym,
                result.agent_trade_id,
            )
        return None, entry, sl, target, "not_filled", _gates
    except Exception as exc:
        _swing_log.error("swing_lifecycle_err sym=%s err=%s", sym, exc)
        _msg = str(exc).lower()
        if "amo" in _msg and "trading hours" in _msg:
            # Broker says its AMO window isn't open yet even though our clock
            # thinks it is — let the scan-loop caller retry instead of dropping
            # the signal. notify() is deferred to the caller's final give-up so
            # a mid-retry attempt doesn't page as a rejection.
            return None, entry, sl, target, "amo_window_not_open", _gates
        try:
            from src.notifications import EventKind, notify
            from src.notifications.events import Segment

            notify(
                EventKind.ENTRY_REJECTED,
                is_paper=getattr(lifecycle, "_is_paper", True),
                symbol=sym,
                segment=Segment.SWING,
                error=str(exc)[:200],
            )
        except Exception:  # notify() must never mask the original failure
            pass
        return None, entry, sl, target, "lifecycle_err", _gates


async def _fundamentals_fetch_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    """Once-daily pre-market fundamentals snapshot (src/market_data/fundamentals_fetcher.py,
    yfinance MVP source). Gated OFF by config/global.json `fundamentals.enabled`
    (default false) — when off, this loop only polls the flag and sleeps, never
    importing yfinance or making any network call. Writes fundamentals_snapshot
    (DB, for scripts/backtest_fundamentals_filter.py) and populates
    shared["fundamentals_cache"] (symbol -> latest row) for the log-only shadow
    attach in the main signal loop and for src/strategies/llm_swing/'s
    market context. Fundamentals don't move intraday, so this never re-runs
    same-day."""
    from datetime import datetime as _fdt
    from zoneinfo import ZoneInfo as _fZI

    _FIST = _fZI("Asia/Kolkata")
    _f_last_run_date = None
    # Shares market_data.log with src/market_data/fundamentals_fetcher.py's own
    # logger -- was flat "fundamentals_fetcher", orphaned from any _COMPONENTS handler.
    _flog = get_logger("src.market_data.fundamentals_fetcher")

    while not stop.is_set():
        fcfg = _cfg("global").get("fundamentals", {})
        if not fcfg.get("enabled", False):
            await asyncio.sleep(300.0)
            continue

        refresh_hour = int(fcfg.get("refresh_hour_ist", 8))
        now = _fdt.now(_FIST)
        today = now.date()
        is_weekday = today.weekday() < 5
        past_refresh_time = now.hour >= refresh_hour

        if not (is_weekday and past_refresh_time and _f_last_run_date != today):
            await asyncio.sleep(300.0)
            continue

        try:
            from src.market_data.fundamentals_fetcher import fetch_fundamentals_batch

            _trade_universe = _cfg("universe").get("trade_universe", "NIFTY500")
            universe = _resolve_index_universe(_trade_universe)
            rows = await fetch_fundamentals_batch(universe)

            fundamentals_repo = shared.get("fundamentals_repo")
            if fundamentals_repo is not None and rows:
                n = await fundamentals_repo.upsert_snapshot_batch(today, rows)
                _flog.info("fundamentals_snapshot_persisted n=%d", n)

            shared["fundamentals_cache"] = {row["symbol"]: row for row in rows}
            _flog.info("fundamentals_fetch_done ok=%d/%d", len(rows), len(universe))
            _f_last_run_date = today
            _f_problem = None
            if universe and len(rows) < 0.5 * len(universe):
                _f_problem = (
                    "CRITICAL" if not rows else "WARNING",
                    f"fundamentals fetch returned {len(rows)}/{len(universe)} symbols "
                    "(yfinance) — market-cap filter coverage is degraded",
                )
            _sync_scan_alert(shared, "fundamentals:fetch", _f_problem)
        except Exception as exc:
            _flog.error("fundamentals_fetch_err err=%s: %s", type(exc).__name__, exc)
            _sync_scan_alert(
                shared,
                "fundamentals:fetch",
                ("CRITICAL", f"fundamentals fetch failed: {type(exc).__name__}: {str(exc)[:120]}"),
            )

        await asyncio.sleep(300.0)


def _swing_regime_blocked(gcfg: dict[str, Any], regime: Any) -> bool:
    """True when config/global.json llm_swing.block_regimes lists the current market regime.

    Decided 2026-09-28 (user-approved): in STRONG_BEAR the swing agent buys nothing. It had been
    proposing longs every day in STRONG_BEAR (regime STRONG_BEAR on every recorded run since 8-Sep),
    and the prompt's own "reduced conviction" wording did not stop it. Enforced in code twice: the
    LLM is not even called (no tokens spent on ideas that can only be refused) and CRM G4 blocks any
    entry that still arrives. Exits and position management are never affected."""
    return str(regime) in {str(r) for r in (gcfg.get("block_regimes") or [])}


async def _recently_exited_symbols(pool: Any, days: int) -> set[str]:
    """Bare symbols of every position (live or paper, any strategy) closed in the last `days`
    days. Used as a re-entry cooldown: LAURUSLABS was exited for +Rs3,624 on 24 Sep and re-bought
    on 28 Sep because the only guard was 'currently held'."""
    if pool is None or days <= 0:
        return set()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT replace(symbol, 'NSE:', '') AS sym FROM (
                SELECT symbol, exit_timestamp FROM trades
                UNION ALL
                SELECT symbol, exit_timestamp FROM paper_trades
            ) t
            WHERE exit_timestamp IS NOT NULL AND exit_timestamp > now() - make_interval(days => $1)
            """,
            days,
        )
    return {r["sym"] for r in rows}


@strategy_logged(fixed="llm_swing_agent")
async def _llm_swing_scan_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    """LLM swing agent — once-daily pre-market reasoning pass (src/strategies/llm_swing/).

    Gated OFF entirely by the "llm_swing_agent" entry's `enabled: false`
    in config/strategies_registry.json — same on/off mechanism as every other
    strategy (a disabled entry is never loaded into strategy_registry). ALSO
    requires config/global.json llm_swing.enabled — both must be true.
    Every candidate the LLM proposes is submitted through the ordinary
    _execute_swing_signal CRM/cash/dedup/order-placement pipeline, and this
    strategy's registry `execution_mode: "paper"` forces every resulting order
    into paper mode regardless of the swing segment's global paper_mode — that
    field is a human-edited config lock, never written by this loop.
    """
    from datetime import datetime as _ddt
    from zoneinfo import ZoneInfo as _dZI

    _DIST = _dZI("Asia/Kolkata")
    # File-backed fallback ONLY, for when the DB is unreachable -- see below. This used
    # to be the primary guard, read once here before the loop, which is exactly why it
    # failed: written only after a fully successful pass (none completed on 2026-09-22,
    # then again on 2026-09-23 when every attempt died mid-flight), and never re-read
    # once the loop started, so a pass completing via another mechanism was invisible
    # to it. Kept as a last resort; the DB is the real answer now (has_run_today, below).
    _d_state_path = _LOGS / "llm_swing_scan_state.json"
    _d_last_run_date_fallback = None
    try:
        import json as _dstate_json

        _d_saved = _dstate_json.loads(_d_state_path.read_text(encoding="utf-8")).get(
            "last_run_date"
        )
        if _d_saved:
            _d_last_run_date_fallback = _ddt.fromisoformat(_d_saved).date()
    except Exception:  # noqa: BLE001 — no state file yet is the normal first-run case
        _d_last_run_date_fallback = None

    while not stop.is_set():
        strategy_reg = shared.get("strategy_registry")
        if strategy_reg is None or strategy_reg.get("llm_swing_agent") is None:
            await asyncio.sleep(300.0)
            continue

        gcfg = _cfg("global").get("llm_swing", {})
        if not gcfg.get("enabled", False):
            await asyncio.sleep(300.0)
            continue

        _scan_t = _sessions.schedule_time("llm_swing_morning_scan")
        scan_hour, scan_minute = _scan_t.hour, _scan_t.minute
        now = _ddt.now(_DIST)
        today = now.date()
        # weekday() alone let this run on NSE holidays; the intraday loop already
        # guards with is_trading_day().
        is_weekday = today.weekday() < 5 and is_trading_day(today)
        past_scan_time = (now.hour, now.minute) >= (scan_hour, scan_minute)

        # Already-ran-today check is asked of the DB fresh on EVERY tick (2026-09-23),
        # not read once from a file at loop start -- has_run_today() only counts a row
        # that actually got an LLM response (see repo docstring), so a killed/failed
        # attempt does not falsely mark the day done, and a successful pass from ANY
        # process is seen on the very next tick, restart or not.
        _journal_repo_for_guard = shared.get("llm_swing_journal_repo")
        if _journal_repo_for_guard is not None:
            try:
                _already_ran = await _journal_repo_for_guard.has_run_today(today)
            except Exception as _hrt_exc:  # noqa: BLE001 — DB hiccup: fall back, don't crash
                _swing_log.warning("llm_swing_has_run_today_err err=%s", str(_hrt_exc)[:120])
                _already_ran = _d_last_run_date_fallback == today
        else:
            _already_ran = _d_last_run_date_fallback == today

        if not (is_weekday and past_scan_time and not _already_ran):
            await asyncio.sleep(300.0)
            continue

        _regime_now = str(shared.get("regime", "UNKNOWN"))
        if _swing_regime_blocked(gcfg, _regime_now):
            # Nothing is booked (no llm_call_log row, no journal row): the day stays open and is
            # re-checked every 5 min, so the agent resumes by itself when the regime improves.
            _blk_key = (today.isoformat(), _regime_now)
            if shared.get("_swing_regime_block_logged") != _blk_key:
                shared["_swing_regime_block_logged"] = _blk_key
                from src.strategies.llm_intraday.funnel import funnel_log as _blk_log

                _blk_log(
                    _swing_log,
                    agent="llm_swing_agent",
                    stage="call_sent",
                    outcome="blocked",
                    reason=f"regime_{_regime_now}_blocked",
                    block_regimes=",".join(gcfg.get("block_regimes") or []),
                    note="no LLM call, no tokens spent; re-checked every 5 min",
                )
            await asyncio.sleep(300.0)
            continue

        # Hard daily cap on swing LLM calls SENT, from config/global.json
        # llm_swing.max_calls_per_day (2026-09-24). Counted from llm_call_log, which is
        # written BEFORE each call, so a call killed mid-flight still counts and a restart
        # cannot reset it (the old cap counted only journalled replies: the orchestrator
        # was killed ~23 times mid-call on 09-24 and every one looked like "no attempt").
        # Fail CLOSED: if the count cannot be read, do not call the LLM.
        _call_log_repo = shared.get("llm_call_log_repo")
        try:
            from src.llm.call_caps import swing_max_calls
            from src.llm.token_ledger import daily_limit as _swing_daily_limit

            if _call_log_repo is None:
                raise RuntimeError("llm_call_log_repo unavailable")
            _max_swing_calls = swing_max_calls(gcfg, _swing_daily_limit("swing"))
            _calls_today = await _call_log_repo.count_today("swing", today)
        except Exception as _cap_exc:  # noqa: BLE001
            _swing_log.error(
                "llm_swing_call_cap_unverifiable err=%s -- NOT calling the LLM", str(_cap_exc)[:120]
            )
            await asyncio.sleep(300.0)
            continue
        if _calls_today >= _max_swing_calls:
            _swing_log.warning(
                "llm_swing_call_cap_reached calls_today=%d cap=%d (llm_swing.max_calls_per_day) -- "
                "no further swing LLM calls today",
                _calls_today,
                _max_swing_calls,
            )
            await asyncio.sleep(300.0)
            continue

        try:
            from src.llm.providers import make_discretionary_llm_client
            from src.strategies.llm_swing.pipeline import (
                SwingLimits,
                build_market_context,
                mechanical_score,
                run_daily_reasoning,
            )
            from src.strategies.llm_swing.pipeline import (
                build_symbol_features as build_swing_features,
            )
            from src.strategies.uss.compiler import Signal
            from src.strategies.uss.schema import Direction

            ohlcv_repo = shared.get("ohlcv_repo")
            journal_repo = shared.get("llm_swing_journal_repo")
            if ohlcv_repo is None:
                _swing_log.warning("llm_swing_scan_skipped: ohlcv_repo unavailable")
                await asyncio.sleep(300.0)
                continue

            rs_top20: set[str] = shared.get("rs_top20", set())
            rs_blacklist: set[str] = shared.get("rs_blacklist", set())
            if not rs_top20:
                # RS ranking hasn't completed even once yet this process lifetime
                # (bug found 2026-09-08: on every restart today this loop's
                # wall-clock gate fired 14-21s after boot, well before
                # _rs_ranking_loop's first pass — every run saw an empty
                # candidate_universe and reported candidates=0 "no_trade_today"
                # regardless of the actual market, 3x in a row). Retry in 5 min
                # instead of committing to skipping the rest of the day.
                _swing_log.info("llm_swing_scan_waiting: rs_top20 not populated yet")
                await asyncio.sleep(300.0)
                continue

            # 250 bars, not 2: RSI/ATR/50-200DMA and 1m/3m strength vs NIFTY all need
            # history. The old 2-bar fetch is why the model only ever saw a last price.
            _nifty_close = None
            try:
                _nifty_df = await ohlcv_repo.fetch_dataframe("NIFTY 50", "1d", 250)
                if _nifty_df is not None and not _nifty_df.empty:
                    _nifty_close = _nifty_df["close"]
            except Exception as _nfe:  # noqa: BLE001 — no benchmark just drops the RS-vs-index
                _swing_log.debug("swing_nifty_fetch_failed err=%s", str(_nfe)[:100])
            _rs_records = shared.get("rs_records", {})
            last_close: dict[str, float] = {}
            swing_features: dict[str, dict[str, Any]] = {}
            from src.strategies.llm_swing.pipeline import exclude_recent_exits

            _cool_days = int(gcfg.get("reentry_cooldown_days", 5))
            try:
                _recent_exits = await _recently_exited_symbols(shared.get("db_pool"), _cool_days)
            except Exception as _rex:  # noqa: BLE001 -- fail CLOSED on the guard? no: log loudly, continue
                _swing_log.error("llm_swing_recent_exit_lookup_failed err=%s", str(_rex)[:120])
                _recent_exits = set()
            rs_top20, _cooled = exclude_recent_exits(set(rs_top20), _recent_exits)
            _swing_log.info(
                "llm_swing_menu candidates=%d excluded_recent_exit=%s cooldown_days=%d "
                "rule=leaders_not_exited_in_last_cooldown_days",
                len(rs_top20),
                _cooled or "-",
                _cool_days,
            )
            for sym in rs_top20:
                df = await ohlcv_repo.fetch_dataframe(sym, "1d", 250)
                if df is not None and not df.empty:
                    last_close[sym] = float(df["close"].iloc[-1])
                    swing_features[sym] = build_swing_features(
                        sym,
                        df,
                        nifty_close=_nifty_close,
                        rs=_rs_records.get(sym),
                        sector=_resolve_sector(sym),
                    )

            if not last_close:
                # rs_top20 was non-empty but OHLCV for those symbols isn't in the
                # DB yet (e.g. startup backfill still running) — same "retry, don't
                # commit to skipping today" reasoning as the rs_top20 check above.
                _swing_log.info("llm_swing_scan_waiting: no OHLCV for rs_top20 symbols yet")
                await asyncio.sleep(300.0)
                continue

            market_context = build_market_context(
                regime=str(shared.get("regime", "UNKNOWN")),
                vix=float(shared.get("vix", 15.0)),
                rs_top20=rs_top20,
                rs_blacklist=rs_blacklist,
                sector_status=shared.get("sector_status", {}),
                last_close=last_close,
                fundamentals=shared.get("fundamentals_cache") or None,
                features=swing_features,
            )

            # The gates the idea must clear, read from the SAME config the gates read so
            # the prompt cannot drift from enforcement, plus live spendable cash (CNC is
            # fully funded, no leverage — an unaffordable idea is a wasted idea).
            _op_hl = {}
            try:
                import json as _swing_json

                _op_hl = _swing_json.loads(
                    (_CONFIG / "operator.json").read_text(encoding="utf-8")
                ).get("hard_limits", {})
            except Exception as _ohe:  # noqa: BLE001 — fall back to SwingLimits defaults
                _swing_log.debug("swing_limits_config_read_failed err=%s", str(_ohe)[:100])
            _swing_limits = SwingLimits(
                min_reward_risk=float(_op_hl.get("min_risk_reward_ratio", 1.8)),
                max_stop_loss_pct=float(_op_hl.get("max_stop_loss_pct", 8.0)),
                max_entry_drift_pct=3.0,
                spendable_cash=float(shared.get("available_cash", 0.0) or 0.0),
                max_candidates=int(gcfg.get("max_candidates_per_day", 3)),
                max_proposals=int(gcfg.get("max_proposals", 8)),
            )

            recent_runs = (
                list(await journal_repo.recent_runs(int(gcfg.get("memory_lookback_days", 14))))
                if journal_repo
                else []
            )

            client = make_discretionary_llm_client(purpose="swing")
            if client is None:
                # Do NOT set _d_last_run_date here: a provider that is momentarily
                # unavailable used to cost the ENTIRE day's origination with no retry.
                # Retry on the next 5-min tick instead, same as the rs_top20 wait.
                _swing_log.warning(
                    "llm_swing_scan_skipped: no LLM provider available — retrying in 5 min"
                )
                await asyncio.sleep(300.0)
                continue

            # Recorded BEFORE the call so a kill mid-call still counts against the cap.
            await _call_log_repo.record("swing", today, "origination")
            from src.strategies.llm_intraday.funnel import funnel_log as _f_log_sent

            _f_log_sent(
                _swing_log,
                agent="llm_swing_agent",
                stage="call_sent",
                outcome="sent",
                reason="daily_origination_pass",
                call_n=_calls_today + 1,
                cap=_max_swing_calls,
                candidates_offered=len(last_close),
            )
            result = await run_daily_reasoning(
                client,
                market_context,
                recent_runs,
                last_close,
                memory_lookback_days=int(gcfg.get("memory_lookback_days", 14)),
                max_candidates_per_day=int(gcfg.get("max_candidates_per_day", 3)),
                limits=_swing_limits,
                timeout_secs=float(gcfg.get("llm_timeout_secs", 90)),
            )
            from src.strategies.llm_intraday.funnel import funnel_alert as _f_alert
            from src.strategies.llm_intraday.funnel import funnel_log as _f_log

            _f_log(
                _swing_log,
                agent="llm_swing_agent",
                stage="response",
                outcome="failed" if result.raw_llm_response is None else result.decision,
                reason="no_reply_from_any_provider" if result.raw_llm_response is None else "",
                level=40 if result.raw_llm_response is None else 20,
                model=result.llm_model_used,
                candidates=len(result.candidates),
            )
            if result.raw_llm_response is None:
                # Every provider in the chain (plus retries) failed to return
                # ANY text — distinct from a genuine 0-candidate decision, which
                # always has raw_llm_response populated. Push it, don't just log
                # it: this is exactly the class of failure that sat silent in
                # trading.log for days before anyone noticed (2026-09-15/16).
                try:
                    from src.notifications import EventKind, notify
                    from src.notifications.events import Segment

                    notify(
                        EventKind.STRATEGY_ERROR,
                        is_paper=True,
                        segment=Segment.SWING,
                        strategy="llm_swing_agent",
                    )
                except Exception:  # notify() must never break the scan loop
                    pass

            executed_candidates: list[dict[str, Any]] = []
            any_traded = False
            for cand in result.candidates:
                full_sym = f"NSE:{cand.symbol}"
                sig = Signal(
                    "llm_swing_agent",
                    Direction.LONG,
                    cand.entry_price,
                    cand.stop_loss,
                    cand.target,
                    confidence=cand.confidence,
                )
                _risk = cand.entry_price - cand.stop_loss
                _rr = (cand.target - cand.entry_price) / _risk if _risk > 0 else 0.0
                _f_log(
                    _swing_log,
                    agent="llm_swing_agent",
                    stage="idea_published",
                    outcome="proposed",
                    symbol=cand.symbol,
                    reason="ranked_within_cap",
                    entry=f"{cand.entry_price:.2f}",
                    sl=f"{cand.stop_loss:.2f}",
                    target=f"{cand.target:.2f}",
                    rr=f"{_rr:.2f}",
                    conf=f"{cand.confidence:.2f}",
                )
                _f_alert(
                    shared,
                    day=today.isoformat(),
                    agent="llm_swing_agent",
                    stage="idea_published",
                    outcome="proposed",
                    symbol=cand.symbol,
                    reason="ranked_within_cap",
                    detail=(
                        f"LONG entry={cand.entry_price:.2f} sl={cand.stop_loss:.2f} "
                        f"target={cand.target:.2f} rr={_rr:.2f} conf={cand.confidence:.2f}. "
                        f"Why: {cand.reasoning[:140]}"
                    ),
                )
                (
                    agent_trade_id,
                    fill_price,
                    sl_price,
                    target_price,
                    reason,
                    _gates,
                ) = await _execute_swing_signal(
                    shared, "llm_swing_agent", full_sym, sig, now, compiled=None
                )
                _blk = next((g for g in _gates if g.get("decision") == "BLOCK"), None)
                _blk_txt = f"{_blk['gate_id']}: {_blk['reason']}" if _blk else "-"
                _f_log(
                    _swing_log,
                    agent="llm_swing_agent",
                    stage="execution",
                    outcome="placed"
                    if agent_trade_id is not None
                    else ("resting" if reason == "not_filled" else "refused"),
                    symbol=cand.symbol,
                    reason=str(reason),
                    blocked_by=_blk_txt,
                    tid=agent_trade_id or "-",
                )
                _f_alert(
                    shared,
                    day=today.isoformat(),
                    agent="llm_swing_agent",
                    stage="execution",
                    outcome="placed"
                    if agent_trade_id is not None
                    else ("resting" if reason == "not_filled" else "refused"),
                    symbol=cand.symbol,
                    reason=str(reason),
                    detail=(
                        f"trade {agent_trade_id}"
                        if agent_trade_id is not None
                        else f"blocked_by {_blk_txt}"
                    ),
                )
                _swing_log.info(
                    "llm_swing_signal symbol=%s outcome=%s entry=%.2f sl=%.2f "
                    "target=%.2f rr=%.2f blocked_by=%s thesis=%s",
                    cand.symbol,
                    reason,
                    cand.entry_price,
                    cand.stop_loss,
                    cand.target,
                    _rr,
                    f"{_blk['gate_id']}: {_blk['reason']}" if _blk else "-",
                    cand.reasoning,
                )
                # mechanical_score is an arithmetic cross-check the model never sees
                # (R:R + relative-strength + RSI heuristic) -- previously only reached
                # a DEBUG log line and was discarded. Persisted here so it can later be
                # compared against the model's own confidence and against outcomes.
                # Observe only: does not affect ranking, execution, or any gate.
                _mech_score = mechanical_score(
                    cand, (market_context.get("_features") or {}).get(cand.symbol)
                )
                executed_candidates.append(
                    {
                        "symbol": cand.symbol,
                        "direction": "BUY",
                        "entry_price": cand.entry_price,
                        "stop_loss": cand.stop_loss,
                        "target": cand.target,
                        "confidence": cand.confidence,
                        "mechanical_score": _mech_score,
                        "reasoning": cand.reasoning,
                        "agent_trade_id": agent_trade_id,
                        "outcome_reason": reason,
                    }
                )
                if agent_trade_id is not None:
                    any_traded = True

            # A total provider-chain failure (no candidates ever considered) used to be
            # journalled as "no_trade_today" -- indistinguishable from a genuine decision
            # to stand aside, and it silently latched the day-done file (before A2/A2b)
            # even though the model never actually looked at anything. "llm_failed" keeps
            # it out of has_run_today() so the day is correctly retried, bounded, rather
            # than either looping forever (the old bug) or being mistaken for a real
            # "nothing to do today" verdict.
            final_decision = (
                "llm_failed"
                if result.raw_llm_response is None
                else "traded"
                if any_traded
                else ("all_blocked" if result.candidates else "no_trade_today")
            )
            _session_window = (
                "market_hours"
                if _sessions.regular_open() <= now.time() <= _sessions.regular_close()
                else "after_hours"
            )
            if journal_repo is not None:
                try:
                    await journal_repo.upsert_run(
                        run_date=today,
                        market_context_summary=market_context,
                        thesis_text=result.thesis_text,
                        candidates_json=executed_candidates,
                        decision=final_decision,
                        llm_model_used=result.llm_model_used,
                        raw_llm_response=result.raw_llm_response,
                        session_window=_session_window,
                    )
                except Exception as exc:
                    _swing_log.error("llm_swing_journal_write_failed err=%s", exc)

            _swing_log.info(
                "llm_swing_scan_result candidates=%d decision=%s model=%s session=%s",
                len(result.candidates),
                final_decision,
                result.llm_model_used,
                _session_window,
            )
            if final_decision != "llm_failed":
                # Mirrors has_run_today(): a failed attempt must NOT latch the day,
                # even in the fallback file, or the bounded-retry check above becomes
                # unreachable the moment the DB is briefly unavailable.
                _d_last_run_date_fallback = today
                # Survive a restart, so the day's pass is not re-run (and re-billed) on
                # every bounce. Best-effort: failing to persist must not fail the pass.
                # Fallback path only now -- has_run_today() above is the primary guard.
                try:
                    import json as _dstate_w

                    _tmp = _d_state_path.with_suffix(".tmp")
                    _tmp.write_text(
                        _dstate_w.dumps({"last_run_date": today.isoformat()}), encoding="utf-8"
                    )
                    _tmp.replace(_d_state_path)
                except Exception as _dpe:  # noqa: BLE001
                    _swing_log.debug("llm_swing_state_persist_failed err=%s", str(_dpe)[:100])
        except Exception as exc:
            # Previously logged only -- the day stayed unlatched (no journal row at
            # all), so the pass retried every 5 minutes until midnight with nobody
            # told. Same alert path as the "zero response" case above: an exception
            # here is the identical operator-facing problem, just raised instead of
            # returned. 2026-09-23: this is the path that actually fired 8 times today
            # (the LLM call itself never got a chance to raise -- the PROCESS was
            # killed by the launcher's hang watchdog before this except could even
            # run; A1 fixes that half, this fixes what happens for every other
            # exception shape that reaches here).
            _swing_log.error("llm_swing_scan_err err=%s", exc)
            try:
                from src.notifications import EventKind, notify
                from src.notifications.events import Segment

                notify(
                    EventKind.STRATEGY_ERROR,
                    is_paper=True,
                    segment=Segment.SWING,
                    strategy="llm_swing_agent",
                )
            except Exception:  # notify() must never break the scan loop
                pass
            # Re-fetched from shared rather than relying on the try block's own
            # `journal_repo` local, which may never have been assigned if the
            # exception was raised before that line ran.
            _err_journal_repo = shared.get("llm_swing_journal_repo")
            if _err_journal_repo is not None:
                try:
                    await _err_journal_repo.upsert_run(
                        run_date=today,
                        market_context_summary={},
                        thesis_text=None,
                        candidates_json=[],
                        decision="llm_failed",
                        llm_model_used=None,
                        raw_llm_response=f"<exception: {type(exc).__name__}: {str(exc)[:300]}>",
                        session_window=(
                            "market_hours"
                            if _sessions.regular_open() <= now.time() <= _sessions.regular_close()
                            else "after_hours"
                        ),
                    )
                except Exception as _jexc:  # noqa: BLE001 — never let logging break the loop
                    _swing_log.error("llm_swing_journal_write_failed err=%s", _jexc)

        await asyncio.sleep(300.0)


async def _ensure_intraday_adv(shared: dict[str, Any], now: Any) -> None:
    """Load the {symbol: avg daily traded value, Rs crore} map once per day (from daily bars)."""
    day = now.date().isoformat()
    cur = shared.get("intraday_adv") or {}
    if cur.get("day") == day and cur.get("map"):
        return
    repo = shared.get("ohlcv_repo")
    if repo is None:
        return
    try:
        shared["intraday_adv"] = {"day": day, "map": await repo.adv_crores(20)}
        _intraday_log.info(
            "llm_intraday_liquidity_map_loaded symbols=%d", len(shared["intraday_adv"]["map"])
        )
    except Exception as exc:  # noqa: BLE001
        _intraday_log.error("llm_intraday_liquidity_map_failed err=%s -- menu will be EMPTY", exc)
        shared["intraday_adv"] = {"day": day, "map": {}}


def _intraday_menu(
    shared: dict[str, Any],
    features: dict[str, dict[str, Any]],
    cfg: dict[str, Any],
    tcfg: dict[str, Any],
    now: Any,
    must_show: Any = (),
) -> tuple[list[str], dict[str, Any]]:
    """Choose which stocks the LLM is shown: liquid, not shown in the last re_offer_minutes,
    best activity first (pipeline.select_menu). Marks the chosen ones as shown so the next pass
    cannot repeat them, and returns counts for the 'how were these chosen' log line.

    `must_show` = the symbols whose fresh signal FIRED this call. They lead the menu and are exempt
    from the shown-recently rule: the tick marks them offered the moment it fires, so without this
    the stock that triggered the call was excluded from its own menu (2026-09-28 14:14: call
    triggered by UNOMINDA, menu of ten other stocks, answer PASS -- a wasted call)."""
    from src.strategies.llm_intraday.pipeline import TriggerState, select_menu

    adv = (shared.get("intraday_adv") or {}).get("map") or {}
    state: TriggerState = shared.setdefault("llm_intraday_trigger_state", TriggerState())
    min_adv = float(cfg.get("min_adv_crores", 50))
    re_offer_s = float(tcfg.get("re_offer_minutes", 60)) * 60.0
    now_ts = now.timestamp()
    # Symbols _intraday_refusal() marked persistent=True today (currently only
    # counter_trend_short_blocked -- a stock's own 50/200-DMA position cannot change
    # within a session, so re-offering it only burns another idea slot on a trade that
    # will be refused again at execution; 2026-10-08: JYOTICNC proposed SHORT 3x, refused
    # 7x). Symmetric by construction: whichever direction a future persistent block
    # applies to (today that is SHORT-only -- counter-trend LONGs are deliberately only
    # sized down, not refused, see src/risk/trend_filter.py), it lands in the same set and
    # is excluded here the same way. Dropped from the menu entirely, not just the blocked
    # direction -- simpler than teaching select_menu a per-direction exclusion, at the
    # small cost of also losing a same-day look at the other direction on that one name.
    blocked_today = (shared.get("_intraday_blocked_today") or {}).get(now.date().isoformat(), set())
    features = {s: f for s, f in features.items() if s not in blocked_today}
    priced = {s: f for s, f in features.items() if float(f.get("ltp", 0.0)) >= 100.0}
    lead = [s for s in dict.fromkeys(must_show) if s in priced]
    illiquid = sum(1 for s in priced if min_adv > 0 and float(adv.get(s, 0.0)) < min_adv)
    recent = sum(
        1
        for s in priced
        if (min_adv <= 0 or float(adv.get(s, 0.0)) >= min_adv)
        and now_ts - float(state.offered.get(s, 0.0)) < re_offer_s
    )
    menu = select_menu(
        features,
        n=int(cfg.get("universe_size", 10)),
        min_price=100.0,
        adv_cr=adv,
        min_adv_cr=min_adv,
        offered={k: v for k, v in state.offered.items() if k not in lead},
        now_ts=now_ts,
        re_offer_s=re_offer_s,
    )
    menu = (lead + [s for s in menu if s not in lead])[
        : max(int(cfg.get("universe_size", 10)), len(lead))
    ]
    for s in menu:
        state.offered[s] = now_ts
    return menu, {
        "considered": len(features),
        "excluded_price": len(features) - len(priced),
        "excluded_blocked_today": len(blocked_today),
        "excluded_illiquid": illiquid,
        "excluded_recent": recent,
    }


async def _reseed_llm_offered(shared: dict[str, Any], now: Any) -> None:
    """After a restart the in-memory 'already shown' map is empty, so the same stocks would be
    shown again. Rebuild it once per day from today's journalled menus."""
    from src.strategies.llm_intraday.pipeline import TriggerState

    day = now.date().isoformat()
    if shared.get("_llm_offered_reseeded_day") == day:
        return
    shared["_llm_offered_reseeded_day"] = day
    repo = shared.get("llm_intraday_journal_repo")
    if repo is None:
        return
    state: TriggerState = shared.setdefault("llm_intraday_trigger_state", TriggerState())
    try:
        for row in await repo.todays_runs(now.date()):
            ctx = row["market_context"]
            ctx = json.loads(ctx) if isinstance(ctx, str) else ctx
            ts = row["run_ts"].timestamp()
            for s in (ctx or {}).get("candidate_universe", []):
                sym = s.get("symbol")
                if sym:
                    state.offered[sym] = max(state.offered.get(sym, 0.0), ts)
    except Exception as exc:  # noqa: BLE001
        _intraday_log.warning("llm_intraday_offered_reseed_failed err=%s", str(exc)[:120])


@strategy_logged(fixed="llm_intraday_agent")
async def _run_llm_intraday_pass(
    shared: dict[str, Any],
    cfg: dict[str, Any],
    now: Any,
    slot: str,
    log: Any,
    must_show: Any = (),
    before_call: Any = None,
) -> None:
    """One llm_intraday reasoning pass: snapshot → LLM → validated ideas published
    to shared["llm_intraday_ideas"] (bare symbol → idea dict) + a journal row."""
    import time as _t
    from dataclasses import asdict

    from src.llm.providers import make_discretionary_llm_client
    from src.strategies.llm_intraday.pipeline import (
        ValidationLimits,
        build_market_context,
        build_symbol_features,
        minutes_to_flat,
        run_intraday_reasoning,
    )

    ohlcv_repo = shared.get("ohlcv_repo")
    journal_repo = shared.get("llm_intraday_journal_repo")
    if ohlcv_repo is None:
        log.warning("llm_intraday_scan_skipped slot=%s reason=ohlcv_repo_unavailable", slot)
        _llm_idea_funnel(
            shared,
            now,
            "response",
            "failed",
            "",
            "ohlcv_repo_unavailable",
            "pass skipped, no LLM call made",
        )
        return

    # Sequential, not an unbounded gather — a 400-way DB fan-out starves the event
    # loop (heartbeat) exactly like the dashboard incident of 2026-09-10.
    traded_today = shared.get("intraday_traded", {}).get(now.date().isoformat(), set())
    features: dict[str, dict[str, Any]] = {}
    _fetch_errors: list[str] = []
    for sym in list(shared.get("watchlist", [])):
        bare = sym.split(":")[-1]
        if sym in traded_today or bare in traded_today:
            continue
        try:
            df = await ohlcv_repo.fetch_dataframe(sym, "5m", 100)
        except Exception as _fe:
            _fetch_errors.append(f"{bare}:{str(_fe)[:60]}")
            continue
        feat = build_symbol_features(df, now) if df is not None and not df.empty else None
        if feat is not None:
            features[bare] = feat
    if _fetch_errors:
        log.warning(
            "llm_intraday_snapshot_fetch_errors slot=%s count=%d first=%s",
            slot,
            len(_fetch_errors),
            _fetch_errors[:3],
        )
    if not features:
        log.info("llm_intraday_scan_skipped slot=%s reason=no_intraday_data", slot)
        _llm_idea_funnel(
            shared,
            now,
            "response",
            "failed",
            "",
            "no_intraday_data",
            "pass skipped, no LLM call made",
        )
        return

    await _ensure_intraday_adv(shared, now)
    universe, _menu_stats = _intraday_menu(
        shared, features, cfg, dict(cfg.get("trigger", {})), now, must_show=must_show
    )
    from src.strategies.llm_intraday.funnel import funnel_log as _menu_log

    _menu_log(
        _intraday_log,
        agent="llm_intraday_agent",
        stage="menu",
        outcome="selected" if universe else "empty",
        reason="liquid_not_recently_shown_top_activity",
        shown=",".join(universe) or "-",
        **_menu_stats,
        rule="price>=100, avg daily value>="
        + str(cfg.get("min_adv_crores", 50))
        + "cr, not shown in last "
        + str(dict(cfg.get("trigger", {})).get("re_offer_minutes", 60))
        + "min, ranked by |chg%|xvolume",
    )
    if not universe:
        log.info("llm_intraday_scan_skipped slot=%s reason=nothing_new_to_show", slot)
        return
    uni_feats = {s: features[s] for s in universe}
    from src.strategies.llm_intraday.pipeline import add_daily_trend, add_relative_strength

    add_relative_strength(uni_feats, shared.get("index_day_change_pct"))
    await add_daily_trend(uni_feats, shared.get("ohlcv_repo"))
    # RSI is recorded with each idea (for validation) but withheld from the prompt.
    _rsi_by_sym = {sym: f.pop("rsi14", None) for sym, f in uni_feats.items()}
    news: dict[str, str] = {}
    for s, n in (shared.get("news_recent") or {}).items():
        if s in uni_feats and _t.time() - float(n.get("ts", 0)) < 7200:
            news[s] = f"{n.get('sentiment')}/{n.get('magnitude')}: {n.get('headline', '')}"[:160]
    context = build_market_context(
        nifty_chg_pct=shared.get("index_day_change_pct"),
        intraday_regime=str(shared.get("intraday_regime", "UNKNOWN")),
        daily_regime=str(shared.get("regime", "UNKNOWN")),
        bearish_pct=shared.get("_bearish_pct"),
        vix=float(shared.get("vix", 15.0)),
        sector_status=shared.get("sector_status", {}),
        features=uni_feats,
        universe=universe,
        news=news,
        minutes_to_flat=minutes_to_flat(now),
    )

    client = make_discretionary_llm_client(purpose="intraday")
    if client is None:
        log.warning("llm_intraday_scan_skipped slot=%s reason=no_llm_provider", slot)
        _llm_idea_funnel(
            shared,
            now,
            "response",
            "failed",
            "",
            "no_llm_provider",
            "pass skipped, no LLM call made",
        )
        return
    recent = (
        list(await journal_repo.recent_outcomes(int(cfg.get("memory_trades", 20))))
        if journal_repo
        else []
    )
    todays = list(await journal_repo.todays_runs(now.date())) if journal_repo else []
    if before_call is not None and not await before_call():
        return  # the call could not be committed (e.g. cap row unwritable): do NOT call the LLM
    result = await run_intraday_reasoning(
        client,
        context,
        recent,
        todays,
        uni_feats,
        ValidationLimits.from_cfg(cfg),
        max_ideas=int(cfg.get("max_ideas_per_llm_call", cfg.get("max_ideas_per_pass", 2))),
        timeout_secs=float(cfg.get("llm_timeout_secs", 30)),
        minutes_left=minutes_to_flat(now),
    )
    if result.decision in ("llm_failed", "unusable_reply") or result.raw_llm_response is None:
        # Our failure, not a market judgement: every provider in the chain (plus
        # retries) failed to return usable text. Distinct from a genuine 0-idea
        # decision. Keyed on `decision` now that the failure paths persist whatever
        # text they did get -- the `is None` check remains only as a backstop.
        # Push it, don't just log it: this exact slot silently returning nothing for
        # days is what prompted this hook
        # (2026-09-15/16 — see llm_intraday_scan_result / _response_malformed).
        try:
            from src.notifications import EventKind, notify
            from src.notifications.events import Segment

            notify(
                EventKind.STRATEGY_ERROR,
                is_paper=True,
                segment=Segment.INTRADAY,
                strategy="llm_intraday_agent",
                slot=slot,
            )
        except Exception:  # notify() must never break the reasoning pass
            pass

    from src.strategies.llm_intraday.funnel import funnel_log as _funnel_log

    _funnel_log(
        _intraday_log,
        agent="llm_intraday_agent",
        stage="response",
        outcome="failed" if result.raw_llm_response is None else result.decision,
        reason="no_reply_from_any_provider" if result.raw_llm_response is None else "",
        level=40 if result.raw_llm_response is None else 20,
        slot=slot,
        model=result.llm_model_used,
        ideas=len(result.ideas),
        rejected=len(result.rejected_ideas),
        universe=len(universe),
        thesis=repr((result.thesis_text or "")[:240]),  # WHY: the model's own one-line reasoning
    )
    store = shared.setdefault("llm_intraday_ideas", {})
    published: list[dict[str, Any]] = []
    for idea in result.ideas:
        rec = {
            **asdict(idea),
            "atr14_pct": uni_feats[idea.symbol].get("atr14_pct"),
            # Recorded with every idea so RSI / relative strength can be validated against the
            # eventual outcome (they are context for the model, not a code filter).
            "rsi14": _rsi_by_sym.get(idea.symbol),
            "rs_vs_nifty_pct": uni_feats[idea.symbol].get("rs_vs_nifty_pct"),
            "ts": _t.time(),
            "slot": slot,
        }
        store[idea.symbol] = rec
        published.append(rec)
        log.info(
            "llm_intraday_idea slot=%s symbol=%s dir=%s entry=%.2f sl=%.2f target=%.2f "
            "conf=%.2f reason=%s",
            slot,
            idea.symbol,
            idea.direction,
            idea.entry_price,
            idea.stop_loss,
            idea.target,
            idea.confidence,
            idea.reasoning[:120],
        )
        _llm_idea_funnel(
            shared,
            now,
            "idea_published",
            "proposed",
            idea.symbol,
            "validated",
            f"{idea.direction} entry={idea.entry_price:.2f} sl={idea.stop_loss:.2f} "
            f"target={idea.target:.2f} conf={idea.confidence:.2f} "
            f"rsi14={_rsi_by_sym.get(idea.symbol)} "
            f"rs_vs_nifty={uni_feats[idea.symbol].get('rs_vs_nifty_pct')}. Waiting for price to be within "
            f"{float(cfg.get('entry_tolerance_pct', 0.4)):.2f}% of entry at the next 5-min scan "
            f"(valid {int(cfg.get('idea_ttl_minutes', 15))} min). Why: {idea.reasoning[:140]}",
        )

    if journal_repo is not None:
        try:
            await journal_repo.insert_run(
                run_ts=now,
                market_context=context,
                thesis_text=result.thesis_text,
                ideas_json=published,
                decision=result.decision,
                llm_model_used=result.llm_model_used,
                raw_llm_response=result.raw_llm_response,
                rejected_json=result.rejected_ideas,
            )
        except Exception as exc:
            log.error("llm_intraday_journal_write_failed err=%s", exc)
    log.info(
        "llm_intraday_scan_result slot=%s scanned=%d universe=%d ideas=%d rejected=%d "
        "decision=%s model=%s shown=%s thesis=%r",
        slot,
        len(features),
        len(universe),
        len(result.ideas),
        len(result.rejected_ideas),
        result.decision,
        result.llm_model_used,
        ",".join(universe),
        (result.thesis_text or "")[:240],
    )
    for _rej in result.rejected_ideas:
        # One line per killed idea at INFO: these are trades the agent wanted to take,
        # and their reasons are the fastest read on whether the limits are miscalibrated
        # rather than the model being wrong.
        log.info(
            "llm_intraday_idea_rejected slot=%s symbol=%s dir=%s reason=%s detail=%s",
            slot,
            _rej.get("symbol"),
            _rej.get("direction"),
            _rej.get("reason"),
            {
                k: v
                for k, v in _rej.items()
                if k not in ("symbol", "direction", "reason", "entry_price", "stop_loss", "target")
            },
        )
        _llm_idea_funnel(
            shared,
            now,
            "validation",
            "rejected",
            str(_rej.get("symbol") or ""),
            str(_rej.get("reason") or ""),
            f"{_rej.get('direction')} rejected by validation limits: "
            + ", ".join(
                f"{k}={v}"
                for k, v in _rej.items()
                if k not in ("symbol", "direction", "reason", "entry_price", "stop_loss", "target")
            ),
        )


def _g3_entry_window_open(now: Any) -> bool:
    """Would CRM gate G3 (time-of-day) allow an intraday entry right now? Asks the SAME
    TimeFilter G3 uses, with the strategy_type G3 actually sees for these signals (nothing sets
    signal.meta["strategy_type"], so it defaults to "SWING"). Fails OPEN on error: a config
    problem must not silently stop the agent -- G3 still enforces at execution."""
    try:
        from src.risk.time_filter import TimeFilter

        return bool(TimeFilter().check(now, "SWING")[0])
    except Exception as exc:  # noqa: BLE001
        _intraday_log.warning("g3_window_check_failed err=%s -- assuming open", str(exc)[:100])
        return True


def _log_llm_ideas_waiting(shared: dict[str, Any], now: Any) -> None:
    """Every 30s: for each LLM idea that has NOT yet been price-checked, say exactly what
    it is waiting for. Ideas are only checked inside the 5-minute scan, whose data refresh
    takes ~3 minutes, so a fresh idea can sit unchecked for minutes -- previously with no
    log line at all (2026-09-24: AXISBANK proposed 13:48:58, first checked 13:53:04)."""
    ideas = shared.get("llm_intraday_ideas") or {}
    timing = shared.get("intraday_scan_timing") or {}
    for sym, idea in list(ideas.items()):
        if idea.get("_checked_at"):
            continue
        age = now.timestamp() - float(idea.get("ts", 0))
        done = timing.get("done_ts")
        last = f"{now.timestamp() - done:.0f}s ago" if done else "not yet since boot"
        _llm_idea_funnel(
            shared,
            now,
            "entry_check",
            "waiting",
            sym,
            "awaiting_scan",
            f"idea is {age:.0f}s old and NOT yet price-checked: ideas are checked inside "
            f"the 5-min scan, which spends ~{timing.get('cycle_s', 180):.0f}s refreshing "
            f"market data before it reaches the symbol loop (last scan finished {last}). "
            "Next check = when the running scan reaches this symbol.",
        )


def _liquid_scores(
    shared: dict[str, Any], cfg: dict[str, Any], tcfg: dict[str, Any]
) -> dict[str, tuple[float, float]]:
    """The setup scores the trigger may fire on: liquid stocks only, so a call is never triggered
    by (and then wasted on) a thin name the menu would refuse to show."""
    scores = dict(shared.get("intraday_setup_scores") or {})
    min_adv = float(cfg.get("min_adv_crores", 50))
    if min_adv <= 0:
        return scores
    adv = (shared.get("intraday_adv") or {}).get("map") or {}
    return {s: v for s, v in scores.items() if float(adv.get(s, 0.0)) >= min_adv}


@strategy_logged(fixed="llm_intraday_agent")
async def _llm_intraday_signal_tick(
    shared: dict[str, Any], cfg: dict[str, Any], now: Any, log: Any
) -> None:
    """One evaluation of llm_intraday's SIGNAL trigger (config llm_intraday.trigger).

    Instead of fixed clock slots, ask the LLM for ideas when the 5-min scan has seen a
    fresh high-score setup (|chg%| x vol_ratio20 >= min_setup_score), with a baseline pass
    at schedules.llm_intraday_first_pass. Pure decision in pipeline.evaluate_trigger; this
    wires state, the token budget and the pass itself."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from src.llm.token_ledger import LEDGER
    from src.strategies.llm_intraday.pipeline import TriggerState, evaluate_trigger

    _log_llm_ideas_waiting(shared, now)
    await _ensure_intraday_adv(shared, now)
    await _reseed_llm_offered(shared, now)
    tcfg = dict(cfg.get("trigger", {}))
    state: TriggerState = shared.setdefault("llm_intraday_trigger_state", TriggerState())
    if state.day != now.date().isoformat():
        state.day, state.last_pass_ts, state.passes_today, state.offered = (
            now.date().isoformat(),
            None,
            0,
            {},
        )
    # Rebuild the day's pass counter / last-pass time from llm_call_log every tick
    # (2026-09-24): passes_today was in-memory only, so each of the ~30 restarts that day
    # reset it to 0 and re-fired the baseline pass, and max_passes_per_day was exceeded
    # (13 passes on 09-22 and 09-23 vs a cap of 8). Fail CLOSED if the DB can't be read.
    _call_log_repo = shared.get("llm_call_log_repo")
    try:
        from src.llm.call_caps import sync_intraday_state

        if _call_log_repo is None:
            raise RuntimeError("llm_call_log_repo unavailable")
        _day = now.date()
        sync_intraday_state(
            state,
            await _call_log_repo.count_today("intraday", _day),
            await _call_log_repo.last_call_ts("intraday", _day),
        )
    except Exception as _cap_exc:  # noqa: BLE001
        log.error(
            "llm_intraday_call_cap_unverifiable err=%s -- NOT calling the LLM", str(_cap_exc)[:120]
        )
        return
    if now.timestamp() < float(shared.get("_llm_intraday_retry_after", 0.0)):
        return  # the last pass sent nothing; wait the short retry delay, not the min-gap
    ttl_min = int(cfg.get("idea_ttl_minutes", 15))
    last_entry = _sessions.last_entry_by()
    last_pass = (_dt.combine(now.date(), last_entry) - _td(minutes=ttl_min)).time()
    fire, reason, fresh = evaluate_trigger(
        state,
        tcfg,
        now,
        first_pass=_sessions.schedule_time("llm_intraday_first_pass"),
        last_pass=last_pass,
        scores=_liquid_scores(shared, cfg, tcfg),
        traded_today=set(shared.get("intraday_traded", {}).get(now.date().isoformat(), set())),
        trades_today=int(shared.get("llm_intraday_traded", {}).get(now.date().isoformat(), 0)),
        max_trades=int(cfg.get("max_trades_per_day", 2)),
        tokens_left=await LEDGER.remaining("intraday"),
        entry_window_open=_g3_entry_window_open(now),
    )
    if not fire:
        return
    # The call is BOOKED only at the moment the pass is about to dispatch it (inside the pass, via
    # `_commit_call`): llm_call_log row written BEFORE the call so a kill mid-call still counts against
    # the cap, then the pass counter and the min-gap timer. A pass that never reaches the LLM (no
    # data yet after a restart, empty menu, no provider) books NOTHING -- 2026-09-28: the baseline
    # pass was skipped for `no_intraday_data` at 10:11 yet still counted as a pass, which started a
    # 30-minute min_gap and cost the morning's first real call.
    _committed = {"v": False}

    async def _commit_call() -> bool:
        try:
            await _call_log_repo.record("intraday", now.date(), reason)
        except Exception as _rec_exc:  # noqa: BLE001 — cannot prove the call is counted: do not make it
            log.error(
                "llm_intraday_call_record_failed err=%s -- NOT calling the LLM", str(_rec_exc)[:120]
            )
            return False
        state.passes_today += 1
        state.last_pass_ts = now.timestamp()
        _committed["v"] = True
        from src.strategies.llm_intraday.funnel import funnel_log as _funnel_log

        _funnel_log(
            log,
            agent="llm_intraday_agent",
            stage="call_sent",
            outcome="sent",
            reason=reason,
            call_n=state.passes_today,
            cap=int(tcfg.get("max_passes_per_day", 5)),
            fresh_symbols=",".join(fresh[:5]) or "-",
        )
        for sym in fresh:
            state.offered[sym] = now.timestamp()
        log.info(
            "llm_intraday_trigger reason=%s pass=%d fresh=%d top=%s",
            reason,
            state.passes_today,
            len(fresh),
            ",".join(fresh[:5]),
        )
        return True

    await _run_llm_intraday_pass(
        shared,
        cfg,
        now,
        f"{reason}-{now:%H%M}",
        log,
        must_show=fresh,
        before_call=_commit_call,
    )
    if not _committed["v"]:
        # Nothing was sent, so nothing was consumed. Look again shortly instead of every 30 s
        # (the pass re-reads bars/menu each time) -- and never wait out the 30-min min_gap.
        shared["_llm_intraday_retry_after"] = now.timestamp() + float(
            tcfg.get("retry_after_skipped_seconds", 120)
        )
        log.info(
            "llm_intraday_pass_not_called reason=%s -- no call sent, no slot consumed; "
            "re-evaluating in %ss",
            reason,
            int(tcfg.get("retry_after_skipped_seconds", 120)),
        )


async def _llm_health_loop(stop: asyncio.Event) -> None:
    """Morning / evening / startup LLM provider + gate-chain probes (src/llm/model_health.py)."""
    from src.llm.model_health import llm_health_loop

    await llm_health_loop(stop)


@strategy_logged(fixed="llm_intraday_agent")
async def _llm_intraday_agent_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    """LLM intraday agent — signal-driven intraday reasoning passes (src/strategies/llm_intraday/).

    Gated by the "llm_intraday_agent" registry entry (strategy_registry.get() is
    not None) AND config/global.json llm_intraday.enabled. At each
    llm_intraday.trigger event on a trading day it runs one pass
    (_run_llm_intraday_pass). It never places orders: the 5-minute intraday scan
    turns a fresh idea into a Signal via _evaluate_llm_intraday_signal, so every
    trade goes through ranked placement, CRM G0-G10 and the fail-closed
    llm.intraday_g9 review, and the registry's execution_mode "paper" forces it
    into paper.

    When a pass fires is decided by _llm_intraday_signal_tick (setup-score trigger with a
    baseline pass, gap / cap / token guards); its per-day state resets each day and a restart
    only costs the baseline check, so a stale pass is never replayed."""
    from datetime import datetime as _ddt
    from zoneinfo import ZoneInfo as _dZI

    _tz = _dZI("Asia/Kolkata")
    # Shares intraday.log with src/strategies/llm_intraday/pipeline.py's own
    # "intraday" logger and the rest of the intraday engine's orchestrator-side
    # decisions (2026-09-23) -- was the dotted "src.strategies.llm_intraday.pipeline"
    # form, which nested under strategies.log instead, split from the engine's own
    # orchestrator.log decisions. Before that it was flat "llm_intraday", orphaned
    # from any _COMPONENTS handler entirely.
    _llog = get_logger("intraday")

    while not stop.is_set():
        try:
            strategy_reg = shared.get("strategy_registry")
            cfg = _cfg("global").get("llm_intraday", {})
            now = _ddt.now(_tz)
            if (
                strategy_reg is not None
                and strategy_reg.get("llm_intraday_agent") is not None
                and cfg.get("enabled", False)
                and is_trading_day(now.date())
            ):
                await _llm_intraday_signal_tick(shared, cfg, now, _llog)
        except Exception as exc:
            _llog.error("llm_intraday_scan_err err=%s", exc)
        await asyncio.sleep(30.0)


class _CausalInsightRepo:
    """Adapts LearningRepository to CausalMemory's InsightRepository protocol.

    Module-level (not nested inside _learning_loop, as it originally was) so the
    G9 signal-build path can also retrieve real, regime-conditional insights at
    the point a trade is actually being evaluated — not just the nightly writer.
    """

    def __init__(self, lr: Any) -> None:
        self._lr = lr

    async def save(self, insight: Any) -> None:
        _db_tid = int(insight.trade_id) if str(insight.trade_id).isdigit() else None
        if _db_tid is None:
            return
        await self._lr.insert_insight(
            trade_id=_db_tid,
            was_signal_valid=insight.was_signal_valid,
            failure_was_environmental=insight.failure_was_environmental,
            regime_at_trade=insight.regime_at_trade,
            what_i_would_do_differently=insight.what_i_would_do_differently or None,
            generalizable_insight=insight.generalizable_insight or None,
            confidence_in_insight=insight.confidence_in_insight,
            applicable_when=insight.applicable_when or None,
            sector_at_trade=insight.sector_at_trade or None,
            llm_model_used=None,
            raw_llm_response=None,
        )

    async def find_similar(self, strategy: str, regime: str, k: int) -> list[Any]:
        """Regime-conditional retrieval — fixed 2026-09-17. Previously this
        ignored regime entirely (learning_repo.insights_for_regime existed but
        was never called), returning the strategy's k most recent insights
        regardless of whether they happened in a comparable regime. Fetches a
        larger strategy-recency pool, ranks regime-matches first, falls back to
        strategy-only recency for any remaining slots so a thin regime sample
        still returns k results rather than fewer.
        """
        from src.learning.causal_memory import TradeInsight

        try:
            rows = list(await self._lr.recent_insights(strategy=strategy, limit=k * 3))
            regime_rows = [r for r in rows if (r.get("regime_at_trade") or "") == regime]
            other_rows = [r for r in rows if r not in regime_rows]
            selected = (regime_rows + other_rows)[:k]
            return [
                TradeInsight(
                    trade_id=str(r.get("trade_id", "")),
                    was_signal_valid=bool(r.get("was_signal_valid", True)),
                    failure_was_environmental=bool(r.get("failure_was_environmental", False)),
                    what_i_would_do_differently=r.get("what_i_would_do_differently") or "",
                    generalizable_insight=r.get("generalizable_insight") or "",
                    confidence_in_insight=float(r.get("confidence_in_insight") or 0.5),
                    applicable_when=r.get("applicable_when") or "",
                    regime_at_trade=r.get("regime_at_trade") or regime,
                    sector_at_trade=r.get("sector_at_trade") or "",
                    strategy=strategy,
                )
                for r in selected
            ]
        except Exception:
            return []


async def _learning_loop(
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Per closed trade: autopsy + causal memory (L7) + attribution + pattern.
    Nightly 21:00 writes logs/learning_summary.json (live) +
    logs/learning_summary_paper.json.

    Tracks paper and live COMPLETELY SEPARATELY — own autopsy/attribution/
    pattern-detector engine instances per mode, own dedup set, own summary
    file — previously this fetched ALL closed trades (mode="all") into one
    shared set of engines, so the dashboard's Win/Loss Attribution and
    Detected Patterns panels blended simulated paper outcomes with real
    live ones for any strategy that has run in both modes over its history.
    """
    import json as _json
    from dataclasses import asdict
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    from src.learning.attribution import AttributionEngine
    from src.learning.causal_memory import CausalMemory
    from src.learning.pattern_detector import PatternDetector
    from src.learning.trade_autopsy import TradeAutopsyEngine

    _IST = _ZI("Asia/Kolkata")
    _learn_log = _LOGS / "learning_summary.json"
    _learn_log_paper = _LOGS / "learning_summary_paper.json"
    _engines: dict[str, dict[str, Any]] = {
        m: {
            "autopsy": TradeAutopsyEngine(),
            "attr": AttributionEngine(),
            "pattern": PatternDetector(),
        }
        for m in ("live", "paper")
    }
    _seen_ids: dict[str, set[str]] = {"live": set(), "paper": set()}
    _last_summary_date: dict[str, Any] = {"live": None, "paper": None}

    # LLM client for Layer 7 causal analysis — GROQ → OLLAMA only (never Gemini),
    # so nightly causal calls don't compete with the live G9 gate's Gemini quota.
    from src.llm.providers import make_background_llm_client as _make_bg_llm

    _llm: Any = _make_bg_llm() or _NullLLM()

    while not stop.is_set():
        _lr = shared.get("learning_repo")
        _repo: Any = _CausalInsightRepo(_lr) if _lr is not None else None
        _causal = CausalMemory(_llm, _repo) if _repo is not None else None

        for _fetch_mode, _eng in _engines.items():
            autopsy_engine, attr_engine, pattern_det = (
                _eng["autopsy"],
                _eng["attr"],
                _eng["pattern"],
            )
            for t in await _fetch_closed_trades_from_db(shared.get("db_pool"), mode=_fetch_mode):
                tid = str(t.get("agent_trade_id", ""))
                if not tid or tid in _seen_ids[_fetch_mode]:
                    continue
                _seen_ids[_fetch_mode].add(tid)
                try:
                    strategy = str(t.get("strategy_name", "unknown"))
                    regime = str(t.get("regime_at_entry", shared.get("regime", "UNKNOWN")))
                    pnl_r = float(t.get("pnl_r", 0.0))
                    autopsy = autopsy_engine.analyse(
                        trade_id=tid,
                        strategy=strategy,
                        pnl_r=pnl_r,
                        regime_at_entry=regime,
                    )
                    attr_engine.add(autopsy)
                    pattern_det.add(autopsy)

                    # Layer 7: causal analysis — gated by config/learning.json
                    # causal_memory.mode (off by default). Left off by default: running
                    # this for the full DB-sourced trade backlog means one real LLM call
                    # per historical trade the first time this loop sees them, and the
                    # 2026-08-20 LLM-backfill failure this flag originally guarded
                    # against must be re-verified fixed before enabling for real (see
                    # the learning-engine wiring plan's PR6). Runs for both modes —
                    # storage has no FK tying trade_id to live-only (migration 059)
                    # — but retrieval (_CausalInsightRepo.find_similar, via
                    # learning_repo.recent_insights' JOIN against `trades`) only
                    # ever surfaces LIVE-derived insights, so a paper insight is
                    # stored but never fed into a live G9 prompt.
                    if _causal is not None and _learning_mode("causal_memory") in (
                        "shadow",
                        "live",
                    ):
                        try:
                            # trade_insights.trade_id is the numeric paper_trades/trades PK,
                            # not agent_trade_id — _LRInsightRepo.save() silently no-ops on a
                            # non-digit trade_id, so this MUST be db_id, not tid.
                            insight = await _causal.analyse_and_store(
                                trade_id=str(t.get("db_id", "")),
                                strategy=strategy,
                                pnl_r=pnl_r,
                                regime=regime,
                                sector=str(t.get("sector", "")),
                                entry_reasoning=str(t.get("entry_reasoning", "")),
                            )
                            # Pre-cache generalizable insight for G9 CRM context
                            # (legacy cache — the real retrieval path is the
                            # signal-build-time CausalMemory.retrieve() call).
                            _ctx_key = f"{strategy}|{regime}"
                            _ctx = shared.setdefault("causal_context", {})
                            _ctx.setdefault(_ctx_key, []).append(insight.generalizable_insight)
                            _ctx[_ctx_key] = _ctx[_ctx_key][-10:]  # keep last 10
                        except Exception as _ce:
                            _log.warning(
                                "learning_loop_causal_error mode=%s tid=%s err=%s",
                                _fetch_mode,
                                tid,
                                _ce,
                            )
                except Exception as exc:
                    _log.warning(
                        "learning_loop_autopsy_error mode=%s tid=%s err=%s", _fetch_mode, tid, exc
                    )

            now = _dt.now(_IST)
            today = now.date()
            if _last_summary_date[_fetch_mode] != today and now.hour >= 21:
                try:
                    summaries: dict[str, Any] = {}
                    for strat in list(attr_engine._autopsies.keys()):
                        s = attr_engine.summary(strat)
                        summaries[strat] = asdict(s)
                    patterns = [asdict(p) for p in pattern_det.detect()]
                    payload: dict[str, Any] = {
                        "summaries": summaries,
                        "patterns": patterns,
                        "trade_count": len(_seen_ids[_fetch_mode]),
                        "computed_at": now.isoformat(),
                    }
                    _log_path = _learn_log if _fetch_mode == "live" else _learn_log_paper
                    tmp = _log_path.with_suffix(".tmp")
                    tmp.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
                    tmp.replace(_log_path)
                    _last_summary_date[_fetch_mode] = today
                    _log.info(
                        "learning_summary_written mode=%s strategies=%d",
                        _fetch_mode,
                        len(summaries),
                    )
                except Exception as exc:
                    _log.warning("learning_loop_summary_error mode=%s err=%s", _fetch_mode, exc)
        await asyncio.sleep(300.0)


async def _gap_risk_loop(
    broker: Any,
    order_monitor: Any,
    total_capital: float,
    holidays: set[Any],
    shared: dict[str, Any],
    gap_config: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Evaluate gap risk for open CNC positions daily at 14:30–15:00 IST."""
    from datetime import date as _date
    from zoneinfo import ZoneInfo as _ZI

    from src.risk.gap_risk import OpenPosition, evaluate_gap_risk

    _IST = _ZI("Asia/Kolkata")
    _gap_log = _LOGS / "gap_risk.json"
    _last_check_date: _date | None = None

    while not stop.is_set():
        now = __import__("datetime").datetime.now(_IST)
        today = now.date()
        # Run once per day inside schedules.gap_risk_check (config/market_hours.json)
        _gap_start, _gap_end = _sessions.schedule_window("gap_risk_check")
        if today != _last_check_date and _sessions.minutes(
            _gap_start
        ) <= now.hour * 60 + now.minute < _sessions.minutes(_gap_end):
            try:
                await broker.get_positions()
                symbols = [p.symbol for p in order_monitor.get_monitored()]
                ltp_map = await broker.get_ltp(symbols) if symbols else {}
                open_positions: list[OpenPosition] = []
                earnings_map: dict[str, int] = shared.get("earnings", {})
                ohlcv_repo = shared.get("ohlcv_repo")
                for pos in order_monitor.get_monitored():
                    ltp = float(ltp_map.get(pos.symbol, pos.fill_price))
                    clean_sym = pos.symbol.split(":")[-1]
                    hist_gap: float | None = None
                    if ohlcv_repo is not None:
                        try:
                            hist_gap = await ohlcv_repo.max_gap_pct(clean_sym, years=2)
                        except Exception:
                            pass
                    open_positions.append(
                        OpenPosition(
                            symbol=pos.symbol,
                            quantity=pos.quantity,
                            avg_price=pos.fill_price,
                            current_ltp=ltp,
                            product=pos.product or "CNC",
                            stop_loss_price=pos.current_sl,
                            earnings_within_days=earnings_map.get(clean_sym),
                            max_historical_gap_pct=hist_gap,
                        )
                    )
                decisions = evaluate_gap_risk(
                    open_positions,
                    total_capital,
                    holidays=holidays,
                    gap_config=gap_config,
                )
                for _d in decisions:
                    if _d.action not in ("CLOSE", "REDUCE"):
                        continue
                    for _pos in order_monitor.get_monitored():
                        if _pos.symbol != _d.symbol:
                            continue
                        _gltp = float(ltp_map.get(_pos.symbol, _pos.fill_price))
                        try:
                            if _d.action == "CLOSE" or _pos.quantity <= 0:
                                await order_monitor.force_exit(
                                    _pos.agent_trade_id, _gltp, "GAP_RISK_CLOSE"
                                )
                            else:
                                # REDUCE means trim to suggested_quantity, not a full
                                # exit — book_partial(fraction) sells the fraction of
                                # the CURRENT qty that brings it down to that target.
                                _frac = 1.0 - (
                                    min(_d.suggested_quantity, _pos.quantity) / _pos.quantity
                                )
                                if _frac > 0:
                                    await order_monitor.book_partial(
                                        _pos.agent_trade_id, _frac, _gltp
                                    )
                            _log.warning(
                                "gap_risk_exit symbol=%s action=%s suggested_qty=%d",
                                _d.symbol,
                                _d.action,
                                _d.suggested_quantity,
                            )
                        except Exception as _exc:
                            _log.error("gap_risk_exit_failed symbol=%s err=%s", _d.symbol, _exc)
                        break
                results = [
                    {
                        "symbol": d.symbol,
                        "action": d.action,
                        "reason": d.reason,
                        "gap_risk_pct": round(d.gap_risk_pct, 4),
                    }
                    for d in decisions
                ]
                _gap_log.write_text(__import__("json").dumps(results, indent=2), encoding="utf-8")
                shared["gap_risk"] = results
                _last_check_date = today
                _log.info(
                    "gap_risk_evaluated positions=%d decisions=%d",
                    len(open_positions),
                    len(decisions),
                )
            except Exception as exc:
                _log.warning("gap_risk_loop_error err=%s", exc)
        await asyncio.sleep(60.0)


async def _correlation_loop(
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Detect correlation crisis from OHLCV data every 30 min."""
    import json as _json

    from src.risk.correlation_detector import detect_correlation_crisis

    _corr_log = _LOGS / "correlation.json"

    while not stop.is_set():
        try:
            ohlcv_dir = _BASE / "data" / "ohlcv" / "day"
            returns: dict[str, list[float]] = {}
            if ohlcv_dir.exists():
                import pandas as _pd

                for pq in list(ohlcv_dir.glob("*.parquet"))[:30]:
                    try:
                        df = _pd.read_parquet(pq)
                        if "close" in df.columns and len(df) >= 10:
                            closes = df["close"].astype(float).tolist()
                            rets = [
                                (closes[i] - closes[i - 1]) / closes[i - 1]
                                for i in range(1, len(closes))
                            ]
                            returns[pq.stem] = rets[-60:]
                    except Exception:
                        continue
            status = detect_correlation_crisis(returns)
            _corr_log.write_text(
                _json.dumps(
                    {
                        "level": status.level,
                        "avg": round(status.avg_correlation, 3),
                        "max": round(status.max_pair_correlation, 3),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            shared["correlation_level"] = status.level
            if status.level in ("HIGH", "EXTREME"):
                _log.warning(
                    "correlation_crisis level=%s avg=%.2f", status.level, status.avg_correlation
                )
        except Exception as exc:
            _log.warning("correlation_loop_error err=%s", exc)
        await asyncio.sleep(1800.0)  # every 30 min


async def _monitoring_loop_25(
    shared: dict[str, Any],
    risk_cfg: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Phase 2.5 monitoring: cost erosion + edge decay from accumulated trade data.

    Sources closed trades from the DB (_fetch_closed_trades_from_db), not
    shared['closed_trades'] — that in-memory list only ever holds trades closed
    during the current process's runtime, so frequent orchestrator restarts kept
    this stuck at "Insufficient data" (found 2026-08-21: DB had 370 closed trades,
    cache showed trade_count=2) even though real cost/edge data was sitting in
    the DB the whole time. Same fix pattern as _attribution_loop/_expectancy_loop.
    """
    import json as _json

    from src.monitoring.cost_erosion import TradeWithCosts, check_cost_erosion
    from src.monitoring.edge_decay import TradeResult, audit_edge_decay

    _max_cost_pct = (
        float(risk_cfg.get("cost_tracking", {}).get("max_cost_pct_of_gross", 35)) / 100.0
    )
    # risk.json cost_tracking.auto_raise_quality_on_overtrade — YELLOW raises the CRM
    # quality bar by this amount; RED doubles it. Drives shared["quality_raise"].
    _quality_step = float(
        risk_cfg.get("cost_tracking", {}).get("auto_raise_quality_on_overtrade", 5)
    )

    _erosion_log = _LOGS / "cost_erosion.json"
    _decay_log = _LOGS / "edge_decay.json"

    while not stop.is_set():
        try:
            # live-only: this loop's output (quality_raise) gates NEW LIVE entries —
            # a paper strategy's simulated costs/losses must never raise or lower
            # that bar for real trading. See _fetch_closed_trades_from_db docstring.
            raw_trades = await _fetch_closed_trades_from_db(shared.get("db_pool"), mode="live")
            if raw_trades:
                # Only rows with a recorded cost (charges_taxes) can compute a real
                # cost_ratio — treating a missing cost as zero would understate erosion.
                cost_trades = [
                    TradeWithCosts(
                        gross_pnl=float(t["gross_pnl"]),
                        total_costs=float(t["total_costs"]),
                        risk_rupees=float(t["risk_rupees"] or 1),
                    )
                    for t in raw_trades
                    if t.get("total_costs") is not None
                ]
                erosion = check_cost_erosion(cost_trades, max_cost_pct_of_gross=_max_cost_pct)
                _erosion_log.write_text(
                    _json.dumps(
                        {
                            "level": erosion.level,
                            "cost_ratio": round(erosion.cost_ratio, 4),
                            "trade_count": erosion.trade_count,
                            "reason": erosion.reason,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

                # Cost erosion → raise CRM quality bar via shared state
                # (GREEN=0, YELLOW=step, RED=2×step), step from cost_tracking config.
                if erosion.level == "RED":
                    shared["quality_raise"] = _quality_step * 2.0
                elif erosion.level == "YELLOW":
                    shared["quality_raise"] = _quality_step
                else:
                    shared["quality_raise"] = 0.0

                decay_trades = [TradeResult(pnl_r=float(t.get("pnl_r", 0))) for t in raw_trades]
                decay_reports: dict[str, Any] = {}
                by_strategy: dict[str, list[TradeResult]] = {}
                for t, dr in zip(raw_trades, decay_trades, strict=False):
                    by_strategy.setdefault(str(t.get("strategy_name", "unknown")), []).append(dr)
                for strategy, strades in by_strategy.items():
                    if len(strades) >= 20:
                        rpt = audit_edge_decay(
                            strategy, strades, recent_window=20, baseline_window=40
                        )
                        decay_reports[strategy] = {"verdict": rpt.verdict, "reason": rpt.reason}
                if decay_reports:
                    _decay_log.write_text(_json.dumps(decay_reports, indent=2), encoding="utf-8")
            else:
                shared["quality_raise"] = 0.0
        except Exception as exc:
            _log.warning("monitoring_25_error err=%s", exc)
        await asyncio.sleep(3600.0)  # hourly


async def _weekly_pnl_loop(
    shared: dict[str, Any],
    halt_after: int,
    stop: asyncio.Event,
) -> None:
    """
    Aggregate weekly net P&L from closed_trades; count consecutive negative weeks.
    Sets shared['halt_trading_weeks'] = True when count >= halt_after_negative_weeks.
    Writes logs/weekly_pnl.json hourly.
    """
    from datetime import datetime as _dt

    _weekly_log = _LOGS / "weekly_pnl.json"

    while not stop.is_set():
        try:
            trades = shared.get("closed_trades", [])
            weekly: dict[str, float] = {}
            for t in trades:
                ts = str(t.get("exit_ts", ""))[:10]
                try:
                    d = _dt.fromisoformat(ts).isocalendar()
                    key = f"{d.year}-W{d.week:02d}"
                    weekly[key] = weekly.get(key, 0.0) + float(t.get("net_pnl", 0.0))
                except Exception:
                    continue

            consecutive_negative = 0
            for wk in sorted(weekly.keys(), reverse=True):
                if weekly[wk] < 0:
                    consecutive_negative += 1
                else:
                    break

            should_halt = consecutive_negative >= halt_after
            shared["halt_trading_weeks"] = should_halt
            shared["consecutive_negative_weeks"] = consecutive_negative
            _weekly_log.write_text(
                json.dumps(
                    {
                        "weeks": weekly,
                        "consecutive_negative": consecutive_negative,
                        "halt_threshold": halt_after,
                        "halted": should_halt,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            if should_halt:
                _log.critical(
                    "halt_after_negative_weeks consecutive=%d >= threshold=%d",
                    consecutive_negative,
                    halt_after,
                )
        except Exception as exc:
            _log.warning("weekly_pnl_loop_error err=%s", exc)
        await asyncio.sleep(3600.0)


async def _sanity_watchdog_loop(broker: Any, shared: dict[str, Any], stop: asyncio.Event) -> None:
    """Every 30 min: LTP-replay pending LLM decisions, evaluate watchdog, set bypass flag."""
    import time as _time

    from src.llm.sanity_watchdog import SignalOutcome, evaluate_llm_sanity

    while not stop.is_set():
        await asyncio.sleep(1800.0)
        try:
            pending = shared.get("llm_pending", [])
            now_ns = _time.time_ns()
            mature = [p for p in pending if now_ns - p["ts_ns"] >= 1800 * 1_000_000_000]
            if not mature:
                continue
            symbols = list({p["symbol"] for p in mature})
            ltp_map = await broker.get_ltp(symbols) if symbols else {}
            outcomes: list[SignalOutcome] = []
            for p in mature:
                ltp = float(ltp_map.get(p["symbol"], p["entry_price"]))
                moved_up = ltp > p["entry_price"]
                would_win = moved_up if p["direction"] == "BUY" else not moved_up
                outcomes.append(
                    SignalOutcome(was_approved=p["was_approved"], would_have_won=would_win)
                )
            report = evaluate_llm_sanity(outcomes)
            newly_bypassed = report.bypass_llm and not shared.get("bypass_llm", False)
            if newly_bypassed:
                shared["_dsl_only_trade_count"] = 0  # reset counter when bypass starts

            shared["bypass_llm"] = bool(report.bypass_llm)
            shared["llm_pending"] = [p for p in pending if p not in mature][-50:]
            _log.info("sanity_watchdog verdict=%s bypass=%s", report.verdict, report.bypass_llm)

            # Recovery: count DSL-only trades while bypassed; re-enable gate after 20
            if shared.get("bypass_llm"):
                closed = shared.get("closed_trades", [])
                shared["_dsl_only_trade_count"] = len(closed)
                from src.llm.sanity_watchdog import LLMRecoveryState, should_resume_llm

                if should_resume_llm(LLMRecoveryState(dsl_only_trades=len(closed))):
                    shared["bypass_llm"] = False
                    shared["_dsl_only_trade_count"] = 0
                    _log.info("llm_gate_resumed dsl_only_trades=%d", len(closed))

            (_LOGS / "llm_sanity.json").write_text(
                __import__("json").dumps(
                    {
                        "bypass_llm": bool(shared.get("bypass_llm", False)),
                        "pending_signals": len(shared["llm_pending"]),
                        "dsl_only_trades": shared.get("_dsl_only_trade_count", 0),
                    }
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            _log.warning("sanity_watchdog_error err=%s", exc)


async def _equity_loop(
    broker: Any, shared: dict[str, Any], alert_manager: Any, stop: asyncio.Event
) -> None:
    """Fetch broker margins every 60 s, reconcile vs DB equity, update shared state."""
    from src.risk.capital_truth import CapitalSnapshot, ReconcileStatus, reconcile_capital

    while not stop.is_set():
        try:
            margin = await broker.get_margins()
            broker_equity = float(margin.equity)
            snap = CapitalSnapshot(
                broker_equity=broker_equity,
                broker_available=float(margin.available_cash),
                broker_used_margin=float(margin.used_margin),
            )
            rec = reconcile_capital(snap, float(shared.get("current_equity", broker_equity)))
            if rec.status == ReconcileStatus.CRITICAL:
                shared["capital_critical"] = True
                _log.critical("capital_critical %s", rec.reason)
                if alert_manager is not None:
                    from src.infrastructure.alert_system import AlertTier

                    alert_manager.raise_alert(
                        AlertTier.CRITICAL, rec.reason, source="capital_truth"
                    )
                # Raise DB incident so the supervisor log has a record
                _sup_repo = shared.get("supervisor_repo")
                if _sup_repo is not None:
                    try:
                        await _sup_repo.open_incident("CAPITAL_DRIFT", rec.reason)
                    except Exception as _inc_exc:
                        _log.warning("capital_drift_incident_write_failed err=%s", _inc_exc)
            else:
                shared["capital_critical"] = False
                if rec.status == ReconcileStatus.RECONCILE:
                    _log.warning("capital_reconcile %s", rec.reason)
                    _sup_repo = shared.get("supervisor_repo")
                    if _sup_repo is not None:
                        try:
                            await _sup_repo.open_incident("CAPITAL_DRIFT", rec.reason)
                        except Exception as _inc_exc:
                            _log.warning("capital_drift_incident_write_failed err=%s", _inc_exc)
            shared["current_equity"] = broker_equity
            shared["available_cash"] = float(margin.available_cash)
            # Keep config/capital.json's shared risk pool (src/risk/budget.py) in
            # lockstep with CRM's own equity figure -- every RiskBudget-derived
            # rupee limit is a % of THIS value.
            from src.risk.budget import set_current_equity as _set_budget_equity

            _set_budget_equity(broker_equity)
        except Exception as exc:
            _log.warning("equity_fetch_failed err=%s — keeping last known", exc)
        await asyncio.sleep(60.0)


_EQUITY_FETCH_TIMEOUT_S = 10.0  # same bound as the swing delivery-cash margin read


async def _fetch_live_stack_equity(shared: dict[str, Any], key: str, broker: Any) -> bool:
    """Per-broker risk (system.per_broker_equity): one live stack's equity from ITS broker.

    Success: shared["equity_by_key"][key] / ["cash_by_key"][key] are refreshed, the first
    broker's baseline (["equity_baseline_by_key"]: the 25% floor and the drawdown staircase
    measure from it) is loaded once from the DB, or stored there from this fetch when the
    broker has none (_persisted_equity_baseline; restarts never reset it), and
    src.risk.budget gets the per-key equity. Failure (raise, timeout, equity <= 0): the
    key is REMOVED from both maps -- CRM G1 then blocks new entries for that broker until
    a fetch succeeds (fail safe) -- and a CRITICAL operator alert is raised."""
    from src.risk.budget import clear_current_equity, set_current_equity

    eq_map = shared.setdefault("equity_by_key", {})
    cash_map = shared.setdefault("cash_by_key", {})
    base_map = shared.setdefault("equity_baseline_by_key", {})
    try:
        margin = await asyncio.wait_for(broker.get_margins(), timeout=_EQUITY_FETCH_TIMEOUT_S)
        equity = float(margin.equity)
        cash = float(margin.available_cash)
        if not equity > 0:
            raise ValueError(f"non-positive equity {equity}")
    except Exception as exc:  # noqa: BLE001 -- fail safe: no equity => no entries
        eq_map.pop(key, None)
        cash_map.pop(key, None)
        clear_current_equity(key)
        _log.critical("broker_equity_fetch_failed key=%s err=%s -- new entries blocked", key, exc)
        try:
            from src.infrastructure import operator_alerts as _oa

            _oa.raise_alert(
                f"Equity fetch failed for live broker {stack_broker(key) or key}: NO new "
                f"entries on it until a fetch succeeds ({str(exc)[:120]})",
                source=f"broker_equity_fetch_failed:{key}",
                tier="CRITICAL",
                dedupe_seconds=3600,
            )
        except Exception as _ae:  # noqa: BLE001 -- the CRITICAL log already fired
            _log.error("broker_equity_alert_failed key=%s err=%s", key, _ae)
        return False
    eq_map[key] = equity
    cash_map[key] = cash
    if key not in base_map:
        baseline = await _persisted_equity_baseline(shared.get("db_pool"), key, equity)
        if baseline is not None:
            base_map[key] = baseline
    set_current_equity(equity, key=key)
    return True


async def _persisted_equity_baseline(pool: Any, key: str, equity: float) -> float | None:
    """The broker's drawdown baseline (migration 092): the stored row, else `equity` stored
    now as the first-ever baseline. A restart therefore never resets a broker's drawdown;
    only an operator does (delete/update the row, restart). No DB pool: the in-process
    first fetch (no persistence -- logged). DB error: None -- the key stays without a
    baseline, CRM G1 blocks that broker's entries and the next fetch retries (fail safe)."""
    if pool is None:
        _log.warning("broker_equity_baseline_not_persisted key=%s -- no DB pool", key)
        return equity
    try:
        stored = await pool.fetchval(
            "WITH ins AS (INSERT INTO broker_equity_baseline (stack_key, baseline_equity) "
            "VALUES ($1, $2) ON CONFLICT (stack_key) DO NOTHING RETURNING baseline_equity) "
            "SELECT baseline_equity FROM ins UNION ALL "
            "SELECT baseline_equity FROM broker_equity_baseline WHERE stack_key = $1 LIMIT 1",
            key,
            round(equity, 2),
        )
        if stored is None or not float(stored) > 0:
            raise ValueError(f"no usable stored baseline ({stored!r})")
        return float(stored)
    except Exception as exc:  # noqa: BLE001 -- fail safe: no baseline => no entries
        _log.critical(
            "broker_equity_baseline_unavailable key=%s err=%s -- new entries blocked", key, exc
        )
        try:
            from src.infrastructure import operator_alerts as _oa

            _oa.raise_alert(
                f"Drawdown baseline unavailable for live broker {stack_broker(key) or key}: NO "
                f"new entries on it until it loads ({str(exc)[:120]})",
                source=f"broker_equity_baseline_unavailable:{key}",
                tier="CRITICAL",
                dedupe_seconds=3600,
            )
        except Exception as _ae:  # noqa: BLE001 -- the CRITICAL log already fired
            _log.error("broker_equity_baseline_alert_failed key=%s err=%s", key, _ae)
        return None


def _broker_funds(shared: Mapping[str, Any], key: str) -> tuple[float, float] | None:
    """(equity, available_cash) of live stack `key` from the last successful per-broker
    fetch, or None when either is unknown (callers fail closed)."""
    eq = (shared.get("equity_by_key") or {}).get(key)
    cash = (shared.get("cash_by_key") or {}).get(key)
    if eq is None or cash is None:
        return None
    return float(eq), float(cash)


async def _per_broker_equity_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    """Every 60 s (the primary _equity_loop's cadence), each live stack's own equity."""
    while not stop.is_set():
        await asyncio.sleep(60.0)
        for _key, _stack in live_stacks(shared.get("stacks") or {}).items():
            await _fetch_live_stack_equity(shared, _key, _stack["broker"])


def _live_key_for(shared: Mapping[str, Any], strategy_name: str) -> str:
    """Stack key of the live broker a LIVE-resolved strategy routes to (R1, legacy broker
    when unrouted) -- the per-broker risk key CRM's mode_of() yields for it."""
    broker, _reason = _resolve_broker(shared, strategy_name, False)
    return stack_key(False, broker)


def _reconcile_targets(
    shared: Mapping[str, Any], stacks: Mapping[str, Any], fallback_broker: Any
) -> list[tuple[str | None, Any, Any]]:
    """[(stack key, broker, order_monitor)] to reconcile `trades` against: every live stack,
    the primary (legacy broker's) first. With no live stack: [(None, fallback_broker, None)],
    exactly the legacy single-broker behaviour (unfiltered rows, no monitor)."""
    live = live_stacks(stacks)
    if not live:
        return [(None, fallback_broker, None)]
    primary = primary_live_key(stacks, shared.get("legacy_live_broker"))
    keys = [primary] + [k for k in live if k != primary] if primary else list(live)
    return [(k, live[k]["broker"], live[k].get("order_monitor")) for k in keys]


def _reconciler_for(key: str | None, broker: Any, pool: Any) -> Any:
    """A Reconciler restricted to the `trades` rows `key`'s broker holds (exec_broker; NULL and
    unregistered values belong to kite, as for the reseed). None/bare "live" = unfiltered."""
    from src.supervisor.reconciler import Reconciler

    name = stack_broker(key) if key else None
    known = _known_live_brokers() if name == LEGACY_LIVE_BROKER else None
    return Reconciler(broker, pool, broker_name=name, known_brokers=known)


async def _reconcile_live_brokers(
    shared: dict[str, Any],
    stacks: Mapping[str, Any],
    fallback_broker: Any,
    pool: Any,
    trade_repo: Any,
    alert_manager: Any,
    *,
    call_timeout: float,
    fetch_timeout: float | None = None,
    ghost_timeout: float | None = None,
    suppress_ghost_errors: bool = True,
) -> list[dict[str, Any]]:
    """One reconcile per live broker (Phase 2 task 9): its own rows vs its own positions and
    holdings, ghosts auto-closed through ITS broker and monitor only. Returns one dict per
    broker reconciled ({key, broker, db_open, result, auto_closed}), primary first. A failure
    of the PRIMARY raises (the caller skips the cycle, as before); another broker's failure
    is logged ERROR and that broker is skipped this cycle (stale ghost alerts then stay)."""
    out: list[dict[str, Any]] = []
    failed = False
    for i, (key, brk, monitor) in enumerate(_reconcile_targets(shared, stacks, fallback_broker)):
        rec = _reconciler_for(key, brk, pool)
        try:
            if fetch_timeout is None:
                db_open = await rec._fetch_db_open_trades()
            else:
                db_open = await asyncio.wait_for(rec._fetch_db_open_trades(), timeout=fetch_timeout)
            res = await asyncio.wait_for(rec.reconcile(db_open), timeout=call_timeout)
        except Exception as exc:
            if i == 0:
                raise
            failed = True
            _log.error("reconciliation_broker_failed key=%s err=%s -- skipped this cycle", key, exc)
            continue
        out.append(
            {"key": key, "broker": brk, "monitor": monitor, "db_open": db_open, "result": res}
        )
    all_ghosts = {
        g.agent_trade_id
        for r in out
        for g in r["result"].gaps
        if g.kind == "GHOST_DB_TRADE" and g.agent_trade_id
    }
    for r in out:
        r["auto_closed"] = 0
        coro = _auto_close_ghost_trades(
            r["result"],
            r["broker"],
            trade_repo,
            r.pop("monitor"),
            alert_manager=alert_manager,
            current_ghost_ids=all_ghosts,
            resolve_stale=not failed,
        )
        if suppress_ghost_errors:
            with contextlib.suppress(Exception):
                r["auto_closed"] = await coro
        elif ghost_timeout is None:
            r["auto_closed"] = await coro
        else:
            r["auto_closed"] = await asyncio.wait_for(coro, timeout=ghost_timeout)
    return out


def _held_from_positions(raw: Any) -> set[str]:
    """Bare symbols with a nonzero position in a broker's day book (get_positions(): a
    list[Position], or a raw {"net": [...]} dict tolerated defensively)."""
    net: list[Any] = (raw.get("net", []) or []) if isinstance(raw, dict) else list(raw or [])
    out: set[str] = set()
    for p in net:
        s = getattr(p, "symbol", None) or (p.get("tradingsymbol") if isinstance(p, dict) else None)
        q = getattr(p, "quantity", None)
        if q is None and isinstance(p, dict):
            q = p.get("quantity") or p.get("net_quantity")
        if s and float(q or 0) != 0:
            out.add(s.split(":")[-1])
    return out


async def _held_symbols_live_union(
    shared: dict[str, Any], targets: list[tuple[str | None, Any, Any]], *, call_timeout: float
) -> tuple[set[str], set[str], bool]:
    """(union of every live broker's held symbols, the primary's own set, all_loaded).

    R3: the same symbol must never be held by two live strategies at once, across brokers,
    so the entry guard's live set is the UNION over brokers. Each broker: day positions plus
    delivery holdings; a holdings failure keeps that broker's last known set (never shrink the
    guard). A PRIMARY positions failure raises (the cycle is skipped, as before); another
    broker's keeps its last known set and `all_loaded` is False until it has loaded once, so
    the caller keeps entries fail-closed until every live broker's books were read."""
    last: dict[Any, set[str]] = shared.setdefault("held_symbols_live_by_key", {})
    union: set[str] = set()
    primary: set[str] = set()
    all_loaded = True
    unloaded: set[Any] = set()
    for i, (key, brk, _mon) in enumerate(targets):
        try:
            held = _held_from_positions(await asyncio.wait_for(brk.get_positions(), call_timeout))
        except Exception as exc:
            if i == 0:
                raise
            _log.error("live_positions_fetch_err key=%s %s", key, str(exc)[:80])
            if key not in last:
                all_loaded = False
                unloaded.add(key)
                _alert_held_symbols_not_loaded(key, exc)
            union |= last.get(key, set())
            continue
        try:
            held |= _held_symbols_from_holdings(
                await asyncio.wait_for(brk.get_holdings(), call_timeout)
            )
        except Exception as _he:
            held |= last.get(key, set())
            _log.error("live_holdings_fetch_err %s", str(_he)[:80])
        last[key] = held
        union |= held
        if i == 0:
            primary = held
    # Live entries on these brokers stay fail-closed (_held_symbols_unknown_for); paper and
    # the brokers that did load trade normally.
    shared["held_symbols_unloaded_keys"] = unloaded
    return union, primary, all_loaded


def _alert_held_symbols_not_loaded(key: Any, exc: BaseException) -> None:
    """One CRITICAL operator alert per broker per hour while its books have never loaded."""
    try:
        from src.infrastructure import operator_alerts as _oa

        _oa.raise_alert(
            f"Live broker {stack_broker(key) or key}: positions could not be read since "
            f"start -- NO new live entries on it until they load (paper and other brokers "
            f"unaffected). Check its session/login ({str(exc)[:120]})",
            source=f"held_symbols_not_loaded:{key}",
            tier="CRITICAL",
            dedupe_seconds=3600,
        )
    except Exception as _ae:  # noqa: BLE001 -- the ERROR log already fired
        _log.error("held_symbols_alert_failed key=%s err=%s", key, _ae)


def _reconciliation_log_fields(
    runs: list[dict[str, Any]], held_primary: set[str], held_paper: set[str]
) -> tuple[int, int, int, str, str]:
    """reconciliation_log values: (db_positions, broker_positions, matched, gaps JSON,
    by_broker JSON). The top-level counts are the PRIMARY broker's, as before (dashboard
    compatible); `gaps` is the primary's list unchanged followed by every other broker's gaps
    tagged with "broker" (so a second broker's ghost is still visible on the dashboard);
    `by_broker` maps each broker to its own counts and gaps."""
    import json as _json

    def _gap(g: Any) -> dict[str, Any]:
        return {"kind": g.kind, "symbol": g.symbol, "broker_qty": g.broker_qty, "db_qty": g.db_qty}

    primary = runs[0]
    gaps = [_gap(g) for g in primary["result"].gaps]
    by_broker: dict[str, Any] = {}
    for r in runs:
        name = stack_broker(r["key"]) if r["key"] else None
        label = name or "legacy"
        if r is not primary:
            gaps += [{**_gap(g), "broker": label} for g in r["result"].gaps]
        n_db = len(r["db_open"])
        by_broker[label] = {
            "db_positions": n_db,
            "matched": max(n_db - len(r["result"].gaps), 0),
            "auto_closed": r.get("auto_closed", 0),
            "gaps": [_gap(g) for g in r["result"].gaps],
        }
    dn = len(primary["db_open"])
    bn = len(held_primary) + len(held_paper)
    matched = max(dn - len(primary["result"].gaps), 0)
    return dn, bn, matched, _json.dumps(gaps), _json.dumps(by_broker)


async def _price_truth_phase2_sweep(
    price_truth: Any,
    positions: list[Any],
    trade_repo: Any,
) -> None:
    """Run Phase 2 price reconciliation for each monitored position. Best-effort."""
    from decimal import Decimal

    for pos in positions:
        try:
            result = await price_truth.phase2_event_gated(
                symbol=pos.symbol,
                broker_order_id=pos.agent_trade_id,
                recorded_fill_price=pos.fill_price,
                recorded_quantity=pos.quantity,
                product=pos.product,
            )
            if result.status.value == "CORRECTED" and trade_repo is not None:
                await trade_repo.update_fill(
                    pos.agent_trade_id,
                    Decimal(str(result.corrected_price)),
                    __import__("datetime").datetime.now(
                        __import__("zoneinfo").ZoneInfo("Asia/Kolkata")
                    ),
                )
                # Update the in-memory copy too, not just the DB row — this loop
                # reads pos.fill_price from the same OrderMonitor object every
                # sweep, so without this it never converges: it re-reads the old
                # price, "corrects" it again, forever (found 2026-09-02: IFCI was
                # logging this every ~30s for hours straight).
                pos.fill_price = result.corrected_price
                _log.info(
                    "price_truth_corrected symbol=%s old=%.2f new=%.2f",
                    pos.symbol,
                    result.expected_price,
                    result.corrected_price,
                )
            elif result.status.value == "CRITICAL":
                _log.critical(
                    "price_truth_qty_mismatch symbol=%s msg=%s", pos.symbol, result.message
                )
        except Exception as exc:
            _log.debug("price_truth_phase2_err symbol=%s err=%s", pos.symbol, exc)


async def _auto_close_ghost_trades(
    reconciliation: Any,
    live_broker: Any,
    trade_repo: Any,
    order_monitor: Any,
    alert_manager: Any = None,
    current_ghost_ids: set[str] | None = None,
    resolve_stale: bool = True,
) -> int:
    """Close any live DB trade the broker has already fully exited outside the
    bot (GHOST_DB_TRADE), using the broker's own order history as the exit —
    never guessed, never left silently open.

    Per-broker reconciliation calls this once per live broker with THAT broker and its
    stack's monitor; `current_ghost_ids` is then every broker's ghost ids, so one broker's
    run never resolves another broker's still-active ghost alert, and `resolve_stale` is
    False when some broker could not be reconciled this cycle (its ghosts are unknown).

    Runs once, blocking, before the main loop starts, and every reconciliation
    cycle after that (see call sites) — a stale-open live position is a risk
    gap, not a display nit, so this must self-heal without manual DB surgery
    (found 2026-09-02: IFCI was sold outside the bot and sat "open" in the DB
    for days, still showing "active" on the dashboard).

    A gap only gets closed when a matching FILLED exit order is actually found
    in the broker's order book; otherwise it's left open and logged CRITICAL
    for human review. Inventing an exit price would be worse than the bug.
    """
    from decimal import Decimal

    ghosts = [g for g in reconciliation.gaps if g.kind == "GHOST_DB_TRADE" and g.agent_trade_id]
    if alert_manager is not None:
        # A trade that stopped being a ghost — reconciled cleanly again, or
        # genuinely closed below — must have its alert clear too. This function
        # only ever runs against the CURRENT gap list, so a trade that quietly
        # stops recurring (as opposed to being explicitly closed by the code
        # below) gets no other chance to notice "this isn't a problem anymore"
        # (found 2026-09-08: CGCL's ghost alerts from before the reconciliation
        # fix stayed active forever once the fix landed and it stopped being
        # flagged as a gap at all — nothing ever told the alert to clear).
        _current_ghost_ids = (
            current_ghost_ids
            if current_ghost_ids is not None
            else {g.agent_trade_id for g in ghosts}
        )
        for _a in list(alert_manager.active()) if resolve_stale else []:
            if (
                _a.source.startswith("ghost:")
                and _a.source[len("ghost:") :] not in _current_ghost_ids
            ):
                alert_manager.resolve(_a.id)
    if not ghosts or trade_repo is None or live_broker is None:
        return 0
    _log.info(
        "ghost_trade_check_start count=%d symbols=%s",
        len(ghosts),
        [g.symbol for g in ghosts],
    )
    try:
        orders = await live_broker.get_all_orders()
    except Exception as exc:
        _log.warning(
            "ghost_trade_check_aborted reason=orders_fetch_failed err=%s — will retry next cycle",
            exc,
        )
        return 0
    try:
        holdings = {h.symbol: h for h in await live_broker.get_holdings()}
    except Exception:
        holdings = {}

    closed = 0
    for gap in ghosts:
        bare_symbol = gap.symbol.split(":")[-1]
        try:
            row = await trade_repo.get_by_agent_id(gap.agent_trade_id, is_paper=False)
        except Exception as exc:
            _log.warning(
                "ghost_trade_check symbol=%s tid=%s action=SKIP reason=db_row_fetch_failed err=%s",
                bare_symbol,
                gap.agent_trade_id,
                exc,
            )
            continue
        if row is None:
            _log.warning(
                "ghost_trade_check symbol=%s tid=%s action=SKIP reason=db_row_vanished "
                "(closed by something else between reconcile and this check)",
                bare_symbol,
                gap.agent_trade_id,
            )
            continue
        direction = str(row["direction"]).upper()
        quantity = int(row["quantity"])
        exit_side = "SELL" if direction == "LONG" else "BUY"
        matches = [
            o
            for o in orders
            if o.symbol == bare_symbol
            and o.direction == exit_side
            and o.product == "DELIVERY"
            and o.status == "FILLED"
            and o.filled_quantity > 0
        ]
        _log.info(
            "ghost_trade_check symbol=%s tid=%s db_direction=%s db_qty=%d expected_exit_side=%s "
            "orders_scanned=%d matching_exit_orders_found=%d",
            bare_symbol,
            gap.agent_trade_id,
            direction,
            quantity,
            exit_side,
            len(orders),
            len(matches),
        )
        if not matches:
            _log.critical(
                "ghost_trade_check symbol=%s tid=%s action=LEFT_OPEN reason=no_matching_exit_order "
                "— DB says open (qty=%d), symbol absent from broker positions AND holdings past the "
                "T+1 settlement window, but no FILLED %s DELIVERY order "
                "for this symbol exists in the broker's order book to prove when/at what price it "
                "exited; refusing to invent one — needs manual review",
                bare_symbol,
                gap.agent_trade_id,
                quantity,
                exit_side,
            )
            if alert_manager is not None:
                # One active alert per trade, not one per reconciliation cycle —
                # this runs every ~30-60s, and without this check it raised a
                # fresh duplicate alert every single time the condition simply
                # persisted (found 2026-09-08: CGCL alerted 4x in under 90s).
                # "ghost:" prefix distinguishes this alert's source from a bare
                # trade id used by other alert types (e.g. GTT re-arm failures)
                # for the same trade — needed so the auto-resolve above (which
                # scans by this prefix) never touches an unrelated alert.
                _already_active = any(
                    a.source == f"ghost:{gap.agent_trade_id}" for a in alert_manager.active()
                )
                if not _already_active:
                    from src.infrastructure.alert_system import AlertTier

                    alert_manager.raise_alert(
                        AlertTier.CRITICAL,
                        f"ghost_trade_check {bare_symbol}: DB open but no matching broker exit "
                        "found — needs manual review",
                        source=f"ghost:{gap.agent_trade_id}",
                    )
            continue
        exit_order = max(matches, key=lambda o: o.placed_at)
        # Holdings survive a same-day full sell as a zero-quantity row that still
        # carries the true cost-basis average_price — the only reliable entry
        # price once the DB's own fill_price may itself be corrupted (the bug
        # this whole change exists to stop). Falls back to the DB row only if
        # the broker has no holdings record at all for the symbol.
        entry_source = "kite_holdings" if bare_symbol in holdings else "db_fill_price_fallback"
        entry_price = (
            float(holdings[bare_symbol].average_price)
            if bare_symbol in holdings
            else float(row["fill_price"] or 0)
        )
        exit_price = float(exit_order.average_price)
        pnl = (
            (exit_price - entry_price) * quantity
            if direction == "LONG"
            else (entry_price - exit_price) * quantity
        )
        try:
            await trade_repo.update_pnl(
                gap.agent_trade_id,
                Decimal(str(round(pnl, 2))),
                Decimal("0"),
                Decimal(str(exit_price)),
                exit_order.placed_at,
                is_paper=False,
                exit_reason="GHOST_TRADE_AUTO_RECONCILED",
            )
        except Exception as exc:
            _log.warning(
                "ghost_trade_check symbol=%s tid=%s action=SKIP reason=db_write_failed err=%s "
                "— will retry next cycle, no data lost",
                bare_symbol,
                gap.agent_trade_id,
                exc,
            )
            continue
        if order_monitor is not None:
            order_monitor.untrack(gap.agent_trade_id)
        closed += 1
        _log.critical(
            "ghost_trade_check symbol=%s tid=%s action=CLOSED entry=%.2f (source=%s) exit=%.2f "
            "qty=%d pnl=%.2f exit_order=%s exit_time=%s reason=broker_already_exited_outside_bot",
            bare_symbol,
            gap.agent_trade_id,
            entry_price,
            entry_source,
            exit_price,
            quantity,
            pnl,
            exit_order.broker_order_id,
            exit_order.placed_at,
        )
        try:
            from src.notifications import EventKind, notify
            from src.notifications.events import segment_for_product

            notify(
                EventKind.EXIT,
                is_paper=False,
                agent_trade_id=gap.agent_trade_id,
                symbol=bare_symbol,
                # This path only ever matches product=="DELIVERY" exit orders
                # (see the `matches` filter above) -- always swing in practice,
                # but derive it from the row rather than hardcode that.
                segment=segment_for_product(str(row.get("product", "CNC"))),
                dedupe_key=f"exit:{gap.agent_trade_id}",
                reason="GHOST_TRADE_AUTO_RECONCILED",
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                net_pnl=pnl,
            )
        except Exception:  # notify() must never fail reconciliation
            pass
    return closed


def _charges_dict(raw: Any) -> dict[str, float]:
    """charges_breakdown JSONB (str or dict, from asyncpg) -> dict; {} when absent/corrupt."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return {k: float(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


async def _persist_closed_trades(
    trade_repo: Any, trades: list[dict[str, Any]], is_paper: bool = False
) -> None:
    """UPDATE the trades/paper_trades table with realized P&L for each newly-closed
    trade. is_paper routes to paper_trades. Best-effort."""
    from datetime import UTC, datetime
    from decimal import Decimal

    for t in trades:
        tid = t.get("agent_trade_id")
        if not tid:
            continue
        try:
            exit_ts = t.get("exit_ts")
            ts = (
                datetime.fromisoformat(exit_ts)
                if isinstance(exit_ts, str) and exit_ts
                else datetime.now(UTC)
            )
            # net_pnl is the WHOLE trade; update_pnl adds partial_pnl_booked itself, so
            # hand it the last leg only (whole - partial) or the partial counts twice.
            _partial = t.get("partial_pnl_booked")
            _last_leg = Decimal(str(t.get("net_pnl", 0))) - Decimal(str(_partial or 0))
            await trade_repo.update_pnl(
                tid,
                _last_leg,
                Decimal(str(t.get("total_costs", 0))),
                Decimal(str(t.get("exit_price", 0))),
                ts,
                is_paper=is_paper,
                partial_pnl_booked=(
                    Decimal(str(round(_partial, 2))) if _partial is not None else None
                ),
                partial_qty_booked=t.get("partial_qty_booked"),
                exit_reason=str(t["reason"]) if t.get("reason") else None,
                charges_breakdown=(
                    json.dumps(t["charges_breakdown"]) if t.get("charges_breakdown") else None
                ),
                broker=t.get("broker"),
            )
        except Exception as exc:
            _log.error("trade_pnl_persist_failed trade_id=%s err=%s", tid, exc)


async def _persist_monitor_telemetry(
    pool: Any,
    snapshots: list[dict[str, Any]],
    orders_monitored: int,
    checks_per_minute: int,
    avg_latency_ms: int,
    auto_actions: int,
    cycle_seconds: float = 2.0,
) -> None:
    """Upsert order_monitor_stats (id=1) + sync order_monitor_breaches. Best-effort.

    Active breaches are deduplicated by (symbol, breach_type): a key present in the
    current snapshot but not yet in the table is inserted; an unresolved row whose key
    is gone is marked resolved. Powers the Advanced Order Monitor tab cross-process."""
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            current = {(str(s["symbol"]), str(s["breach_type"])): s for s in snapshots}
            rows = await conn.fetch(
                "SELECT id, symbol, breach_type FROM order_monitor_breaches WHERE resolved_at IS NULL"
            )
            existing = {(r["symbol"], r["breach_type"]): r["id"] for r in rows}
            for key, s in current.items():
                if key not in existing:
                    await conn.execute(
                        "INSERT INTO order_monitor_breaches "
                        "(agent_trade_id, symbol, breach_type, ltp, expected_price, gap_pct, order_type) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7)",
                        str(s.get("agent_trade_id", "")),
                        key[0],
                        key[1],
                        float(s.get("ltp", 0)),
                        float(s.get("expected_price", 0)),
                        float(s.get("gap_pct", 0)),
                        str(s.get("order_type", "")),
                    )
            for key, rid in existing.items():
                if key not in current:
                    await conn.execute(
                        "UPDATE order_monitor_breaches SET resolved_at = NOW(), "
                        "action_result = COALESCE(action_result, 'RESOLVED') WHERE id = $1",
                        rid,
                    )
            await conn.execute(
                "INSERT INTO order_monitor_stats "
                "(id, check_interval_ms, checks_per_minute, avg_latency_ms, "
                " orders_monitored, breaches_detected, auto_actions, updated_at) "
                "VALUES (1, $6, $1, $2, $3, $4, $5, NOW()) "
                "ON CONFLICT (id) DO UPDATE SET check_interval_ms = $6, "
                "checks_per_minute = $1, avg_latency_ms = $2, orders_monitored = $3, "
                "breaches_detected = $4, auto_actions = $5, updated_at = NOW()",
                checks_per_minute,
                avg_latency_ms,
                orders_monitored,
                len(current),
                auto_actions,
                int(cycle_seconds * 1000),
            )
    except Exception as exc:
        _log.debug("monitor_telemetry_persist_failed err=%s", exc)


async def _classify_strategies_for_regime(db_pool: Any, regime: str) -> tuple[list[str], list[str]]:
    """Suitable/avoid strategies for the CURRENT regime, from real per-(strategy,regime)
    win rate + profit factor in DB closed-trade history. Suitable: win_rate>=0.45 and
    profit_factor>=1.0 with at least 5 trades in this regime. Avoid: profit_factor<1.0
    with at least 5 trades. Strategies with <5 trades in this regime are omitted from
    both lists — not enough evidence either way, not a claim of neutrality.
    """
    trades = await _fetch_closed_trades_from_db(db_pool)
    by_strategy: dict[str, list[float]] = {}
    for t in trades:
        if str(t.get("regime_at_entry", "")) != regime:
            continue
        by_strategy.setdefault(str(t.get("strategy_name", "unknown")), []).append(
            float(t.get("pnl_r", 0.0))
        )
    suitable: list[str] = []
    avoid: list[str] = []
    for strat, pnls in by_strategy.items():
        if len(pnls) < 5:
            continue
        wins = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p <= 0]
        win_rate = len(wins) / len(pnls)
        gross_win = sum(wins)
        gross_loss = sum(losses)
        pf = (
            (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
        )
        if pf >= 1.0 and win_rate >= 0.45:
            suitable.append(strat)
        elif pf < 1.0:
            avoid.append(strat)
    return suitable, avoid


async def _compute_regime_historical_accuracy(db_pool: Any) -> float:
    """Hit rate of directional regime calls vs next-trading-day NIFTY close, over the
    last 60 logged regime rows. BULL/STRONG_BULL scored correct if the next close was
    higher, BEAR/STRONG_BEAR if lower; NEUTRAL/UNKNOWN carry no directional claim and
    are excluded from scoring. Returns 0.0 if fewer than 5 scoreable rows exist.
    """
    if db_pool is None:
        return 0.0
    try:
        rows = await db_pool.fetch(
            """
            SELECT regime, created_at
              FROM market_regime_log
             WHERE regime IN ('STRONG_BULL','BULL','BEAR','STRONG_BEAR')
             ORDER BY created_at DESC LIMIT 60
            """
        )
        candles = await db_pool.fetch(
            """
            SELECT ts, close FROM ohlcv_candles
             WHERE symbol='NIFTY 50' AND timeframe='1d'
             ORDER BY ts
            """
        )
    except Exception:
        return 0.0
    if not rows or len(candles) < 2:
        return 0.0
    closes = [(c["ts"].date(), float(c["close"])) for c in candles]
    correct = 0
    scored = 0
    for r in rows:
        call_date = r["created_at"].date()
        after = [c for c in closes if c[0] > call_date]
        on_or_before = [c for c in closes if c[0] <= call_date]
        if not after or not on_or_before:
            continue
        next_close = after[0][1]
        base_close = on_or_before[-1][1]
        went_up = next_close > base_close
        bullish_call = r["regime"] in ("STRONG_BULL", "BULL")
        scored += 1
        if went_up == bullish_call:
            correct += 1
    return round(correct / scored, 4) if scored >= 5 else 0.0


async def _fetch_closed_trades_from_db(db_pool: Any, mode: str = "all") -> list[dict[str, Any]]:
    """DB truth for closed-trade history (paper_trades UNION trades, both CLOSED).

    _attribution_loop previously sourced attr_trades from shared['closed_trades'],
    an in-memory list only ever populated by OrderMonitor detecting a close during
    the CURRENT process's runtime. That meant months of already-closed paper trades
    (real strategy_name, regime_at_entry, llm_confidence — all sitting in the DB)
    were invisible to attribution, and the daily job kept re-aggregating whatever
    scraps were in memory under a blank strategy_name (found 2026-08-20: 372 closed
    paper_trades in the DB, but every performance_attribution row was blank-strategy
    because _record_closed_trade never carried strategy_name either — see MonitoredPosition
    fix). Querying the DB directly is both correct and immune to restarts/timing.

    Excludes exit_reason='manual_dup_entry_cleanup' rows (paper_trades only — trades
    has no exit_reason column) — accidental duplicate order fills closed out by hand,
    not real trades. gross_pnl/total_costs are None when charges_taxes was never
    recorded (older rows, pre cost-tracking) — callers that need cost erosion must
    skip those rather than treating a missing cost as a zero cost.

    `mode`: "all" (default, unchanged historical behavior — attribution/expectancy
    display) | "paper" | "live". Callers that feed a LIVE risk/quality gate
    (cost-erosion's quality_raise, expectancy's STRATEGY_DECAY pause) must pass
    "live" — mixing paper simulated losses into either would let a paper strategy
    raise the quality bar or hard-pause a strategy's real trading, and vice versa.
    Found 2026-09-04 doing the dashboard paper/live separation work: this was the
    same class of bug already fixed for /api/pnl_breakdown on 2026-09-02, just not
    yet caught in the two loops that actually gate live trades.
    """
    if db_pool is None:
        return []
    try:
        if mode == "live":
            rows = await db_pool.fetch(
                """
                SELECT id, agent_trade_id, strategy_name, pnl_realized, initial_sl, fill_price,
                       quantity, entry_timestamp, exit_timestamp, regime_at_entry, llm_confidence,
                       charges_taxes, partial_qty_booked
                  FROM trades
                 WHERE order_state = 'CLOSED' AND exit_timestamp IS NOT NULL
                """
            )
        elif mode == "paper":
            rows = await db_pool.fetch(
                """
                SELECT id, agent_trade_id, strategy_name, pnl_realized, initial_sl, fill_price,
                       quantity, entry_timestamp, exit_timestamp, regime_at_entry, llm_confidence,
                       charges_taxes, partial_qty_booked
                  FROM paper_trades
                 WHERE order_state = 'CLOSED' AND exit_timestamp IS NOT NULL
                   AND exit_reason IS DISTINCT FROM 'manual_dup_entry_cleanup'
                """
            )
        else:
            rows = await db_pool.fetch(
                """
                SELECT id, agent_trade_id, strategy_name, pnl_realized, initial_sl, fill_price,
                       quantity, entry_timestamp, exit_timestamp, regime_at_entry, llm_confidence,
                       charges_taxes, partial_qty_booked
                  FROM paper_trades
                 WHERE order_state = 'CLOSED' AND exit_timestamp IS NOT NULL
                   AND exit_reason IS DISTINCT FROM 'manual_dup_entry_cleanup'
                UNION ALL
                SELECT id, agent_trade_id, strategy_name, pnl_realized, initial_sl, fill_price,
                       quantity, entry_timestamp, exit_timestamp, regime_at_entry, llm_confidence,
                       charges_taxes, partial_qty_booked
                  FROM trades
                 WHERE order_state = 'CLOSED' AND exit_timestamp IS NOT NULL
                """
            )
    except Exception as exc:
        _log.warning("closed_trades_db_fetch_failed err=%s", str(exc)[:160])
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        fill = float(r["fill_price"] or 0.0)
        sl = float(r["initial_sl"] or 0.0)
        # quantity is what REMAINED after partials; risk is over the original size, since
        # pnl_realized is the whole trade's P&L.
        qty = int(r["quantity"] or 0) + int(r.get("partial_qty_booked") or 0)
        risk = abs(fill - sl) * qty or 1.0
        net_pnl = float(r["pnl_realized"] or 0.0)
        _entry = r["entry_timestamp"]
        _exit = r["exit_timestamp"]
        _costs = float(r["charges_taxes"]) if r["charges_taxes"] is not None else None
        out.append(
            {
                "db_id": r["id"],
                "agent_trade_id": r["agent_trade_id"] or "",
                "strategy_name": r["strategy_name"] or "unknown",
                "pnl_r": round(net_pnl / risk, 4),
                "net_pnl": net_pnl,
                "gross_pnl": round(net_pnl + _costs, 2) if _costs is not None else None,
                "total_costs": _costs,
                "risk_rupees": round(risk, 2),
                "entry_ts": _entry.isoformat() if _entry else "",
                "exit_ts": _exit.isoformat() if _exit else "",
                "regime_at_entry": r["regime_at_entry"] or "UNKNOWN",
                "llm_confidence": float(r["llm_confidence"])
                if r["llm_confidence"] is not None
                else 0.5,
            }
        )
    return out


def _load_closed_trades_log() -> list[dict[str, Any]]:
    """Rehydrate shared['closed_trades'] from logs/closed_trades.json at startup.

    _monitor_loop persists the full running list on every update, but the
    in-memory list itself was previously hardcoded to [] on every process
    restart — the same class of bug _reseed_order_monitor fixes for open
    positions, just never applied to closed-trade history (feeds
    _attribution_loop/_expectancy_loop/_learning_loop).
    """
    path = _LOGS / "closed_trades.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


async def _ensure_live_broker_for_open_positions(
    live_brokers: dict[str, Any], db_pool: Any, create: Any, legacy_name: str
) -> dict[str, list[str]]:
    """A position outlives the switch that opened it.

    The live brokers are decided at boot from TODAY's paper/live switches, but a real position
    stays open across restarts and config edits. Flip the swing agent to "paper" the day after it
    took a live trade and there was no live broker at all: no OrderMonitor, no lifecycle, and the
    reconciler compared the real holding to the PAPER adapter and shouted GHOST_DB_TRADE
    (2026-09-28, LAURUSLABS: 14 real shares, GTT active, nothing watching). The config switch only
    governs NEW entries (_resolve_is_paper); anything still open in `trades` -- the live table --
    must be monitored by its own live broker regardless (`exec_broker`, NULL = "kite").

    Adds `create(name)` to `live_brokers` (in place) for every broker that holds open live trades
    but was not built, and returns {name: [agent_trade_id, ...]} of the trades that forced each.
    NULL `exec_broker` (and an older DB without the column) means "kite": every row written
    before the column existed was placed through Kite. A value that is not a registered live
    broker is never built (CRITICAL log + alert); like NULL, those trades force "kite" -- the
    stack that reseeds unstamped/unknown rows (_reseed_scope) -- so they are still tracked. If the DB cannot be read we cannot prove there is nothing to
    protect, so the legacy broker is built anyway.
    """
    from src.brokers.registry import list_brokers

    _valid = set(list_brokers()) - {"paper"}
    _open_sql = "FROM trades WHERE order_state NOT IN ('CLOSED','REJECTED','CANCELLED','TIMED_OUT')"
    by_broker: dict[str, list[str]] = {}
    if db_pool is not None:
        try:
            async with db_pool.acquire() as conn:
                try:
                    rows = await conn.fetch(f"SELECT agent_trade_id, exec_broker {_open_sql}")
                    pairs = [(str(r["agent_trade_id"]), r.get("exec_broker")) for r in rows]
                except Exception as exc:  # noqa: BLE001 -- e.g. column not migrated yet
                    _log.warning("live_position_check_exec_broker_unavailable err=%s", exc)
                    rows = await conn.fetch(f"SELECT agent_trade_id {_open_sql}")
                    pairs = [(str(r["agent_trade_id"]), None) for r in rows]
            for tid, raw in pairs:
                _b = str(raw or "kite")  # NULL = historical Kite row
                if _b not in _valid:
                    _log.critical(
                        "exec_broker_invalid trade=%s exec_broker=%r -- not a registered live "
                        "broker; tracking it with kite",
                        tid,
                        _b,
                    )
                    try:
                        from src.infrastructure.operator_alerts import raise_alert as _op_alert

                        _op_alert(
                            f"Open live trade {tid} has invalid exec_broker {_b!r}; tracked "
                            "with kite. Fix the trade row.",
                            source="exec_broker_invalid",
                            tier="CRITICAL",
                            dedupe_seconds=4 * 3600,
                        )
                    except Exception as _ae:  # noqa: BLE001 -- the CRITICAL log already fired
                        _log.error("exec_broker_invalid_alert_failed err=%s", _ae)
                    _b = "kite"  # same owner as NULL / the reseed's catch-all stack
                by_broker.setdefault(_b, []).append(tid)
        except Exception as exc:  # noqa: BLE001
            _log.critical(
                "live_position_check_failed err=%s -- building the live broker anyway", exc
            )
            by_broker = {legacy_name: ["<unknown: db read failed>"]}
    else:
        _log.critical("live_position_check_skipped reason=no_db -- building the live broker anyway")
        by_broker = {legacy_name: ["<unknown: no db>"]}
    forced: dict[str, list[str]] = {}
    for name, open_ids in by_broker.items():
        if name in live_brokers:
            continue
        _log.warning(
            "live_broker_forced_by_open_positions broker=%s count=%d trades=%s -- config says "
            "paper but these real positions are still open, so they stay monitored/reconciled "
            "live (new entries still follow the config switch)",
            name,
            len(open_ids),
            open_ids,
        )
        live_brokers[name] = create(name)
        forced[name] = open_ids
    return forced


# (what, broker_name, outcome) already logged for a missing exec_broker column: the 30 s
# reconcile loop reseeds every cycle, so the condition is logged once per process.
_RESEED_LOGGED: set[tuple[str, str, str]] = set()


async def _fetch_reseed_rows(
    db_pool: Any,
    sql: str,
    paper_mode: bool,
    broker_name: str | None,
    known_brokers: Sequence[str] | None,
    unfiltered_if_no_column: bool,
    what: str,
) -> list[Any]:
    """Run a reseed SELECT (`sql` ends in its WHERE clause), restricted for a live stack to the
    rows its broker holds -- see _reseed_scope. `known_brokers` (kite stack only) adds rows
    whose exec_broker is NULL or not a registered broker. Paper, or broker_name None (legacy
    callers), runs `sql` unchanged. Raises whatever the fetch raises, except a missing column."""
    if paper_mode or broker_name is None:
        return list(await db_pool.fetch(sql))
    args: list[Any] = [broker_name]
    own = "COALESCE(exec_broker, 'kite') = $1"
    if known_brokers:
        args.append(list(known_brokers))
        own = f"({own} OR COALESCE(exec_broker, 'kite') <> ALL($2::text[]))"
    try:
        return list(await db_pool.fetch(f"{sql} AND {own}", *args))
    except Exception as exc:
        if "exec_broker" not in str(exc) or "does not exist" not in str(exc):
            raise  # only the missing-column case (UndefinedColumnError) is handled here
        outcome = "unfiltered" if unfiltered_if_no_column else "none"
        if (what, broker_name, outcome) not in _RESEED_LOGGED:
            _RESEED_LOGGED.add((what, broker_name, outcome))
            if unfiltered_if_no_column:
                _log.warning(
                    "%s_reseed_exec_broker_missing broker=%s -- migration 089 not applied; the "
                    "kite stack reseeds every live row (all pre-routing, placed through Kite)",
                    what,
                    broker_name,
                )
            else:
                _log.error(
                    "%s_reseed_exec_broker_missing broker=%s -- migration 089 not applied; "
                    "this non-kite live stack reseeds nothing",
                    what,
                    broker_name,
                )
        if unfiltered_if_no_column:
            return list(await db_pool.fetch(sql))
        return []


# Last orphan summary logged by _report_orphaned_live_rows (log again only when it changes).
_ORPHANS_LOGGED: dict[str, str] = {}


async def _report_orphaned_live_rows(db_pool: Any, stacks: Mapping[str, Any]) -> int:
    """Count open live `trades` rows that NO stack reseeds (their owner has no live stack) and
    raise a CRITICAL log + operator alert, deduplicated. Owner = exec_broker; NULL, unknown
    values and a DB without the column mean "kite" (as _reseed_scope). Such positions are left
    unmonitored on purpose: handing them to another broker's monitor would send their exits to
    the wrong account. Returns the orphan count (0 when the DB cannot be read)."""
    if db_pool is None:
        return 0
    _open = "FROM trades WHERE order_state NOT IN ('CLOSED','REJECTED','CANCELLED','TIMED_OUT')"
    try:
        try:
            rows = await db_pool.fetch(
                f"SELECT exec_broker, COUNT(*) AS n {_open} GROUP BY exec_broker"  # noqa: S608
            )
        except Exception as exc:
            if "exec_broker" not in str(exc) or "does not exist" not in str(exc):
                raise
            rows = [  # pre-089 DB: every row is a pre-routing (Kite) row
                {"exec_broker": None, "n": r["n"]}
                for r in await db_pool.fetch(f"SELECT COUNT(*) AS n {_open}")  # noqa: S608
            ]
    except Exception as exc:  # noqa: BLE001 -- a check only; the reseed itself logs its errors
        _log.warning("reseed_orphan_check_failed err=%s", str(exc)[:160])
        return 0
    known = set(_known_live_brokers())
    have = {stack_broker(k) for k in live_stacks(stacks)}
    orphans: dict[str, int] = {}
    for r in rows:
        owner = str(r["exec_broker"] or LEGACY_LIVE_BROKER)
        if owner not in known:
            owner = LEGACY_LIVE_BROKER
        if owner not in have and int(r["n"] or 0) > 0:
            orphans[owner] = orphans.get(owner, 0) + int(r["n"])
    summary = " ".join(f"{b}={n}" for b, n in sorted(orphans.items()))
    if summary != _ORPHANS_LOGGED.get("last", ""):
        _ORPHANS_LOGGED["last"] = summary
        if orphans:
            _log.critical(
                "reseed_orphaned_live_rows %s -- open live trades whose broker has no live "
                "stack; NOT monitored (never handed to another broker's monitor)",
                summary,
            )
            try:
                from src.infrastructure import operator_alerts as _oa

                _oa.raise_alert(
                    f"Open live trades not monitored ({summary}): their broker has no live "
                    "stack. Exits/SL are not managed by the engine; check the broker.",
                    source="reseed_orphaned_live_rows",
                    tier="CRITICAL",
                    dedupe_seconds=4 * 3600,
                )
            except Exception as _ae:  # noqa: BLE001 -- the CRITICAL log already fired
                _log.error("reseed_orphaned_live_rows_alert_failed err=%s", _ae)
    return sum(orphans.values())


async def _reseed_order_monitor(
    order_monitor: Any,
    db_pool: Any,
    paper_mode: bool,
    broker_name: str | None = None,
    known_brokers: Sequence[str] | None = None,
    unfiltered_if_no_column: bool = False,
) -> int:
    """Register every DB-open trade with OrderMonitor.

    OrderMonitor.track() is only ever called at the moment of entry (see the
    two call sites in this file). Its ``_positions`` dict is in-memory only —
    a process restart wipes it, and every position opened before that restart
    then sits with zero SL/target monitoring even though the DB still shows
    live sl/target prices (found 2026-08-17: 14 open swing positions, 0
    "Orders Watched" on the dashboard, after dozens of restarts since entry).
    Idempotent — already-tracked agent_trade_ids are skipped, so this is safe
    to call repeatedly (both once at startup and every reconciliation cycle).

    A live stack passes its `broker_name` (+ _reseed_scope's extras) so it reseeds only
    the rows its own broker holds; None = every row of the table (legacy).
    """
    if db_pool is None:
        return 0
    from src.execution.order_lifecycle import MonitoredPosition

    table = "paper_trades" if paper_mode else "trades"
    try:
        rows = await _fetch_reseed_rows(
            db_pool,
            f"SELECT agent_trade_id, symbol, direction, quantity, fill_price, "  # noqa: S608
            f"initial_sl, current_sl, initial_target, current_target, "
            f"sl_order_id, target_order_id, gtt_sl_id, gtt_target_id, product, "
            f"entry_timestamp, strategy_name, partial_pnl_booked, partial_qty_booked, "
            f"charges_breakdown "
            f"FROM {table} "
            f"WHERE order_state NOT IN ('CLOSED','REJECTED','CANCELLED','TIMED_OUT') "
            f"AND fill_price IS NOT NULL",
            paper_mode,
            broker_name,
            known_brokers,
            unfiltered_if_no_column,
            "order_monitor",
        )
    except Exception as exc:
        _log.error("order_monitor_reseed_fetch_err table=%s err=%s", table, str(exc)[:160])
        return 0

    tracked = {p.agent_trade_id for p in order_monitor.get_monitored()}
    added = 0
    for r in rows:
        tid = r["agent_trade_id"]
        if not tid or tid in tracked:
            continue
        _direction = "BUY" if str(r["direction"] or "").lower() == "long" else "SELL"
        _entry_ts = r["entry_timestamp"]
        order_monitor.track(
            MonitoredPosition(
                agent_trade_id=tid,
                symbol=r["symbol"],
                direction=_direction,
                quantity=int(r["quantity"] or 0),
                fill_price=float(r["fill_price"] or 0.0),
                initial_sl=float(r["initial_sl"] or 0.0),
                current_sl=float(r["current_sl"] or r["initial_sl"] or 0.0),
                initial_target=float(r["initial_target"] or 0.0),
                current_target=float(r["current_target"] or r["initial_target"] or 0.0),
                sl_order_id=r["sl_order_id"] or "",
                target_order_id=r["target_order_id"] or "",
                gtt_sl_id=r["gtt_sl_id"] or "",
                gtt_target_id=r["gtt_target_id"] or "",
                product=r["product"] or "CNC",
                entry_ts=_entry_ts.isoformat() if _entry_ts else "",
                strategy_name=r["strategy_name"] or "",
                partial_pnl_booked=float(r["partial_pnl_booked"] or 0.0),
                partial_qty_booked=int(r.get("partial_qty_booked") or 0),
                partial_costs=_charges_dict(r.get("charges_breakdown")),
            )
        )
        added += 1
    if added:
        _log.warning("order_monitor_reseeded count=%d table=%s", added, table)
    return added


async def _reseed_pending_entries(
    lifecycle: Any,
    db_pool: Any,
    paper_mode: bool,
    order_types: dict[str, str],
    broker_name: str | None = None,
    known_brokers: Sequence[str] | None = None,
    unfiltered_if_no_column: bool = False,
) -> int:
    """Re-register every DB-resting entry order for the per-cycle fill re-poll.

    A swing entry is placed as an AMO after the close and legitimately rests until
    the next open, but ``_pending_entry_ids`` and the MonitoredPosition template
    that goes with it are in-memory only. The orchestrator restarts many times a
    day, so an AMO placed in the evening was always orphaned before it filled:
    nothing re-polled it, the fill was never detected, _arm_deferred_protection
    never ran, and the position opened with no stop, no target and no monitoring
    at all (2026-08-28: IFCI and JINDALSAW both filled at the open completely
    naked). Same class of bug as _reseed_order_monitor, for unfilled entries.

    Idempotent — ids already registered as pending are skipped, so this is safe
    at startup and on every reconciliation cycle. Broker filter: as _reseed_order_monitor.
    """
    if db_pool is None or lifecycle is None:
        return 0
    from src.brokers.base import OrderRequest
    from src.execution.order_lifecycle import MonitoredPosition, OrderState

    table = "paper_trades" if paper_mode else "trades"
    try:
        rows = await _fetch_reseed_rows(
            db_pool,
            f"SELECT agent_trade_id, broker_order_id, symbol, exchange, direction, "  # noqa: S608
            f"quantity, intended_price, initial_sl, current_sl, initial_target, "
            f"current_target, product, strategy_name, entry_timestamp FROM {table} "
            f"WHERE order_state IN ('SUBMITTED','OPEN') AND fill_price IS NULL",
            paper_mode,
            broker_name,
            known_brokers,
            unfiltered_if_no_column,
            "pending_entry",
        )
    except Exception as exc:
        _log.error("pending_entry_reseed_fetch_err table=%s err=%s", table, str(exc)[:160])
        return 0

    already = lifecycle.pending_entry_ids()
    added = 0
    for r in rows:
        tid = r["agent_trade_id"]
        if not tid or tid in already or not r["broker_order_id"]:
            continue
        _dir = "BUY" if str(r["direction"] or "").lower() == "long" else "SELL"
        _qty = int(r["quantity"] or 0)
        _product = r["product"] or "CNC"
        _exchange = r["exchange"] or "NSE"
        _entry = float(r["intended_price"] or 0.0)
        _sl = float(r["current_sl"] or r["initial_sl"] or 0.0)
        _tgt = float(r["current_target"] or r["initial_target"] or 0.0)
        _entry_ts = r["entry_timestamp"]
        record = lifecycle.create_trade(
            OrderRequest(
                symbol=r["symbol"],
                exchange=_exchange,
                direction=_dir,
                quantity=_qty,
                order_type="LIMIT",
                product=_product,
                validity="DAY",
                agent_trade_id=tid,
                signal_id="",
                price=_entry,
            )
        )
        record.broker_order_id = str(r["broker_order_id"])
        record.direction = _dir
        record.entry_price = _entry
        record.sl_price = _sl
        record.target_price = _tgt
        # SUBMITTED cannot transition straight to FILLED (VALID_TRANSITIONS), and a
        # resting AMO is precisely what comes back FILLED on the first poll after the
        # open — left at SUBMITTED, poll() would silently drop that fill. OPEN is the
        # honest state for an order already live at the exchange.
        lifecycle.transition(tid, OrderState.SUBMITTED)
        lifecycle.transition(tid, OrderState.OPEN)
        if _product.upper() in ("CNC", "DELIVERY"):
            record.defer_protection = True
            record.protection_ctx = {
                "signal_id": "",
                "symbol": r["symbol"],
                "exchange": _exchange,
                "product": _product,
                "order_types": dict(order_types),
            }
        record.monitor_template = MonitoredPosition(
            agent_trade_id=tid,
            symbol=r["symbol"],
            direction=_dir,
            quantity=_qty,
            fill_price=0.0,
            initial_sl=_sl,
            current_sl=_sl,
            initial_target=_tgt,
            current_target=_tgt,
            product=_product,
            entry_ts=_entry_ts.isoformat() if _entry_ts else "",
            strategy_name=r["strategy_name"] or "",
        )
        lifecycle.track_pending_entry(tid)
        added += 1
    if added:
        _log.warning("pending_entries_reseeded count=%d table=%s", added, table)
    return added


def _daily_bars_after_entry(bar_dates: Any, entry_date: Any) -> int:
    """Completed daily bars strictly after the entry day -- the tracker's bars_held as if
    it had stepped once per daily close since entry (the fresh-entry tracker steps first
    on the bar after entry)."""
    return sum(1 for d in set(bar_dates) if d > entry_date)


def _tracker_from_db_row(
    _SET: Any,
    spec: Any,
    r: Any,
    fill_price: float,
    sl: float,
    target: float,
    direction: Any,
) -> Any:
    """Rebuild a SmartExitTracker for a position that is already open.

    The R distance MUST come from the ORIGINAL stop: after breakeven/trail the
    current stop sits at (or past) the entry, so seeding risk from it gives
    initial_risk=0 and every R-based rule (partial, breakeven, trail activation) is
    silently skipped for the rest of the trade — exactly what happened to
    LAURUSLABS after its stop moved to entry (2026-09-21). The already-earned
    state (stop, breakeven, partial) is restored so nothing fires twice.
    """
    _init = r["initial_sl"] if "initial_sl" in r.keys() else None  # noqa: SIM118
    init_sl = float(_init or 0.0)
    if init_sl <= 0 or init_sl == fill_price:
        init_sl = sl
    tracker = _SET(spec, fill_price, init_sl, target, direction)
    st = tracker._state
    st.sl = sl
    st.be_moved = (sl <= fill_price) if st.is_short else (sl >= fill_price)
    _pp = r["partial_pnl_booked"] if "partial_pnl_booked" in r.keys() else None  # noqa: SIM118
    st.partial_booked = bool(_pp)
    return tracker


async def _reseed_swing_exit_state(shared: dict[str, Any]) -> int:
    """Register every DB-open CNC/swing position's SmartExitTracker.

    swing_exit_state is in-memory only, seeded exclusively at fresh entry
    (_execute_swing_signal) — same class of bug _reseed_order_monitor fixes
    for SL/target monitoring, just for the newer partial/breakeven/trail
    layer (2026-08-21). Without this, every swing position opened before a
    restart — including every position open before this feature shipped —
    never gets partial-booked, breakeven'd, or trailed, no matter how far
    into profit it runs. Idempotent (skips trade ids already in swing_exit_state).

    Keyed by agent_trade_id, NOT symbol (found 2026-09-23: a paper and a live
    position in the same symbol collided on a shared symbol key — "trades" is
    always scanned before "paper_trades" below, so the live one always won the
    key and the paper one silently never got a tracker at all. LAURUSLABS sat
    completely unmanaged for two full sessions with zero error anywhere). A
    trade id is unique per position by construction, so any number of open
    positions sharing a symbol — paper vs live, or any other combination —
    each always get their own independent tracker. This is the general rule;
    it is not a live/paper special case.

    Peak/trough are seeded from the daily high/low since entry (not just the
    entry price) so a position already deep in profit is evaluated against
    its true run-up on the very next daily step, not treated as flat.
    """
    db_pool = shared.get("db_pool")
    strategy_reg = shared.get("strategy_registry")
    ohlcv_repo = shared.get("ohlcv_repo")
    if db_pool is None or strategy_reg is None or ohlcv_repo is None:
        return 0

    from src.strategies.uss.compiler import SmartExitTracker as _SET
    from src.strategies.uss.schema import Direction

    existing: dict[str, Any] = shared.setdefault("swing_exit_state", {})
    added = 0
    # Phase 2: a CNC/swing position can be open in EITHER table depending on its
    # own segment mode — always check both (idempotent per-trade-id via `existing`,
    # same simplify-don't-branch approach as the reconciliation held-symbol merge).
    for table in ("trades", "paper_trades"):
        try:
            rows = await db_pool.fetch(
                f"SELECT agent_trade_id, symbol, direction, fill_price, "  # noqa: S608
                f"initial_sl, partial_pnl_booked, current_sl, current_target, strategy_name, "
                f"entry_timestamp FROM {table} "
                f"WHERE product = 'CNC' "
                f"AND order_state NOT IN ('CLOSED','REJECTED','CANCELLED','TIMED_OUT') "
                f"AND fill_price IS NOT NULL"
            )
        except Exception as exc:
            _log.error("swing_exit_reseed_fetch_err table=%s err=%s", table, str(exc)[:160])
            continue

        for r in rows:
            tid = r["agent_trade_id"]
            sym = r["symbol"]
            if not tid or tid in existing:
                continue
            # get(), not all_enabled(): an open position must keep its own SL/target/
            # trailing management regardless of whether its strategy is currently
            # enabled for NEW entries — enabled/disabled only gates fresh signals,
            # never abandons risk management on capital already at risk. Only a
            # strategy whose JSON file is gone entirely (not just disabled) has no
            # spec to rebuild the tracker from.
            _sname = r["strategy_name"] or ""
            _spec_fn = getattr(strategy_reg, "spec_for_open_positions", None)
            compiled = _spec_fn(_sname) if _spec_fn is not None else strategy_reg.get(_sname)
            if compiled is None:
                # Capital at risk with no exit management: loud, alerted (deduped). The
                # reseed runs every 30s, so the log line is once per trade id.
                _warned_s: set[str] = shared.setdefault("_swing_no_spec_warned", set())
                if tid in _warned_s:
                    continue
                _warned_s.add(tid)
                _log.warning(
                    "swing_exit_reseed_no_spec tid=%s symbol=%s strategy=%s -- position has "
                    "NO trailing/time-stop management, only its resting stop",
                    tid,
                    sym,
                    _sname,
                )
                from src.infrastructure.operator_alerts import raise_alert as _op_alert

                _op_alert(
                    f"Swing position {sym} ({tid}) has no exit management: strategy "
                    f"'{_sname}' spec not found. Only its resting stop protects it.",
                    source=f"swing_exit_no_spec:{tid}",
                    dedupe_seconds=6 * 3600,
                )
                continue
            fill_price = float(r["fill_price"] or 0.0)
            sl = float(r["current_sl"] or 0.0)
            target = float(r["current_target"] or 0.0)
            if fill_price <= 0 or sl <= 0:
                _log.debug(
                    "swing_exit_reseed_bad_data tid=%s symbol=%s fill=%s sl=%s",
                    tid,
                    sym,
                    fill_price,
                    sl,
                )
                continue
            direction = (
                Direction.LONG if str(r["direction"] or "").lower() == "long" else Direction.SHORT
            )
            peak = trough = fill_price
            bars_since_entry = 0
            try:
                df = await ohlcv_repo.fetch_dataframe(sym.split(":")[-1], "1d", 400)
                _entry_ts = r["entry_timestamp"]
                if df is not None and not df.empty and _entry_ts is not None:
                    since_entry = df[df.index.date >= _entry_ts.date()]
                    if not since_entry.empty:
                        peak = max(fill_price, float(since_entry["high"].max()))
                        trough = min(fill_price, float(since_entry["low"].min()))
                        bars_since_entry = _daily_bars_after_entry(
                            since_entry.index.date, _entry_ts.date()
                        )
            except Exception as exc:
                _log.warning("swing_exit_reseed_history_err sym=%s err=%s", sym, str(exc)[:80])
            tracker = _tracker_from_db_row(
                _SET, compiled.spec, r, fill_price, sl, target, direction
            )
            tracker._state.peak = peak
            tracker._state.trough = trough
            # time_stop_days counts daily bars held SINCE ENTRY. The tracker's own counter
            # starts at 0 when it is built, so without this every restart reset the clock
            # and a 20/60-day time stop could never fire (found 2026-10-06, RADICO).
            tracker._bars = bars_since_entry
            existing[tid] = {"tracker": tracker, "tid": tid, "symbol": sym, "table": table}
            added += 1
            _log.debug("swing_exit_reseed_added tid=%s symbol=%s table=%s", tid, sym, table)
    if added:
        _log.warning("swing_exit_state_reseeded count=%d", added)
    return added


@strategy_logged(fixed="llm_intraday_agent")
async def _reseed_llm_intraday_ideas(shared: dict[str, Any], now: Any = None) -> int:
    """Restore still-fresh llm_intraday_agent ideas from the journal on restart.

    shared["llm_intraday_ideas"] is in-memory only, and the launcher restarts
    the orchestrator whenever its heartbeat watchdog decides it's hung (see
    orchestrator_hung_restarting in launcher.py) -- 2026-09-23: a swing-chain
    LLM call ran long enough to trip that watchdog mid-session, the restart
    silently erased two just-proposed intraday ideas (IIFL, AEGISVOPAK) before
    the 5-min scan ever got to evaluate them, and nothing logged that loss --
    a query would have shown them sitting in llm_intraday_journal the whole
    time. Same bug class as OrderMonitor never reseeding from DB on restart
    (fixed 2026-08-17); the DB was already the source of truth, it just wasn't
    being read back.

    llm_intraday_journal.insert_run() persists ideas_json = the exact dicts
    published to shared["llm_intraday_ideas"] (asdict(idea) + atr14_pct + ts +
    slot -- see _run_llm_intraday_pass), so this can restore the store
    byte-for-byte rather than reconstructing it. Only ideas still within
    idea_ttl_minutes of their OWN per-idea `ts` are restored -- an idea that
    would have expired anyway during the outage is correctly left out, not
    resurrected with a stale clock. When a symbol appears in more than one of
    today's runs, the newest one wins, matching the live store's own
    overwrite-on-republish behaviour.

    A rehydrated idea still goes through the SAME entry checks a live one
    would (TTL, entry-drift, still_valid_at re-validation, already-held guard,
    max_trades_reached) before it can ever place an order -- this only
    restores the candidate, it does not shortcut anything downstream.

    `now` is injectable for tests (defaults to real IST wall-clock time in
    production, like _evaluate_llm_intraday_signal's own `now` parameter).
    """
    journal_repo = shared.get("llm_intraday_journal_repo")
    if journal_repo is None:
        return 0
    try:
        from datetime import datetime as _reseed_dt
        from zoneinfo import ZoneInfo as _reseed_tz

        cfg = _cfg("global").get("llm_intraday", {})
        ttl_min = float(cfg.get("idea_ttl_minutes", 15))
        _now_ist = now if now is not None else _reseed_dt.now(_reseed_tz("Asia/Kolkata"))
        now_ts = _now_ist.timestamp()

        rows = await journal_repo.todays_runs(_now_ist.date())
        store: dict[str, Any] = shared.setdefault("llm_intraday_ideas", {})
        restored: dict[str, float] = {}  # symbol -> age_min, for the log line
        for row in rows:  # oldest first (todays_runs orders by run_ts) -> newest wins
            ideas = row["ideas_json"] or []
            if isinstance(ideas, str):
                ideas = json.loads(ideas)
            for idea in ideas:
                symbol = idea.get("symbol")
                ts = idea.get("ts")
                if not symbol or ts is None:
                    continue
                age_min = (now_ts - float(ts)) / 60.0
                if age_min > ttl_min:
                    continue
                store[symbol] = idea
                restored[symbol] = age_min
        if restored:
            # Logged here (not just via the call site's count) because WHICH
            # symbols and how stale they are is exactly the detail a restart
            # investigation needs -- a bare count would repeat this incident's
            # own "no visibility" complaint one level up.
            _intraday_log.warning(
                "llm_intraday_ideas_startup_reseed count=%d ages_min=%s",
                len(restored),
                {k: round(v, 1) for k, v in restored.items()},
            )
        return len(restored)
    except Exception as exc:  # best-effort; never block boot
        _intraday_log.error("llm_intraday_ideas_reseed_err err=%s", exc)
        return 0


async def _reseed_intraday_exit_state(shared: dict[str, Any]) -> int:
    """Register every DB-open MIS/intraday position's SmartExitTracker.

    intraday_exit_state is in-memory only, seeded exclusively at fresh entry
    (the intraday signal-placement block) — same class of bug
    _reseed_swing_exit_state fixes for CNC/swing, just for the intraday
    partial(1R)/breakeven(1R)/ATR-trail(1R) layer. The orchestrator restarts
    routinely (config reloads, crashes, manual bounces) — every restart between
    an intraday entry and its exit silently drops that position's tracker, so
    it never gets partial-booked, breakeven'd, or trailed again for the rest
    of the day, no matter how far into profit it runs; only the hard SL/target
    (via order_monitor, which IS reseeded) still protects it. Idempotent
    (skips symbols already in intraday_exit_state).

    Same-day only, matching /api/positions' intraday same-day filter — a MIS
    row from a prior day is a reconciliation anomaly, not a live position to
    re-arm. Peak/trough are seeded from the 5-min run-up since entry (not just
    the entry price) so a position already deep in profit on reseed is
    evaluated against its true run-up on the very next 5-min step, not treated
    as flat.
    """
    db_pool = shared.get("db_pool")
    strategy_reg = shared.get("strategy_registry")
    ohlcv_repo = shared.get("ohlcv_repo")
    if db_pool is None or strategy_reg is None or ohlcv_repo is None:
        return 0

    from src.strategies.uss.compiler import SmartExitTracker as _SET
    from src.strategies.uss.schema import Direction

    existing: dict[str, Any] = shared.setdefault("intraday_exit_state", {})
    added = 0
    for table in ("trades", "paper_trades"):
        try:
            rows = await db_pool.fetch(
                f"SELECT agent_trade_id, symbol, direction, fill_price, "  # noqa: S608
                f"initial_sl, partial_pnl_booked, current_sl, current_target, strategy_name, "
                f"entry_timestamp FROM {table} "
                f"WHERE product = 'MIS' "
                f"AND order_state NOT IN ('CLOSED','REJECTED','CANCELLED','TIMED_OUT') "
                f"AND fill_price IS NOT NULL "
                f"AND (entry_timestamp AT TIME ZONE 'Asia/Kolkata')::date "
                f"= (NOW() AT TIME ZONE 'Asia/Kolkata')::date"
            )
        except Exception as exc:
            _log.error("intraday_exit_reseed_fetch_err table=%s err=%s", table, str(exc)[:160])
            continue

        for r in rows:
            sym = r["symbol"]
            if sym in existing:
                continue
            # get(), not all_enabled() — see _reseed_swing_exit_state: an open
            # position keeps its own risk management regardless of whether its
            # strategy is currently enabled for new entries.
            _iname = r["strategy_name"] or ""
            _ispec_fn = getattr(strategy_reg, "spec_for_open_positions", None)
            compiled = _ispec_fn(_iname) if _ispec_fn is not None else strategy_reg.get(_iname)
            if compiled is None:
                _warned_i: set[str] = shared.setdefault("_intraday_no_spec_warned", set())
                if r["agent_trade_id"] not in _warned_i:
                    _warned_i.add(r["agent_trade_id"])
                    _log.warning(
                        "intraday_exit_reseed_no_spec tid=%s strategy=%s -- no smart-exit "
                        "management, only its resting stop",
                        r["agent_trade_id"],
                        _iname,
                    )
                continue
            fill_price = float(r["fill_price"] or 0.0)
            sl = float(r["current_sl"] or 0.0)
            target = float(r["current_target"] or 0.0)
            if fill_price <= 0 or sl <= 0:
                continue
            direction = (
                Direction.LONG if str(r["direction"] or "").lower() == "long" else Direction.SHORT
            )
            peak = trough = fill_price
            try:
                df = await ohlcv_repo.fetch_dataframe(sym.split(":")[-1], "5m", 100)
                _entry_ts = r["entry_timestamp"]
                if df is not None and not df.empty and _entry_ts is not None:
                    since_entry = df[df.index >= _entry_ts]
                    if not since_entry.empty:
                        peak = max(fill_price, float(since_entry["high"].max()))
                        trough = min(fill_price, float(since_entry["low"].min()))
            except Exception as exc:
                _log.debug("intraday_exit_reseed_history_err sym=%s err=%s", sym, str(exc)[:80])
            tracker = _tracker_from_db_row(
                _SET, compiled.spec, r, fill_price, sl, target, direction
            )
            tracker._state.peak = peak
            tracker._state.trough = trough
            existing[sym] = {"tracker": tracker, "tid": r["agent_trade_id"]}
            added += 1
    if added:
        _log.warning("intraday_exit_state_reseeded count=%d", added)
    return added


def _swing_trace(
    tid: str, ex: dict[str, Any], ltp: float, monitor_pos: Any, mon_idx: int, phase: str
) -> None:
    """Step-by-step DEBUG trace of every profit-trailing check for ONE tracked swing
    trade: how it was found, price/R, partial, breakeven, trail, decision, next action.
    Read-only — computes from the tracker state, never mutates it."""
    st = ex["tracker"]._state
    sym = ex["symbol"]
    d = -1.0 if st.is_short else 1.0
    risk = st.initial_risk
    head = f"swing_trace tid={tid} sym={sym} phase={phase}"
    _log.debug(
        "%s step=1_lookup table=%s monitor_idx=%s monitor_found=%s monitor_sl=%s "
        "tracker_sl=%.2f sl_drift=%s",
        head,
        ex.get("table", "fresh_entry"),
        mon_idx,
        monitor_pos is not None,
        f"{float(monitor_pos.current_sl):.2f}" if monitor_pos is not None else "n/a",
        st.sl,
        (f"{float(monitor_pos.current_sl) - st.sl:+.2f}" if monitor_pos is not None else "n/a"),
    )
    if risk <= 0:
        _log.debug("%s step=2_price ABORT initial_risk=%s (invalid)", head, risk)
        return
    pnl_r = d * (ltp - st.entry) / risk if ltp > 0 else None
    peak_ref = st.trough if st.is_short else st.peak
    peak_r = d * (peak_ref - st.entry) / risk
    _log.debug(
        "%s step=2_price side=%s ltp=%s entry=%.2f initial_sl=%.2f initial_risk=%.2f "
        "target=%.2f pnl_r=%s peak=%.2f peak_r=%.2f",
        head,
        "SHORT" if st.is_short else "LONG",
        f"{ltp:.2f}" if ltp > 0 else "unavailable",
        st.entry,
        st.entry - d * risk,
        risk,
        st.target,
        f"{pnl_r:+.2f}R" if pnl_r is not None else "n/a",
        peak_ref,
        peak_r,
    )
    plan: list[str] = []
    for step, name, r_val, done in (
        ("3_partial", "partial_book", st.partial_r, st.partial_booked),
        ("4_breakeven", "breakeven", st.be_r, st.be_moved),
    ):
        if r_val is None:
            _log.debug("%s step=%s result=NOT_CONFIGURED", head, step)
            continue
        lvl = st.entry + d * r_val * risk
        if done:
            _log.debug(
                "%s step=%s trigger_r=%.2f level=%.2f result=ALREADY_DONE", head, step, r_val, lvl
            )
            continue
        gap = d * (lvl - ltp) if ltp > 0 else None
        hit = gap is not None and gap <= 0
        _log.debug(
            "%s step=%s trigger_r=%.2f level=%.2f ltp=%s gap_to_level=%s result=%s",
            head,
            step,
            r_val,
            lvl,
            f"{ltp:.2f}" if ltp > 0 else "n/a",
            f"{gap:+.2f}" if gap is not None else "n/a",
            "HIT" if hit else "NOT_HIT",
        )
        plan.append(
            f"{name}:{'FIRE_NEXT_LIVE_CYCLE' if hit and phase == 'market_open' else 'fires_when_ltp_reaches_' + format(lvl, '.2f')}"
        )
    if st.trail is None:
        _log.debug("%s step=5_trail result=NOT_CONFIGURED", head)
    else:
        armed = peak_r >= st.activation_r
        _log.debug(
            "%s step=5_trail method=%s activation_r=%.2f peak_r=%.2f armed=%s "
            "current_sl=%.2f note=trail_value_is_recomputed_by_DAILY_step_only",
            head,
            getattr(st.trail.method, "value", st.trail.method),
            st.activation_r,
            peak_r,
            armed,
            st.sl,
        )
        plan.append(
            "trail:armed_awaiting_daily_step"
            if armed
            else f"trail:arms_at_peak_r_{st.activation_r:.2f}"
        )
    _log.debug(
        "%s step=6_decision action=%s next=[%s]",
        head,
        "NONE_MARKET_CLOSED(live checks paused until 09:15 IST; any HIT above fires on first open cycle)"
        if phase == "market_closed"
        else "see_step_3_4_results(HIT fires this cycle, NOT_HIT waits)",
        " | ".join(plan) or "none",
    )


async def _check_swing_live_triggers(
    shared: dict[str, Any], order_monitors: list[Any] | None = None
) -> None:
    """Live, market-hours-continuous partial-book/breakeven check for every open
    swing position — called every _monitor_loop cycle (~2s), same cadence as
    SL/target breach detection.

    2026-08-21: the trail VALUE (chandelier/indicator) is legitimately a daily-
    bar concept and stays computed once/day in _swing_signal_scan_loop — but
    WHETHER a position has already reached its partial-book or breakeven
    threshold must never wait for that once-daily step. A position already
    deep in profit intraday has to act now, not at 15:45 (or later, if the
    process happens to be mid-restart at 15:45) — same principle already
    applied to SL/target breach, just not extended to this trigger until now.
    """
    swing_exit_state: dict[str, Any] = shared.get("swing_exit_state") or {}
    # Phase 2: a swing position's tid may live in EITHER the paper or the live
    # OrderMonitor depending on that trade's own segment mode — search every
    # active stack rather than assuming the single "primary" one (falls back to
    # shared["order_monitor"] for any caller not yet stack-aware).
    _monitors_raw = order_monitors if order_monitors else [shared.get("order_monitor")]
    _monitors: list[Any] = [m for m in _monitors_raw if m is not None]
    broker = shared.get("broker")
    if not swing_exit_state or not _monitors or broker is None:
        if swing_exit_state:
            # Positions ARE being tracked but can't be checked this cycle — worth
            # flagging every time (rare state, unlike the common "nothing open"
            # case below which isn't logged at all to avoid 2s-cycle spam).
            _log.warning(
                "swing_live_check_skip tracked_count=%d monitors=%d has_broker=%s",
                len(swing_exit_state),
                len(_monitors),
                broker is not None,
            )
        return
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    _now = _dt.now(_ZI("Asia/Kolkata"))
    _mins = _now.hour * 60 + _now.minute
    # Outside 09:15-15:30, broker.get_ltp() just returns the last traded price
    # (yesterday's close, or a stale pre-market quote) — reacting to that isn't
    # "live during market hours," it's firing on stale data before the session
    # has even opened (found 2026-08-21: partial booked at 07:47 IST, 90+
    # minutes before open, off the prior close).
    if not (is_trading_day(_now.date()) and _sessions.in_regular_minutes(_mins)):
        # Previously silent — made this visible (2026-09-23) because "is it even
        # looking at this position?" had no log to answer with outside market
        # hours, only code to read. Rate-limited to ~once/minute (not every 2s
        # cycle) via the second-of-minute check.
        if _now.second < 2:
            _log.debug(
                "swing_live_check_market_closed tracked_count=%d symbols=%s "
                "reason=%s live_partial_breakeven_checks_resume_at_market_open",
                len(swing_exit_state),
                ",".join(sorted({ex["symbol"] for ex in swing_exit_state.values()})),
                "not_a_trading_day" if not is_trading_day(_now.date()) else "outside_09:15-15:30",
            )
            # Display-only price (never acted on outside market hours) so the
            # trace shows exactly where each trade stands against its triggers.
            try:
                _closed_ltp = await broker.get_ltp(
                    list({ex["symbol"] for ex in swing_exit_state.values()})
                )
            except Exception as exc:
                _closed_ltp = {}
                _log.warning("swing_trace_closed_ltp_err err=%s", str(exc)[:120])
            for _ttid, _tex in list(swing_exit_state.items()):
                _tpos, _tidx = None, -1
                for _i, _om in enumerate(_monitors):
                    _tpos = _om.get(_ttid)
                    if _tpos is not None:
                        _tidx = _i
                        break
                _swing_trace(
                    _ttid,
                    _tex,
                    float(_closed_ltp.get(_tex["symbol"], 0.0)),
                    _tpos,
                    _tidx,
                    "market_closed",
                )
        return
    symbols = list({ex["symbol"] for ex in swing_exit_state.values()})
    try:
        ltp_map = await broker.get_ltp(symbols)
    except Exception as exc:
        # Previously silent — a broker LTP failure here means EVERY tracked swing
        # position skips its live check for this whole cycle with no trace at all.
        _log.warning("swing_live_check_ltp_fetch_err symbols=%s err=%s", symbols, str(exc)[:160])
        return
    for tid in list(swing_exit_state.keys()):
        ex = swing_exit_state.get(tid)
        if ex is None:
            continue
        sym = ex["symbol"]
        ltp = ltp_map.get(sym, 0.0)
        if ltp <= 0:
            _log.debug("swing_live_check_no_ltp symbol=%s tid=%s", sym, tid)
            continue
        tracker = ex["tracker"]
        pos = None
        order_monitor = None
        _mon_idx = -1
        for _i, _om in enumerate(_monitors):
            pos = _om.get(tid)
            if pos is not None:
                order_monitor = _om
                _mon_idx = _i
                break
        if _now.second % 10 < 2:
            _swing_trace(tid, ex, ltp, pos, _mon_idx, "market_open")
        if pos is None or order_monitor is None:
            _log.warning(
                "swing_live_check_drop symbol=%s tid=%s reason=not_found_in_any_monitor",
                sym,
                tid,
            )
            swing_exit_state.pop(tid, None)
            continue
        state = tracker._state
        risk = state.initial_risk
        if risk <= 0:
            _log.warning(
                "swing_live_check_bad_risk symbol=%s tid=%s initial_risk=%s", sym, tid, risk
            )
            continue
        is_short = state.is_short

        if state.partial_r is not None and not state.partial_booked:
            lvl = (
                (state.entry - state.partial_r * risk)
                if is_short
                else (state.entry + state.partial_r * risk)
            )
            hit = (ltp <= lvl) if is_short else (ltp >= lvl)
            if hit:
                state.partial_booked = True
                new_sl = min(state.sl, state.entry) if is_short else max(state.sl, state.entry)
                if new_sl != state.sl:
                    state.sl = new_sl
                    state.be_moved = True
                    await order_monitor.update_sl(tid, new_sl)
                await order_monitor.book_partial(tid, 0.5, ltp)
                _log.warning(
                    "swing_live_partial_book symbol=%s tid=%s ltp=%.2f new_sl=%.2f",
                    sym,
                    tid,
                    ltp,
                    state.sl,
                )
                continue  # breakeven already folded into the partial above

        if state.be_r is not None and not state.be_moved:
            lvl = (
                (state.entry - state.be_r * risk) if is_short else (state.entry + state.be_r * risk)
            )
            hit = (ltp <= lvl) if is_short else (ltp >= lvl)
            if hit:
                state.be_moved = True
                new_sl = min(state.sl, state.entry) if is_short else max(state.sl, state.entry)
                if new_sl != state.sl:
                    state.sl = new_sl
                    await order_monitor.update_sl(tid, new_sl)
                _log.warning("swing_live_breakeven symbol=%s tid=%s ltp=%.2f", sym, tid, ltp)


async def _process_manual_order_actions(
    order_monitor: Any,
    db_pool: Any,
    broker: Any,
    all_order_monitors: list[Any] | None = None,
) -> None:
    """Execute dashboard-queued manual actions (Market Exit button) against the
    live OrderMonitor.

    Root cause fixed 2026-08-17: the dashboard's Market Exit/Modify Order
    buttons only mutated local frontend state (resolveBreachEvent in
    advancedStore.ts) — no backend call existed at all, so the position was
    never actually touched and the next breach poll just showed the same live
    breach again. This is the real channel: POST /api/orders/{id}/force-exit
    (server.py) inserts a PENDING row here; this picks it up every monitor
    cycle (~2s) since server.py has no direct reference to order_monitor.

    Called once per stack (each _monitor_loop) with that stack's monitor and broker (the
    LTP source). A stack that does not track the trade leaves the row PENDING when another
    monitor in `all_order_monitors` does -- that stack's loop executes it; FAILED only
    when no stack tracks it.
    """
    if db_pool is None:
        return
    try:
        rows = await db_pool.fetch(
            "SELECT id, agent_trade_id, action FROM manual_order_actions "
            "WHERE status = 'PENDING' ORDER BY requested_at LIMIT 20"
        )
    except Exception as exc:
        _log.error("manual_order_action_fetch_err err=%s", str(exc)[:160])
        return

    for r in rows:
        _id, tid, action = r["id"], r["agent_trade_id"], r["action"]
        pos = order_monitor.get(tid)
        if pos is None:
            if any(
                m is not order_monitor and m.get(tid) is not None
                for m in (all_order_monitors or [])
            ):
                continue  # another stack owns it; left PENDING for that stack's loop
            await db_pool.execute(
                "UPDATE manual_order_actions SET status='FAILED', "
                "result='not_tracked_or_already_closed', resolved_at=NOW() WHERE id=$1",
                _id,
            )
            continue
        if action != "MARKET_EXIT":
            await db_pool.execute(
                "UPDATE manual_order_actions SET status='FAILED', "
                "result='unsupported_action', resolved_at=NOW() WHERE id=$1",
                _id,
            )
            continue
        try:
            _ltp = float((await broker.get_ltp([pos.symbol])).get(pos.symbol, pos.current_sl))
        except Exception:
            _ltp = pos.current_sl
        try:
            await order_monitor.force_exit(tid, _ltp, "MANUAL_MARKET_EXIT")
            await db_pool.execute(
                "UPDATE manual_order_actions SET status='DONE', "
                "result=$2, resolved_at=NOW() WHERE id=$1",
                _id,
                f"exited_at_{_ltp:.2f}",
            )
            _log.warning(
                "manual_market_exit_executed tid=%s symbol=%s price=%.2f", tid, pos.symbol, _ltp
            )
        except Exception as exc:
            await db_pool.execute(
                "UPDATE manual_order_actions SET status='FAILED', "
                "result=$2, resolved_at=NOW() WHERE id=$1",
                _id,
                str(exc)[:200],
            )


async def _monitor_loop(
    order_monitor: Any,
    shared: dict[str, Any],
    stop: asyncio.Event,
    trade_repo: Any = None,
    price_truth: Any = None,
    cycle_seconds: float = 2.0,
    is_paper: bool | None = None,
    all_order_monitors: list[Any] | None = None,
    lifecycle: Any = None,
    broker: Any = None,
) -> None:
    """Cycle OrderMonitor every `cycle_seconds` (execution.json monitoring.cycle_seconds,
    2026-08-20 — was hardcoded); drain closed_trades into shared + logs + DB.
    Runs Price Truth Phase 2 sweep every 30 s against all open monitored positions."""
    import time as _time

    _closed_log = _LOGS / "closed_trades.json"
    _pt2_counter = 0  # incremented every cycle; sweep fires every ~30s
    _pt2_cycles = max(1, round(30.0 / cycle_seconds))
    _rate_window: list[float] = []  # cycle timestamps in trailing 60 s → checks/min
    _lat_ema = 0.0
    _actions_total = 0
    while not stop.is_set():
        try:
            _t0 = _time.monotonic()
            events = await order_monitor.cycle()
            # Resting entry orders — re-polled against live LTP on the same cadence as
            # filled positions. order_monitor.cycle() only ever walks filled positions,
            # so without this an unfilled entry is never looked at again.
            if lifecycle is not None:
                try:
                    for _tid in await lifecycle.poll_pending_entries():
                        _rec = lifecycle._trades.get(_tid)
                        _tmpl = getattr(_rec, "monitor_template", None)
                        if _rec is None or _tmpl is None:
                            continue
                        _tmpl.quantity = int(_rec.filled_quantity or _tmpl.quantity)
                        _tmpl.fill_price = float(_rec.fill_price)
                        # poll() re-anchors SL/target to the actual fill before we get here.
                        _tmpl.initial_sl = _tmpl.current_sl = float(_rec.sl_price)
                        _tmpl.initial_target = _tmpl.current_target = float(_rec.target_price)
                        # Armed by poll_pending_entries a moment ago — without carrying
                        # the ids the position is monitored but its real exit legs are
                        # invisible to trailing and the GTT integrity check.
                        _tmpl.sl_order_id = _rec.sl_order_id or ""
                        _tmpl.target_order_id = _rec.target_order_id or ""
                        _tmpl.gtt_sl_id = _rec.gtt_sl_id or ""
                        _tmpl.gtt_target_id = _rec.gtt_sl_id or ""
                        order_monitor.track(_tmpl)
                        # A resting AMO swing entry fills here, not in
                        # _execute_swing_signal, so seed its SmartExitTracker too —
                        # otherwise it is never breakeven'd/trailed/partial-booked
                        # until the next reseed (LAURUSLABS, 2026-09-16).
                        _late_reg = shared.get("strategy_registry")
                        _late_compiled = (
                            _late_reg.get(_tmpl.strategy_name or "")
                            if _late_reg is not None and _tmpl.product == "CNC"
                            else None
                        )
                        _late_state = shared.setdefault("swing_exit_state", {})
                        if _late_compiled is not None and _tid not in _late_state:
                            from src.strategies.uss.compiler import SmartExitTracker as _SET
                            from src.strategies.uss.schema import Direction as _Dir

                            _late_state[_tid] = {
                                "tracker": _SET(
                                    _late_compiled.spec,
                                    _tmpl.fill_price,
                                    _tmpl.current_sl,
                                    _tmpl.current_target,
                                    _Dir.SHORT
                                    if str(_tmpl.direction).lower() in ("short", "sell")
                                    else _Dir.LONG,
                                ),
                                "tid": _tid,
                                "symbol": _tmpl.symbol,
                            }
                        _log.info(
                            "late_entry_fill_tracked tid=%s sym=%s fill=%.2f sl=%.2f tgt=%.2f",
                            _tid,
                            _tmpl.symbol,
                            _tmpl.fill_price,
                            _tmpl.current_sl,
                            _tmpl.current_target,
                        )
                except Exception as _pe_exc:
                    _log.debug("pending_entry_cycle_failed err=%s", _pe_exc)
            _lat = (_time.monotonic() - _t0) * 1000.0
            _lat_ema = _lat if _lat_ema == 0 else _lat_ema * 0.8 + _lat * 0.2
            if events:
                await order_monitor.execute_events(events)
                _actions_total += len(events)
            await _check_swing_live_triggers(shared, all_order_monitors)
            # LTP from THIS stack's broker (the one holding its positions); shared["broker"]
            # is the primary (paper under --paper), kept only as the legacy fallback.
            await _process_manual_order_actions(
                order_monitor,
                shared.get("db_pool"),
                broker if broker is not None else shared.get("broker"),
                all_order_monitors=all_order_monitors,
            )
            # NOTE: paper SL/target close now runs through order_monitor.cycle() above
            # (one wiring) — the paper adapter simulates the exit fill. No separate
            # paper monitor loop.
            _now_m = _time.monotonic()
            _rate_window.append(_now_m)
            _rate_window[:] = [t for t in _rate_window if _now_m - t <= 60.0]
            # order_monitor_stats is one shared row (id=1) across every stack, but each
            # mode (paper/live) runs its OWN _monitor_loop with its OWN OrderMonitor —
            # passing only this loop's order_monitor here made whichever stack wrote
            # last clobber the other's count, so "Orders Watched" only ever showed one
            # stack's positions (found 2026-09-02: dashboard showed 2 — the live count —
            # while paper's 7 monitored positions were invisible). Aggregate across every
            # active stack's OrderMonitor so the dashboard reflects everything actually
            # being watched, not just whichever loop happened to write most recently.
            _stats_monitors = all_order_monitors or [order_monitor]
            await _persist_monitor_telemetry(
                shared.get("db_pool"),
                [s for m in _stats_monitors for s in m.breach_snapshots],
                sum(len(m.get_monitored()) for m in _stats_monitors),
                len(_rate_window),
                int(_lat_ema),
                _actions_total,
                cycle_seconds,
            )
            if order_monitor.closed_trades:
                _drained = order_monitor.closed_trades
                order_monitor.closed_trades = []
                # is_paper is this loop's own stack mode when provided (Phase 2,
                # one _monitor_loop task per active paper/live stack) — falls back
                # to the global flag only for any caller not yet stack-aware.
                _batch_is_paper = (
                    is_paper if is_paper is not None else bool(shared.get("paper_mode", True))
                )
                shared.setdefault("closed_trades", []).extend(_drained)
                _closed_log.write_text(
                    json.dumps(shared["closed_trades"], indent=2), encoding="utf-8"
                )
                if trade_repo is not None:
                    await _persist_closed_trades(trade_repo, _drained, is_paper=_batch_is_paper)
                from src.notifications import EventKind, notify
                from src.notifications.events import segment_for_product

                for _ct in _drained:
                    try:
                        _ct_payload = {
                            k: v
                            for k, v in _ct.items()
                            if k not in ("agent_trade_id", "symbol", "product")
                        }
                        notify(
                            EventKind.EXIT,
                            is_paper=_batch_is_paper,
                            agent_trade_id=str(_ct.get("agent_trade_id", "")),
                            symbol=str(_ct.get("symbol", "")),
                            segment=segment_for_product(str(_ct.get("product", "CNC"))),
                            dedupe_key=f"exit:{_ct.get('agent_trade_id')}",
                            **_ct_payload,
                        )
                    except Exception:  # notify() must never fail the monitor loop
                        pass
            _pt2_counter += 1
            if price_truth is not None and _pt2_counter >= _pt2_cycles:
                _pt2_counter = 0
                monitored = order_monitor.get_monitored()
                if monitored:
                    await _price_truth_phase2_sweep(price_truth, monitored, trade_repo)
        except Exception as exc:
            _log.error("monitor_loop_error err=%s", exc)
        await asyncio.sleep(cycle_seconds)


async def _reload_loop(strategy_reg: Any, shared: dict[str, Any], stop: asyncio.Event) -> None:
    """Reload strategy files from disk every 5 min (picks up new agent-created strategies)."""
    while not stop.is_set():
        await asyncio.sleep(300.0)
        try:
            strategy_reg.reload()
            _log.info("strategies_reloaded count=%d", len(strategy_reg.all_enabled()))
            _pool = shared.get("db_pool")
            if _pool is not None:
                await strategy_reg.persist_to_db(_pool)
        except Exception as exc:
            _log.warning("strategy_reload_failed err=%s", exc)


async def _breadth_for(strategies: list[Any], pool: Any, lookback_days: int = 800) -> Any:
    """Breadth series for MarketFilter(mode="breadth"), fetched once per pass and only
    when some strategy uses it. None when unused (gate inactive anyway); an empty
    series when the DB is unavailable, which the gate treats as BLOCK (fail-closed)."""
    import pandas as _pd

    periods = {
        int(c.spec.market_filter.sma_period)
        for c in strategies
        if getattr(c.spec, "market_filter", None) is not None
        and c.spec.market_filter.enabled
        and c.spec.market_filter.mode == "breadth"
    }
    if not periods:
        return None
    if pool is None:
        _log.error("breadth_no_db_pool — breadth-gated strategies will not enter")
        return _pd.Series(dtype=float)
    from src.market_data.breadth import fetch_breadth_series

    if len(periods) > 1:
        _log.warning("breadth_multiple_sma_periods %s — using %d", sorted(periods), min(periods))
    return await fetch_breadth_series(pool, sma_period=min(periods), lookback_days=lookback_days)


def _backtest_one_symbol(
    compiled: Any,
    sym: str,
    df: Any,
    engine: Any,
    bt_logger: Any,
    IST: Any,
) -> tuple[list[Any], list[dict[str, Any]], bool]:
    """Backtest ONE (strategy, symbol) pair. Thread-safe — no shared state.

    Returns (signals, trade_rows, had_signals). This is the unit of parallelism:
    every (strategy × symbol) pair is dispatched to its own thread so nothing
    runs sequentially.
    """
    name = compiled.spec.meta.name
    sig_list: list[Any] = []
    trade_rows: list[dict[str, Any]] = []
    # Per-thread isolation: engine.run() MUTATES the compiled object (cached masks,
    # _df_ref, market context) for the df it's given. Sharing one compiled across
    # parallel symbol threads races and corrupts those caches → iloc out-of-bounds.
    # A fresh CompiledStrategy per (strategy, symbol) is cheap and fully isolated.
    from src.strategies.uss.compiler import CompiledStrategy

    _mkt_df = getattr(compiled, "_market_df", None)  # capture from original FIRST
    _breadth = getattr(compiled, "_breadth", None)
    compiled = CompiledStrategy(spec=compiled.spec)
    try:
        compiled.set_market_context(_mkt_df)
        compiled.set_breadth_context(_breadth)
    except Exception:
        pass
    try:
        bt = engine.run(compiled, df)
        if bt.signals:
            sig_list = list(bt.signals)
            for _s in bt.signals:
                _et = df.index[_s.bar_index]
                _xt = df.index[_s.exit_bar] if _s.exit_bar is not None else None
                _sign = 1.0 if _s.direction.value == "LONG" else -1.0
                _pct = (_s.exit_price / _s.entry_price - 1) * 100 * _sign if _s.exit_price else 0.0
                trade_rows.append(
                    {
                        "symbol": sym,
                        "direction": _s.direction.value,
                        "entry_time": (
                            _et.tz_convert(IST).strftime("%Y-%m-%d %H:%M")
                            if hasattr(_et, "tz_convert")
                            else str(_et)
                        ),
                        "entry": round(_s.entry_price, 2),
                        "sl": round(_s.sl, 2),
                        "target": round(_s.target, 2),
                        "exit_time": (
                            _xt.tz_convert(IST).strftime("%Y-%m-%d %H:%M")
                            if (_xt is not None and hasattr(_xt, "tz_convert"))
                            else "-"
                        ),
                        "exit": round(_s.exit_price, 2) if _s.exit_price else None,
                        "exit_reason": _s.exit_reason,
                        "pnl_r": round(_s.pnl_r, 3) if _s.pnl_r is not None else None,
                        "pct": round(_pct, 2),
                    }
                )
            bt_logger.info(
                "backtest_symbol",
                extra={
                    "ctx": {
                        "strategy": name,
                        "symbol": sym,
                        "trades": len(bt.signals),
                        "win_rate": round(bt.win_rate, 4),
                        "pf": round(min(bt.profit_factor, 99.0), 3),
                        "expectancy_r": round(bt.expectancy_r, 3),
                    }
                },
            )
        else:
            try:
                n_true = int(compiled._entry_mask(df).sum())
            except Exception:
                n_true = -1
            bt_logger.info(
                "backtest_symbol_no_signals",
                extra={
                    "ctx": {"strategy": name, "symbol": sym, "mask_hits": n_true, "bars": len(df)}
                },
            )
    except Exception as _e:
        bt_logger.error(
            "backtest_symbol_error",
            extra={"ctx": {"strategy": name, "symbol": sym, "err": str(_e)[:120]}},
        )
    return sig_list, trade_rows, bool(sig_list)


def _aggregate_strategy(
    compiled: Any,
    all_signals: list[Any],
    strat_trades: list[dict[str, Any]],
    syms_with_signals: int,
    total_syms: int,
    engine: Any,
    bt_logger: Any,
    now: Any,
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Aggregate per-symbol results for ONE strategy into a summary row."""
    name = compiled.spec.meta.name
    agg = engine._score(name, all_signals) if all_signals else None
    row: dict[str, Any] = {
        "display_name": compiled.spec.meta.display_name,
        "direction": compiled.spec.meta.direction.value,
        "total_trades": agg.total_trades if agg else 0,
        "long_trades": agg.long_trades if agg else 0,
        "short_trades": agg.short_trades if agg else 0,
        "win_rate": round(agg.win_rate, 4) if agg else 0.0,
        "profit_factor": round(min(agg.profit_factor, 99.0), 3) if agg else 0.0,
        "expectancy_r": round(agg.expectancy_r, 3) if agg else 0.0,
        "avg_win_r": round(agg.avg_win_r, 3) if agg else 0.0,
        "avg_loss_r": round(agg.avg_loss_r, 3) if agg else 0.0,
        "max_drawdown_r": round(agg.max_drawdown_r, 3) if agg else 0.0,
        "symbols_with_signals": syms_with_signals,
        "symbols_tested": total_syms,
        "passed": agg.passed if agg else False,
        "reject_reason": agg.reject_reason if agg else "no_signals",
        "computed_at": now.isoformat(),
    }
    level = "info" if row["passed"] else "warning"
    getattr(bt_logger, level)(
        "backtest_strategy_result",
        extra={
            "ctx": {
                "strategy": name,
                "status": "PASS" if row["passed"] else "FAIL",
                "reject_reason": row["reject_reason"],
                "trades": row["total_trades"],
                "win_rate": row["win_rate"],
                "pf": row["profit_factor"],
                "expectancy_r": row["expectancy_r"],
                "avg_win_r": row["avg_win_r"],
                "avg_loss_r": row["avg_loss_r"],
                "max_dd_r": row["max_drawdown_r"],
                "symbols_with_signals": syms_with_signals,
                "symbols_tested": total_syms,
            }
        },
    )
    return name, row, strat_trades[-500:]


def _load_backtest_max_parallel_pairs() -> int:
    """execution.json backtest.max_parallel_pairs — see that key's _note.

    Bounds concurrent (strategy x symbol) backtests so the nightly sweep cannot
    starve the event loop that runs live order monitoring.
    """
    try:
        _cfg = json.loads((_CONFIG / "execution.json").read_text(encoding="utf-8"))
        return max(1, int(_cfg.get("backtest", {}).get("max_parallel_pairs", 4)))
    except Exception:
        return 4


_BACKTEST_MAX_PARALLEL_PAIRS = _load_backtest_max_parallel_pairs()

# Max concurrent OHLCV fetches during a backtest pass. The candle loads below used
# asyncio.gather over the WHOLE universe (~490 symbols) at once against a DB pool of
# max_size=20, so every other pool consumer — including _heartbeat_loop — queued
# behind hundreds of waiters. The engine process stayed alive but stopped beating,
# /api/agent_status saw a 181s-old heartbeat against its 45s threshold, and the
# dashboard correctly showed "Agent Stopped" during every startup backtest
# (2026-09-11). Bounding the CPU pairs alone (see _BACKTEST_MAX_PARALLEL_PAIRS) did
# not cover this: that guard is on engine.run(), not on the fetches feeding it.
# 8 keeps well clear of the pool so live monitoring and heartbeats always get a slot.
_BACKTEST_MAX_PARALLEL_FETCHES = 8


async def _backtest_loop(
    strategy_reg: Any,
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """At startup + nightly 22:00: load strategies with backtest. Writes logs/backtest_summary.json."""
    import json as _json
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    from src.strategies.backtest.engine import BacktestEngine

    _IST = _ZI("Asia/Kolkata")
    _bt_log = _LOGS / "backtest_summary.json"
    _bt_trades_log = _LOGS / "backtest_trades.json"
    _engine = BacktestEngine()
    _last_run_date = None
    _ran_heavy_once = False
    # Persistent across iterations so an intraday-only cycle doesn't wipe the
    # daily/weekly cards written during the last heavy run.
    summary: dict[str, Any] = {}
    trades_detail: dict[str, Any] = {}

    while not stop.is_set():
        now = _dt.now(_IST)
        today = now.date()
        # DAILY + WEEKLY + INTRADAY backtests all run at startup + nightly 22:00 only.
        # During market hours we SCAN for live signals (see _intraday_signal_scan_loop)
        # and write logs/intraday_signals.json — we do NOT re-backtest. Re-running the
        # ORB basket every 60s during the session added nothing to live trading.
        _run_heavy = not _ran_heavy_once or (
            _last_run_date != today
            and now.hour == _sessions.schedule_time("heavy_learning_run").hour
            and now.minute < 5
        )
        _mins = now.hour * 60 + now.minute
        _in_market_hours = is_trading_day(now.date()) and _sessions.in_regular_minutes(_mins)
        if True:
            try:
                prices_df, _ = await _load_ohlcv_prices_db(shared)
                if prices_df is None or prices_df.empty:
                    await asyncio.sleep(60.0)
                    continue

                import pandas as _pd

                from src.infrastructure.logger import get_logger as _get_logger

                _ohlcv_repo = shared.get("ohlcv_repo")
                _bt_log_writer = _get_logger("backtest")

                # Reload registry (compile only, no backtest at registry level)
                strategy_reg.reload(df=None)
                all_strategies = strategy_reg.all_enabled()
                _bt_breadth = await _breadth_for(
                    all_strategies, shared.get("db_pool"), lookback_days=2500
                )
                for _c in all_strategies:
                    _set_breadth = getattr(_c, "set_breadth_context", None)
                    if _set_breadth is not None:
                        _set_breadth(_bt_breadth)

                async def _run_pass(strategy_list: list[Any], sym_dfs: list[Any]) -> None:
                    """Backtest EVERY (strategy × symbol) pair fully in parallel.

                    Each pair runs in its own thread (engine.run releases the GIL during
                    pandas/numpy C work), so neither strategies nor symbols run
                    sequentially. Results are grouped per strategy and aggregated.
                    """
                    if not strategy_list or not sym_dfs:
                        return
                    _pairs = [
                        (c, sym, df)
                        for c in strategy_list
                        for sym, df in sym_dfs
                        if not c.spec.universe_filter.symbols
                        or sym in c.spec.universe_filter.symbols
                    ]
                    # Bounded, NOT all-at-once. engine.run() is pandas/numpy work that
                    # holds the GIL in bursts; dispatching every pair (10 strategies x
                    # ~500 symbols = thousands of to_thread tasks) starved the event
                    # loop so hard the 2s order monitor fell to one cycle per ~24s and
                    # the DB pool began timing out (INV_C8 "DB unreachable", 2026-08-28
                    # 21:31). Live SL/target monitoring must never lose the loop to a
                    # background backtest.
                    _sem = asyncio.Semaphore(_BACKTEST_MAX_PARALLEL_PAIRS)

                    async def _bt_pair(c: Any, sym: str, df: Any) -> Any:
                        async with _sem:
                            return await asyncio.to_thread(
                                _backtest_one_symbol, c, sym, df, _engine, _bt_log_writer, _IST
                            )

                    _pair_results = await asyncio.gather(
                        *[_bt_pair(c, sym, df) for c, sym, df in _pairs]
                    )
                    # group per strategy (preserve strategy order)
                    _by_strat: dict[str, dict[str, Any]] = {}
                    for (c, _sym, _df), (sigs, trows, had) in zip(
                        _pairs, _pair_results, strict=False
                    ):
                        _b = _by_strat.setdefault(
                            c.spec.meta.name,
                            {"compiled": c, "signals": [], "trades": [], "with_sig": 0},
                        )
                        _b["signals"].extend(sigs)
                        _b["trades"].extend(trows)
                        if had:
                            _b["with_sig"] += 1
                    for _b in _by_strat.values():
                        _name, _row, _trades = _aggregate_strategy(
                            _b["compiled"],
                            _b["signals"],
                            _b["trades"],
                            _b["with_sig"],
                            len(sym_dfs),
                            _engine,
                            _bt_log_writer,
                            now,
                        )
                        summary[_name] = _row
                        trades_detail[_name] = _trades

                # ---- DAILY timeframe pass (heavy — startup + nightly only) ----
                # Same market-hours guard as the intraday pass below (_run_intraday_bt):
                # a process restart during the live session must NOT trigger a heavy
                # 500-symbol backtest mid-market — that's live-scan time, not backtest
                # time. Without this, every dev/launcher restart between 09:15-15:30
                # re-ran the full DAILY validation pass (seen 2026-07-30 at 09:10/09:22/
                # 09:29 IST), competing with live intraday scanning for no benefit.
                if _run_heavy and not _in_market_hours:

                    async def _fetch_1d(sym: str) -> tuple[str, Any] | None:
                        try:
                            # 500 bars: matches the live swing scan's fetch window.
                            df = (
                                await _ohlcv_repo.fetch_dataframe(sym, "1d", 500)
                                if _ohlcv_repo
                                else _pd.DataFrame()
                            )
                            if (
                                not df.empty
                                and {"open", "high", "low", "close"}.issubset(df.columns)
                                and len(df) >= 50
                            ):
                                return (sym, df)
                        except Exception:
                            pass
                        return None

                    compiled_strategies = [
                        c
                        for c in all_strategies
                        if (getattr(c.spec.meta, "timeframe", "DAILY") or "DAILY").upper()
                        == "DAILY"
                    ]

                    # Per-strategy universe: a DAILY strategy declaring universe_filter.index_filter
                    # (e.g. minervini_vcp/oneil_canslim -> NIFTY500) must be validated against THAT
                    # universe, not the global NIFTY50 watchlist — otherwise the PASS/FAIL feeding
                    # CRM Gate 6 (strategy_health) is computed from a sample the strategy barely
                    # trades. Mirrors the resolution in _swing_signal_scan_loop.
                    default_universe = list(prices_df.columns)
                    daily_strategy_universes: dict[str, list[str]] = {}
                    for c in compiled_strategies:
                        _idx = getattr(
                            getattr(c.spec, "universe_filter", None), "index_filter", None
                        )
                        daily_strategy_universes[c.spec.meta.name] = (
                            _resolve_index_universe(_idx[0]) if _idx else default_universe
                        )

                    _needed_syms: set[str] = set(default_universe)
                    for _syms in daily_strategy_universes.values():
                        _needed_syms.update(_syms)

                    _fetch_sem_1d = asyncio.Semaphore(_BACKTEST_MAX_PARALLEL_FETCHES)

                    async def _fetch_1d_bounded(sym: str) -> Any:
                        async with _fetch_sem_1d:
                            return await _fetch_1d(sym)

                    _raw = await asyncio.gather(
                        *[_fetch_1d_bounded(sym) for sym in sorted(_needed_syms)]
                    )
                    frames_by_sym: dict[str, _pd.DataFrame] = dict(r for r in _raw if r is not None)

                    _log.info(
                        "backtest_reload strategies=%d symbols=%d",
                        len(compiled_strategies),
                        len(frames_by_sym),
                    )
                    for c in compiled_strategies:
                        _own_dfs = [
                            (sym, frames_by_sym[sym])
                            for sym in daily_strategy_universes[c.spec.meta.name]
                            if sym in frames_by_sym
                        ]
                        await _run_pass([c], _own_dfs)

                # Intraday (5MIN/15MIN) pass — load 5m candles per symbol so the
                # ORB/intraday Strategy Studio cards show real WR/PF/E + long/short
                # instead of a permanent "Backtesting on startup…" placeholder.
                intraday_strategies = [
                    c
                    for c in all_strategies
                    if (getattr(c.spec.meta, "timeframe", "") or "").upper()
                    in ("5MIN", "15MIN", "5M", "15M")
                    and "NFO" not in (c.spec.universe_filter.exchanges or [])
                ]
                # NFO strategies (index options structures) declare their own exchange
                # correctly in universe_filter.exchanges, but nothing ever read it before
                # this -- they were silently backtested against individual stocks' 5m
                # candles, which their option-chain entry logic can never fire against.
                # That isn't "no edge", it's the harness testing the wrong instrument
                # class entirely. No options-aware backtest engine exists yet
                # (src/strategies/backtest/engine.py is single-instrument-OHLC only) --
                # surface that honestly instead of a misleading 0%/"no signals" verdict.
                options_strategies = [
                    c
                    for c in all_strategies
                    if (getattr(c.spec.meta, "timeframe", "") or "").upper()
                    in ("5MIN", "15MIN", "5M", "15M")
                    and "NFO" in (c.spec.universe_filter.exchanges or [])
                ]
                if _run_heavy and not _in_market_hours:
                    for _oc in options_strategies:
                        summary[_oc.spec.meta.name] = {
                            "display_name": _oc.spec.meta.display_name,
                            "direction": _oc.spec.meta.direction.value,
                            "total_trades": 0,
                            "long_trades": 0,
                            "short_trades": 0,
                            "win_rate": 0.0,
                            "profit_factor": 0.0,
                            "expectancy_r": 0.0,
                            "avg_win_r": 0.0,
                            "avg_loss_r": 0.0,
                            "max_drawdown_r": 0.0,
                            "symbols_with_signals": 0,
                            "symbols_tested": 0,
                            "passed": False,
                            "not_applicable": True,
                            "reject_reason": (
                                "options structure (NFO) -- no options-aware backtest "
                                "engine exists yet; not tested against the stock universe"
                            ),
                            "computed_at": now.isoformat(),
                        }
                        trades_detail[_oc.spec.meta.name] = []
                # Intraday backtest: startup + nightly only, and NEVER during market
                # hours (09:15–15:30). Live signal generation is handled by the scan loop.
                # Log the decision every cycle so it is unambiguous WHEN it runs vs blocks.
                _run_intraday_bt = bool(intraday_strategies) and _run_heavy and not _in_market_hours
                if intraday_strategies:
                    if _run_intraday_bt:
                        _log.info(
                            "backtest_intraday_RUN reason=%s run_heavy=True in_market_hours=False "
                            "time=%s strategies=%d",
                            ("nightly" if _ran_heavy_once else "startup"),
                            now.strftime("%H:%M"),
                            len(intraday_strategies),
                        )
                    else:
                        _reason = (
                            "market_hours(09:15-15:30)"
                            if _in_market_hours
                            else "not_scheduled(startup+nightly only)"
                        )
                        _log.info(
                            "backtest_intraday_BLOCKED reason=%s run_heavy=%s in_market_hours=%s "
                            "time=%s",
                            _reason,
                            _run_heavy,
                            _in_market_hours,
                            now.strftime("%H:%M"),
                        )
                if _run_intraday_bt:
                    _ohlcv_repo_i = shared.get("ohlcv_repo")
                    # Attach the benchmark (e.g. NIFTY 50) so the market-direction
                    # gate applies in the startup backtest. Missing data → fail-open.
                    _bench_cache_bt: dict[str, Any] = {}
                    for compiled in intraday_strategies:
                        _mf = getattr(compiled.spec, "market_filter", None)
                        if (
                            _mf is not None
                            and getattr(_mf, "enabled", False)
                            and _ohlcv_repo_i is not None
                        ):
                            _bsym = _mf.symbol
                            if _bsym not in _bench_cache_bt:
                                try:
                                    _bench_cache_bt[_bsym] = await _ohlcv_repo_i.fetch_dataframe(
                                        _bsym, "5m", 2500
                                    )
                                except Exception:
                                    _bench_cache_bt[_bsym] = None
                            _bdf = _bench_cache_bt.get(_bsym)
                            if _bdf is None or _bdf.empty:
                                _log.warning(
                                    "backtest_intraday: benchmark %s 5m unavailable — "
                                    "market-direction gate is OPEN for %s",
                                    _bsym,
                                    compiled.spec.meta.name,
                                )
                            compiled.set_market_context(_bdf)

                    async def _fetch_5m(sym: str) -> tuple[str, Any] | None:
                        try:
                            df5 = (
                                await _ohlcv_repo_i.fetch_dataframe(sym, "5m", 2500)
                                if _ohlcv_repo_i
                                else _pd.DataFrame()
                            )
                            if (
                                not df5.empty
                                and {"open", "high", "low", "close"}.issubset(df5.columns)
                                and len(df5) >= 100
                            ):
                                return (sym, df5)
                        except Exception:
                            pass
                        return None

                    _fetch_sem_5m = asyncio.Semaphore(_BACKTEST_MAX_PARALLEL_FETCHES)

                    async def _fetch_5m_bounded(sym: str) -> Any:
                        async with _fetch_sem_5m:
                            return await _fetch_5m(sym)

                    _raw5 = await asyncio.gather(
                        *[_fetch_5m_bounded(sym) for sym in prices_df.columns]
                    )
                    symbol_dfs_5m: list[tuple[str, _pd.DataFrame]] = [
                        r for r in _raw5 if r is not None
                    ]
                    _log.info(
                        "backtest_intraday strategies=%d symbols=%d",
                        len(intraday_strategies),
                        len(symbol_dfs_5m),
                    )
                    if symbol_dfs_5m:
                        await _run_pass(intraday_strategies, symbol_dfs_5m)

                # Weekly timeframe pass — load weekly candles for WEEKLY strategies
                # (e.g. Weinstein Stage 2) so their cards show real metrics instead
                # of a permanent "Backtesting on startup…" placeholder.
                weekly_strategies = [
                    c
                    for c in all_strategies
                    if (getattr(c.spec.meta, "timeframe", "") or "").upper() in ("WEEKLY", "1W")
                ]
                if _run_heavy and not _in_market_hours and weekly_strategies:
                    _ohlcv_repo_w = shared.get("ohlcv_repo")

                    async def _fetch_1w(sym: str) -> tuple[str, Any] | None:
                        try:
                            dfw = (
                                await _ohlcv_repo_w.fetch_dataframe(sym, "1w", 300)
                                if _ohlcv_repo_w
                                else _pd.DataFrame()
                            )
                            if (
                                not dfw.empty
                                and {"open", "high", "low", "close"}.issubset(dfw.columns)
                                and len(dfw) >= 30
                            ):
                                return (sym, dfw)
                        except Exception:
                            pass
                        return None

                    _fetch_sem_1w = asyncio.Semaphore(_BACKTEST_MAX_PARALLEL_FETCHES)

                    async def _fetch_1w_bounded(sym: str) -> Any:
                        async with _fetch_sem_1w:
                            return await _fetch_1w(sym)

                    _raw1w = await asyncio.gather(
                        *[_fetch_1w_bounded(sym) for sym in prices_df.columns]
                    )
                    symbol_dfs_1w: list[tuple[str, _pd.DataFrame]] = [
                        r for r in _raw1w if r is not None
                    ]
                    _log.info(
                        "backtest_weekly strategies=%d symbols=%d",
                        len(weekly_strategies),
                        len(symbol_dfs_1w),
                    )
                    if symbol_dfs_1w:
                        await _run_pass(weekly_strategies, symbol_dfs_1w)

                tmp = _bt_log.with_suffix(".tmp")
                tmp.write_text(_json.dumps(summary, indent=2), encoding="utf-8")
                tmp.replace(_bt_log)
                # Expose in-memory so live signal construction can gate on backtest
                # pass/fail (CRM G6 backtest-quality override) without a file re-read.
                shared["backtest_summary"] = summary
                tmp_t = _bt_trades_log.with_suffix(".tmp")
                tmp_t.write_text(_json.dumps(trades_detail, indent=2), encoding="utf-8")
                tmp_t.replace(_bt_trades_log)
                if _run_heavy and not _in_market_hours:
                    _last_run_date = today
                    _ran_heavy_once = True
                _bt_log_writer.info(
                    "backtest_complete",
                    extra={"ctx": {"strategies": len(summary), "heavy": _run_heavy}},
                )
            except Exception as exc:
                _log.warning("backtest_loop_error err=%s", exc)
                _ran_heavy_once = True
        # Loop wakes every 60s but only does work at startup/nightly (heavy) — all
        # backtests (daily/weekly/intraday) are gated off during market hours.
        await asyncio.sleep(60.0)


async def _discovery_loop(
    strategy_reg: Any,
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Every 5 min: scan strategy dirs, auto-register new/changed files. Writes logs/discovery_scan.json."""
    import json as _json
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    from src.strategies.discovery import StrategyDiscoveryEngine

    _IST = _ZI("Asia/Kolkata")
    _disc_log = _LOGS / "discovery_scan.json"
    engine = StrategyDiscoveryEngine(
        registry=strategy_reg,
        dirs=[
            _BASE / "strategies" / "builtin",
            _BASE / "strategies" / "agent_created",
        ],
    )

    while not stop.is_set():
        try:
            result = engine.scan(df=None, skip_backtest=True)
            payload = {
                "accepted": [r.strategy_name for r in result.accepted],
                "rejected": [
                    {"name": r.strategy_name, "reason": r.reason} for r in result.rejected
                ],
                "unchanged": len(result.unchanged),
                "scanned_at": _dt.now(_IST).isoformat(),
            }
            tmp = _disc_log.with_suffix(".tmp")
            tmp.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(_disc_log)
            shared["discovery_scan"] = payload
            if result.accepted:
                _log.info(
                    "discovery_accepted strategies=%s", [r.strategy_name for r in result.accepted]
                )
        except Exception as exc:
            _log.warning("discovery_loop_error err=%s", exc)
        await asyncio.sleep(300.0)


async def _options_structure_exit_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    """Every 15 minutes: sweep OPEN buyer-first (structure_selector.py)
    options_structures rows for a close condition — Options learning-engine
    PR5. Coarser cadence than seller-desk's 5-minute sweep since these
    positions have no active Greeks-breach management, just a resting SL and
    a stated target. Gated by config/learning.json
    options_structure_exit_tracking.mode: "off" = doesn't run at all (today's
    behavior — options_structures rows stay OPEN forever). "shadow" = runs,
    makes read-only broker order-status polls, logs would-be close decisions,
    writes nothing. "live" = actually closes TARGET_REACHED structures at
    market and persists real outcomes for both exit reasons.
    """
    from src.options.structure_monitor import evaluate_structures

    while not stop.is_set():
        try:
            mode = _learning_mode("options_structure_exit_tracking")
            opt = shared.get("options")
            if mode in ("shadow", "live") and opt is not None:
                repo = opt.get("repo")
                broker = opt.get("broker")
                executor = opt.get("executor")
                if repo is not None and broker is not None:
                    open_rows = [dict(r) for r in await repo.load_open_structures()]
                    if open_rows:
                        sl_statuses: dict[str, str] = {}
                        for row in open_rows:
                            sl_id = row.get("sl_order_id")
                            if sl_id:
                                try:
                                    order = await broker.get_order_status(sl_id)
                                    sl_statuses[str(sl_id)] = str(getattr(order, "status", ""))
                                except Exception as _se:
                                    _log.debug(
                                        "options_exit_sl_status_err order_id=%s err=%s",
                                        sl_id,
                                        str(_se)[:80],
                                    )
                        symbols = {str(r["symbol"]) for r in open_rows if r.get("symbol")}
                        current_prices: dict[str, float] = {}
                        if symbols:
                            try:
                                current_prices = await broker.get_ltp(list(symbols))
                            except Exception as _le:
                                _log.debug("options_exit_ltp_err err=%s", str(_le)[:80])

                        decisions = evaluate_structures(open_rows, current_prices, sl_statuses)
                        rows_by_id = {str(r["structure_id"]): r for r in open_rows}
                        for d in decisions:
                            if mode == "shadow":
                                _log.info(
                                    "options_structure_exit_shadow structure_id=%s "
                                    "exit_reason=%s realized_pnl=%s",
                                    d.structure_id,
                                    d.exit_reason,
                                    d.realized_pnl,
                                )
                                continue
                            # live
                            realized_pnl = d.realized_pnl
                            if d.needs_market_close and executor is not None:
                                matched_row = rows_by_id.get(d.structure_id)
                                legs = matched_row.get("legs") if matched_row else None
                                if isinstance(legs, str):
                                    import json as _json_legs

                                    legs = _json_legs.loads(legs)
                                realized_pnl = await executor.close_structure_at_market(
                                    legs or [],
                                    matched_row.get("sl_order_id") if matched_row else None,
                                    d.structure_id,
                                )
                                if (
                                    matched_row
                                    and matched_row.get("structure_type") == "protective_put"
                                ):
                                    _log.warning(
                                        "options_structure_exit_protective_put_equity_leg_"
                                        "untracked structure_id=%s — closed the put leg only; "
                                        "the equity leg needs separate/manual handling "
                                        "(never persisted to options_structures)",
                                        d.structure_id,
                                    )
                            try:
                                await repo.close_structure(
                                    d.structure_id,
                                    exit_reason=d.exit_reason,
                                    realized_pnl=realized_pnl,
                                )
                                _log.info(
                                    "options_structure_exit_closed structure_id=%s "
                                    "exit_reason=%s realized_pnl=%s",
                                    d.structure_id,
                                    d.exit_reason,
                                    realized_pnl,
                                )
                            except Exception as _ce:
                                _log.warning(
                                    "options_structure_exit_close_persist_err structure_id=%s "
                                    "err=%s",
                                    d.structure_id,
                                    str(_ce)[:80],
                                )
        except Exception as exc:
            _log.warning("options_structure_exit_loop_error err=%s", exc)
        await asyncio.sleep(900.0)


_OPTIONS_CONVICTION_CLAMP_DEFAULT = (0.45, 0.70)
_OPTIONS_CONVICTION_FLOOR_RANGE = (0.30, 0.60)
_OPTIONS_CONVICTION_CEILING_RANGE = (0.60, 0.85)
_OPTIONS_CONVICTION_ADJUST_STEP = 0.03
_OPTIONS_CONVICTION_DECAY_STEP = 0.01
_OPTIONS_CONVICTION_TRIGGER_COUNT = 10


def _adjust_conviction_bounds(
    prior_bounds: tuple[float, float], result: Any
) -> tuple[float, float]:
    """One calibration-cycle adjustment to a structure_type's (p_win_floor,
    p_win_ceiling) pair, given a src.llm.calibration.CalibrationResult.

    Mirrors equity's calibration_thresholds adjustment shape (raise/lower by a
    fixed step, decay back toward the default when not miscalibrated) but
    applied to a bound PAIR instead of a single floor:
    - miscalibrated: tighten the ceiling when the worst-gap bucket's actual
      win rate came in BELOW what was predicted (structure winning less often
      than conviction implied); raise the floor when it came in ABOVE
      (winning more often than conviction implied — floor was too low).
    - not miscalibrated: decay both bounds one step back toward the original
      hardcoded (0.45, 0.70) defaults — "un-punish once behavior normalizes",
      same spirit as equity's own decay.
    Both bounds are always clamped to their sane absolute ranges and the
    ceiling is never allowed to sit at or below the floor.
    """
    lo, hi = prior_bounds
    if result.overall_miscalibrated:
        worst_bucket = max(result.buckets, key=lambda b: b.gap, default=None)
        if worst_bucket is not None:
            if worst_bucket.actual_win_rate < worst_bucket.predicted_confidence:
                hi -= _OPTIONS_CONVICTION_ADJUST_STEP
            else:
                lo += _OPTIONS_CONVICTION_ADJUST_STEP
    else:
        default_lo, default_hi = _OPTIONS_CONVICTION_CLAMP_DEFAULT
        if lo > default_lo:
            lo = max(default_lo, lo - _OPTIONS_CONVICTION_DECAY_STEP)
        if hi < default_hi:
            hi = min(default_hi, hi + _OPTIONS_CONVICTION_DECAY_STEP)
    lo = max(_OPTIONS_CONVICTION_FLOOR_RANGE[0], min(lo, _OPTIONS_CONVICTION_FLOOR_RANGE[1]))
    hi = max(_OPTIONS_CONVICTION_CEILING_RANGE[0], min(hi, _OPTIONS_CONVICTION_CEILING_RANGE[1]))
    hi = max(hi, lo + 0.01)  # never inverted
    return lo, hi


async def _options_conviction_calibration_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    """Options learning-engine PR6 — the actual conviction-routing fix.

    Count-triggered, not calendar-triggered (checked every 30 min, fires per
    structure_type once it has accumulated _OPTIONS_CONVICTION_TRIGGER_COUNT
    newly-closed structures since its last evaluation) — options trade volume
    is far lower than equity's, so a fixed weekly cadence would either
    evaluate on too little data or wait arbitrarily long past the sample
    being ready.

    Requires config/learning.json options_structure_exit_tracking.mode=="live"
    to have produced real closed structures with entry_conviction/realized_pnl
    (PR5) — with nothing closed yet, this loop simply has no data and does
    nothing, which is safe and correct, not an error.

    Gated by options_conviction_calibration.mode: "off" = doesn't run at all.
    "shadow"/"live" both compute+persist the same (p_win_floor, p_win_ceiling,
    brier_score) — the DIFFERENCE is entirely in structure_selector.py's
    _p_win()/_bounds_for(), which only ever USES the persisted bounds when
    mode=="live"; this loop's own persistence is identical either way so the
    bounds are ready the moment an operator flips the mode.
    """
    from src.llm.calibration import CalibrationEngine

    _last_evaluated_count: dict[tuple[str, bool], int] = {}

    while not stop.is_set():
        try:
            mode = _learning_mode("options_conviction_calibration")
            opt = shared.get("options")
            if mode in ("shadow", "live") and opt is not None:
                repo = opt.get("repo")
                _opt_is_paper = _segment_mode(shared, "options") == "paper"
                if repo is not None:
                    for structure_type in (
                        "long_call",
                        "bull_call_spread",
                        "protective_put",
                    ):
                        rows = await repo.load_closed_structures_with_outcome(
                            structure_type, is_paper=_opt_is_paper
                        )
                        rows = [
                            r
                            for r in rows
                            if r.get("entry_conviction") is not None
                            and r.get("realized_pnl") is not None
                        ]
                        prev_count = _last_evaluated_count.get((structure_type, _opt_is_paper), 0)
                        if len(rows) - prev_count < _OPTIONS_CONVICTION_TRIGGER_COUNT:
                            continue
                        _last_evaluated_count[(structure_type, _opt_is_paper)] = len(rows)

                        engine = CalibrationEngine()
                        for r in rows:
                            engine.add(
                                structure_type,
                                llm_confidence=float(r["entry_conviction"]),
                                was_win=float(r["realized_pnl"]) > 0,
                            )
                        result = engine.evaluate(structure_type)

                        existing = await repo.load_conviction_calibration(is_paper=_opt_is_paper)
                        prior_bounds = existing.get(
                            structure_type, _OPTIONS_CONVICTION_CLAMP_DEFAULT
                        )
                        lo, hi = _adjust_conviction_bounds(prior_bounds, result)

                        await repo.upsert_conviction_calibration(
                            structure_type,
                            lo,
                            hi,
                            len(rows),
                            result.brier_score,
                            result.worst_gap,
                            is_paper=_opt_is_paper,
                        )
                        try:
                            await repo.insert_learning_finding(
                                structure_type,
                                "conviction_calibration",
                                f"p_win bounds ({lo:.3f}, {hi:.3f}) from {len(rows)} closed "
                                f"structures, brier={result.brier_score:.3f} "
                                f"worst_gap={result.worst_gap:.3f} "
                                f"miscalibrated={result.overall_miscalibrated}",
                                result.brier_score,
                            )
                        except Exception:
                            pass
                        _log.info(
                            "options_conviction_calibration structure_type=%s "
                            "bounds=(%.3f,%.3f) n=%d mode=%s",
                            structure_type,
                            lo,
                            hi,
                            len(rows),
                            mode,
                        )
        except Exception as exc:
            _log.warning("options_conviction_calibration_loop_error err=%s", exc)
        await asyncio.sleep(1800.0)


async def _expectancy_resume_backtest_passed(shared: dict[str, Any], strategy_name: str) -> bool:
    """Walk-forward (train/test) backtest for one queued strategy — the actual
    "resume only if backtest E>0" gate from spec §8.4, run on demand from
    _expectancy_loop rather than waiting for the weekly evolution cycle. Fails
    open to False (stays paused/queued) on any error — a paused strategy
    resuming because a backtest COULDN'T run would be the wrong default."""
    from src.learning.simulation_gate import SimulationGate
    from src.strategies.backtest.engine import BacktestEngine

    try:
        strategy_reg = shared.get("strategy_registry")
        compiled = strategy_reg.get(strategy_name) if strategy_reg else None
        if compiled is None:
            return False
        prices_df, _ = await _load_ohlcv_prices_db(shared)
        if prices_df is None or prices_df.empty:
            return False
        split_idx = max(1, int(len(prices_df) * 0.7))
        train_df, test_df = prices_df.iloc[:split_idx], prices_df.iloc[split_idx:]
        if len(test_df) == 0:
            return False
        bt_engine = BacktestEngine()
        bt_train = bt_engine.run(compiled, train_df)
        bt_test = bt_engine.run(compiled, test_df)
        result = SimulationGate().evaluate(
            strategy_name,
            {},
            {
                "win_rate": bt_train.win_rate,
                "profit_factor": bt_train.profit_factor,
                "expectancy_r": bt_train.expectancy_r,
                "max_drawdown": bt_train.max_drawdown_r,
            },
            {
                "win_rate": bt_test.win_rate,
                "profit_factor": bt_test.profit_factor,
                "expectancy_r": bt_test.expectancy_r,
                "max_drawdown": bt_test.max_drawdown_r,
            },
        )
        return bool(result.passed and bt_test.expectancy_r > 0)
    except Exception as exc:
        _log.warning("expectancy_resume_backtest_error strategy=%s err=%s", strategy_name, exc)
        return False


async def _evolution_loop(
    strategy_reg: Any,
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Weekly Saturday 21:00 IST: evolve DEGRADED strategies. Writes logs/evolution_report.json.

    Also sweeps every cycle (not just Saturday) for new closed trades on any
    strategy with an active (not-yet-rolled-back) promotion, so Layer 6's
    20-trade rollback evaluation (spec §9, PromotionEngine.check_rollback)
    actually advances instead of sitting permanently at 0 trades — previously
    record_trade()/check_rollback() were never called from anywhere.
    """
    import json as _json
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    from src.learning.promotion import PromotionEngine
    from src.strategies.evolution import StrategyEvolutionEngine

    _IST = _ZI("Asia/Kolkata")
    _evo_log = _LOGS / "evolution_report.json"
    _learning_repo = shared.get("learning_repo")
    promotion = PromotionEngine(repo=_learning_repo)
    await promotion.restore_from_repo()
    engine = StrategyEvolutionEngine(registry=strategy_reg, promotion=promotion)
    _last_run_week: int | None = None
    _seen_promo_trade_ids: set[str] = set()

    while not stop.is_set():
        now = _dt.now(_IST)
        iso_week = now.isocalendar()[1]

        # Per-tick rollback sweep — runs every cycle, independent of the weekly
        # evolve() branch below. Mode-aware: a promoted strategy's rollback
        # tracking must only count trades from whichever mode (paper/live)
        # that specific strategy is CURRENTLY actually running in — a strategy
        # currently live (e.g. swing) must never have its rollback decision
        # swayed by stale/simulated paper trades, and vice versa (same
        # separation principle as _fetch_closed_trades_from_db's own
        # docstring). Fetches both streams and filters each by the matching
        # strategy's own segment mode, rather than mixing them.
        _active = promotion.active_strategies()
        if _active:
            try:
                for _fetch_mode in ("live", "paper"):
                    for t in await _fetch_closed_trades_from_db(
                        shared.get("db_pool"), mode=_fetch_mode
                    ):
                        tid = str(t.get("agent_trade_id", ""))
                        strat = str(t.get("strategy_name", ""))
                        if not tid or tid in _seen_promo_trade_ids or strat not in _active:
                            continue
                        _strat_segment = _segment_for_strategy(strategy_reg, strat)
                        if _strat_segment is None:
                            continue  # can't confidently place this trade's mode — skip, don't guess
                        if _segment_mode(shared, _strat_segment) != _fetch_mode:
                            continue  # this trade's mode doesn't match the strategy's CURRENT mode
                        _seen_promo_trade_ids.add(tid)
                        rolled_back = await promotion.record_and_check_rollback(
                            strat, float(t.get("pnl_r", 0.0))
                        )
                        if rolled_back:
                            prior = promotion.prior_params(strat)
                            if prior:
                                promotion._write_config(strat, prior)  # noqa: SLF001
                                strategy_reg.reload()
                                _log.warning(
                                    "promotion_rolled_back strategy=%s reverted_to=%s",
                                    strat,
                                    prior,
                                )
            except Exception as exc:
                _log.warning("promotion_rollback_sweep_error err=%s", exc)

        if now.weekday() == 5 and now.hour == 21 and now.minute < 5 and _last_run_week != iso_week:
            try:
                health_scores = shared.get("health_scores", {})
                degraded = [s for s, h in health_scores.items() if h == "DEGRADED"]
                prices_df, _ = await _load_ohlcv_prices_db(shared)
                _dry_run = _learning_mode("promotion") != "live"
                report: list[dict[str, Any]] = []
                for strat_name in degraded:
                    try:
                        if prices_df is None or prices_df.empty:
                            _log.warning("evolution_skipped_no_ohlcv strategy=%s", strat_name)
                            continue
                        _param_bias = None
                        if _learning_mode("failure_scorer") in ("shadow", "live"):
                            from src.learning.failure_scorer import FailureScorer, param_bias_for

                            _recent = [
                                t
                                for t in await _fetch_closed_trades_from_db(
                                    shared.get("db_pool"), mode="live"
                                )
                                if str(t.get("strategy_name", "")) == strat_name
                            ]
                            _report = FailureScorer().score(strat_name, _recent)
                            _mutable = engine.mutable_params(strategy_reg.get(strat_name).spec)
                            _bias = param_bias_for(_report.dominant_failure, list(_mutable))
                            _log.info(
                                "failure_scorer strategy=%s dominant=%s bias=%s mode=%s",
                                strat_name,
                                _report.dominant_failure,
                                _bias,
                                _learning_mode("failure_scorer"),
                            )
                            if _learning_mode("failure_scorer") == "live":
                                _param_bias = _bias
                        result = engine.evolve(
                            strat_name,
                            prices_df,
                            baseline_wr=0.40,
                            dry_run=_dry_run,
                            use_simulation_gate=True,
                            param_bias=_param_bias,
                        )
                        if result.promoted:
                            await promotion.persist_promotion(strat_name)
                        report.append(
                            {
                                "strategy": result.strategy_name,
                                "promoted": result.promoted,
                                "reason": result.reason,
                                "best_expectancy_r": (
                                    round(result.winner.backtest_result.expectancy_r, 4)
                                    if result.winner
                                    else 0.0
                                ),
                            }
                        )
                    except Exception as exc:
                        _log.warning("evolution_strategy_error name=%s err=%s", strat_name, exc)
                payload: dict[str, Any] = {"evolutions": report, "run_at": now.isoformat()}
                tmp = _evo_log.with_suffix(".tmp")
                tmp.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
                tmp.replace(_evo_log)
                _last_run_week = iso_week
                _log.info("evolution_complete strategies=%d dry_run=%s", len(report), _dry_run)
            except Exception as exc:
                _log.warning("evolution_loop_error err=%s", exc)
        await asyncio.sleep(300.0)


def _effective_health(
    monitor: Any,
    recovery_monitor: Any,
    name: str,
    live_trades: list[Any],
    paper_trades: list[Any],
    paper_only: bool,
    baseline: dict[str, float],
    recovery_window: int,
) -> tuple[Any, str]:
    """Health tier that G6 should see for one strategy, plus a one-line reason.

    - Baseline is the caller's (own-backtest or default) win-rate / avg-R.
    - paper_only (registry execution_mode "paper"): no real money is at risk, so it is
      scored on its PAPER record and never worse than DEGRADED — a paper strategy
      blocked from trading could never gather the evidence to recover.
    - live strategy SUSPENDED on its live record: lifted to CAUTIOUS once its last
      `recovery_window` paper trades score >= 60 (the operator gets it there by
      running it in paper mode; the live record alone can never improve while blocked).
    """
    from dataclasses import replace

    from src.monitoring.strategy_health import HealthTier, _sizing_from_tier

    trades = paper_trades if paper_only else live_trades
    health = monitor.score(name, trades, **baseline)
    basis = "paper" if paper_only else "live"
    note = f"basis={basis} trades={health.trade_count}"

    if paper_only and health.tier in (HealthTier.STRESSED, HealthTier.SUSPENDED):
        note += f" capped_from={health.tier} (paper-mode strategy is never blocked by health)"
        health = replace(
            health,
            tier=HealthTier.DEGRADED,
            sizing_multiplier=_sizing_from_tier(HealthTier.DEGRADED),
        )
    elif not paper_only and health.tier == HealthTier.SUSPENDED:
        if len(paper_trades) >= recovery_window:
            rec = recovery_monitor.score(name, paper_trades, **baseline)
            note += f" paper_recovery_score={rec.score} over_last={recovery_window}"
            if rec.score >= 60:
                note += " -> lifted to CAUTIOUS"
                health = replace(
                    health,
                    tier=HealthTier.CAUTIOUS,
                    sizing_multiplier=_sizing_from_tier(HealthTier.CAUTIOUS),
                )
        else:
            note += (
                f" paper_trades={len(paper_trades)}<{recovery_window} (no recovery evidence yet)"
            )
    return health, note


async def _health_loop(
    compiled: list[Any],
    shared: dict[str, Any],
    stop: asyncio.Event,
    health_cfg: dict[str, Any] | None = None,
) -> None:
    """Score strategy health every 5 min and publish to shared state + logs/strategy_health.json."""
    from src.monitoring.strategy_health import StrategyHealthMonitor

    _hcfg = health_cfg or {}
    monitor = StrategyHealthMonitor(
        evaluation_window_trades=int(_hcfg.get("evaluation_window_trades", 20)),
        degraded_win_rate_threshold=float(_hcfg.get("degraded_win_rate_threshold", 0.40)),
        suspended_win_rate_threshold=float(_hcfg.get("suspended_win_rate_threshold", 0.30)),
        recovery_window_trades=int(_hcfg.get("recovery_window_trades", 10)),
        min_trades_for_suspension=int(_hcfg.get("min_trades_for_suspension", 10)),
    )
    _recovery_window = int(_hcfg.get("recovery_window_trades", 10))
    _min_bt_trades = int(_hcfg.get("min_trades_for_suspension", 10))
    # Recovery is judged on the last `recovery_window` paper trades only.
    recovery_monitor = StrategyHealthMonitor(
        evaluation_window_trades=_recovery_window, min_trades_for_suspension=0
    )
    from src.monitoring.strategy_health import HealthTier
    from src.monitoring.strategy_health import TradeRecord as _TR

    health_log = _LOGS / "strategy_health.json"
    _any_stressed = False
    while not stop.is_set():
        try:
            scores: dict[str, Any] = {}
            _any_stressed = False
            _sup_repo = shared.get("supervisor_repo")
            # DB truth, live-only — was shared['closed_trades'] (in-memory, reset
            # every restart AND, worse, not paper/live-separated), same staleness
            # bug already fixed for attribution/expectancy/cost-erosion, plus the
            # same live-gating mixing bug fixed alongside it: this tier directly
            # sizes/blocks new LIVE entries (HEALTH_TIER_SIZE_MULTIPLIER / SUSPENDED
            # below), so a strategy's health must reflect its own live track
            # record, not simulated paper trades mixed in.
            closed = await _fetch_closed_trades_from_db(shared.get("db_pool"), mode="live")
            closed_paper = await _fetch_closed_trades_from_db(shared.get("db_pool"), mode="paper")

            def _trs(rows: list[dict[str, Any]], strat: str) -> list[Any]:
                return [
                    _TR(pnl_r=float(t.get("pnl_r", 0.0)))
                    for t in rows
                    if str(t.get("strategy_name", t.get("strategy", ""))) == strat
                ]

            for strategy in compiled:
                name = getattr(getattr(strategy.spec, "meta", None), "name", "unknown")
                # Baseline = the strategy's OWN backtest win-rate / expectancy when it has
                # enough trades to mean something (same floor as the suspension floor);
                # else the monitor's generic 50% / 1.5R defaults, which no real strategy is
                # promised to meet.
                _bt = shared.get("backtest_summary", {}).get(name) or {}
                if int(_bt.get("total_trades", 0) or 0) >= _min_bt_trades:
                    _baseline = {
                        "baseline_wr": float(_bt["win_rate"]),
                        "baseline_avg_r": float(_bt["expectancy_r"]),
                    }
                    _bt_note = f"own_backtest(n={_bt['total_trades']})"
                else:
                    _baseline = {}
                    _bt_note = "default_50%/1.5R (no usable backtest yet)"
                _paper_only = bool(shared.get("paper_mode_locked", False)) or (
                    shared.get("strategy_execution_mode", {}).get(name) == "paper"
                )
                health, _hnote = _effective_health(
                    monitor,
                    recovery_monitor,
                    name,
                    _trs(closed, name),
                    _trs(closed_paper, name),
                    _paper_only,
                    _baseline,
                    _recovery_window,
                )
                _log.info(
                    "health_scored_strategy strategy=%s tier=%s score=%.1f baseline=%s %s",
                    name,
                    health.tier,
                    health.score,
                    _bt_note,
                    _hnote,
                )
                scores[name] = str(health.tier)
                # §8.3: DB incidents for tier changes
                if _sup_repo is not None:
                    try:
                        if health.tier == HealthTier.DEGRADED:
                            await _sup_repo.open_incident(
                                dedup_key=f"health_degraded_{name}",
                                invariant_id="SH_DEGRADED",
                                severity="WARNING",
                                message=f"Strategy {name} DEGRADED score={health.score:.0f}",
                            )
                        elif health.tier == HealthTier.SUSPENDED:
                            await _sup_repo.open_incident(
                                dedup_key=f"health_suspended_{name}",
                                invariant_id="SH_SUSPENDED",
                                severity="CRITICAL",
                                message=f"Strategy {name} SUSPENDED score={health.score:.0f} — auto-paused",
                            )
                            shared.setdefault("paused_strategies", set()).add(name)
                        elif health.tier in (HealthTier.HEALTHY, HealthTier.CAUTIOUS):
                            await _sup_repo.resolve_incident(f"health_degraded_{name}")
                            await _sup_repo.resolve_incident(f"health_suspended_{name}")
                            shared.get("paused_strategies", set()).discard(name)
                    except Exception as _ie:
                        _log.warning(
                            "health_incident_write_failed strategy=%s tier=%s err=%s: %s",
                            name,
                            health.tier,
                            type(_ie).__name__,
                            _ie,
                        )
                # Alerts tab: say WHO suspended it, WHY (the numbers), and what unblocks it.
                _sync_scan_alert(
                    shared,
                    f"health_suspended:{name}",
                    (
                        "CRITICAL",
                        f"{name} SUSPENDED by StrategyHealthMonitor (_health_loop): score "
                        f"{health.score:.0f} < 20 from {health.trade_count} LIVE closed trades "
                        f"(win rate {health.win_rate:.0%}, avg {health.avg_r}R, "
                        f"{health.consecutive_losses} consecutive losses). CRM gate G6 blocks "
                        "new entries until the score recovers to >= 60.",
                    )
                    if health.tier == HealthTier.SUSPENDED
                    else None,
                )
                # §8.3 STRESSED → ask_mode=True
                if health.tier == HealthTier.STRESSED:
                    _any_stressed = True
            shared["health_scores"] = scores
            shared["ask_mode"] = _any_stressed
            health_log.write_text(json.dumps(scores, indent=2), encoding="utf-8")
            _log.info("health_scored strategies=%d ask_mode=%s", len(scores), _any_stressed)
        except Exception as exc:
            _log.warning("health_loop_error err=%s", exc)
        await asyncio.sleep(300.0)


async def _persist_index_intraday(
    broker: Any,
    repo: Any,
    days: int,
    *,
    broker_symbol: str = "NSE:NIFTY 50",
    db_symbol: str = "NIFTY 50",
) -> int:
    """Write the index's 5-minute bars through to the DB. Returns rows written.

    The daily bars were persisted by the regime loop but the 5m series was only ever written by a
    one-off boot backfill, so 'NIFTY 50' 5m in the DB stopped on 2026-09-10 and stayed there:
    every reader (research/backtests, any market-state feature built from stored bars) was blind
    to the last weeks. Live day-change comes from the live LTP and was unaffected."""
    import pandas as _pd_local

    fetch = getattr(broker, "get_ohlcv_with_source", None)
    if fetch is not None:
        df, source = await fetch(broker_symbol, "5minute", days)
    else:
        df, source = await broker.get_ohlcv(broker_symbol, "5minute", days), None
    if df is None or df.empty:
        return 0
    ts_col = df["date"] if "date" in df.columns else _pd_local.Series(df.index)
    rows = [
        {
            "ts": ts_col.iloc[i],
            "open": float(df["open"].iloc[i]),
            "high": float(df["high"].iloc[i]),
            "low": float(df["low"].iloc[i]),
            "close": float(df["close"].iloc[i]),
            "volume": float(df["volume"].iloc[i]) if "volume" in df.columns else 0,
        }
        for i in range(len(df))
    ]
    return int(await repo.upsert_candles(db_symbol, "5m", rows, source=source) or len(rows))


async def _regime_loop(
    broker: Any, shared: dict[str, Any], interval_s: float, stop: asyncio.Event
) -> None:
    """Detect market regime per intelligence.json interval and publish to shared state + logs/market_regime.json."""
    from src.market_data.regime_detector import RegimeDetector

    detector = RegimeDetector(log_path=_LOGS / "market_regime.json")
    while not stop.is_set():
        try:
            _fetch = getattr(broker, "get_ohlcv_with_source", None)
            if _fetch is not None:
                df, _src = await _fetch("NSE:NIFTY 50", "day", 400)
            else:
                df, _src = await broker.get_ohlcv("NSE:NIFTY 50", "day", 400), None
            # Persist the index's daily bars — nothing else in the regular
            # ingestion pipeline writes them (equities get their own backfill
            # loop; the index was never added to it), so every reader of
            # "NIFTY 50" daily OHLCV from the DB (RS ranking benchmark, swing
            # scan's set_market_context) was reading a table stuck at whatever
            # date the last one-off backfill happened to stop. This loop
            # already fetches the index fresh every cycle; just write it through.
            _repo = shared.get("ohlcv_repo")
            if _repo is not None and df is not None and not df.empty:
                try:
                    import pandas as _pd_local

                    _ts_col = df["date"] if "date" in df.columns else _pd_local.Series(df.index)
                    _rows = [
                        {
                            "ts": _ts_col.iloc[i],
                            "open": float(df["open"].iloc[i]),
                            "high": float(df["high"].iloc[i]),
                            "low": float(df["low"].iloc[i]),
                            "close": float(df["close"].iloc[i]),
                            "volume": float(df["volume"].iloc[i]) if "volume" in df.columns else 0,
                        }
                        for i in range(len(df))
                    ]
                    await _repo.upsert_candles("NIFTY 50", "1d", _rows, source=_src)
                except Exception as _npe:
                    _log.debug("nifty_daily_persist_err err=%s", str(_npe)[:80])
            if _repo is not None:
                try:
                    # first cycle of a process re-reads 20 days so any hole (a stopped service, a
                    # holiday backfill) heals itself; afterwards 3 days is plenty.
                    _n5 = await _persist_index_intraday(
                        broker, _repo, 3 if shared.get("_nifty5m_seeded") else 20
                    )
                    if _n5 and not shared.get("_nifty5m_seeded"):
                        _log.info("nifty_5m_backfilled rows=%d", _n5)
                    shared["_nifty5m_seeded"] = True
                except Exception as _n5e:  # noqa: BLE001 -- loud: a silent stop is how this went stale
                    _log.warning("nifty_5m_persist_failed err=%s", str(_n5e)[:120])
            vix_map = await broker.get_ltp(["NSE:INDIA VIX"])
            vix = float(next(iter(vix_map.values()), 15.0))
            # ad_ratio from RS ranking: leaders/(leaders+laggards), proxy for NSE A/D
            _rs_top = len(shared.get("rs_top20", []))
            _rs_black = len(shared.get("rs_blacklist", []))
            _rs_total = _rs_top + _rs_black
            ad_ratio = _rs_top / _rs_total if _rs_total > 5 else 0.5
            result = detector.detect(df, vix=vix, ad_ratio=ad_ratio)
            shared["regime"] = result.trend
            shared["vix"] = vix
            # Publish LIVE index day-change for the day-scoped intraday regime.
            # 2026-07-09 postmortem: the 5m 'NIFTY 50' candles in the DB only
            # refresh at boot backfill, so idx_chg froze at +0.47% all day and
            # the regime gate blocked the wrong direction. LTP is live truth.
            try:
                import time as _time
                from datetime import datetime as _dt
                from zoneinfo import ZoneInfo as _ZI

                import pandas as _pd

                _idx_ltp_map = await broker.get_ltp(["NSE:NIFTY 50"])
                _idx_ltp = float(next(iter(_idx_ltp_map.values()), 0.0))
                _today_ist = _dt.now(_ZI("Asia/Kolkata")).date()
                # Kite get_ohlcv returns a 'date' COLUMN on a RangeIndex (not a
                # DatetimeIndex) — 2026-07-10 assuming .index here silently broke
                # the publish and the day regime sat UNKNOWN all morning.
                _dts = _pd.to_datetime(df["date"]) if "date" in df.columns else _pd.Series(df.index)
                _mask = [_pd.Timestamp(d).date() < _today_ist for d in _dts]
                _prev_rows = df[_mask]
                if _idx_ltp > 0 and len(_prev_rows) > 0:
                    _prev_close = float(_prev_rows["close"].iloc[-1])
                    shared["index_day_change_pct"] = (_idx_ltp - _prev_close) / _prev_close * 100
                    shared["index_day_change_ts"] = _time.time()
            except Exception as _idx_exc:
                # loud on purpose — a silent pass here hid the broken feed for a day
                _log.warning("index_day_change_publish_failed err=%s", str(_idx_exc)[:120])
            # §8.1: DB write (truth store)
            _mdr = shared.get("market_data_repo")
            if _mdr is not None:
                from decimal import Decimal as _D

                _posture_multiplier = {
                    "AGGRESSIVE": 1.5,
                    "NORMAL": 1.0,
                    "DEFENSIVE": 0.5,
                    "CASH": 0.0,
                }.get(getattr(result, "recommended_posture", "NORMAL"), 1.0)
                _db_pool_r = shared.get("db_pool")
                _suitable, _avoid = await _classify_strategies_for_regime(_db_pool_r, result.trend)
                _accuracy = await _compute_regime_historical_accuracy(_db_pool_r)
                try:
                    await _mdr.log_regime(
                        regime=result.trend,
                        vix=_D(str(round(vix, 2))),
                        nifty_ma50=_D(str(round(getattr(result, "ma50", 0.0), 2))),
                        nifty_ma200=_D(str(round(getattr(result, "ma200", 0.0), 2))),
                        breadth_pct=_D(str(round(ad_ratio * 100, 2))),
                        confidence=_D(str(round(getattr(result, "confidence", 0.0), 4))),
                        adx=_D(str(round(getattr(result, "adx", 0.0), 2))),
                        atr_pct=_D(str(round(getattr(result, "atr_pct", 0.0), 3))),
                        suitable_strategies=_suitable,
                        avoid_strategies=_avoid,
                        historical_accuracy=_D(str(_accuracy)),
                        position_size_multiplier=_D(str(_posture_multiplier)),
                    )
                except Exception:
                    pass
            # §8.1: Redis write — regime_current key
            _redis = shared.get("redis_client")
            if _redis is not None:
                try:
                    await _redis.set("regime_current", result.trend, ex=3600)
                except Exception:
                    pass
            # §8.1: UNKNOWN regime — emergency flags
            if result.trend == "UNKNOWN":
                shared["emergency_exit_mis"] = True
                shared["tighten_sl_to_50pct"] = True
                _log.critical("regime_UNKNOWN — emergency_exit_mis=True tighten_sl_to_50pct=True")
            else:
                shared.pop("emergency_exit_mis", None)
                shared.pop("tighten_sl_to_50pct", None)
            _log.info(
                "regime_detected regime=%s vix=%.1f confidence=%.2f",
                result.trend,
                vix,
                getattr(result, "confidence", 0.0),
            )
        except Exception as exc:
            _log.warning(
                "regime_detection_failed err=%s — keeping %s", exc, shared.get("regime", "BULL")
            )
        await asyncio.sleep(interval_s)


async def _earnings_loop(
    calendar: Any, symbols: list[str], shared: dict[str, Any], stop: asyncio.Event
) -> None:
    """Refresh NSE earnings calendar once per day at 08:45 IST; populate shared state."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")
    last_refresh_date = None

    def _update_shared() -> None:
        earnings_map: dict[str, int] = {}
        for sym in symbols:
            clean = sym.split(":")[-1]
            days = calendar.days_to_earnings(clean)
            if days is not None:
                earnings_map[clean] = days
        shared["earnings"] = earnings_map

    _update_shared()

    while not stop.is_set():
        now = datetime.now(_IST)
        today = now.date()
        if last_refresh_date != today:
            try:
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    await calendar.refresh(session)
                last_refresh_date = today
                _log.info("earnings_calendar_refreshed")
            except Exception as exc:
                _log.warning("earnings_refresh_failed err=%s", exc)
            _update_shared()
            # §8.6: DB upsert for each upcoming earnings event
            _mdr = shared.get("market_data_repo")
            if _mdr is not None:
                from datetime import date as _date

                for _sym, _days in shared.get("earnings", {}).items():
                    try:
                        _report_date = _date.fromordinal(_date.today().toordinal() + _days)
                        await _mdr.upsert_earnings(_sym, _report_date)
                    except Exception:
                        pass
        await asyncio.sleep(60.0)


# Registry name the buyer-first options path runs under (its mode override, see boot).
_OPTIONS_DESK_STRATEGY = "options_seller_desk_v3"


async def _try_options_structure(
    *,
    shared: dict[str, Any],
    crm_ctx: Any,
    strategy: Any,
    symbol: str,
    direction: str,
    entry: float,
    sl_pct: float,
    tgt_pct: float,
    conviction: float,
    earnings_days: int,
    signal_id: str,
    paper_mode: bool,
    paper_engine: Any,
) -> bool:
    """Phase 6 §11 structure selection + routing.

    Returns True iff an options structure was placed (caller then skips the
    equity path). Returns False for every other outcome — options disabled, not
    a long signal, intraday horizon, equity chosen, gate block, or any error —
    so the equity path always runs as a safe fallback.
    """
    opt = shared.get("options")
    if not opt or not opt.get("enabled"):
        return False
    if not getattr(crm_ctx, "segment_master_switch", {}).get("options", True):
        return False  # options segment paused — equity path handles this signal instead
    if direction != "BUY":
        return False  # no preferred bearish options structure
    if not getattr(crm_ctx, "can_trade_options", False):
        return False  # G0 permission
    # R1 for a LIVE execution: this path runs the options segment as the seller desk's
    # strategy (options_seller_desk_v3) through ONE boot-time executor. An unusable route, or a
    # usable one naming another broker than that executor's, places nothing (the signal falls
    # back to the equity path like any other options block).
    if not getattr(opt.get("executor"), "_is_paper", True):
        from src.brokers.routing import route_rejection

        _routes = getattr(crm_ctx, "strategy_routes", None) or shared.get("strategy_routes")
        _why = route_rejection(_routes, _OPTIONS_DESK_STRATEGY)
        _route = (_routes or {}).get(_OPTIONS_DESK_STRATEGY)
        _boot_broker = opt.get("exec_broker") or LEGACY_LIVE_BROKER
        if _why is None and _route is not None and _route.broker != _boot_broker:
            _why = (
                f"route names broker {_route.broker} but the boot options executor trades "
                f"through {_boot_broker}"
            )
        if _why is not None:
            _opt_log.warning(
                "broker_route_rejected strategy=%s segment=options_buyer_first symbol=%s "
                "reason=%s",
                _OPTIONS_DESK_STRATEGY,
                symbol,
                _why,
            )
            return False

    # Time horizon from the strategy product: MIS → intraday (equity only).
    product = getattr(getattr(strategy.spec, "meta", None), "product", None)
    if str(product) == "MIS":
        return False
    time_horizon = "swing"

    from src.options.structure_selector import Opportunity, PortfolioState

    provider = opt["provider"]
    selector = opt["selector"]
    gate = opt["gate"]
    executor = opt["executor"]
    repo = opt.get("repo")
    cfg = opt["cfg"]

    root = symbol.split(":")[-1].upper()
    min_dte = int(cfg.get("min_days_to_expiry", 14))
    chain = await provider.get_chain(symbol, min_days_to_expiry=min_dte, spot=entry)
    if chain is None:
        return False
    iv_history = await repo.recent_atm_iv(root) if repo is not None else []

    opp = Opportunity(
        symbol=symbol,
        direction=direction,
        conviction=conviction,
        entry_price=entry,
        stop_loss_pct=sl_pct,
        target_pct=tgt_pct,
        risk_rupees=float(crm_ctx.max_risk_per_trade),
        time_horizon=time_horizon,
        earnings_proximity_days=earnings_days,
    )
    # Options has its OWN open-position budget, independent of the equity CRM's
    # swing/intraday counters — it used to be fed crm_ctx's equity-only count
    # (e.g. 14 open swing positions), which had nothing to do with how many
    # options positions were actually open, and could block or wrongly permit
    # options entries based on unrelated equity book state.
    _options_open_count = 0
    if repo is not None:
        try:
            _options_open_count = len(await repo.load_open_positions())
        except Exception as _ope:
            _opt_log.debug("options_open_position_count_err err=%s", str(_ope)[:80])
    portfolio = PortfolioState(
        open_position_count=_options_open_count,
        max_open_positions=int(cfg.get("max_open_positions", 5)),
        sector_concentrated=bool(shared.get("sector_concentrated", False)),
    )

    choice = selector.select(opp, portfolio, chain, iv_history)
    if choice.structure_type == "equity":
        return False  # §11 chose equity → use the equity path

    verdict = gate.verify(choice, chain, iv_history)
    if not verdict.passed:
        _opt_log.info("crm_blocked gate=G_OPT symbol=%s reason=%s", symbol, verdict.reason)
        return False  # fall back to equity

    result = await executor.place_structure(choice, signal_id)
    try:
        from src.notifications import EventKind, notify
        from src.notifications.events import Segment

        # No exit-side tracking exists anywhere for this path (Phase 6
        # directional structures aren't held in any position book the way
        # SellerDesk holds its own) -- entry-only notification, same
        # asymmetry the code already has (see options_spec follow-ups).
        notify(
            EventKind.ENTRY_FILL,
            is_paper=getattr(executor, "_is_paper", True),
            agent_trade_id=result.structure_id,
            symbol=symbol,
            segment=Segment.OPTIONS,
            dedupe_key=f"entry:{result.structure_id}",
            direction=choice.structure_type,
            qty=len(choice.legs),
            fill=choice.entry,
            sl=choice.stop,
            target=choice.target,
            product=choice.structure_type,
        )
    except Exception:  # notify() must never fail the trade path
        pass
    return True


async def _kite_token_refresh_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    """Kite access tokens expire roughly once a day (~early morning IST); a
    long-running process holds the token it started with in memory and never
    reloads .env, so it silently goes blind to every broker call once the
    token rotates (observed 2026-08-05: ~17 hours, zero trades, until a
    manual restart). Reuses the same headless auto_refresh() launcher.py
    already calls at startup — just re-runs the check periodically instead
    of only once.

    Refreshes EVERY distinct Kite-credentialed broker instance in
    shared["kite_brokers"], not just shared["broker"] -- when segments are
    split paper/live, the live segment's broker is a separately constructed
    KiteAdapter object (see its construction in _run() for why), and a
    refresh applied only to `broker` silently leaves that one still holding
    the stale token (found live 2026-09-09: swing's live reconciliation and
    the options desk's chain fetcher, both preferring the live broker, kept
    failing with "Incorrect api_key or access_token" 25+ minutes after this
    loop logged a successful refresh).
    """
    import os

    from src.brokers.kite_auth import auto_refresh, is_token_valid

    _CHECK_INTERVAL_SECONDS = 1800.0

    brokers = shared.get("kite_brokers") or (
        [shared["broker"]]
        if shared.get("broker") is not None and hasattr(shared["broker"], "set_access_token")
        else []
    )
    if not brokers:
        return  # non-Kite-backed broker (e.g. test doubles) — nothing to refresh
    if not os.environ.get("KITE_API_KEY", "").strip():
        _log.info("kite_token_refresh_loop_skipped reason=no_kite_credentials")
        return

    while not stop.is_set():
        try:
            api_key = os.environ.get("KITE_API_KEY", "")
            token = os.environ.get("KITE_ACCESS_TOKEN", "")
            if not is_token_valid(api_key, token):
                _log.warning("kite_token_stale_refreshing")
                new_token = await asyncio.to_thread(auto_refresh)
                for b in brokers:
                    b.set_access_token(new_token)
                _log.info("kite_token_refreshed count=%d", len(brokers))
        except Exception as exc:  # noqa: BLE001 — one bad refresh must not kill the loop
            _log.warning("kite_token_refresh_failed err=%s", exc)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


async def _angel_token_refresh_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    """Angel One twin of _kite_token_refresh_loop: refresh-token renewal (no TOTP needed)
    then push the new token into every Angel adapter. Tokens are broker-specific, so this
    never touches Kite objects."""
    brokers = shared.get("angel_brokers") or []
    if not brokers:
        return
    from src.brokers import angel_one_auth  # only when Angel is in use (needs SmartApi)

    while not stop.is_set():
        try:
            new_token = await asyncio.to_thread(angel_one_auth.auto_refresh)
            for b in brokers:
                b.set_access_token(new_token)
        except Exception as exc:  # noqa: BLE001 — one bad refresh must not kill the loop
            _log.warning("angel_token_refresh_failed err=%s", exc)
        await asyncio.sleep(1800.0)


def _flattrade_headless_login(env_path: Path | None = None) -> str:
    """HOOK ONLY -- where a future headless (TOTP) Flattrade login would plug in.

    Deliberately not implemented and not called anywhere: Flattrade documents no headless
    login and its terms 2(a)/2(e) rule out scripting auth.flattrade.in, so whether to build
    one is an owner decision that is still pending. Until then the daily login is the manual
    `python -m src.brokers.flattrade_auth` and _flattrade_token_refresh_loop only validates
    and picks up the token it writes to .env."""
    raise NotImplementedError(
        "Flattrade headless login is not implemented (owner decision pending; ToS). "
        "Run: python -m src.brokers.flattrade_auth"
    )


# How long after the operator's manual Flattrade login the running system picks the new
# token up from .env. One UserDetails call per check, against a 200 calls/min API limit.
_FLATTRADE_CHECK_INTERVAL_SECONDS = 300.0
# The "log in" alert is only useful when the operator can act on it before/while trading:
# NSE trading days (weekends/holidays stay quiet, like the 2026-10-02 quiet mode), from this
# time until the regular close (config/market_hours.json).
_FLATTRADE_ALERT_FROM_MINUTES = 8 * 60  # 08:00 IST


def _flattrade_now() -> Any:
    """Current IST datetime (a seam for tests)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Kolkata"))


def _flattrade_alert_due(now: Any) -> bool:
    """True on an NSE trading day between 08:00 IST and the regular close."""
    mins = now.hour * 60 + now.minute
    return is_trading_day(now.date()) and (
        _FLATTRADE_ALERT_FROM_MINUTES <= mins <= _sessions.minutes(_sessions.regular_close())
    )


async def _flattrade_token_refresh_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    """Flattrade twin of _angel_token_refresh_loop, but it NEVER logs in (the daily login is
    manual-assisted). Each check validates the token in .env (flattrade_auth.auto_refresh,
    which re-reads .env, so a token written by the manual login is picked up without a
    restart) and pushes a changed token to every Flattrade object via
    shared["flattrade_token_targets"] (router.set_provider_token("flattrade", .) for the
    market-data provider -- the router has no set_access_token -- and set_access_token for
    a live Flattrade broker). An invalid/missing token raises one operator alert per IST day,
    only on NSE trading days inside the alert window (_flattrade_alert_due); a valid check
    re-arms it, so a session that dies again later the same day alerts again.

    No-op when no Flattrade object was built (Flattrade is neither an eligible data provider
    nor a live strategy's broker) or when Flattrade has no credentials."""
    import os
    from datetime import date

    targets = shared.get("flattrade_token_targets") or []
    if not targets:
        return
    try:
        from src.brokers import flattrade_auth
        from src.brokers.adapters.flattrade.client import FlattradeError
        from src.brokers.routing import broker_is_configured
        from src.infrastructure import operator_alerts
    except Exception as exc:  # noqa: BLE001 — the main gather would swallow this silently
        _log.error("flattrade_token_refresh_loop_disabled reason=import_failed err=%r", exc)
        return
    if not broker_is_configured("flattrade", os.environ):
        _log.info("flattrade_token_refresh_loop_skipped reason=no_flattrade_credentials")
        return
    pushed = ""
    alerted_on: date | None = None
    while not stop.is_set():
        try:
            token = await asyncio.to_thread(flattrade_auth.auto_refresh)
            alerted_on = None  # valid again: a later expiry today must alert again
            if token != pushed:
                for push in targets:
                    push(token)
                pushed = token
                _log.info("flattrade_token_picked_up targets=%d", len(targets))
        except FlattradeError as exc:  # API/network trouble, not an expired session
            _log.warning("flattrade_token_check_failed err=%s", exc)
        except RuntimeError as exc:  # no token / expired: only a human can log in
            _log.warning(
                "flattrade_token_invalid action='run python -m src.brokers.flattrade_auth' err=%s",
                exc,
            )
            now = _flattrade_now()
            today = now.date()
            if alerted_on != today and _flattrade_alert_due(now):
                operator_alerts.raise_alert(
                    "Flattrade session is not valid -- no Flattrade market data or orders "
                    "until you log in: python -m src.brokers.flattrade_auth",
                    source="flattrade_token_invalid",
                    tier="CRITICAL",
                    dedupe_seconds=0.0,  # once per IST day is enforced here
                    push_title="FLATTRADE LOGIN NEEDED",
                )
                alerted_on = today
        except Exception as exc:  # noqa: BLE001 — one bad check must not kill the loop
            _log.warning("flattrade_token_check_failed err=%s", exc)
        await asyncio.sleep(_FLATTRADE_CHECK_INTERVAL_SECONDS)


async def _options_seller_vol_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    """Runs the seller-desk VolatilityEngine's own loop until stopped.

    Thin wrapper so this follows the same `shared`/`stop` signature as every
    other background loop registered in `_run()`; all retry/backoff logic
    lives in VolatilityEngine.run_loop itself (one bad underlying must not
    kill the loop — handled there).
    """
    seller = shared.get("seller_options")
    if not seller or seller.get("engine") is None:
        return
    await seller["engine"].run_loop(stop)


def _refresh_options_risk_budget(crm_ctx: Any) -> None:
    """Keep every config/capital.json-derived rupee cap (options, swing, intraday) in
    step with live equity (src/risk/budget.py tracks it via set_current_equity,
    refreshed every 60s at the broker-equity fetch) -- called at every site that
    refreshes crm_ctx.current_equity, so G1's daily stops and G7/G8's per-trade and
    open-risk caps are never evaluated against a stale, boot-time rupee figure."""
    from src.options import seller_settings as _rb_settings
    from src.risk.budget import get_budget as _rb_get_budget
    from src.risk.budget import get_current_equity as _rb_get_equity

    crm_ctx.max_options_daily_loss = _rb_settings.max_options_daily_loss_all_strategies_rupees()
    crm_ctx.max_options_structure_loss_per_trade = (
        _rb_settings.max_options_structure_loss_per_trade_rupees()
    )
    _b = _rb_get_budget()
    _eq = _rb_get_equity()
    crm_ctx.max_swing_loss_per_trade = _b.swing.max_loss_per_trade_rupees(_eq)
    crm_ctx.max_swing_shared_risk = _b.swing.max_share_of_shared_risk_rupees(_eq)
    crm_ctx.max_intraday_loss_per_trade = _b.intraday.max_loss_per_trade_rupees(_eq)
    crm_ctx.max_intraday_shared_risk = _b.intraday.max_share_of_shared_risk_rupees(_eq)
    crm_ctx.max_intraday_daily_loss = _b.intraday.max_loss_per_day_rupees(_eq)
    if getattr(crm_ctx, "per_broker_equity", False):
        # Per-broker risk (system.per_broker_equity): the same capital.json percentages,
        # applied to each live broker's own equity and baseline. A broker without a fresh
        # equity gets no caps at all -- CRM G1 blocks its entries before any cap is read.
        from src.risk.crm import per_key_rupee_caps

        _caps: dict[str, dict[str, float]] = {}
        _dd: dict[str, float] = {}
        for _key, _keq in dict(crm_ctx.mode_current_equity).items():
            if not _keq or _keq <= 0:
                continue
            _caps[_key] = per_key_rupee_caps(_b, float(_keq))
            _base = float(crm_ctx.mode_total_capital.get(_key, 0.0) or 0.0)
            if _base > 0:
                _dd[_key] = max(0.0, (_base - float(_keq)) / _base * 100.0)
        crm_ctx.mode_rupee_caps = _caps
        crm_ctx.mode_drawdown_pct = _dd


async def _hosted_margin(shared: dict[str, Any], desk: Any, broker: Any, is_paper: bool) -> float:
    """Margin input for a desk-hosted strategy in ITS OWN mode: the paper sleeve when paper,
    the live broker's real free cash when live."""
    if is_paper:
        return float(desk.paper_sleeve_margin())
    # Primary live stack: the options desk is single-broker until its executors are keyed.
    live_broker = (_pick_stack(shared, False) or {}).get("broker") or broker
    return float((await live_broker.get_margins()).available_cash)


async def _hosted_options_step(
    shared: dict[str, Any], desk: Any, broker: Any, today: Any, now: Any, crm_ctx: Any
) -> None:
    """Desk-hosted strategies' entry attempts: options_expiry_day (F2, legacy butterfly). Runs only
    when its registry entry is enabled AND its own options_settings block is enabled, in its own
    registry execution_mode."""
    from src.options import seller_settings as _ss

    # 2026-10-07: unconditional, every call -- the two branch-level logs below
    # (hosted_options_step_skip) still produced zero output even after being added,
    # so this proves whether the function is reached at all, before anything else runs.
    _opt_log.info("hosted_options_step_entered")
    reg = shared.get("strategy_registry")
    vix: float | None = None
    for cfg, default_name, runner in (
        (_ss.expiry_day(), "options_expiry_day", desk.run_expiry_day_cycle),
    ):
        name = str(cfg.get("registry_name", default_name))
        # 2026-10-07: both pre-checks below used to `continue` with ZERO logging --
        # found live: a hosted strategy produced not one log line, in either direction,
        # across its first 3 days enabled, with no way to tell which of these two
        # silently fired. Never again.
        if not cfg.get("enabled", False):
            _opt_log.info(
                "hosted_options_step_skip strategy=%s reason=disabled_in_options_settings", name
            )
            continue
        if reg is None:
            _opt_log.warning(
                "hosted_options_step_skip strategy=%s reason=strategy_registry_unavailable", name
            )
            continue
        if reg.get(name) is None:
            _opt_log.warning(
                "hosted_options_step_skip strategy=%s reason=not_in_strategy_registry", name
            )
            continue
        underlying = f"NSE:{str(cfg.get('underlying', 'NIFTY')).upper()}"
        try:
            if vix is None:
                vix_map = await broker.get_ltp(["NSE:INDIA VIX"])
                vix = next(iter(vix_map.values()), None) if vix_map else None
            is_paper = desk.strategy_is_paper(name, crm_ctx)
            margin = await _hosted_margin(shared, desk, broker, is_paper)
            await runner(
                underlying,
                today=today,
                now=now,
                available_margin=margin,
                regime=desk._last_regime.get(underlying, "UNKNOWN"),
                vix=vix,
                crm=shared.get("crm"),
                crm_ctx=crm_ctx,
            )
        except Exception as exc:  # noqa: BLE001 -- one hosted strategy must not stop the other
            _opt_log.warning("hosted_options_step_failed strategy=%s err=%s", name, exc)


async def _capture_options_pnl_snapshot(shared: dict[str, Any], today: Any) -> int:
    """The actual once-a-day capture (migration 086): every still-OPEN options
    position's unrealized P&L right now, saved as `today`'s baseline so the
    dashboard's Today's P&L (src/api/server.py get_options_positions) has
    something to diff against for a position opened on an earlier day. Pulled
    out of _options_pnl_snapshot_loop's scheduling wrapper so it is directly
    testable without driving the while-loop. Returns the number of positions
    captured (0 if the desk/repo/broker aren't wired yet, or no positions are
    open -- never raises, same fail-soft contract the loop relies on)."""
    _seller_options = shared.get("seller_options")
    desk = _seller_options.get("desk") if _seller_options else None
    repo = _seller_options.get("repo") if _seller_options else None
    broker = shared.get("broker")
    if desk is None or repo is None or broker is None:
        return 0

    from src.db.repositories.options import _serialize_leg
    from src.options.position_analytics import compute_leg_pnl

    positions = list(desk.positions.values())
    all_syms = {leg.tradingsymbol for p in positions for leg in p.protection_legs + p.income_legs}
    ltp: dict[str, Any] = {}
    if all_syms:
        try:
            # broker.get_ltp returns {symbol: float}; compute_leg_pnl expects the
            # {"last_price": float} shape (matches server.py's raw-Kite .ltp()
            # convention, which it was modeled on) -- normalize here.
            flat = await broker.get_ltp(list(all_syms))
            ltp = {sym: {"last_price": px} for sym, px in flat.items()}
        except Exception as exc:  # noqa: BLE001
            _opt_log.warning("options_pnl_snapshot_ltp_fetch_failed err=%s", exc)

    snapshot: dict[str, float] = {}
    for p in positions:
        legs = [_serialize_leg(leg) for leg in p.protection_legs + p.income_legs]
        pnl, _ = compute_leg_pnl(legs, ltp)
        if pnl is not None:
            snapshot[p.position_id] = pnl

    await repo.save_daily_pnl_snapshots(today, snapshot)
    _opt_log.info(
        "options_pnl_snapshot_captured date=%s positions=%d captured=%d",
        today,
        len(positions),
        len(snapshot),
    )
    return len(snapshot)


async def _options_pnl_snapshot_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    """Scheduling wrapper for _capture_options_pnl_snapshot: fires once at/after
    09:16 IST on a weekday (NSE opens 09:15 -- one minute for LTPs to be live),
    same at-or-after-the-hour pattern as _fundamentals_fetch_loop, not an exact
    window -- a restart after 09:16 still captures once, using current price as
    the best available baseline for the rest of the day. Idempotent per day
    (ON CONFLICT DO UPDATE in save_daily_pnl_snapshots), so re-running after a
    same-day restart just refreshes the baseline rather than duplicating it."""
    from datetime import datetime as _pdt
    from zoneinfo import ZoneInfo as _pZI

    _PIST = _pZI("Asia/Kolkata")
    _p_last_run_date = None

    while not stop.is_set():
        now = _pdt.now(_PIST)
        today = now.date()
        is_weekday = today.weekday() < 5
        past_capture_time = now.hour > 9 or (now.hour == 9 and now.minute >= 16)

        if not (is_weekday and past_capture_time and _p_last_run_date != today):
            await asyncio.sleep(300.0)
            continue

        try:
            await _capture_options_pnl_snapshot(shared, today)
            _p_last_run_date = today
        except Exception as exc:  # noqa: BLE001 -- one bad day must not kill the loop
            _opt_log.warning("options_pnl_snapshot_failed err=%s", exc)

        await asyncio.sleep(300.0)


def _options_desk_market_open(now: Any) -> bool:
    """True only on an NSE trading day inside the regular session. The seller desk opens, adjusts
    and closes positions off live quotes -- outside the session those are frozen, so any action
    is taken at a stale price (2026-10-16 00:05: a condor closed at -Rs2,059 on a date roll, then
    two spreads opened at 00:10/00:15 against the same frozen chain)."""
    return is_trading_day(now.date()) and _sessions.is_regular_session(now)


async def _seller_desk_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    """Seller-desk decision cycle + safety sweeps, every 5 minutes (matches
    position_manager's own spec'd monitoring cadence — options_spec.md §10).

    Margin sweep is wired but effectively inactive: there is no per-position
    margin API in the current broker interface (only account-level
    ``get_margins()``), so ``current_margins`` is always empty and the sweep
    is a documented no-op until that data source exists — not silently
    pretended to be live.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    from src.options import seller_settings as _seller_settings
    from src.options.expiry_selector import load_expiry_calendar
    from src.options.nfo_chain import index_spot_symbol
    from src.options.regime_classifier import (
        classify_regime,
        mean_revert_score,
        trend_direction,
        trending_score,
        wilder_adx,
    )
    from src.options.volatility_formulas import iv_rank

    _IST = _ZI("Asia/Kolkata")

    while not stop.is_set():
        try:
            _seg_ctx = shared.get("crm_ctx")
            if _seg_ctx is not None and not _seg_ctx.segment_master_switch.get("options", True):
                await asyncio.sleep(30)
                continue
            _seg_autonomy = (
                _seg_ctx.segment_autonomy_mode.get("options") if _seg_ctx is not None else None
            ) or "autonomous"
            _seg_shadow = bool(
                _seg_ctx is not None and _seg_ctx.segment_shadow_mode.get("options", False)
            )
            if _seg_autonomy == "manual" or _seg_shadow:
                # Phase 1 limitation: SellerDesk.run_cycle() evaluates and places in
                # one call with no dry-run/manual-approval-queue split, so "manual"
                # and "shadow" for this engine both mean "skip the cycle entirely"
                # (no new position opened) rather than true evaluate-and-log —
                # unlike the equity/CRM path above where shadow signals still run
                # through full G0-G10 evaluation and get logged as crm_shadow_skip.
                if _seg_shadow:
                    _opt_log.info(
                        "seller_desk_shadow_skip universe=%s", _seller_settings.universe_indices()
                    )
                await asyncio.sleep(30)
                continue
            seller = shared.get("seller_options")
            if seller and seller.get("desk") is not None:
                desk = seller["desk"]
                chain_fetcher = seller.get("chain_fetcher")
                broker = shared.get("broker")
                repo = seller.get("repo")
                if chain_fetcher is not None and broker is not None and repo is not None:
                    now = _dt.now(_IST)
                    today = now.date()
                    if not _options_desk_market_open(now):
                        _opt_log.debug("seller_desk_market_closed_skip now=%s", now.isoformat())
                        await asyncio.sleep(300.0)
                        continue
                    calendar = load_expiry_calendar()
                    try:
                        _n_reconciled = await desk.reconcile_duplicate_positions()
                        if _n_reconciled:
                            _opt_log.warning(
                                "seller_desk_duplicates_reconciled count=%d", _n_reconciled
                            )
                    except Exception as exc:  # noqa: BLE001 — reconciliation must never block the cycle
                        _opt_log.warning("seller_desk_reconcile_failed err=%s", exc)
                    _fetched_chains = {}
                    for underlying in _seller_settings.universe_indices():
                        try:
                            root = underlying.upper()
                            chain = await chain_fetcher.fetch_tier1(f"NSE:{root}")
                            if chain is None:
                                continue
                            _fetched_chains[f"NSE:{root}"] = chain
                            # 40 calendar days resolves to ~28 trading-day candles after
                            # weekends/holidays — one short of wilder_adx's 29-bar minimum
                            # (period*2+1, period=14), so ADX was always None and every
                            # cycle skipped silently (observed 2026-07-30). 60 calendar days
                            # clears that floor with margin.
                            ohlcv = await broker.get_ohlcv(
                                f"NSE:{index_spot_symbol(root)}", "day", 60
                            )
                            closes = (
                                list(ohlcv["close"])
                                if ohlcv is not None and not ohlcv.empty
                                else []
                            )
                            highs = (
                                list(ohlcv["high"]) if ohlcv is not None and not ohlcv.empty else []
                            )
                            lows = (
                                list(ohlcv["low"]) if ohlcv is not None and not ohlcv.empty else []
                            )
                            adx = wilder_adx(highs, lows, closes) if closes else None
                            trend = trending_score(closes) if closes else None
                            vix_map = await broker.get_ltp(["NSE:INDIA VIX"])
                            vix = next(iter(vix_map.values()), None) if vix_map else None
                            history = await repo.recent_vol_intel_ivs(root)
                            current_iv = chain.atm_iv()
                            ivr = iv_rank(current_iv, history) if current_iv is not None else None
                            if current_iv is None or ivr is None:
                                # Diagnostic only (2026-09-08): NIFTY specifically
                                # showed ivr=None all day 2026-09-07 while BANKNIFTY
                                # resolved fine on the identical code path, with no
                                # error/warning logged anywhere -- couldn't pin down
                                # the real cause without live chain data to inspect,
                                # so this captures exactly what atm_iv()/iv_rank() saw
                                # for the NEXT time it happens, instead of silence.
                                _atm_row = chain.atm()
                                _opt_log.info(
                                    "seller_desk_ivr_unavailable underlying=%s current_iv=%s "
                                    "ivr=%s chain_strikes=%d atm_strike=%s atm_call_iv=%s "
                                    "atm_put_iv=%s history_len=%d",
                                    root,
                                    current_iv,
                                    ivr,
                                    len(chain.strikes),
                                    _atm_row.strike if _atm_row is not None else None,
                                    _atm_row.call_iv if _atm_row is not None else None,
                                    _atm_row.put_iv if _atm_row is not None else None,
                                    len(history),
                                )
                            # Read-only: reuses VolatilityEngine's own 30-min tier2 scan
                            # rather than re-fetching the option chain here (that scan is
                            # rate-limit-sensitive by design — see volatility_engine.py).
                            term_structure = await repo.latest_term_structure(root)
                            if adx is None or trend is None or vix is None:
                                _opt_log.info(
                                    "seller_desk_cycle_skipped underlying=%s reason=missing_inputs "
                                    "adx=%s trend=%s vix=%s closes=%d",
                                    root,
                                    adx,
                                    trend,
                                    vix,
                                    len(closes),
                                )
                                continue
                            regime_result = classify_regime(
                                vix, adx, trend, mean_revert_score(trend)
                            )
                            # SIGNED drift (trend_direction), not trend
                            # (trending_score) — the latter is R², it can't
                            # tell an uptrend from a downtrend, and
                            # candidates()'s moderate-trend override needs
                            # the sign to pick bull_call_spread vs
                            # bear_put_spread (2026-08-20).
                            signed_bias = trend_direction(closes) if closes else None
                            # Paper mode: simulate available_margin from the
                            # desk's own ring-fenced capital sleeve, not the
                            # real broker account (unrelated in paper mode --
                            # see SellerDesk.available_margin's docstring).
                            # Skips the live broker call entirely when paper.
                            if desk.is_paper:
                                _available_margin = desk.available_margin()
                            else:
                                _margins = await broker.get_margins()
                                _available_margin = desk.available_margin(
                                    float(_margins.available_cash)
                                )
                            await desk.run_cycle(
                                f"NSE:{root}",
                                regime=regime_result.regime,
                                ivr=ivr,
                                term_structure=term_structure,
                                today=today,
                                now=now,
                                expiry_calendar=calendar,
                                available_margin=_available_margin,
                                directional_bias=signed_bias,
                                vix=vix,
                                crm=shared.get("crm"),
                                crm_ctx=_seg_ctx,
                            )
                        except Exception as exc:  # noqa: BLE001 — one bad underlying must not kill the loop
                            _opt_log.warning(
                                "seller_desk_cycle_failed underlying=%s err=%s", underlying, exc
                            )

                    try:
                        _opt_log.info("hosted_options_step_calling")
                        await _hosted_options_step(shared, desk, broker, today, now, _seg_ctx)
                    except Exception as exc:  # noqa: BLE001 — must not kill the desk loop
                        _opt_log.warning("hosted_options_step_failed err=%s", exc)

                    try:
                        await desk.sweep_expiry_guardian(today, now)
                        leg_symbols = [
                            leg.tradingsymbol
                            for pos in desk.positions.values()
                            for leg in pos.protection_legs + pos.income_legs
                        ]
                        current_prices = await broker.get_ltp(leg_symbols) if leg_symbols else {}
                        await desk.sweep_position_manager(
                            today=today,
                            blackout_active=False,
                            current_prices=current_prices,
                            current_ivs={},
                            chains=_fetched_chains,
                        )
                        await desk.sweep_margin_monitor(
                            {}
                        )  # see docstring — no per-position margin source yet
                        await desk.sweep_portfolio_risk(
                            today=today,
                            current_prices=current_prices,
                            chains=_fetched_chains,
                            now=now,
                        )
                        # config/capital.json per-strategy daily-loss kill (2026-10-07):
                        # closes ONE breached strategy's positions only -- separate from
                        # the segment-wide backstop above, which only halts new entries.
                        await desk.sweep_strategy_daily_loss(today, current_prices)
                    except Exception as exc:  # noqa: BLE001
                        _opt_log.warning("seller_desk_sweep_failed err=%s", exc)
        except Exception as exc:  # noqa: BLE001
            _opt_log.warning("seller_desk_loop_error err=%s", exc)
        await asyncio.sleep(300.0)


async def _options_intraday_short_premium_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    """options_intraday_short_premium (src/options/short_premium.py): fast loop, default every 10s, for the daily
    10:30 entry, the per-leg stop-loss and the 15:01 exit. The 5-minute _seller_desk_loop cannot
    hit those times (median stop in the real StockMock run came 29 minutes after entry). Parameters
    are re-read every tick from config/options_settings.json (options_intraday_short_premium block)."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    from src.options import seller_settings as _ss
    from src.options import short_premium as _short_premium

    _IST = _ZI("Asia/Kolkata")
    while not stop.is_set():
        poll = 10.0
        try:
            poll = float(_ss.short_premium().get("poll_seconds", 10) or 10)
            await _short_premium.tick(shared, _dt.now(_IST))
        except Exception as exc:  # noqa: BLE001 -- one bad tick must never end the loop
            _opt_log.warning("options_intraday_short_premium_tick_failed err=%s", exc)
        await asyncio.sleep(max(1.0, poll))


async def _nfo_ingester_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    """Daily post-close ATM-IV snapshot per traded underlying → DB (Phase 6).

    Builds the IV-percentile history the structure selector / options gate need.
    Runs hourly, acts once per calendar day after the close.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    _IST = _ZI("Asia/Kolkata")
    opt = shared.get("options")
    last_snapshot_date: Any = None
    while not stop.is_set():
        try:
            if opt and opt.get("enabled") and opt.get("repo") is not None:
                now = _dt.now(_IST)
                after_close = (now.hour, now.minute) >= (15, 35)
                if after_close and now.date() != last_snapshot_date:
                    provider = opt["provider"]
                    repo = opt["repo"]
                    cfg = opt["cfg"]
                    min_dte = int(cfg.get("min_days_to_expiry", 14))
                    universe = list(shared.get("options_universe", []))
                    snapped = 0
                    for sym in universe:
                        try:
                            chain = await provider.get_chain(sym, min_days_to_expiry=min_dte)
                            if chain is None:
                                continue
                            atm_iv = chain.atm_iv()
                            if atm_iv is None:
                                continue
                            await repo.record_atm_iv(
                                chain.underlying,
                                atm_iv,
                                snapshot_date=now.date(),
                                expiry=chain.expiry.date(),
                                days_to_expiry=chain.days_to_expiry,
                            )
                            snapped += 1
                        except Exception as exc:  # noqa: BLE001 — per-symbol best-effort
                            _log.debug("nfo_iv_snapshot_skip sym=%s err=%s", sym, exc)
                    last_snapshot_date = now.date()
                    _log.info("nfo_iv_snapshot_done count=%d", snapped)
        except Exception as exc:  # noqa: BLE001
            _log.warning("nfo_ingester_error err=%s", exc)
        await asyncio.sleep(3600.0)


async def _crm_loop(
    signal_ring: Any,
    crm: Any,
    crm_ctx: Any,
    compiled: list[Any],
    registry: Any,
    autonomy_mode: str,
    lifecycle: Any,
    paper_mode: bool,
    paper_engine: Any,
    shared: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    """Read signals from the ring, evaluate through CRM, log decisions."""
    from src.intelligence.opportunity_model import OpportunityValueModel
    from src.risk.crm import CRMSignal

    _ovm = OpportunityValueModel()

    # G9 advisory LLM client — created once for the loop lifetime.
    # Trade gate provider order: GEMINI → GROQ → OLLAMA (local qwen, last resort).
    from src.llm.providers import make_trade_llm_client as _make_trade_llm_client
    from src.llm.trade_evaluator import evaluate_trade as _evaluate_trade
    from src.news.context import build_news_context

    _g9_llm: Any = _make_trade_llm_client()  # None → DSL-only, G9 defaults to pass
    _g9_malformed = 0

    while not stop.is_set():
        try:
            slot = await asyncio.wait_for(signal_ring.wait_and_pop(), timeout=1.0)
        except TimeoutError:
            continue

        symbol = registry.symbol_of(slot.ticker_idx)
        if not symbol:
            continue
        if slot.strategy_idx >= len(compiled):
            continue

        strategy = compiled[slot.strategy_idx]
        sl_spec = getattr(strategy.spec, "stop_loss", None)
        tgt_spec = getattr(strategy.spec, "target", None)
        sl_pct = (
            float(getattr(sl_spec, "max_sl_pct", None) or getattr(sl_spec, "fixed_pct", 5.0) or 5.0)
            / 100.0
        )
        tgt_pct = float(getattr(tgt_spec, "fixed_pct", None) or (sl_pct * 200.0)) / 100.0

        # Phase 3: block if symbol currently has news HALT/AVOID
        import time as _time_crm

        _sym_clean = symbol.split(":")[-1]
        _news_avoid = shared.get("news_avoid_symbols", {})
        if _sym_clean in _news_avoid and _news_avoid[_sym_clean] > _time_crm.time():
            _log.info("crm_blocked_news symbol=%s", symbol)
            continue

        # Never trade a symbol already in a position — no matter what. Covers both
        # engine-tracked positions (order_monitor, paper & live) and any broker /
        # external holding (cached by the reconciliation loop). Mirrors the intraday
        # guard in _execute_intraday_signal.
        _om_crm = shared.get("order_monitor")
        _crm_om_syms = (
            {p.symbol.split(":")[-1] for p in _om_crm.get_monitored()}
            if _om_crm is not None
            else set()
        )
        if _sym_clean in _crm_om_syms or _sym_clean in (shared.get("held_symbols") or set()):
            _log.info("crm_blocked_already_in_position symbol=%s", symbol)
            continue

        # Phase 4: block if strategy is paused (expectancy decay)
        _strat_name = getattr(getattr(strategy.spec, "meta", None), "name", "unknown")
        if _strat_name in shared.get("paused_strategies", set()):
            _log.info("crm_blocked_paused symbol=%s strategy=%s", symbol, _strat_name)
            continue

        # Infrastructure survival gate — block new trades at MONITOR_ONLY/EMERGENCY
        _survival = shared.get("survival_mode")
        if _survival is not None and not _survival.allow_new_trades():
            _log.info("crm_blocked_survival level=%s symbol=%s", _survival.label, symbol)
            continue

        # §8.3 ask_mode: STRESSED strategy → block autonomous execution
        if shared.get("ask_mode") and autonomy_mode != "manual":
            _log.critical(
                "crm_ask_mode_active symbol=%s — STRESSED strategy detected; human confirmation required",
                symbol,
            )
            continue

        # §8.1 UNKNOWN regime — block all new entries (belt-and-suspenders on top of G4)
        if shared.get("tighten_sl_to_50pct"):
            _log.info("crm_blocked_unknown_regime symbol=%s", symbol)
            continue

        direction = "BUY" if slot.signal_type == 1 else "SELL"
        entry = slot.price
        risk_rupees = entry * sl_pct

        _rs_new_high = shared.get("rs_flags", {}).get(_sym_clean, False)
        from src.risk.crm import engine_of as _engine_of

        _spec_meta = getattr(strategy.spec, "meta", None)
        _spec_tf = getattr(_spec_meta, "timeframe", None) or getattr(strategy.spec, "timeframe", "")
        _sig_engine = _engine_of(_spec_tf)
        crm_signal = CRMSignal(
            signal_id=f"W{slot.worker_id}-{slot.timestamp_ns}",
            symbol=symbol,
            sector_declining=_resolve_sector_declining(symbol),
            sector=_resolve_sector(symbol),
            direction=direction,
            strategy_name=getattr(getattr(strategy.spec, "meta", None), "name", "unknown"),
            intended_quantity=1,
            stop_loss_pct=sl_pct,
            target_pct=tgt_pct,
            risk_rupees=risk_rupees,
            entry_price=entry,
            regime=shared.get("regime", "BULL"),
            strategy_health=shared.get("health_scores", {}).get(
                getattr(getattr(strategy.spec, "meta", None), "name", "unknown"), "HEALTHY"
            ),
            earnings_proximity_days=shared.get("earnings", {}).get(symbol.split(":")[-1], 999),
            vix=float(shared.get("vix", 15.0)),
            engine=_sig_engine,
            # §8.2: rs_new_high passed to LLM (G9) for +0.10 confidence boost
            meta={
                "autonomy_mode": autonomy_mode,
                "rs_new_high": _rs_new_high,
                "backtest_passed": shared.get("backtest_summary", {})
                .get(getattr(getattr(strategy.spec, "meta", None), "name", "unknown"), {})
                .get("passed", True),
            },
        )

        # Sync equity + drawdown + Phase 2.5 survival flags before every CRM evaluation
        crm_ctx.current_equity = float(shared.get("current_equity", crm_ctx.current_equity))
        _refresh_options_risk_budget(crm_ctx)
        _start_eq = float(shared.get("starting_equity", crm_ctx.total_capital))
        if _start_eq > 0:
            crm_ctx.drawdown_pct = max(
                0.0, (_start_eq - crm_ctx.current_equity) / _start_eq * 100.0
            )
        crm_ctx.capital_critical = bool(shared.get("capital_critical", False))
        crm_ctx.correlation_block = shared.get("correlation_level", "NORMAL") in ("HIGH", "EXTREME")
        crm_ctx.llm_bypass = bool(shared.get("bypass_llm", False))
        crm_ctx.quality_raise = float(shared.get("quality_raise", 0.0))
        crm_ctx.halt_trading_weeks = bool(shared.get("halt_trading_weeks", False))
        crm_ctx.calibration_thresholds = dict(shared.get("calibration_thresholds", {}))
        crm_ctx.paused_strategies = set(shared.get("paused_strategies", set()))

        # Inject causal context (Layer 7) into signal meta for G9 advisory.
        # Real regime-conditional retrieval (spec §9: "before the next similar
        # trade, retrieve the 10 most relevant past insights") — gated by
        # config/learning.json causal_memory.mode. "shadow" retrieves and logs
        # for review but does NOT set crm_signal.meta, so the G9 prompt never
        # sees it; "live" sets it for real. Previously this read a hand-rolled
        # cache of the last 3 insights written at trade-CLOSE time, regardless
        # of regime relevance — retrieval now happens here, at signal-build
        # time, against the actual current regime.
        if _learning_mode("causal_memory") in ("shadow", "live"):
            try:
                from src.learning.causal_memory import CausalMemory as _CausalMemory

                _lr_causal = shared.get("learning_repo")
                if _lr_causal is not None:
                    _causal_retrieve = _CausalMemory(None, _CausalInsightRepo(_lr_causal))
                    _insights = await _causal_retrieve.retrieve(
                        _strat_name, str(shared.get("regime", "UNKNOWN")), k=10
                    )
                    _joined_insight = "; ".join(
                        i.generalizable_insight for i in _insights if i.generalizable_insight
                    )
                    if _joined_insight:
                        if _learning_mode("causal_memory") == "live":
                            crm_signal.meta["causal_context"] = _joined_insight
                        else:
                            _log.debug(
                                "causal_memory_shadow strategy=%s regime=%s would_inject=%s",
                                _strat_name,
                                shared.get("regime", "UNKNOWN"),
                                _joined_insight[:200],
                            )
            except Exception as _cme:
                _log.debug("causal_memory_retrieve_err err=%s", str(_cme)[:80])

        # Log-only fundamentals shadow (config/global.json fundamentals.enabled).
        # Never read by CRM or the G9 prompt (that payload dict below is built from
        # explicit named fields, not crm_signal.meta) until
        # scripts/backtest_fundamentals_filter.py validates a real edge.
        _fund_shadow = shared.get("fundamentals_cache", {}).get(symbol.split(":")[-1])
        if _fund_shadow:
            crm_signal.meta["fundamentals_shadow"] = _fund_shadow

        # G9: LLM confirmation — runs BEFORE crm.evaluate() so G3 (pre-close
        # confidence window) and G9 (calibration threshold + REJECT flag) can read it.
        # BLOCKING: a REJECT or sub-floor confidence stops the trade. Fails open when
        # no genuine verdict is produced (llm_evaluated=False) so an LLM outage trades
        # DSL-only instead of halting.
        if _g9_llm is not None and not crm_ctx.llm_bypass:
            _d_pri = shared.get("news_priority_symbols", {}).get(symbol)
            _d_pri_aligned = bool(
                _d_pri
                and (
                    (_d_pri["bias"] == "BULLISH" and direction in ("BUY", "LONG"))
                    or (_d_pri["bias"] == "BEARISH" and direction in ("SELL", "SHORT"))
                )
            )
            _verdict = await _evaluate_trade(
                _g9_llm,
                {
                    "symbol": symbol,
                    "direction": direction,
                    "strategy_name": _strat_name,
                    "regime": shared.get("regime", "UNKNOWN"),
                    "entry_price": entry,
                    "stop_loss_pct": sl_pct,
                    "target_pct": tgt_pct,
                    "strategy_health": shared.get("health_scores", {}).get(_strat_name, "HEALTHY"),
                    "vix": float(shared.get("vix", 15.0)),
                    "rs_new_high": _rs_new_high,
                    "causal_context": crm_signal.meta.get("causal_context"),
                    "news_context": build_news_context(symbol, shared),
                    "news_priority_aligned": _d_pri_aligned,
                },
                _g9_malformed,
            )
            _g9_malformed = _verdict["malformed"]
            crm_signal.meta["llm_confidence"] = _verdict["llm_confidence"]
            crm_signal.meta["llm_flag"] = _verdict["llm_flag"]
            crm_signal.meta["llm_reason"] = _verdict["llm_reason"]
            crm_signal.meta["llm_evaluated"] = _verdict["evaluated"]
            crm_signal.meta["llm_provider"] = _verdict.get("provider")
            crm_signal.meta["llm_latency_ms"] = _verdict.get("latency_ms")

        decision = crm.evaluate(crm_signal, crm_ctx)
        log_gate_decisions(crm_signal, decision)

        # §8.3: apply health tier sizing multiplier before placement
        _health = shared.get("health_scores", {}).get(_strat_name, "HEALTHY")
        _health_mult = HEALTH_TIER_SIZE_MULTIPLIER.get(str(_health), 1.0)
        if _health_mult == 0.0 and decision.approved:
            from dataclasses import replace as _dc_replace

            decision = _dc_replace(
                decision,
                approved=False,
                final_quantity=0,
                blocking_gate="G_HEALTH",
                blocking_reason="Strategy SUSPENDED (health tier)",
            )
        elif _health_mult < 1.0 and decision.approved:
            from dataclasses import replace as _dc_replace

            decision = _dc_replace(
                decision, final_quantity=max(1, int(decision.final_quantity * _health_mult))
            )

        # Phase 4: anti-FOMO gate — apply after CRM approves
        if decision.approved:
            _base = 80.0 if _health == "HEALTHY" else 70.0 if _health == "DEGRADED" else 55.0
            _opp = _ovm.evaluate(
                signal_id=crm_signal.signal_id,
                symbol=symbol,
                base_score=_base,
                context={"regime": shared.get("regime", "UNKNOWN"), "price_move_since_signal": 0.0},
            )
            if not _opp.should_take:
                _log.info("crm_fomo_blocked symbol=%s reason=%s", symbol, _opp.reason)
                from dataclasses import replace as _dc_replace

                decision = _dc_replace(
                    decision,
                    approved=False,
                    final_quantity=0,
                    blocking_gate="OPP",
                    blocking_reason=_opp.reason,
                )

        # Record LLM gate outcome for sanity watchdog (LTP-replay 30 min later)
        _g9 = next((g for g in decision.gate_results if g.gate_id == "G9"), None)
        if _g9 is not None and not crm_ctx.llm_bypass:
            import time as _time

            shared["llm_pending"].append(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "entry_price": entry,
                    "was_approved": "[advisory]" not in _g9.reason,
                    "ts_ns": _time.time_ns(),
                }
            )
        if decision.approved:
            from src.risk.crm import is_shadow as _is_shadow

            if _is_shadow(crm_signal, crm_ctx):
                _log.info(
                    "crm_shadow_skip",
                    extra={
                        "ctx": {
                            "signal_id": crm_signal.signal_id,
                            "symbol": symbol,
                            "qty": decision.final_quantity,
                        }
                    },
                )
                continue
            _log.info(
                "crm_approved",
                extra={
                    "ctx": {
                        "signal_id": crm_signal.signal_id,
                        "symbol": symbol,
                        "qty": decision.final_quantity,
                        "entry": entry,
                    }
                },
            )
            sl_price = entry * (1.0 - sl_pct)
            target_price = entry * (1.0 + tgt_pct)

            # Phase 6 §11 — structure selection: equity vs options per opportunity.
            # Returns True only when an options structure was actually placed; on
            # any path (disabled, equity chosen, gate block, error) it returns
            # False and the equity path below runs unchanged.
            _placed_options = False
            try:
                _placed_options = await _try_options_structure(
                    shared=shared,
                    crm_ctx=crm_ctx,
                    strategy=compiled[slot.strategy_idx],
                    symbol=symbol,
                    direction=direction,
                    entry=entry,
                    sl_pct=sl_pct,
                    tgt_pct=tgt_pct,
                    conviction=float(crm_signal.meta.get("llm_confidence", 0.6) or 0.6),
                    earnings_days=int(getattr(crm_signal, "earnings_proximity_days", 999)),
                    signal_id=crm_signal.signal_id,
                    paper_mode=paper_mode,
                    paper_engine=paper_engine,
                )
            except Exception as exc:
                _log.warning(
                    "options_route_error",
                    extra={"ctx": {"signal_id": crm_signal.signal_id, "error": str(exc)}},
                )

            if _placed_options:
                continue

            try:
                # ONE path for paper + live — the injected adapter is the only
                # difference. Place entry+SL+target, realize the fill + reanchor, then
                # register with the shared OrderMonitor for exit management.
                # By design this legacy ring path is NOT broker-routed: `lifecycle` and the
                # monitor below are the primary alias -- paper under the launcher's --paper,
                # and on a live boot the legacy (global.json) broker's stack, which stamps
                # its own exec_broker. Do not add per-strategy routing here.
                _record = await lifecycle.place_entry_protection_and_poll(
                    signal_id=crm_signal.signal_id,
                    symbol=symbol,
                    direction=direction,
                    qty=decision.final_quantity,
                    entry_price=entry,
                    sl_price=sl_price,
                    target_price=target_price,
                    product="CNC",
                    order_types=shared.get("order_types", {}),
                    meta=crm_signal.meta,
                )
                from src.execution.order_lifecycle import MonitoredPosition, OrderState

                _om = shared.get("order_monitor")
                if _record.state == OrderState.FILLED and _record.fill_price and _om is not None:
                    _om.track(
                        MonitoredPosition(
                            agent_trade_id=_record.agent_trade_id,
                            symbol=symbol,
                            direction=direction,
                            quantity=int(_record.filled_quantity or decision.final_quantity),
                            fill_price=float(_record.fill_price),
                            initial_sl=float(_record.sl_price),
                            current_sl=float(_record.sl_price),
                            initial_target=float(_record.target_price),
                            current_target=float(_record.target_price),
                            sl_order_id=_record.sl_order_id or "",
                            target_order_id=_record.target_order_id or "",
                            product="CNC",
                        )
                    )
                # Keep per-engine / per-strategy counts current between the 5-min DB
                # refreshes so multiple placements in one window are gated correctly.
                if _sig_engine == "intraday":
                    crm_ctx.intraday_trades_today = (
                        int(getattr(crm_ctx, "intraday_trades_today", 0)) + 1
                    )
                else:
                    crm_ctx.swing_trades_today = int(getattr(crm_ctx, "swing_trades_today", 0)) + 1
                crm_ctx.new_trades_today = int(getattr(crm_ctx, "new_trades_today", 0)) + 1
                _strat_nm = getattr(getattr(strategy.spec, "meta", None), "name", "unknown")
                _pst2 = getattr(crm_ctx, "per_strategy_trades_today", {})
                _pst2[_strat_nm] = int(_pst2.get(_strat_nm, 0)) + 1
                crm_ctx.per_strategy_trades_today = _pst2
            except Exception as exc:
                _log.error(
                    "placement_failed",
                    extra={"ctx": {"signal_id": crm_signal.signal_id, "error": str(exc)}},
                )
                try:
                    from src.notifications import EventKind, notify
                    from src.notifications.events import Segment

                    notify(
                        EventKind.ENTRY_REJECTED,
                        is_paper=getattr(lifecycle, "_is_paper", paper_mode),
                        symbol=symbol,
                        segment=(Segment.INTRADAY if _sig_engine == "intraday" else Segment.SWING),
                        error=str(exc)[:200],
                    )
                except Exception:  # notify() must never mask the original failure
                    pass
        else:
            _log.info(
                "crm_blocked",
                extra={
                    "ctx": {
                        "signal_id": crm_signal.signal_id,
                        "symbol": symbol,
                        "gate": decision.blocking_gate,
                        "reason": decision.blocking_reason,
                    }
                },
            )
        # DB audit trail for every CRM decision (fire-and-forget)
        _sup = shared.get("supervisor_repo")
        if _sup is not None:
            import asyncio as _aio

            _aio.ensure_future(
                _sup.emit_event(
                    "CRM_DECISION",
                    "crm_loop",
                    severity="INFO",
                    payload={
                        "signal_id": crm_signal.signal_id,
                        "symbol": symbol,
                        "approved": decision.approved,
                        "blocking_gate": decision.blocking_gate,
                    },
                )
            )


async def _run(paper_mode: bool) -> None:
    global_cfg = _cfg("global")
    risk_cfg = _cfg("risk")
    operator_cfg = _cfg("operator")
    universe_cfg = _cfg("universe")
    intelligence_cfg = _cfg("intelligence")
    execution_cfg = _cfg("execution")
    news_cfg = _cfg("news")

    # operator.json account.paper_mode_locked — operator hard-lock to paper trading.
    # Fail-safe: it can only FORCE paper on, never force live on.
    _account_cfg = operator_cfg.get("account", {})
    if _account_cfg.get("paper_mode_locked", False) and not paper_mode:
        _log.warning("paper_mode_locked: operator.json forces paper trading; ignoring --live")
        paper_mode = True
    _account_type = _account_cfg.get("account_type", "individual")

    _log.info(
        "boot_start",
        extra={"ctx": {"paper_mode": paper_mode, "account_type": _account_type}},
    )

    # ── Broker ───────────────────────────────────────────────────────────────
    from src.brokers.factory import create_broker, peek_market_data_router
    from src.market_data.provider_config import (
        parse_provider_config,
        refresh_provider_credentials,
    )

    _broker_cfg = global_cfg.get("broker", "kite")
    _live_broker_name = (
        _broker_cfg.get("name", "kite") if isinstance(_broker_cfg, dict) else str(_broker_cfg)
    )
    # Refresh tokens only for brokers this run can actually use: configured data providers
    # plus the live broker. A user with no Kite account is never asked to log in to Kite.
    # In a worker thread: Angel's auto_refresh uses asyncio.run(), which raises if called on
    # this running loop's thread (Kite's is plain sync, so it never mattered before).
    await asyncio.to_thread(
        refresh_provider_credentials,
        [*parse_provider_config(global_cfg).providers, _live_broker_name],
        _BASE / ".env",
    )
    # UNIFIED ENGINE: paper and live run the exact same execution wiring (lifecycle +
    # OrderMonitor + SL/target/reanchor/exit). The ONLY difference is the injected
    # broker adapter: paper_mode → the paper adapter simulates fills against real LTP
    # and never sends an order to the live broker; live → the real Kite adapter.
    _broker_name = "paper" if paper_mode else _live_broker_name
    broker = create_broker(_broker_name)
    # broker above remains the single "primary" adapter used everywhere in this
    # function that is NOT order placement (market data, reconciliation, regime/
    # health/news scans, options chain data) — none of that needs to know about
    # per-segment paper/live, only which broker has live market access, and the
    # paper adapter already proxies market data through its own internal Kite
    # instance (src/brokers/factory.py:_create_paper).
    #
    # Per-segment (intraday/swing/options) paper_mode — config/global.json
    # system.segments.*.paper_mode, falling back to the --paper/--live boot flag
    # for any segment not listed. operator.json paper_mode_locked forces every
    # segment to paper (same fail-safe direction as the boot flag override above).
    _segments_cfg2 = global_cfg.get("system", {}).get("segments", {})
    _seg_paper_mode: dict[str, bool] = {
        _seg: paper_mode for _seg in ("intraday", "swing", "options")
    }
    for _seg_name2, _seg_cfg2 in _segments_cfg2.items():
        if "paper_mode" in _seg_cfg2:
            _seg_paper_mode[_seg_name2] = bool(_seg_cfg2["paper_mode"])
    if _account_cfg.get("paper_mode_locked", False):
        _seg_paper_mode = {k: True for k in _seg_paper_mode}

    # Per-strategy paper/live override — config/strategies_registry.json
    # "execution_mode": "paper" | "live" — explicit on every entry. A missing
    # or invalid value fails safe to "paper" (never to the segment default,
    # which can be live) and logs a warning. Read once at boot, same
    # "restart to pick up changes" pattern as segment paper_mode.
    _strategy_execution_mode: dict[str, str] = {}
    _reg_entries: list[dict[str, Any]] = []
    try:
        _reg_raw = json.loads((_CONFIG / "strategies_registry.json").read_text(encoding="utf-8"))
        _reg_entries = list(_reg_raw.get("strategies", []))
        for _entry in _reg_entries:
            if not _entry.get("name"):
                continue
            _mode = _entry.get("execution_mode")
            if _mode not in ("paper", "live"):
                _log.warning(
                    "strategy_execution_mode_invalid strategy=%s value=%r -> paper",
                    _entry["name"],
                    _mode,
                )
                _mode = "paper"
            _strategy_execution_mode[_entry["name"]] = _mode
    except Exception as exc:
        _log.warning("strategy_execution_mode_load_failed err=%s", exc)

    # A per-strategy override can require a stack no segment default alone
    # would (e.g. one strategy forced to "paper" inside an all-live segment) —
    # both flags must also account for it, or that stack never gets built and
    # _resolve_is_paper's decision silently falls back to whichever stack IS
    # primary, exactly defeating the override (worse: a "paper" override
    # silently trading live is the dangerous direction, not the safe one).
    # paper_mode_locked already forces every override to paper at decision
    # time (_resolve_is_paper), so a "live" override never needs a live stack
    # while locked — skip building one.
    _locked = bool(_account_cfg.get("paper_mode_locked", False))
    _any_seg_paper = any(_seg_paper_mode.values()) or "paper" in _strategy_execution_mode.values()
    # Reuse `broker` above for whichever mode it already is; only construct the
    # second adapter if some segment actually needs the opposite mode.
    broker_paper = broker if paper_mode else (create_broker("paper") if _any_seg_paper else None)
    # One live broker per needed broker name: the legacy (global.json) broker exactly
    # when it was built before (live boot, a live segment, or a live registry entry
    # without a usable route), plus every broker a live registry entry routes to.
    import os as _route_os

    from src.brokers.registry import list_brokers
    from src.brokers.routing import parse_strategy_routes, rejected_routes_message, route_table

    _strategy_routes = parse_strategy_routes(_reg_entries, list_brokers(), _route_os.environ)
    # (the route table + rejected-routes alert are reported once the live stacks exist and
    # the operator-alert hook is configured, see "strategy_routes_table" below)
    # Routed live brokers need fresh tokens before create_broker() reads them, like the
    # legacy broker and data providers refreshed above (only names not refreshed there).
    _routed_refresh = [
        r.broker
        for r in _strategy_routes.values()
        if r.ok
        and r.broker is not None
        and r.broker not in (*parse_provider_config(global_cfg).providers, _live_broker_name)
    ]
    if _routed_refresh and not _locked:
        await asyncio.to_thread(refresh_provider_credentials, _routed_refresh, _BASE / ".env")
    # (a live segment adds the legacy broker inside needed_live_brokers)
    _legacy_live_needed = not paper_mode or any(not r.ok for r in _strategy_routes.values())
    live_brokers: dict[str, Any] = {
        _name: (broker if not paper_mode and _name == _live_broker_name else create_broker(_name))
        for _name in needed_live_brokers(
            _strategy_routes, _seg_paper_mode, _live_broker_name, _locked, _legacy_live_needed
        )
    }
    # _kite_token_refresh_loop needs every DISTINCT Kite-credentialed object,
    # not just `broker` -- when segments are split paper/live (e.g. options
    # paper, swing live), broker_live above is a SEPARATELY constructed
    # KiteAdapter instance, not the same object `broker` refers to. Found
    # live 2026-09-09: the refresh loop reported "kite_token_refreshed"
    # successfully, yet seller_desk (whose chain fetcher prefers broker_live
    # for real market data even in paper mode) and swing's LIVE reconciliation
    # kept failing with "Incorrect api_key or access_token" 25+ minutes later
    # -- the loop had refreshed `broker`'s token, but broker_live's separate
    # KiteConnect client instance never received it. De-duplicated by
    # identity since broker/broker_paper/broker_live legitimately alias the
    # same object in the common (not-segment-split) case.
    # Only Kite adapters get a Kite token: a live non-Kite broker (even when it is the
    # primary `broker`) must never be in this list; its own refresh list is below.
    _non_kite_live = {id(b) for n, b in live_brokers.items() if n != "kite"}
    _kite_brokers = list(
        {
            id(b): b
            for b in (broker, broker_paper, live_brokers.get("kite"))
            if b is not None and hasattr(b, "set_access_token") and id(b) not in _non_kite_live
        }.values()
    )
    # The market-data router's Angel provider is not a trading broker, so it is not in
    # _kite_brokers; register it for its own token-refresh loop. (Kite tokens reach the
    # router's kite provider via PaperAdapter.set_access_token.)
    _router = peek_market_data_router()
    _angel_brokers: list[Any] = []
    if _router is not None:
        _router_angel = _router.provider("angel_one")
        if _router_angel is not None:
            _angel_brokers.append(_router_angel)
    if live_brokers.get("angel_one") is not None:
        _angel_brokers.append(live_brokers["angel_one"])
    # Flattrade: the router provider gets its token by name (the router has no
    # set_access_token); a live Flattrade broker directly. Empty => the loop is a no-op.
    _flattrade_targets: list[Any] = []
    if _router is not None and _router.provider("flattrade") is not None:
        _flattrade_targets.append(functools.partial(_router.set_provider_token, "flattrade"))
    if live_brokers.get("flattrade") is not None:
        _flattrade_targets.append(live_brokers["flattrade"].set_access_token)
    _log.info(
        "segment_paper_mode_resolved",
        extra={
            "ctx": {
                **_seg_paper_mode,
                "paper_mode_locked": _account_cfg.get("paper_mode_locked", False),
            }
        },
    )

    # ── Initial equity fetch — required for G1 25% floor + G2 drawdown staircase ──
    _initial_equity = float(operator_cfg.get("capital", {}).get("total_capital_rupees", 100_000))
    try:
        _margin = await asyncio.wait_for(broker.get_margins(), timeout=15.0)
        _initial_equity = float(_margin.equity)
        _log.info("boot_equity_fetched equity=%.0f", _initial_equity)
    except TimeoutError:
        _log.warning("boot_equity_fetch_timeout — using operator capital as fallback")
    except Exception as exc:
        _log.warning("boot_equity_fetch_failed err=%s — using operator capital as fallback", exc)

    # ── Database (truth store; JSON remains cache) ────────────────────────────
    from src.db.migrate import run_migrations
    from src.db.pool import close_pool, init_pool
    from src.db.repositories.trades import TradeRepository

    # Per-purpose LLM token budgets (config/global.json llm.token_budgets): persist the daily
    # counts in the shared Redis client so a restart does not reset them, and warn loudly if the
    # reserved shares add up to more than the provider's daily quota (they could not all be met).
    try:
        from src.llm.token_ledger import LEDGER, budget_config

        LEDGER.configure(_make_redis_client())
        _tb = budget_config()
        if _tb.get("enabled", False):
            from src.llm.providers import _guard_cfg

            _soft = set(_guard_cfg().get("soft_budget_purposes") or [])
            # Soft (trade-path) purposes never block and reserve nothing from the provider
            # quota; only hard-capped purposes have to fit inside it.
            _alloc = sum(
                int(v) for k, v in (_tb.get("daily_tokens") or {}).items() if v and k not in _soft
            )
            _quota = int((_tb.get("provider_daily_quota") or {}).get("groq", 0))
            _log.info("llm_token_budgets purposes=%s total=%d", _tb.get("daily_tokens"), _alloc)
            if _quota and _alloc > _quota:
                _log.warning(
                    "llm_token_budgets_over_allocated total=%d > groq_quota=%d - shares cannot "
                    "all be met; lower a purpose in config/global.json llm.token_budgets",
                    _alloc,
                    _quota,
                )
    except Exception as _tb_exc:
        _log.warning("llm_token_budgets_init_failed err=%s", _tb_exc)

    db_pool = None
    try:
        db_pool = await init_pool()  # reads DB_URL from .env
        _applied = await run_migrations(db_pool)  # idempotent; 0 if up-to-date
        await db_pool.fetchval("SELECT 1")  # liveness check
        _log.info("db_pool_ready", extra={"ctx": {"migrations_applied": _applied}})
        # Clean up any stale SUBMITTED paper_trades from previous runs (e.g. mid-day
        # restart after a network outage left rows orphaned).  Idempotent and fast.
        try:
            _cleaned = await db_pool.fetchval("SELECT cleanup_stale_submitted_paper_trades(15)")
            if _cleaned:
                _log.warning(
                    "startup_stale_cleanup cleaned=%d stale SUBMITTED paper_trades → TIMED_OUT",
                    _cleaned,
                )
        except Exception as _ce:
            _log.debug("stale_cleanup_skipped (migration 048 not yet applied?): %s", _ce)
    except Exception as exc:
        # Postgres is truth, but a down DB must not brick the engine — fail loud, not silent.
        _log.critical("db_pool_init_failed", extra={"ctx": {"err": str(exc)}})
        db_pool = None
    trade_repo = TradeRepository(db_pool) if db_pool is not None else None

    # ── All other repositories ────────────────────────────────────────────────
    from src.db.repositories.fundamentals import FundamentalsRepository
    from src.db.repositories.learning import LearningRepository
    from src.db.repositories.llm_intraday_journal import LLMIntradayJournalRepository
    from src.db.repositories.llm_swing_journal import LLMSwingJournalRepository
    from src.db.repositories.market_data import MarketDataRepository
    from src.db.repositories.news import NewsRepository
    from src.db.repositories.ohlcv import OHLCVRepository
    from src.db.repositories.supervisor import SupervisorRepository

    news_repo: Any = NewsRepository(db_pool) if db_pool is not None else None
    market_data_repo: Any = MarketDataRepository(db_pool) if db_pool is not None else None
    learning_repo: Any = LearningRepository(db_pool) if db_pool is not None else None
    ohlcv_repo: Any = OHLCVRepository(db_pool) if db_pool is not None else None
    supervisor_repo: Any = SupervisorRepository(db_pool) if db_pool is not None else None
    llm_swing_journal_repo: Any = (
        LLMSwingJournalRepository(db_pool) if db_pool is not None else None
    )
    llm_intraday_journal_repo: Any = (
        LLMIntradayJournalRepository(db_pool) if db_pool is not None else None
    )
    from src.db.repositories.llm_call_log import LLMCallLogRepository

    llm_call_log_repo: Any = LLMCallLogRepository(db_pool) if db_pool is not None else None
    fundamentals_repo: Any = FundamentalsRepository(db_pool) if db_pool is not None else None

    # ── Survival mode (reads thresholds from risk.json) ───────────────────────
    from src.infrastructure.survival_mode import SurvivalMode

    _survival_mode = SurvivalMode(risk_cfg=risk_cfg)

    # ── Shared memory ─────────────────────────────────────────────────────────
    from src.shared.ohlcv_shm import CandleBlock, ORBBlock, SHMRegistry, TickBlock
    from src.shared.signal_ring import SignalRing

    tick_block = TickBlock()
    candle_block = CandleBlock()
    orb_block = ORBBlock()
    registry = SHMRegistry()
    signal_ring = SignalRing()

    # ── Boot reconciliation ───────────────────────────────────────────────────
    from src.supervisor.reconciler import Reconciler

    result = await Reconciler(broker).reconcile()
    if not result.clean:
        _log.warning("boot_gaps", extra={"ctx": {"count": len(result.gaps)}})

    # ── Strategies ────────────────────────────────────────────────────────────
    from src.strategies.registry import StrategyRegistry

    strategy_reg = StrategyRegistry()
    strategy_reg.reload(df=None)
    compiled = strategy_reg.all_enabled()
    _log.info("strategies_loaded", extra={"ctx": {"count": len(compiled)}})

    # ── Worker pool ───────────────────────────────────────────────────────────
    from src.workers.worker_pool import WorkerPool

    pool = WorkerPool(
        n_workers=global_cfg.get("n_strategy_workers", 4),
        tick_block=tick_block,
        candle_block=candle_block,
        orb_block=orb_block,
        registry=registry,
        signal_ring=signal_ring,
    )

    # ── Tick ingester ─────────────────────────────────────────────────────────
    from src.ingestion.tick_ingester import TickIngester

    # Resolve trade universe to actual symbol list (universe.json has no "symbols" key)
    _trade_universe = universe_cfg.get("trade_universe", "NIFTY50").upper()
    _raw = _resolve_index_universe(_trade_universe)
    # Reconcile the configured universe against the broker's live instrument
    # master so delisted/renamed symbols never enter the live trade universe.
    from src.market_data.instrument_master import filter_universe, valid_nse_symbols

    try:
        _valid = await asyncio.wait_for(valid_nse_symbols(broker), timeout=15.0)
        _raw = filter_universe(_raw, _valid)
    except TimeoutError:
        _log.warning("instrument_master_timeout — using unfiltered universe as fallback")
    symbols: list[str] = [f"NSE:{s}" for s in _raw]
    ingester = TickIngester(
        broker=broker,
        tick_block=tick_block,
        candle_block=candle_block,
        orb_block=orb_block,
        registry=registry,
        trigger_queue=pool.trigger_queue,
    )

    # ── CRM ───────────────────────────────────────────────────────────────────
    from src.risk.crm import CentralRiskManager, CRMContext

    crm = CentralRiskManager()
    if db_pool is not None:
        from src.db.repositories.signals import SignalRepository
        from src.risk.signal_journal import SignalJournal

        crm.decision_sink = SignalJournal(SignalRepository(db_pool))
    crm_ctx = CRMContext.from_operator_json(_CONFIG / "operator.json")
    # Apply risk.json + global.json overrides not in operator.json. apply_risk_json owns
    # all risk.json→context wiring (loss-streak / daily-loss / trade-count / position caps).
    crm_ctx.master_switch_on = bool(global_cfg.get("system", {}).get("master_switch", True))
    # options_settings.json position_limits -- G8's shared daily-loss budget
    # (operator.json capital.max_daily_loss_rupees) is sized for equity, not
    # index-options economics (found live 2026-09-10: a single real spread's
    # risk regularly exceeds that whole pot). 0/absent -> G8 falls back to the
    # shared budget unchanged, same convention as every other per-engine cap.
    from src.options import seller_settings as _opt_settings_boot  # noqa: PLC0415

    # 2026-10-07: config/capital.json (src/risk/budget.py) is now the single source
    # of truth for these two, in place of options_settings.json -- see that module.
    crm_ctx.max_options_daily_loss = (
        _opt_settings_boot.max_options_daily_loss_all_strategies_rupees()
    )
    crm_ctx.max_options_structure_loss_per_trade = (
        _opt_settings_boot.max_options_structure_loss_per_trade_rupees()
    )
    crm_ctx.max_options_open_positions = _opt_settings_boot.max_open_strategies()
    # config/capital.json is the single source of truth for swing/intraday COUNTS too
    # (2026-10-07) -- overrides whatever from_operator_json just read from
    # operator.json hard_limits.{swing,intraday} above. Per-strategy override DICTS
    # (intraday_per_strategy_overrides / swing_per_strategy_overrides) are a more
    # granular, optional feature capital.json's simpler design doesn't define, so
    # those stay operator.json-sourced, untouched.
    from src.risk.budget import get_budget as _rb_get_budget_counts

    _rbc = _rb_get_budget_counts()
    crm_ctx.max_swing_trades_per_day = _rbc.swing.max_new_trades_per_day
    crm_ctx.max_swing_per_strategy = _rbc.swing.max_new_trades_per_day_per_strategy
    crm_ctx.max_swing_open_positions = _rbc.swing.max_open_trades
    crm_ctx.max_intraday_trades_per_day = _rbc.intraday.max_new_trades_per_day
    crm_ctx.max_intraday_per_strategy = _rbc.intraday.max_new_trades_per_day_per_strategy
    crm_ctx.max_intraday_open_positions = _rbc.intraday.max_open_trades
    # Per-segment (intraday/swing/options) overrides — config/global.json system.segments.
    # Read once at boot, same "restart to pick up changes" pattern as master_switch_on
    # above; a segment missing from config leaves that dict key absent, so CRM gates
    # fall back to the pre-existing global-only behavior for it.
    _segments_cfg = global_cfg.get("system", {}).get("segments", {})
    for _seg_name, _seg_cfg in _segments_cfg.items():
        if "master_switch" in _seg_cfg:
            crm_ctx.segment_master_switch[_seg_name] = bool(_seg_cfg["master_switch"])
        if "autonomy_mode" in _seg_cfg:
            crm_ctx.segment_autonomy_mode[_seg_name] = str(_seg_cfg["autonomy_mode"])
        if "shadow_mode" in _seg_cfg:
            crm_ctx.segment_shadow_mode[_seg_name] = bool(_seg_cfg["shadow_mode"])
    # _seg_paper_mode was resolved above (broker section) — already folds in the
    # boot --paper/--live flag default and operator.json paper_mode_locked.
    crm_ctx.segment_paper_mode = dict(_seg_paper_mode)
    # Same fail-safe as _resolve_is_paper: a locked account ignores every
    # per-strategy override, so mode_of() falls through to the (all-paper) segment map.
    crm_ctx.strategy_execution_mode = (
        {} if _account_cfg.get("paper_mode_locked", False) else dict(_strategy_execution_mode)
    )
    # The options desk reads routes from crm_ctx (R1). Kept even when locked: routes are only
    # consulted for a LIVE-resolved strategy, so they can only ever add a rejection.
    crm_ctx.strategy_routes = dict(_strategy_routes)
    CRMContext.apply_risk_json(crm_ctx, risk_cfg)

    # Per-strategy LLM gate policy (registry llm_gate_required / llm_originated). A
    # contradictory config raises here at boot rather than trading un-gated.
    from src.llm.gate_policy import load_gate_policy

    _llm_gate_required, _llm_originated_strategies = load_gate_policy()
    crm_ctx.llm_gate_required = dict(_llm_gate_required)

    crm_ctx.morning_open_block_minutes = _sessions.morning_open_block_minutes()
    crm_ctx.last_entry_by_str = f"{_sessions.last_entry_by():%H:%M}"

    autonomy_mode = global_cfg.get("system", {}).get("autonomy_mode", _DEFAULT_AUTONOMY_MODE)
    _exec_order_types = execution_cfg.get("order_types", {})
    _rejection_retry = execution_cfg.get("rejection_retry", {})
    _max_retries = int(_rejection_retry.get("max_retries", 2))
    _retry_delay_seconds = int(_rejection_retry.get("retry_delay_seconds", 1))

    # ── Order execution layer ─────────────────────────────────────────────────
    from src.execution.order_lifecycle import OrderLifecycleManager, OrderMonitor
    from src.execution.price_truth import PriceTruthEngine
    from src.execution.rejection_handler import RejectionHandler

    _monitoring_cfg = execution_cfg.get("monitoring", {})

    # Durable duplicate-order guard (additive; config/execution.json "idempotency").
    # Default mode "shadow" only records + logs duplicates -- it never blocks an order.
    _idem_cfg = execution_cfg.get("idempotency", {})
    _idempotency: Any = None
    if db_pool is not None and str(_idem_cfg.get("mode", "shadow")) != "off":
        from src.db.repositories.order_intents import OrderIntentRepository
        from src.execution.idempotency import DurableIdempotencyGuard

        _intent_repo = OrderIntentRepository(db_pool)
        _idempotency = DurableIdempotencyGuard(
            _intent_repo,
            mode=str(_idem_cfg.get("mode", "shadow")),
            window_seconds=float(_idem_cfg.get("window_seconds", 120)),
            timeout_seconds=float(_idem_cfg.get("db_timeout_seconds", 2.0)),
        )
        try:
            await _intent_repo.purge_older_than(int(_idem_cfg.get("retention_days", 3)))
        except Exception as _pe:  # noqa: BLE001 — housekeeping only
            _log.warning("order_intents_purge_failed err=%s", _pe)
        _log.info("idempotency_guard_enabled mode=%s", _idempotency.mode)

    _rejection_handler = RejectionHandler(
        max_retries=_max_retries,
        retry_delay_seconds=_retry_delay_seconds,
    )

    def _build_stack(
        _broker: Any, _is_paper: bool, broker_name: str | None = None
    ) -> dict[str, Any]:
        # broker_name (live stacks only): stamped on new trade rows as exec_broker and used
        # for the stack's charges; None for paper.
        _lc = OrderLifecycleManager(
            _broker,
            max_risk_rupees=crm_ctx.max_risk_per_trade,
            trade_repo=trade_repo,
            is_paper=_is_paper,
            idempotency=_idempotency,
            rejection_handler=_rejection_handler,
            broker_name=broker_name,
        )
        _om = OrderMonitor(
            _broker,
            mis_flat_by=f"{_sessions.mis_flat_by():%H:%M}",
            is_paper=_is_paper,
            breach_approaching_pct=float(_monitoring_cfg.get("breach_approaching_pct", 0.5)),
            gtt_check_interval_seconds=float(
                _monitoring_cfg.get("gtt_check_interval_seconds", 30.0)
            ),
            trade_repo=trade_repo,
            broker_name=broker_name,
        )
        return {
            "broker": _broker,
            "lifecycle": _lc,
            "order_monitor": _om,
            "price_truth": PriceTruthEngine(_broker),
        }

    # Two independent (broker, OrderLifecycleManager, OrderMonitor, PriceTruthEngine)
    # stacks — one per mode actually in use, not one per segment (a segment's mode
    # is binary, so at most 2 stacks are ever needed). Confirmed no shared mutable
    # state between OrderLifecycleManager/OrderMonitor instances beyond DB rows
    # (which already carry product/strategy_name), so running both concurrently is
    # safe. trade_repo is shared — DB rows are segment/mode-discriminated already.
    _forced_live = await _ensure_live_broker_for_open_positions(
        live_brokers, db_pool, create_broker, _live_broker_name
    )
    for _fname in _forced_live:  # the token-refresh loops must refresh forced brokers too
        _fb = live_brokers[_fname]
        if _fname == "kite" and hasattr(_fb, "set_access_token"):
            _kite_brokers.append(_fb)
        elif _fname == "angel_one":
            _angel_brokers.append(_fb)
        elif _fname == "flattrade":
            _flattrade_targets.append(_fb.set_access_token)
        else:
            _log.info("live_broker_no_token_refresh broker=%s (forced by open positions)", _fname)
    stacks: dict[str, dict[str, Any]] = {}
    if broker_paper is not None:
        stacks[stack_key(True)] = _build_stack(broker_paper, True)
    # One live stack per live broker, legacy broker first ("live:<name>").
    for _lname, _lbroker in live_brokers.items():
        stacks[stack_key(False, _lname)] = _build_stack(_lbroker, False, _lname)
    # Legacy single-live-broker consumers below (options desk, reconciler, boot
    # reconcile) use the primary live stack's broker; per-broker wiring comes later.
    _primary_live = primary_live_key(stacks, _live_broker_name)
    broker_live = stacks[_primary_live]["broker"] if _primary_live else None

    # Restore SL/target monitoring for every trade that was already open before
    # this process started — see _reseed_order_monitor docstring for why this
    # is required (OrderMonitor.track() only fires at entry, never on restart).
    # Each stack reseeds ONLY its own rows (paper stack <- paper_trades, each live
    # stack <- the `trades` rows its broker holds, see _reseed_scope) so a mixed-mode or
    # multi-broker restart never cross-registers positions.
    for _stack_mode, _stack in stacks.items():
        _scope = _reseed_scope(_stack_mode, stacks, _live_broker_name)
        _reseeded = await _reseed_order_monitor(
            _stack["order_monitor"], db_pool, stack_is_paper(_stack_mode), **_scope
        )
        if _reseeded:
            _log.warning("order_monitor_startup_reseed mode=%s count=%d", _stack_mode, _reseeded)
        # Same restart problem for entries that have NOT filled yet: an overnight
        # AMO must still be re-polled after a restart or its fill — and with it the
        # only chance to arm SL/target — is missed entirely.
        _pending = await _reseed_pending_entries(
            _stack["lifecycle"], db_pool, stack_is_paper(_stack_mode), _exec_order_types, **_scope
        )
        if _pending:
            _log.warning("pending_entry_startup_reseed mode=%s count=%d", _stack_mode, _pending)
    # Open live rows whose broker has no stack were reseeded by nobody: say so loudly.
    # Best effort (an alert, never a reason to abort boot), like the 30 s call.
    with contextlib.suppress(Exception):
        await _report_orphaned_live_rows(db_pool, stacks)
    # Legacy singular aliases for every call site not yet stack-aware (options
    # paths, dashboard/status reads via shared["order_monitor"], etc.) — resolve
    # to the mode matching the --paper/--live boot flag, falling back to whichever
    # stack actually exists if that mode has no segments in it.
    if paper_mode and stack_key(True) in stacks:
        _primary_mode = stack_key(True)
    elif not paper_mode and _primary_live:
        _primary_mode = _primary_live
    else:
        _primary_mode = next(iter(stacks))
    lifecycle = stacks[_primary_mode]["lifecycle"]
    order_monitor = stacks[_primary_mode]["order_monitor"]

    # ── Paper ledger ──────────────────────────────────────────────────────────
    # The unified execution path (lifecycle + OrderMonitor) now owns paper entries
    # and exits. PaperTradingEngine is retained ONLY for its table-based ledger
    # helpers over paper_trades — DB-sourced daily risk counters (cap-bypass fix),
    # the EOD/exit-window square-off sweep, and reconciliation. Its execution
    # methods (place_order/monitor_cycle) are no longer called.
    from src.paper_trading.paper_engine import PaperTradingEngine

    paper_ledger = PaperTradingEngine(broker, pool=db_pool)

    # Phase 2 — options market-data (chain/OHLCV quotes) always prefers whichever
    # broker has live market access, independent of which mode actually places
    # options orders; order placement uses the options segment's own resolved
    # paper/live broker.
    _options_data_broker = broker_live if broker_live is not None else (broker_paper or broker)
    _options_is_paper = bool(_seg_paper_mode.get("options", paper_mode))
    # The seller desk's own registry execution_mode wins over the segment
    # default (unless the account is locked to paper).
    if not _account_cfg.get("paper_mode_locked", False):
        _opt_override = _strategy_execution_mode.get("options_seller_desk_v3")
        if _opt_override in ("paper", "live"):
            _options_is_paper = _opt_override == "paper"
    _options_order_broker = (broker_paper if _options_is_paper else broker_live) or broker

    # ── Phase 6 — Options structure selection (execution.json authoritative) ──
    _options_cfg = execution_cfg.get("options_execution", {})
    _options_components: dict[str, Any] | None = None
    if _options_cfg.get("enabled"):
        from src.db.repositories.options import OptionsRepository
        from src.options.nfo_chain import NFOChainProvider
        from src.options.options_executor import OptionsExecutor
        from src.options.options_risk import OptionsRiskGate
        from src.options.structure_selector import StructureSelector

        _options_repo = OptionsRepository(db_pool) if db_pool is not None else None
        # Options learning-engine PR6 — load any persisted conviction-calibration
        # bounds at boot (empty dict if none yet, or DB unavailable); "load at
        # boot" mirrors equity's calibration_thresholds pattern. StructureSelector
        # only ever USES these when options_conviction_calibration.mode=="live"
        # (see _p_win()/_bounds_for()) — loading them unconditionally here is safe.
        _options_conviction_bounds: dict[str, tuple[float, float]] = {}
        if _options_repo is not None:
            try:
                _options_conviction_bounds = await _options_repo.load_conviction_calibration(
                    is_paper=_options_is_paper
                )
            except Exception as _cbe:
                _log.debug("options_conviction_bounds_load_err err=%s", str(_cbe)[:80])
        _options_components = {
            "enabled": True,
            "cfg": _options_cfg,
            "provider": NFOChainProvider(_options_data_broker),
            "selector": StructureSelector(
                _options_cfg, conviction_bounds=_options_conviction_bounds
            ),
            "gate": OptionsRiskGate(_options_cfg),
            "executor": OptionsExecutor(
                _options_order_broker, _options_repo, is_paper=_options_is_paper
            ),
            "repo": _options_repo,
            "broker": _options_order_broker,
            # Live broker the boot executor trades through (None = paper); the buyer-first
            # path rejects a live route naming any other broker (one executor in this phase).
            "exec_broker": (
                None
                if _options_is_paper
                else ((stack_broker(_primary_live) if _primary_live else None) or _live_broker_name)
            ),
        }
        _log.info("options_structure_selection_enabled")

    # ── Seller-first options engine foundation (options_settings.json) ─────────
    # Separate decision path from the buyer-first block above — data infra only
    # (chain fetch + volatility intelligence), no strategy logic, no order
    # placement. Chain/volatility data always uses the market-data broker;
    # order placement (OptionsLegExecutor, below) uses the options segment's own
    # resolved paper/live broker (Phase 2 — was unconditionally the single
    # primary `broker` before per-segment paper_mode existed).
    from src.options import seller_settings as _seller_settings

    _seller_options: dict[str, Any] | None = None
    if _seller_settings.is_enabled():
        from src.db.repositories.options import OptionsRepository as _SellerOptionsRepository
        from src.options.chain_fetcher import ChainFetcher
        from src.options.volatility_engine import VolatilityEngine

        _seller_repo = _SellerOptionsRepository(db_pool) if db_pool is not None else None
        if _seller_repo is not None:
            _seller_chain_fetcher = ChainFetcher(_options_data_broker, _make_redis_client())
            _seller_options = {
                "engine": VolatilityEngine(
                    _options_data_broker,
                    _seller_chain_fetcher,
                    _seller_repo,
                    _seller_settings.universe_indices(),
                ),
                "chain_fetcher": _seller_chain_fetcher,
                "repo": _seller_repo,
            }
            _log.info(
                "seller_options_foundation_enabled indices=%s",
                _seller_settings.universe_indices(),
            )
        else:
            _log.warning("seller_options_foundation_skipped reason=no_db_pool")

    # ── Supervisor ────────────────────────────────────────────────────────────
    from src.supervisor.supervisor import Supervisor, SupervisorState

    capital = operator_cfg.get("capital", {}).get("total_capital_rupees", 100_000)
    max_dd = float(risk_cfg.get("circuit_breakers", {}).get("daily_loss_limit_rupees", 4000.0))
    max_pos = operator_cfg.get("hard_limits", {}).get("max_open_positions", 5)
    supervisor = Supervisor(
        broker=broker,
        state=SupervisorState(
            current_equity=capital,
            starting_capital=capital,
            daily_loss_so_far=0.0,
            max_daily_loss=max_dd,
            open_position_count=0,
            max_open_positions=max_pos,
        ),
        operator_config=operator_cfg,
        db_pool=db_pool,
        supervisor_cfg=_cfg("supervisor"),
        paper_ledger=paper_ledger,
    )
    for _s in stacks.values():
        _s["lifecycle"]._alert_manager = supervisor._alert_manager
        _s["order_monitor"]._alert_manager = supervisor._alert_manager
    # Process-wide hook so deep modules (LLM providers, funnel) can reach the dashboard
    # Alerts tab + Telegram without a handle on the supervisor.
    from src.infrastructure import operator_alerts as _operator_alerts

    _operator_alerts.configure(supervisor._alert_manager)

    # Show at startup which broker each live registry strategy trades through, and which live
    # strategies will have every signal rejected (R1, or an ok route whose broker got no live
    # stack/executor) -- before the first signal does. CRITICAL alert unless paper-locked
    # (then they all run as PAPER and the text says so).
    _route_tbl, _route_rejected = route_table(
        _strategy_routes, {b for b in map(stack_broker, live_stacks(stacks)) if b}
    )
    _log.info("strategy_routes_table locked=%s %s", _locked, _route_tbl)
    if _route_rejected:
        _route_msg = rejected_routes_message(_route_rejected, _locked)
        _log.warning(
            "strategy_routes_rejected count=%d locked=%s %s",
            len(_route_rejected),
            _locked,
            _route_msg,
        )
        if not _locked:
            _operator_alerts.raise_alert(
                _route_msg,
                source="strategy_routes_rejected",
                tier="CRITICAL",
                dedupe_seconds=4 * 3600,
            )

    # Set initial equity on CRM context (G1 25% floor + G2 drawdown staircase)
    crm_ctx.current_equity = _initial_equity
    from src.risk.budget import set_current_equity as _set_budget_equity_boot

    _set_budget_equity_boot(_initial_equity)

    # Shared mutable state — loops write, _crm_loop reads
    _state: dict[str, Any] = {
        "db_pool": db_pool,
        # repositories (None if DB unavailable)
        "news_repo": news_repo,
        "market_data_repo": market_data_repo,
        "learning_repo": learning_repo,
        "ohlcv_repo": ohlcv_repo,
        "supervisor_repo": supervisor_repo,
        "llm_swing_journal_repo": llm_swing_journal_repo,
        "llm_intraday_journal_repo": llm_intraday_journal_repo,
        "llm_call_log_repo": llm_call_log_repo,
        "fundamentals_repo": fundamentals_repo,
        # Process-wide AlertManager (feeds logs/alerts.json -> dashboard Alerts tab).
        "alert_manager": supervisor._alert_manager,  # also -> operator_alerts.configure below
        # Symbol -> latest fundamentals_snapshot row, populated once daily by
        # _fundamentals_fetch_loop. Log-only shadow data — see that loop's docstring.
        "fundamentals_cache": {},
        # redis client for regime_current key
        "redis_client": _make_redis_client(),
        "survival_mode": _survival_mode,
        "regime": "BULL",
        "health_scores": {},
        "ask_mode": False,  # §8.3: True when any strategy is STRESSED
        "earnings": {},
        "current_equity": _initial_equity,
        "available_cash": _initial_equity,  # seeded; equity_loop refreshes from broker every 60s
        "starting_equity": _initial_equity,  # never updated — baseline for drawdown_pct
        "gap_risk": [],
        "correlation_level": "NORMAL",
        "closed_trades": _load_closed_trades_log(),
        # Layer 7 (causal memory) gating moved to config/learning.json
        # causal_memory.mode (see _learning_loop and the G9 signal-build causal-
        # context injection) — this shared-dict flag is no longer read.
        # History: left OFF 2026-08-20 (FK relaxed migration 059, Groq model
        # deprecation fixed, _ollama() error-swallowing fixed — but the real
        # backfill via analyse_and_store's actual prompt STILL failed on every
        # trade despite an isolated raw LLM call succeeding, root cause not
        # isolated). Re-verify this is actually fixed before moving
        # causal_memory.mode off "off" — don't assume the above fixes covered it.
        "capital_critical": False,
        "quality_raise": 0.0,
        "bypass_llm": False,
        "llm_pending": [],
        "halt_trading_weeks": False,
        "consecutive_negative_weeks": 0,
        "news_avoid_symbols": {},
        "news_high_magnitude": {},
        "news_recent": {},
        "news_priority_symbols": {},
        "news_macro_bias": None,
        "rs_top20": set(),
        "rs_blacklist": set(),
        "rs_flags": {},  # §8.2: symbol → rs_new_high bool
        "sector_status": {},
        "slippage_stats": [],
        "paused_strategies": set(),
        "expectancy_status": {},
        "backtest_queue": set(),  # §8.4: strategies queued for walk-forward backtest
        "nightly_setups": {},
        "calibration_thresholds": {},  # §8.9: raised min confidence per strategy — loaded from DB below
        "causal_context": {},  # §9 L7: generalizable insights keyed by strategy|regime
        "discovery_scan": {},
        "watchlist": [s.replace("NSE:", "") for s in symbols],
        # strategy registry — read by _intraday_signal_scan_loop to find 5MIN/15MIN strategies
        "strategy_registry": strategy_reg,
        # execution layer — ONE wiring for paper + live: lifecycle places entries and
        # order_monitor manages SL/target/exit. The injected broker adapter is the only
        # difference (paper simulates, live hits Kite). paper_ledger = table-based
        # paper_trades helpers (counters / EOD square-off / reconcile) only.
        "crm": crm,
        "crm_ctx": crm_ctx,
        "trade_repo": trade_repo,
        "lifecycle": lifecycle,
        "order_monitor": order_monitor,
        # Per-mode execution stacks, keyed "paper"/"live:<broker>". _execute_intraday_signal and
        # _execute_swing_signal resolve their stack from this by the segment's own
        # paper_mode, and fall back to the primary lifecycle above when it is absent.
        # 2026-08-27: this key was read in four places and assigned in none, so the
        # fallback was ALWAYS taken — every swing CNC order went to the paper adapter
        # even with system.segments.swing.paper_mode=false. Per-segment live routing
        # had never actually routed anything to live.
        "stacks": stacks,
        "paper_ledger": paper_ledger,
        "paper_mode": paper_mode,
        "paper_mode_locked": _locked,
        # Per-strategy paper/live override (strategies_registry.json
        # execution_mode) — see _resolve_is_paper for the full precedence.
        "strategy_execution_mode": _strategy_execution_mode,
        # Live registry strategies' broker routes (R1) and the global.json broker used by
        # strategies outside the registry.
        "strategy_routes": _strategy_routes,
        "legacy_live_broker": _live_broker_name,
        # pre-order chase guard: skip an intraday entry if the fill price has drifted
        # more than this many R past the breakout (risk.json hard_limits override).
        "max_entry_chase_r": float(risk_cfg.get("hard_limits", {}).get("max_entry_chase_r", 0.75)),
        "autonomy_mode": autonomy_mode,
        "intraday_traded": {},  # date-keyed set of "strategy|symbol" → 1 trade/day gate
        "swing_traded": {},  # date-keyed set of "strategy|symbol" → 1 swing trade/day gate
        "news_opportunities": {},  # symbol → {confidence_boost, event_type, reasoning, ts}
        # Intraday breadth gate thresholds — read from operator.json hard_limits.intraday.
        # LONG blocked when bearish_pct >= bear_gate_threshold; SHORT blocked when
        # (1-bearish_pct) >= bull_gate_threshold. Lowered from code default 0.80 → 0.60
        # so the gate fires earlier (bear market → no longs at 60% bearish, not 80%).
        "bear_gate_threshold": float(
            operator_cfg.get("hard_limits", {}).get("intraday", {}).get("bear_gate_threshold", 0.60)
        ),
        "bull_gate_threshold": float(
            operator_cfg.get("hard_limits", {}).get("intraday", {}).get("bull_gate_threshold", 0.60)
        ),
        # HARD structural-regime gate — operator rule: no counter-regime intraday
        # entries (BEAR* blocks LONG, BULL* blocks SHORT). Defaults ON (fail-safe).
        "structural_regime_gate": bool(
            operator_cfg.get("hard_limits", {})
            .get("intraday", {})
            .get("structural_regime_gate", True)
        ),
        # Day-scoped intraday regime knobs (index day-move + breadth, per cycle).
        "intraday_regime_cfg": operator_cfg.get("hard_limits", {})
        .get("intraday", {})
        .get("regime", {}),
        # Strategies whose ideas come from an LLM (registry `llm_originated`): their G9
        # reviewer is the gemini-free chain, and the LLM gate is always required.
        "llm_originated_strategies": _llm_originated_strategies,
        # Phase 6 — options structure-selection components (None when disabled)
        "options": _options_components,
        "options_universe": [s for s in symbols],  # underlyings for daily IV snapshots
        "sector_concentrated": False,
        # Seller-first foundation: chain fetch + volatility intelligence (None when disabled)
        "seller_options": _seller_options,
        "broker": broker,
        "kite_brokers": _kite_brokers,
        "angel_brokers": _angel_brokers,
        "flattrade_token_targets": _flattrade_targets,
        # execution.json order_types, read here so callers of
        # place_entry_protection_and_poll (intraday + swing) can pass it through
        # instead of falling back to their own hardcoded defaults (2026-08-20:
        # config existed but nothing ever read it — SL type for CNC/GTT and MIS
        # entry/target silently ignored whatever this file said).
        "order_types": _exec_order_types,
    }

    # Mobile trade notifications (Telegram) — in-process asyncio.Queue + worker,
    # not a DB outbox: every hook site (order_lifecycle, this file, paper_engine)
    # lives in this one process (launcher.py runs the API server and the
    # orchestrator as separate OS processes; the dashboard's own manual-exit
    # action already crosses into this process via manual_order_actions, so its
    # exit is notified from here too). No-op when TELEGRAM_BOT_TOKEN/CHAT_ID are
    # unset in .env — init_notifier() returns None and every notify() call
    # downstream becomes a guaranteed no-op.
    from src.db.repositories.notifications import NotificationRepository
    from src.notifications import init_notifier
    from src.notifications.summary import (
        eod_summary_loop,
        market_holiday_loop,
        position_update_loop,
    )

    _notifier = init_notifier(NotificationRepository(db_pool) if db_pool is not None else None)
    if _notifier is not None:
        _notifier.set_alert_manager(supervisor._alert_manager)

    # Restore partial/breakeven/trail management for every swing position that
    # was already open before this process started (same reasoning as the
    # order_monitor reseed above, applied to swing_exit_state).
    _swing_reseeded = await _reseed_swing_exit_state(_state)
    if _swing_reseeded:
        _log.warning("swing_exit_state_startup_reseed count=%d", _swing_reseeded)

    # Same restart problem, intraday side (partial(1R)/breakeven(1R)/ATR-trail(1R)
    # smart-exit state for intraday strategies) — see _reseed_intraday_exit_state docstring.
    _intraday_reseeded = await _reseed_intraday_exit_state(_state)
    if _intraday_reseeded:
        _log.warning("intraday_exit_state_startup_reseed count=%d", _intraday_reseeded)

    # Same restart problem again, one layer earlier: an llm_intraday_agent idea
    # proposed but not yet evaluated into a trade lived only in memory and a
    # restart silently erased it (2026-09-23 — see _reseed_llm_intraday_ideas
    # docstring for the incident this closes).
    await _reseed_llm_intraday_ideas(_state)

    # Clear stale paper positions left open from a prior trading day: square them
    # off at each symbol's entry-day close so they become proper CLOSED records
    # (not phantom "open" positions on today's dashboard). Startup-only, paper mode.
    if paper_mode:
        try:
            _n_stale = await paper_ledger.square_off_open(
                ohlcv_repo,
                prior_day_only=True,
                reason="STARTUP_STALE",
                order_monitors=_order_monitors_from_shared(_state),
            )
            if _n_stale:
                _log.info("paper_startup_squareoff closed=%d", _n_stale)
        except Exception as _exc:  # best-effort; never block boot
            _log.error("paper_startup_squareoff_err err=%s", _exc)

    # Load persisted calibration thresholds from DB (source of truth)
    if learning_repo is not None:
        try:
            _thresholds_from_db = await learning_repo.load_calibration_thresholds()
            if _thresholds_from_db:
                _state["calibration_thresholds"] = _thresholds_from_db
                _log.info("calibration_thresholds_loaded count=%d", len(_thresholds_from_db))
        except Exception:
            pass
    _regime_log = _LOGS / "market_regime.json"
    if _regime_log.exists():
        try:
            _state["regime"] = json.loads(_regime_log.read_text(encoding="utf-8")).get(
                "regime", "BULL"
            )
        except Exception:
            pass
    _health_log = _LOGS / "strategy_health.json"
    if _health_log.exists():
        try:
            _state["health_scores"] = json.loads(_health_log.read_text(encoding="utf-8"))
        except Exception:
            pass

    # ── Earnings calendar ─────────────────────────────────────────────────────
    from src.market_data.earnings_calendar import EarningsCalendar

    earnings_calendar = EarningsCalendar(log_path=_LOGS / "earnings_calendar.json")

    # ── Seller-desk decision + execution pipeline (needs earnings_calendar) ────
    if _seller_options is not None:
        # _seller_options is only ever set (above) inside `if _seller_repo is not
        # None`, so this invariant always holds here — raised explicitly (not
        # asserted, which -O strips) for mypy, which can't track narrowing
        # across the two separate if-blocks.
        if _seller_repo is None:
            raise RuntimeError("unreachable: _seller_options implies _seller_repo is not None")
        from src.options.event_calendar import EventCalendar
        from src.options.greeks_budget import compute_budget as _seller_compute_budget
        from src.options.options_leg_executor import OptionsLegExecutor
        from src.options.seller_desk import SellerDesk
        from src.options.strategy_selector import StrategySelector as _SellerStrategySelector

        _seller_margin_cfg = _seller_settings.load_settings().get("margin", {})
        _seller_rom_cfg = _seller_settings.load_settings().get("rom", {})
        _seller_capital = float(_seller_settings.load_settings().get("options_capital_rupees", 0))
        # 2026-10-07: config/capital.json options.max_structure_loss_per_strategy is now
        # the single source of truth for the desk's per-trade risk budget (the plan's
        # Phase 2 promise -- "SellerDesk size_position replaces sizing.risk_per_trade_pct"
        # was wired into CRM G8 as a downstream cap, but this upstream proposal still read
        # the old fixed options_capital_rupees sleeve x options_settings.json sizing.
        # risk_per_trade_pct, so the desk over/under-proposed lots before G8 silently
        # trimmed them, and neither side auto-scaled with total_capital_rupees changes).
        # options_capital stays the fixed sleeve (Greeks budget below is deliberately
        # untouched, out of this redesign's scope) -- only risk_per_trade_pct is
        # recomputed so options_capital * risk_per_trade_pct/100 reproduces RiskBudget's
        # own equity-scaled rupee figure exactly, matching G8's cap at boot.
        from src.risk.budget import get_budget as _rb_get_budget_seller_sizing
        from src.risk.budget import get_current_equity as _rb_get_equity_seller_sizing

        _seller_equity_for_sizing = (
            _rb_get_equity_seller_sizing()
            or _rb_get_budget_seller_sizing().account.fallback_equity_rupees
        )
        _seller_risk_per_trade_pct = (
            _rb_get_budget_seller_sizing().options.max_structure_loss_per_strategy_rupees(
                _seller_equity_for_sizing
            )
            / _seller_capital
            * 100.0
            if _seller_capital > 0
            else 0.0
        )
        _seller_options["event_calendar"] = EventCalendar(_seller_repo, earnings=earnings_calendar)
        # One options executor per stack ("paper", "live:<broker>"): each position is placed,
        # closed and rolled through its OWN broker's executor (positions remember exec_broker).
        _seller_options["executors"] = {
            _sk: OptionsLegExecutor(_st["broker"]) for _sk, _st in stacks.items()
        }
        _desk_key = stack_key(True) if _options_is_paper else _primary_live
        _seller_options["executor"] = _seller_options["executors"].get(
            _desk_key or ""
        ) or OptionsLegExecutor(_options_order_broker)
        # Executor for the OTHER mode, so a desk-hosted strategy (weekly condor, options_expiry_day)
        # follows its OWN registry execution_mode -- going live is one registry switch (+restart).
        # None when that mode's broker isn't wired (e.g. account paper_mode_locked) -> fail closed.
        # (Kept for the legacy call style; the desk resolves executors from the map above.)
        _alt_key = _primary_live if _options_is_paper else stack_key(True)
        _seller_options["alt_executor"] = (
            _seller_options["executors"].get(_alt_key) if _alt_key else None
        )
        _seller_options["desk"] = SellerDesk(
            _SellerStrategySelector(),
            _seller_options["event_calendar"],
            _seller_options["executor"],
            _seller_options["chain_fetcher"],
            options_capital=_seller_capital,
            risk_per_trade_pct=_seller_risk_per_trade_pct,
            margin_safety_pct=1.0
            - float(_seller_margin_cfg.get("margin_cushion_minimum_pct", 20)) / 100.0,
            # 2026-10-07: was _seller_capital (the fixed options_capital_rupees sleeve,
            # Rs 10L) -- found live: the desk's real equity is Rs 20L+, so the Greeks
            # budget (gamma/vega/delta/theta headroom) was computed against roughly HALF
            # the real account, leaving no room for the condor alongside the desk's own
            # 2 existing positions (sizing_rejected SIZE_TOO_SMALL_FOR_RISK_LIMIT). Same
            # live-equity figure already used for risk_per_trade_pct above.
            greeks_budget=_seller_compute_budget(
                _seller_equity_for_sizing, _seller_settings.load_settings().get("greeks_budget", {})
            ),
            min_rom_pct=float(_seller_rom_cfg.get("min_annualized_pct", 24)),
            enabled_strategy_types=_seller_settings.enabled_strategies(),
            repo=_seller_repo,
            is_paper=_options_is_paper,
            alt_executor=_seller_options["alt_executor"],
            executors=_seller_options["executors"],
        )
        # The legacy live broker: the primary live executor's broker, used by live strategies
        # without a registry route (a routed live strategy uses its own broker's executor).
        _seller_options["desk"].live_broker_name = (
            (stack_broker(_primary_live) or _live_broker_name) if _primary_live else None
        )
        # system.per_broker_equity: live options entries sized/capped on their broker's equity.
        _seller_options["desk"].per_broker_equity = (
            _cfg("global").get("system", {}).get("per_broker_equity", False) is True
        )
        _seller_restored = await _seller_options["desk"].restore_positions()
        _log.info(
            "seller_desk_pipeline_wired capital=%.0f enabled_strategies=%s restored_positions=%d",
            _seller_capital,
            sorted(_seller_options["desk"].enabled_strategy_types),
            _seller_restored,
        )

    # ── Corporate action pre-check ────────────────────────────────────────────
    from src.market_data.corporate_actions import CorporateActionWatcher, get_rights_symbols

    ca_watcher = CorporateActionWatcher()
    # Refresh from NSE API before checking today's actions
    try:
        import aiohttp as _aiohttp

        async with _aiohttp.ClientSession() as _ca_session:
            _refreshed = await ca_watcher.refresh(_ca_session)
            _log.info("corporate_action_nse_refresh ok=%s", _refreshed)
    except Exception as _ca_exc:
        _log.warning("corporate_action_nse_refresh_failed err=%s — using local cache", _ca_exc)

    monitored_symbols = {p.symbol for p in order_monitor.get_monitored()}
    if monitored_symbols:
        for action in ca_watcher.pending_for_today(monitored_symbols):
            _log.warning(
                "corporate_action_today symbol=%s type=%s ex_date=%s",
                action.symbol,
                action.action_type,
                action.ex_date,
            )
            if supervisor._alert_manager is not None:
                from src.infrastructure.alert_system import AlertTier

                supervisor._alert_manager.raise_alert(
                    AlertTier.CRITICAL,
                    f"Corporate action today: {action.symbol} {action.action_type} "
                    f"{action.ratio_numerator}:{action.ratio_denominator} — review SL/target/qty",
                    source="corporate_action_watcher",
                )

        # Block trading on any RIGHTS ISSUE symbols until settled
        _pos_list = [
            (
                p.symbol,
                p.quantity,
                p.fill_price,
                p.current_sl,
                getattr(p, "target", p.fill_price * 1.1),
            )
            for p in order_monitor.get_monitored()
        ]
        _ca_adjustments = ca_watcher.adjustments_for_positions(_pos_list)
        _rights = get_rights_symbols(_ca_adjustments)
        if _rights:
            _existing_avoid: set[str] = set(_state.get("news_avoid_symbols", []))
            _state["news_avoid_symbols"] = list(_existing_avoid | _rights)
            for _rsym in _rights:
                _log.warning("corporate_action_rights_block symbol=%s trading_blocked=True", _rsym)

    # ── Shutdown ──────────────────────────────────────────────────────────────
    stop = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
    except NotImplementedError:
        # Windows: add_signal_handler not supported
        def _win_stop(*_: object) -> None:
            asyncio.get_event_loop().call_soon_threadsafe(stop.set)

        signal.signal(signal.SIGINT, _win_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _win_stop)

    # Load NSE holidays for gap risk manager (single source of truth: src.market.calendar)
    _nse_holidays: set[Any] = set(load_holidays())

    _news_log = _LOGS / "news_status.json"
    if _news_log.exists():
        _news_log.write_text("[]", encoding="utf-8")

    async def _heartbeat_loop() -> None:
        """Every 10 s upsert one heartbeat row per in-process engine.

        AshwaX runs single-process (engines are asyncio tasks, not separate OS
        processes — see worker_pool.py), so every engine shares this PID and the
        same process-wide CPU/RSS footprint. We report the REAL measured process
        metrics (psutil) instead of fabricating per-engine numbers. If the process
        dies it stops beating, so /api/components marks engines stale → STOPPED.
        The supervisor beats its own 'supervisor' row separately.
        """
        import gc as _gc
        import json as _json
        import os as _os

        import psutil as _psutil

        _pid = _os.getpid()
        _proc = _psutil.Process(_pid)
        _proc.cpu_percent(None)  # prime — first psutil sample is always 0.0
        _engines = [
            "orchestrator",
            "EquityIngester",
            "CentralRiskManager",
            "OrderMonitor",
            "NewsIngester",
        ]
        while not stop.is_set():
            _pool = _state.get("db_pool")
            if _pool is not None:
                try:
                    _meta = _json.dumps(
                        {
                            "cpu": round(_proc.cpu_percent(None), 1),
                            "rss_mb": round(_proc.memory_info().rss / 1_000_000, 1),
                            "gc": sum(_gc.get_count()),
                        }
                    )
                    _rows = [(e, _pid, "ALIVE", _meta) for e in _engines]
                    await _pool.executemany(
                        """
                        INSERT INTO engine_heartbeats (engine_name, pid, status, metadata, last_beat)
                        VALUES ($1, $2, $3, $4::jsonb, NOW())
                        ON CONFLICT (engine_name) DO UPDATE
                            SET pid = EXCLUDED.pid, status = EXCLUDED.status,
                                metadata = EXCLUDED.metadata, last_beat = NOW()
                        """,
                        _rows,
                    )
                except Exception as _hb_exc:
                    _log.debug("heartbeat_write_failed err=%s", _hb_exc)
                # WebSocket health snapshot (DB truth → /api/ws_health)
                try:
                    await _pool.execute(
                        "INSERT INTO ws_health (id, snapshot, updated_at) "
                        "VALUES (1, $1::jsonb, NOW()) "
                        "ON CONFLICT (id) DO UPDATE SET snapshot = EXCLUDED.snapshot, updated_at = NOW()",
                        _json.dumps(ingester.health_snapshot()),
                    )
                except Exception as _ws_exc:
                    _log.debug("ws_health_write_failed err=%s", _ws_exc)
            await asyncio.sleep(10.0)

    async def _notifier_supervisor_loop() -> None:
        """Restart-on-crash wrapper around Notifier.run(). The main gather()
        uses return_exceptions=True, so a dead worker task would otherwise be
        silently swallowed and the phone would just go quiet — mirrors the
        restart idiom other long-lived loops use (e.g. _monitor_loop)."""
        if _notifier is None:
            return
        while not stop.is_set():
            try:
                await _notifier.run(stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.error("notifier_worker_crashed err=%s — restarting in 5s", exc)
                await asyncio.sleep(5.0)

    async def _ledger_health_loop() -> None:
        """Every 5 min: alert (CRITICAL, Alerts tab + Telegram) if a ledger write failed or a closed
        trade's ledger quantities do not add up. The ledger itself is written by DB triggers
        (migration 083); this only makes a silent failure of it impossible."""
        import contextlib

        from src.db.repositories.trade_executions import TradeExecutionsRepository
        from src.infrastructure import operator_alerts

        _seen_err_id = 0
        _first = True
        while not stop.is_set():
            _pool = _state.get("db_pool")
            if _pool is not None:
                try:
                    _repo = TradeExecutionsRepository(_pool)
                    _errs = await _repo.ledger_errors_after(_seen_err_id)
                    if _errs:
                        _seen_err_id = max(e["id"] for e in _errs)
                        if not _first:  # rows from before this boot were reported then
                            operator_alerts.raise_alert(
                                f"LEDGER WRITE FAILED for {len(_errs)} trade event(s): "
                                f"{[e['trade_id'] for e in _errs][:5]} -- trade_executions is missing "
                                "events; see rows with event=LEDGER_ERROR",
                                source="ledger:write_failed",
                                dedupe_seconds=3600,
                            )
                    _first = False
                    _bad = await _repo.find_inconsistencies()
                    if _bad:
                        operator_alerts.raise_alert(
                            f"LEDGER MISMATCH on {len(_bad)} closed trade(s): "
                            f"{[b['trade_id'] for b in _bad][:5]} -- shares entered != shares "
                            "exited in trade_executions",
                            source="ledger:quantity_mismatch",
                            dedupe_seconds=3600,
                        )
                except Exception as _le:
                    _log.warning("ledger_health_check_failed err=%s", str(_le)[:160])
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=300.0)

    async def _reconciliation_loop() -> None:
        """Every 30 s reconcile DB-open trades vs broker positions (DB truth →
        /api/reconciliation). Kept OUT of _heartbeat_loop so a slow/hung broker
        call can never starve the liveness beat. The broker round-trip is bounded
        by asyncio.wait_for so a hung endpoint degrades instead of freezing.
        """
        import contextlib
        import json as _json
        import time as _time

        while not stop.is_set():
            _pool = _state.get("db_pool")
            if _pool is not None:
                try:
                    _t0 = _time.monotonic()
                    # _fetch_db_open_trades() reads the `trades` table — LIVE trades only,
                    # never paper_trades — so it must always be reconciled against the LIVE
                    # broker, not the ambiguous "primary" `broker` (which is the PAPER
                    # adapter whenever the boot/global mode is paper, e.g. a live swing
                    # segment under an otherwise-paper boot). Using `broker` here made
                    # Reconciler compare real IFCI/JINDALSAW trades against the paper
                    # adapter's own empty holdings/positions, so every live position was
                    # unconditionally reported as a GHOST_DB_TRADE (found 2026-09-02).
                    # Phase 2: one reconcile per LIVE broker (its own rows vs its own
                    # books), primary first; with no live stack the primary `broker`
                    # exactly as before. `_res`/`_db_open` stay the primary's.
                    _live_broker_for_positions = broker_live if broker_live is not None else broker
                    _rec_targets = _reconcile_targets(_state, stacks, _live_broker_for_positions)
                    _runs = await _reconcile_live_brokers(
                        _state,
                        stacks,
                        _live_broker_for_positions,
                        _pool,
                        trade_repo,
                        supervisor._alert_manager,
                        call_timeout=8.0,
                    )
                    _db_open = _runs[0]["db_open"]
                    _res = _runs[0]["result"]
                    _auto_closed = sum(int(r["auto_closed"]) for r in _runs)
                    _all_gap_names = [f"{g.kind}:{g.symbol}" for g in _res.gaps] + [
                        f"{g.kind}:{g.symbol}@{stack_broker(r['key']) if r['key'] else 'legacy'}"
                        for r in _runs[1:]
                        for g in r["result"].gaps
                    ]
                    _rec_summary = (
                        len(_db_open),
                        ", ".join(_all_gap_names) or "none",
                        _auto_closed,
                    )
                    # Every 30s, forever, drowned the log with an unchanged line; log at INFO
                    # only when the result changes (still DEBUG every cycle).
                    _rec_changed = _state.get("_rec_summary") != _rec_summary
                    _rec_log = _log.info if _rec_changed else _log.debug
                    _state["_rec_summary"] = _rec_summary
                    if _rec_changed:
                        # reconciliation_log holds only the LATEST run (one upserted row), so an
                        # episode such as a ghost-position alarm left no history. Keep every
                        # CHANGE of the gap set as a durable event with its start time.
                        with contextlib.suppress(Exception):
                            await _pool.execute(
                                "INSERT INTO runtime_events (event_type, source, severity, payload) "
                                "VALUES ('RECONCILIATION_CHANGED', 'reconciler', $1, $2::jsonb)",
                                "WARNING" if any(r["result"].gaps for r in _runs) else "INFO",
                                _json.dumps(
                                    {
                                        "db_open_trades": _rec_summary[0],
                                        "gaps": [
                                            {
                                                "kind": g.kind,
                                                "symbol": g.symbol,
                                                "broker_qty": g.broker_qty,
                                                "db_qty": g.db_qty,
                                                "agent_trade_id": g.agent_trade_id,
                                            }
                                            for g in _res.gaps
                                        ],
                                        "auto_closed": _rec_summary[2],
                                        **(
                                            {
                                                "other_brokers": [
                                                    {
                                                        "broker": stack_broker(r["key"])
                                                        if r["key"]
                                                        else "legacy",
                                                        "gaps": [
                                                            {
                                                                "kind": g.kind,
                                                                "symbol": g.symbol,
                                                                "broker_qty": g.broker_qty,
                                                                "db_qty": g.db_qty,
                                                                "agent_trade_id": g.agent_trade_id,
                                                            }
                                                            for g in r["result"].gaps
                                                        ],
                                                    }
                                                    for r in _runs[1:]
                                                ]
                                            }
                                            if len(_runs) > 1
                                            else {}
                                        ),
                                    }
                                ),
                            )
                    _rec_log(
                        "reconciliation_check db_open_trades=%d gaps_found=%d (%s) auto_closed=%d",
                        _rec_summary[0],
                        sum(len(r["result"].gaps) for r in _runs),
                        _rec_summary[1],
                        _rec_summary[2],
                    )
                    # Phase 2: kept fully separate — a symbol held in paper must NOT block a
                    # live entry on that symbol, and vice versa (confirmed with user). Any
                    # nonzero broker net position (including external/manual holdings) is
                    # "live"; the paper set comes only from paper_trades. Live = the UNION
                    # of every live broker's books (R3), each its day positions PLUS
                    # delivery holdings, which is where any swing position from a prior
                    # day lives (found 2026-09-21: positions() alone never saw them, so
                    # a real-money swing entry could duplicate a held symbol). On a
                    # holdings failure keep the last known set — never silently shrink
                    # the guard for real money.
                    (
                        _held_live,
                        _held_live_primary,
                        _held_all_loaded,  # noqa: F841 -- scoped via held_symbols_unloaded_keys
                    ) = await _held_symbols_live_union(_state, _rec_targets, call_timeout=8.0)
                    _held_paper: set[str] = set()
                    try:
                        # CNC (swing) rows stay held across multiple days, so they must
                        # NOT be restricted to today — only MIS (intraday, always flat by
                        # EOD) needs the same-day check. Without this split, a swing
                        # position still open from a prior day drops out of `held_symbols`
                        # after midnight and the duplicate-entry guard re-enters it.
                        _prows = await _pool.fetch(
                            "SELECT DISTINCT symbol FROM paper_trades "
                            "WHERE order_state NOT IN "
                            "('CLOSED','REJECTED','CANCELLED','TIMED_OUT') "
                            "AND fill_price IS NOT NULL "
                            "AND (product = 'CNC' OR "
                            "(entry_timestamp AT TIME ZONE 'Asia/Kolkata')::date "
                            "= (NOW() AT TIME ZONE 'Asia/Kolkata')::date)"
                        )
                        _held_paper = {str(r["symbol"]).split(":")[-1] for r in _prows}
                    except Exception as _pe:
                        _log.error("paper_held_fetch_err %s", str(_pe)[:80])
                    _state["held_symbols_live"] = _held_live
                    _state["held_symbols_paper"] = _held_paper
                    # Legacy union — kept for any caller not yet mode-aware.
                    _state["held_symbols"] = _held_live | _held_paper
                    # Mark that the held set has been authoritatively loaded at least once,
                    # so the entry guard can fail CLOSED (refuse to trade) before the first
                    # reconciliation completes — no more restart-window re-entries. Every
                    # The PRIMARY's books were read (a primary failure raises above); a live
                    # broker never read yet keeps ITS live entries fail-closed via
                    # held_symbols_unloaded_keys (_held_symbols_unknown_for), not everyone's.
                    _state["held_symbols_loaded"] = True
                    # Self-healing safety net alongside the one-shot startup reseed:
                    # catches any position that entered without a track() call
                    # reaching OrderMonitor (e.g. an exception between fill and
                    # track(), or a future call site that forgets to wire it).
                    for _rc_mode, _rc_stack in stacks.items():
                        _rc_scope = _reseed_scope(_rc_mode, stacks, _live_broker_name)
                        with contextlib.suppress(Exception):
                            await _reseed_order_monitor(
                                _rc_stack["order_monitor"],
                                _pool,
                                stack_is_paper(_rc_mode),
                                **_rc_scope,
                            )
                        with contextlib.suppress(Exception):
                            await _reseed_pending_entries(
                                _rc_stack["lifecycle"],
                                _pool,
                                stack_is_paper(_rc_mode),
                                _state.get("order_types", {}),
                                **_rc_scope,
                            )
                    with contextlib.suppress(Exception):
                        await _report_orphaned_live_rows(_pool, stacks)
                    with contextlib.suppress(Exception):
                        await _reseed_swing_exit_state(_state)
                    with contextlib.suppress(Exception):
                        await _reseed_intraday_exit_state(_state)
                    _dn, _bn, _matched, _gaps_json, _by_broker_json = _reconciliation_log_fields(
                        _runs, _held_live_primary, _held_paper
                    )
                    await _pool.execute(
                        "INSERT INTO reconciliation_log "
                        "(id, ran_at, db_positions, broker_positions, matched, gaps, duration_ms) "
                        "VALUES (1, NOW(), $1,$2,$3,$4::jsonb,$5) "
                        "ON CONFLICT (id) DO UPDATE SET "
                        "ran_at = NOW(), db_positions = EXCLUDED.db_positions, "
                        "broker_positions = EXCLUDED.broker_positions, "
                        "matched = EXCLUDED.matched, gaps = EXCLUDED.gaps, "
                        "duration_ms = EXCLUDED.duration_ms",
                        _dn,
                        _bn,
                        _matched,
                        _gaps_json,
                        int((_time.monotonic() - _t0) * 1000),
                    )
                    # Per-broker detail (migration 091); best-effort, never blocks the row above.
                    with contextlib.suppress(Exception):
                        await _pool.execute(
                            "UPDATE reconciliation_log SET by_broker = $1::jsonb WHERE id = 1",
                            _by_broker_json,
                        )
                except TimeoutError:
                    _log.debug("reconciliation_timeout — broker slow, skipping cycle")
                except Exception as _rec_exc:
                    _log.debug("reconciliation_write_failed err=%s", _rec_exc)
            await asyncio.sleep(30.0)

    # Per-broker equity and risk (config/global.json system.per_broker_equity, default
    # false; owner decision -- it moves live sizing from the primary's equity to each live
    # broker's own). Off: nothing below runs and crm_ctx keeps the legacy single figures.
    _per_broker_tasks: list[Any] = []
    if _cfg("global").get("system", {}).get("per_broker_equity", False) is True:
        from src.risk.budget import get_budget as _pb_budget

        _state["per_broker_equity"] = True
        _state.setdefault("equity_by_key", {})
        _state.setdefault("cash_by_key", {})
        _state.setdefault("equity_baseline_by_key", {})
        _state.setdefault("entries_blocked_brokers", {})
        crm_ctx.per_broker_equity = True
        crm_ctx.legacy_live_broker = _live_broker_name or LEGACY_LIVE_BROKER
        # Same dict objects: the fetch loop and the breaker update CRM's view in place.
        crm_ctx.mode_current_equity = _state["equity_by_key"]
        crm_ctx.mode_total_capital = _state["equity_baseline_by_key"]
        crm_ctx.entries_blocked_brokers = _state["entries_blocked_brokers"]
        crm_ctx.min_viable_equity_rupees = _pb_budget().account.min_viable_equity_rupees
        for _pb_key, _pb_stack in live_stacks(stacks).items():
            await _fetch_live_stack_equity(_state, _pb_key, _pb_stack["broker"])
        _refresh_options_risk_budget(crm_ctx)
        _log.info(
            "per_broker_equity_on equity=%s baseline=%s",
            _state["equity_by_key"],
            _state["equity_baseline_by_key"],
        )
        _per_broker_tasks.append(_per_broker_equity_loop(_state, stop))

    # One _monitor_loop task per active stack (paper and/or live) — each polls
    # only its own broker/OrderMonitor and drains closed trades tagged with its
    # own is_paper, so a mixed-mode batch can never misfile a live close into
    # paper_trades or vice versa (Phase 2).
    _all_order_monitors = [_s["order_monitor"] for _s in stacks.values()]
    _monitor_tasks = [
        _monitor_loop(
            _stack["order_monitor"],
            _state,
            stop,
            trade_repo,
            _stack["price_truth"],
            cycle_seconds=float(_monitoring_cfg.get("cycle_seconds", 2.0)),
            is_paper=stack_is_paper(_mode_name),
            all_order_monitors=_all_order_monitors,
            lifecycle=_stack["lifecycle"],
            broker=_stack["broker"],
        )
        for _mode_name, _stack in stacks.items()
    ]
    # Same one-per-stack pattern for the daily CNC/swing gap-risk sweep — a live
    # swing position must get evaluated by the live stack's OrderMonitor, not
    # only whichever stack happens to be "primary".
    _gap_risk_tasks = [
        _gap_risk_loop(
            _stack["broker"],
            _stack["order_monitor"],
            crm_ctx.total_capital,
            _nse_holidays,
            _state,
            risk_cfg.get("gap_risk", {}),
            stop,
        )
        for _stack in stacks.values()
    ]

    # Order-truth-vs-broker reconciliation runs once, blocking, right here —
    # before any backtest/discovery/scan loop below gets its first tick — so a
    # position the broker already closed outside the bot (GTT fill, manual
    # sell) never sits "open" on the dashboard through an entire boot cycle
    # waiting for the periodic _reconciliation_loop's first pass. Bounded and
    # fail-open: a slow/unavailable broker here must never block the rest of
    # startup, only skip this one priming pass (the periodic loop retries
    # forever afterward regardless).
    _boot_live_broker = broker_live if broker_live is not None else broker
    try:
        # Every live broker (Phase 2), primary first; each with its own rows/books/monitor.
        _boot_runs = await _reconcile_live_brokers(
            _state,
            stacks,
            _boot_live_broker,
            db_pool,
            trade_repo,
            supervisor._alert_manager,
            call_timeout=10.0,
            fetch_timeout=10.0,
            ghost_timeout=15.0,
            suppress_ghost_errors=False,
        )
        for _br in _boot_runs:
            _log.info(
                "boot_reconciliation_done gaps=%d auto_closed=%d%s",
                len(_br["result"].gaps),
                _br["auto_closed"],
                f" broker={stack_broker(_br['key'])}" if len(_boot_runs) > 1 and _br["key"] else "",
            )
    except Exception as _boot_rec_exc:
        _log.warning("boot_reconciliation_failed err=%s — periodic loop will retry", _boot_rec_exc)

    await asyncio.gather(
        _reconciliation_loop(),
        _ledger_health_loop(),
        _startup_ohlcv_backfill(broker, _state),
        pool.start(compiled),
        supervisor.start(),
        ingester.start(symbols),
        _crm_loop(
            signal_ring,
            crm,
            crm_ctx,
            compiled,
            registry,
            autonomy_mode,
            lifecycle,
            paper_mode,
            paper_ledger,
            _state,
            stop,
        ),
        *_monitor_tasks,
        _equity_loop(broker, _state, supervisor._alert_manager, stop),
        *_per_broker_tasks,
        _regime_loop(
            broker,
            _state,
            float(intelligence_cfg.get("regime", {}).get("detection_interval_minutes", 5)) * 60.0,
            stop,
        ),
        _health_loop(compiled, _state, stop, intelligence_cfg.get("strategy_health", {})),
        _earnings_loop(earnings_calendar, symbols, _state, stop),
        _reload_loop(strategy_reg, _state, stop),
        *_gap_risk_tasks,
        _correlation_loop(_state, stop),
        _monitoring_loop_25(_state, risk_cfg, stop),
        _sanity_watchdog_loop(broker, _state, stop),
        _weekly_pnl_loop(
            _state, int(risk_cfg.get("cost_tracking", {}).get("halt_after_negative_weeks", 4)), stop
        ),
        _news_loop(_state, news_cfg, stop),
        _heartbeat_loop(),
        _notifier_supervisor_loop(),
        position_update_loop(_state, stop),
        eod_summary_loop(_state, stop),
        market_holiday_loop(stop),
        _rs_ranking_loop(_state, intelligence_cfg.get("rs_ranking", {}), stop),
        _sector_rotation_loop(_state, stop),
        _attribution_loop(_state, stop),
        _slippage_loop(lifecycle, _state, stop),
        _expectancy_loop(_state, stop),
        _calibration_loop(_state, stop),
        _ohlcv_intraday_loop(broker, _state, stop),
        _eod_ohlcv_download_loop(broker, _state, stop),
        _intraday_signal_scan_loop(_state, stop),
        _nightly_scanner_loop(_state, stop),
        _swing_signal_scan_loop(_state, stop),
        _fundamentals_fetch_loop(_state, stop),
        _llm_swing_scan_loop(_state, stop),
        _llm_intraday_agent_loop(_state, stop),
        _llm_health_loop(stop),
        _nfo_ingester_loop(_state, stop),
        _kite_token_refresh_loop(_state, stop),
        _angel_token_refresh_loop(_state, stop),
        _flattrade_token_refresh_loop(_state, stop),
        _options_seller_vol_loop(_state, stop),
        _seller_desk_loop(_state, stop),
        _options_intraday_short_premium_loop(_state, stop),
        _options_pnl_snapshot_loop(_state, stop),
        _learning_loop(_state, stop),
        _backtest_loop(strategy_reg, _state, stop),
        _discovery_loop(strategy_reg, _state, stop),
        _evolution_loop(strategy_reg, _state, stop),
        _options_structure_exit_loop(_state, stop),
        _options_conviction_calibration_loop(_state, stop),
        return_exceptions=True,
    )

    _log.info(
        "running",
        extra={
            "ctx": {
                "paper_mode": paper_mode,
                "symbols": len(symbols),
            }
        },
    )

    await stop.wait()

    _log.info("shutdown_start")
    await ingester.stop()
    await pool.stop()
    await supervisor.stop()
    if _notifier is not None:
        # Drain briefly so the last exit/critical message actually reaches
        # Telegram before the process (and its HTTP session) goes away.
        await _notifier.aclose()
    await close_pool()
    _log.info("shutdown_complete")


def main() -> None:
    parser = argparse.ArgumentParser(description="ashwax trading engine")
    parser.add_argument("--paper", action="store_true")
    args = parser.parse_args()

    # acquire_singleton_lock() calls sys.exit(99) itself if another instance runs
    acquire_singleton_lock()
    asyncio.run(_run(paper_mode=args.paper))


if __name__ == "__main__":
    main()
