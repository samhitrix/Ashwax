"""
ashwax API server — FastAPI wrapper around live broker and DB data.

Kite is optional: market data comes from the configured market-data router and funds from
each configured broker's get_margins() (src/api/broker_access.py). Kite-account-specific
endpoints (positions/orders/holdings/GTT/WS) return their empty shape without Kite.

Endpoints:
  GET  /api/session          validate access token
  GET  /api/positions        net positions (day + overnight); ?mode=all|paper|live
  GET  /api/swing_positions  open CNC (swing/delivery) positions, any entry date; ?mode=
  GET  /api/closed_positions today's closed positions, live + paper; ?mode=
  GET  /api/pnl_breakdown    closed P&L by product; ?mode=live|paper|all (default live)
  GET  /api/orders           today's orders
  GET  /api/holdings         long-term holdings
  GET  /api/margins          available + utilised margins
  GET  /api/gtt              GTT orders
  GET  /api/ltp?symbols=...  live last-traded price
  GET  /api/logs/files       list readable log files (Logs tab)
  GET  /api/logs/{name}?tail=200  tail a known log file's contents
  GET  /api/regime           current market regime (logs/regime.json)
  GET  /api/strategy_health  strategy health scores (logs/)
  GET  /api/nightly_setups   nightly scanner output
  GET  /api/rs_ranking       RS ranking data
  POST /api/orders/{id}/force-exit    queue a manual market-exit (Order Monitor+ tab)
  GET  /api/orders/{id}/action-status poll a queued manual action
  WS   /ws                   real-time LTP every 2s for open positions + Nifty

Run:
  uvicorn src.api.server:app --port 3000 --reload
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from kiteconnect import KiteConnect

from src.api.broker_access import (
    BROKER_FAILURE_TTL_SECONDS,
    BROKERS,
    LABELS,
    SECONDARY_TIMEOUT_SECONDS,
    TOKEN_ENV,
    BrokerAccess,
)
from src.brokers.kite_auth import CredentialsMissingError, auto_refresh
from src.brokers.registry import list_brokers
from src.brokers.routing import BrokerRoute, broker_is_configured, parse_strategy_routes
from src.infrastructure.logger import get_logger
from src.risk.report_costs import equity_costs, options_costs

_ENV = Path(__file__).parent.parent.parent / ".env"
load_dotenv(_ENV)

_log = get_logger(__name__)  # "src.api.server" -> api.log (was flat "api", orphaned)

_API_KEY = os.environ.get("KITE_API_KEY", "")
_ACCESS_TOKEN = os.environ.get("KITE_ACCESS_TOKEN", "")
_BASE = Path(__file__).parent.parent.parent
_db_pool: Any = None
# Last time _kite_refreshed() actually called auto_refresh(). auto_refresh()
# makes a live Kite profile() API call on every invocation — with no throttle,
# the dashboard's 2s refresh loop (2 endpoints/cycle use _kite_refreshed())
# fired that live call ~2x/2s all day for zero benefit, since a token refreshed
# once at startup stays valid for the rest of the trading day.
_last_auth_check: float = 0.0
_AUTH_CHECK_INTERVAL_S = 300.0


def _config_dir() -> Path:
    """Re-derives config/ from _BASE on every call (not a module-level constant)
    so tests that monkeypatch _BASE after import still land in the right dir."""
    return _BASE / "config"


app = FastAPI(title="ashwax API", version="3.0.0")

from datetime import UTC, date, datetime  # noqa: E402

from dashboard_api.routes.news_api import router as _news_router  # noqa: E402

app.include_router(_news_router)


@app.on_event("startup")
async def _startup() -> None:
    global _ACCESS_TOKEN, _db_pool
    _log = get_logger(__name__)
    if not _kite_configured():
        # Kite is optional: no KITE_API_KEY means no Kite login attempt (spec 3.2).
        _log.info("kite_auth_skipped", extra={"ctx": {"reason": "Kite not configured"}})
    else:
        try:
            # Worker thread: a Kite re-login runs asyncio.run(), illegal on this loop.
            _ACCESS_TOKEN = await asyncio.to_thread(auto_refresh, _ENV)
        except CredentialsMissingError as exc:
            _log.warning("kite_auth_skipped", extra={"ctx": {"reason": str(exc)}})
        except Exception as exc:
            _log.error("kite_auth_failed", extra={"ctx": {"error": str(exc)}})
    try:
        from src.db.pool import init_pool

        _db_pool = await init_pool()
        _log.info("api_db_pool_ready")
    except Exception as exc:
        _log.warning("api_db_pool_failed err=%s — falling back to JSON logs", exc)
        _db_pool = None


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _db_pool is not None:
        from src.db.pool import close_pool

        await close_pool()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# The control routes (strategy live toggle, force-exit, settings) have no auth and rely on
# the localhost bind. Reject any other Host header so a DNS-rebinding web page cannot reach
# them through the browser. The Vite proxy targets localhost:3000, so the dashboard passes.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])


_kite_client: KiteConnect | None = None
_kite_client_token: str = ""
_kite_client_lock = threading.Lock()


def _kite() -> KiteConnect:
    """Process-wide KiteConnect instance.

    Rebuilding this per call also rebuilt its underlying requests.Session, so every
    broker read opened a new TLS connection to Zerodha instead of reusing a pooled
    keep-alive one — paying a full handshake on each of the ~8 broker-backed
    endpoints the dashboard polls per cycle. One instance is reused; only the access
    token is re-applied, and only when auto_refresh has rotated it.
    """
    global _kite_client, _kite_client_token
    with _kite_client_lock:
        if _kite_client is None:
            _kite_client = KiteConnect(api_key=_API_KEY)
            _kite_client_token = ""
        if _kite_client_token != _ACCESS_TOKEN:
            _kite_client.set_access_token(_ACCESS_TOKEN)
            _kite_client_token = _ACCESS_TOKEN
        return _kite_client


def _kite_configured() -> bool:
    """Kite credentials present. Kite-account-specific endpoints are guarded by this."""
    return broker_is_configured("kite", os.environ)


_BROKER_ACCESS = BrokerAccess(_ENV)


def _access() -> BrokerAccess:
    """Read-only market-data router + per-broker funds for this process (tests replace it)."""
    return _BROKER_ACCESS


def _legacy_broker_name() -> str | None:
    """global.json broker.name (display preference for the single funds card only)."""
    try:
        glob = json.loads((_config_dir() / "global.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    cfg = glob.get("broker")
    name = cfg.get("name") if isinstance(cfg, dict) else cfg
    return name if isinstance(name, str) else None


# Short-lived cache for broker reads, with single-flight coalescing.
# The dashboard polls several broker-backed endpoints on one timer, and each was an
# independent uncoalesced Zerodha round-trip; concurrent callers (a second browser
# tab, the WS feed) multiplied that against Zerodha's rate limit. TTLs are set below
# the dashboard's own poll interval for anything position/order shaped, so a poll
# still sees a fresh read — what is removed is duplicate in-flight calls for the
# same resource, not freshness.
_BROKER_CACHE: dict[str, tuple[float, Any]] = {}
_BROKER_CACHE_LOCK = threading.Lock()
_BROKER_KEY_LOCKS: dict[str, threading.Lock] = {}


def _clear_broker_cache() -> None:
    """Drop every cached broker read. Used by tests, which patch _kite() per case
    and must not observe a value cached by an earlier test inside the TTL."""
    global _kite_client, _kite_client_token
    with _BROKER_CACHE_LOCK:
        _BROKER_CACHE.clear()
    _BROKER_ACCESS.clear_cache()
    with _kite_client_lock:
        _kite_client = None
        _kite_client_token = ""


def _broker_cached(key: str, ttl_s: float, fetch: Any) -> Any:
    """Return `fetch()`'s result, reusing a cached value younger than ttl_s.

    Concurrent callers for the same key wait on one in-flight fetch rather than
    each issuing their own. Exceptions are never cached — a failed read retries on
    the next call, and callers keep their existing error handling.
    """
    now = time.monotonic()
    with _BROKER_CACHE_LOCK:
        hit = _BROKER_CACHE.get(key)
        if hit is not None and (now - hit[0]) < ttl_s:
            return hit[1]
        key_lock = _BROKER_KEY_LOCKS.setdefault(key, threading.Lock())

    with key_lock:
        # Re-check under the key lock: another thread may have refreshed while we
        # queued, in which case this call is served from that result.
        with _BROKER_CACHE_LOCK:
            hit = _BROKER_CACHE.get(key)
            if hit is not None and (time.monotonic() - hit[0]) < ttl_s:
                return hit[1]
        value = fetch()
        with _BROKER_CACHE_LOCK:
            _BROKER_CACHE[key] = (time.monotonic(), value)
        return value


_option_chain_broker: Any = None
_option_chain_provider: Any = None


def _get_option_chain_provider() -> Any:
    """Lazily-built, session-cached NFOChainProvider for the read-only
    /api/option_chain endpoint. This API process has no access to the
    orchestrator's in-memory broker/provider instances (separate asyncio
    loop, no shared state beyond the DB) -- it builds its own KiteAdapter
    the same way _kite() already does for /api/ltp, /api/gtt etc.
    NFOChainProvider caches the NFO instrument dump for the session, so the
    provider itself is cached too rather than rebuilt per request."""
    global _option_chain_broker, _option_chain_provider
    from src.brokers.adapters.kite.adapter import KiteAdapter
    from src.options.nfo_chain import NFOChainProvider

    if not _kite_configured():
        # No Kite: chain data through the market-data router (per_use.options_chain order).
        router = _access().require_router()
        if _option_chain_provider is None or _option_chain_broker is not router:
            _option_chain_broker = router
            _option_chain_provider = NFOChainProvider(router)
        return _option_chain_provider
    if _option_chain_broker is None or not isinstance(_option_chain_broker, KiteAdapter):
        _option_chain_broker = KiteAdapter.from_env()
        _option_chain_provider = NFOChainProvider(_option_chain_broker)
    _option_chain_broker.set_access_token(_ACCESS_TOKEN)
    return _option_chain_provider


def _kite_refreshed() -> KiteConnect:
    """Like _kite() but triggers auto_refresh if the current token is stale.

    Throttled to once per _AUTH_CHECK_INTERVAL_S — auto_refresh() makes a live
    Kite profile() call every time it runs, and this is called from endpoints
    the dashboard polls every 2s. See _last_auth_check comment above.
    """
    global _ACCESS_TOKEN, _last_auth_check
    import contextlib
    import time

    now = time.monotonic()
    if now - _last_auth_check >= _AUTH_CHECK_INTERVAL_S:
        with contextlib.suppress(Exception):
            _ACCESS_TOKEN = auto_refresh(_ENV)
        _last_auth_check = now
    return _kite()


def _read_log(name: str, default: Any = None) -> Any:
    path = _BASE / "logs" / name
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _read_cache(name: str, max_age_s: float, default: Any = None) -> Any:
    """Read a JSON cache file only if it is still fresh.

    JSON files under logs/ are CACHE ONLY — the DB is the source of truth.
    A cache older than max_age_s is treated as cleared (stale) and the empty
    default is returned, so the dashboard never shows outdated numbers as live.
    """
    path = _BASE / "logs" / name
    if not path.exists():
        return default
    import time as _t

    if (_t.time() - path.stat().st_mtime) > max_age_s:
        return default
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@app.get("/api/db_status")
async def get_db_status() -> dict[str, Any]:
    """PostgreSQL pool health — used by Agent Status tab."""
    if _db_pool is None:
        return {"connected": False, "pool_size": 0, "idle": 0, "latency_ms": None}
    try:
        import time as _time

        t0 = _time.monotonic()
        await _db_pool.fetchval("SELECT 1")
        latency_ms = round((_time.monotonic() - t0) * 1000, 1)
        return {
            "connected": True,
            "pool_size": _db_pool.get_size(),
            "idle": _db_pool.get_idle_size(),
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        return {
            "connected": False,
            "pool_size": 0,
            "idle": 0,
            "latency_ms": None,
            "error": str(exc),
        }


async def _engine_heartbeats() -> list[dict[str, Any]]:
    """Normalize engine_heartbeats rows (DB truth) into dashboard-friendly dicts.

    Single-process architecture: all engines share the orchestrator PID and the
    same measured process-wide CPU/RSS (psutil), written by the heartbeat loop.
    Status is derived from heartbeat age, so a dead process → STOPPED, not faked.

    A transient DB error raises 503 (NOT an empty list) so the Self-Healing grid
    keeps its last-good rows instead of blanking to "0 engines" — empty list is
    reserved for a genuine absence of heartbeats. Mirrors /api/agent_status.
    """
    if _db_pool is None:
        raise HTTPException(status_code=503, detail="db pool unavailable")
    from datetime import datetime

    try:
        rows = await _db_pool.fetch(
            "SELECT engine_name, pid, status, metadata, last_beat "
            "FROM engine_heartbeats ORDER BY engine_name"
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="heartbeat query failed") from exc
    now = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for r in rows:
        ts = r["last_beat"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age = (now - ts.astimezone(UTC)).total_seconds()
        status = "RUNNING" if age < 45 else "IDLE" if age < 120 else "STOPPED"
        meta = r["metadata"] or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        out.append(
            {
                "name": r["engine_name"],
                "pid": r["pid"] or 0,
                "status": status,
                "cpu": float(meta.get("cpu", 0.0)),
                "memory": float(meta.get("rss_mb", 0.0)),
                "gc": int(meta.get("gc", 0)),
                "last_beat": str(ts),
            }
        )
    return out


@app.get("/api/components")
async def get_components() -> list[Any]:
    """Live engines from engine_heartbeats (DB truth). Empty when engine is offline.
    Single-process: every engine shares the orchestrator PID; core = 'async'."""
    return [
        {
            "name": r["name"],
            "status": r["status"],
            "pid": r["pid"],
            "cpu": r["cpu"],
            "memory": r["memory"],
            "core": "async",
        }
        for r in await _engine_heartbeats()
    ]


@app.get("/api/process_health")
async def get_process_health() -> list[Any]:
    """Per-engine process health for the Self-Healing tab (DB truth: engine_heartbeats)."""
    return [
        {
            "name": r["name"],
            "pid": r["pid"],
            "status": r["status"],
            "cpu": r["cpu"],
            "memory": r["memory"],
            "memoryLimit": 0,
            "uptime": 0,
            "restartCount": 0,
            "lastHeartbeat": r["last_beat"],
            "errors24h": 0,
            "gcPauses": r["gc"],
            "gcPauseAvgMs": 0,
        }
        for r in await _engine_heartbeats()
    ]


@app.get("/api/ws_health")
async def get_ws_health() -> dict[str, Any]:
    """WebSocket health (DB truth: ws_health, written by the ingester via the
    orchestrator heartbeat loop). A snapshot older than 30 s → DISCONNECTED."""
    default = {
        "status": "DISCONNECTED",
        "subscribed": 0,
        "active": 0,
        "stale": [],
        "ticks_per_second": 0,
    }
    if _db_pool is None:
        return default
    try:
        row = await _db_pool.fetchrow("SELECT snapshot, updated_at FROM ws_health WHERE id = 1")
    except Exception:
        return default
    if not row:
        return default
    from datetime import datetime

    snap = row["snapshot"]
    if isinstance(snap, str):
        snap = json.loads(snap)
    ts = row["updated_at"]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - ts.astimezone(UTC)).total_seconds()
    if age > 30:
        return {**snap, "status": "DISCONNECTED"}
    return snap


@app.get("/api/reconciliation")
async def get_reconciliation() -> list[Any]:
    """Recent position reconciliation runs (DB truth: reconciliation_log)."""
    if _db_pool is None:
        return []
    try:
        rows = await _db_pool.fetch(
            "SELECT id, ran_at, db_positions, broker_positions, matched, gaps, "
            "gtts_checked, gtts_rearmed, incidents_raised, duration_ms "
            "FROM reconciliation_log ORDER BY ran_at DESC LIMIT 1"
        )
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        gaps = r["gaps"]
        if isinstance(gaps, str):
            try:
                gaps = json.loads(gaps)
            except Exception:
                gaps = []
        out.append(
            {
                "id": str(r["id"]),
                "runAt": str(r["ran_at"]),
                "duration": r["duration_ms"],
                "dbPositions": r["db_positions"],
                "brokerPositions": r["broker_positions"],
                "matched": r["matched"],
                "mismatches": [
                    {
                        "type": g.get("kind", "DB_ONLY"),
                        "symbol": g.get("symbol", ""),
                        "action": "review",
                        "resolved": False,
                    }
                    for g in (gaps or [])
                ],
                "gttsChecked": r["gtts_checked"],
                "gttsRearmed": r["gtts_rearmed"],
                "incidentsRaised": r["incidents_raised"],
            }
        )
    return out


def _segment_modes() -> dict[str, str]:
    """Per-segment PAPER/LIVE, from config/global.json system.segments.*.paper_mode
    (falls back to the top-level system.paper_mode for a segment not listed)."""
    try:
        glob = json.loads((_config_dir() / "global.json").read_text(encoding="utf-8"))
    except Exception:
        return {"intraday": "PAPER", "swing": "PAPER", "options": "PAPER"}
    system = glob.get("system", {})
    default_paper = bool(system.get("paper_mode", True))
    segments = system.get("segments", {})
    return {
        seg: ("PAPER" if bool(segments.get(seg, {}).get("paper_mode", default_paper)) else "LIVE")
        for seg in ("intraday", "swing", "options")
    }


@app.get("/api/agent_status")
async def agent_status() -> dict[str, Any]:
    """Returns whether the trading engine is active based on recent DB heartbeat.

    On a transient DB error (e.g. shared asyncpg pool momentarily exhausted by engine
    workload) this returns 503 — NOT active:false — so the dashboard keeps last-good
    instead of flickering STOPPED. active:false is reserved for a genuine absence of
    heartbeats (engine never started / truly down).
    """
    from datetime import datetime

    if _db_pool is None:
        raise HTTPException(status_code=503, detail="db pool unavailable")
    try:
        row = await _db_pool.fetchrow(
            "SELECT last_beat FROM engine_heartbeats ORDER BY last_beat DESC LIMIT 1"
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="heartbeat query failed") from exc
    segments = _segment_modes()
    # mode stays a single summary for old dashboard builds/back-compat: PAPER only
    # when every segment is paper, LIVE only when every segment is live, else the
    # dashboard should read `segments` instead (per-segment can be genuinely mixed).
    _modes = set(segments.values())
    mode = _modes.pop() if len(_modes) == 1 else "MIXED"
    if not row:
        return {"active": False, "mode": mode, "segments": segments}
    ts = row["last_beat"]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - ts.astimezone(UTC)).total_seconds()
    return {
        "active": age < 45,
        "mode": mode,
        "segments": segments,
        "heartbeat_age_s": round(age, 1),
    }


async def _kite_profile() -> tuple[dict[str, Any] | None, str]:
    """(Kite profile, "") or (None, error). One call per poll, shared by the Kite card and
    the legacy /api/session fields.

    30s TTL. This drives the dashboard's connected/disconnected badge, so it must still
    detect a dead token promptly — but the profile payload itself is static for the whole
    trading day, and calling it on every poll made this one of the slowest endpoints in a
    refresh cycle (8.8s under load). A failed call is not cached, so a genuine auth failure
    surfaces at once."""
    try:
        k = _kite()
        profile: dict[str, Any] = await asyncio.to_thread(
            _broker_cached, "profile", 30.0, k.profile
        )
        return profile, ""
    except Exception as exc:
        return None, str(exc)


async def _secondary_session(name: str) -> tuple[bool, str]:
    """(token valid, message) for a non-Kite broker. BrokerAccess bounds the check to
    SECONDARY_TIMEOUT_SECONDS and reuses a failure for BROKER_FAILURE_TTL_SECONDS."""
    try:
        status = await _access().session_status(name)
        return bool(status.is_valid), str(status.message or "")
    except Exception as exc:
        return False, str(exc) or type(exc).__name__


def _broker_card(name: str, check: tuple[bool, str] | None) -> dict[str, Any]:
    """Health card: configured (credentials), token present/valid and REST connected (an
    authenticated REST call succeeded). Real checks only; a broker that is not configured
    is reported as such, never as healthy. `check` is None for an unconfigured broker."""
    card: dict[str, Any] = {
        "broker": name,
        "label": LABELS[name],
        "configured": check is not None,
        "token_present": bool(os.environ.get(TOKEN_ENV[name], "").strip()),
        "token_valid": None,
        "rest_connected": None,
        "error": None,
    }
    if check is not None:
        valid, msg = check
        card["token_present"] = card["token_present"] or _access().token_present(name)
        card["token_valid"] = valid
        card["rest_connected"] = valid
        card["error"] = msg[:200] or None
    return card


@app.get("/api/session")
async def session_status() -> dict[str, Any]:
    """Kite profile (the legacy fields the dashboard badge reads) plus `brokers`: one health
    card per broker. Without Kite: ok=False, configured=False, and the cards still report
    Angel One / Flattrade.

    All broker checks run concurrently and each non-Kite check is bounded, so a slow Angel
    One / Flattrade adds at most SECONDARY_TIMEOUT_SECONDS (then its failure is reused) and
    never blanks the Kite badge."""
    kite_on = _kite_configured()
    others = [n for n in BROKERS if n != "kite" and broker_is_configured(n, os.environ)]

    async def _no_kite() -> tuple[dict[str, Any] | None, str]:
        return None, "Kite not configured (no KITE_API_KEY)"

    kite_res, *other_res = await asyncio.gather(
        _kite_profile() if kite_on else _no_kite(),
        *(_secondary_session(n) for n in others),
    )
    checks: dict[str, tuple[bool, str] | None] = dict.fromkeys(BROKERS)
    checks.update(zip(others, other_res, strict=True))
    profile, kite_err = kite_res
    if kite_on:
        checks["kite"] = (profile is not None, kite_err)
    brokers = [_broker_card(n, checks[n]) for n in BROKERS]
    if not kite_on:
        return {"ok": False, "configured": False, "error": kite_err, "brokers": brokers}
    if profile is None:
        return {"ok": False, "error": kite_err, "brokers": brokers}
    return {
        "ok": True,
        "user_id": profile.get("user_id"),
        "user_name": profile.get("user_name"),
        "email": profile.get("email"),
        "brokers": brokers,
    }


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    """Real limits from config/global.json + config/risk.json (no fabricated values)."""
    cfg_dir = _config_dir()
    try:
        glob = json.loads((cfg_dir / "global.json").read_text(encoding="utf-8"))
        risk = json.loads((cfg_dir / "risk.json").read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": str(exc)}

    system = glob.get("system", {})
    cb = risk.get("circuit_breakers", {})
    if system.get("shadow_mode"):
        mode = "SHADOW"
    elif system.get("paper_mode"):
        mode = "PAPER"
    else:
        mode = "LIVE"
    return {
        "mode": mode,
        "autonomy": str(system.get("autonomy_mode", "")).upper(),
        "dailyLossLimit": cb.get("daily_loss_limit_rupees"),
        "consecutiveLossLimit": cb.get("consecutive_loss_count"),
    }


def _config_allowlist() -> set[str]:
    """Names (no .json) of every real file in config/ — the only names these
    endpoints will ever read or write, so a request can never escape config/."""
    config_dir = _config_dir()
    return {p.stem for p in config_dir.glob("*.json")}


def _backup_config_file(path: Path) -> None:
    if not path.exists():
        return
    config_dir = _config_dir()
    backup_dir = config_dir / ".backups"
    backup_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{path.stem}.{ts}.json"
    backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


# exec_broker (migration 089) is the broker that HOLDS a trade/position row. The dashboard can be
# deployed before the migration is applied, so every SELECT of it degrades to a NULL column
# (SQLSTATE 42703 = undefined_column ONLY; any other DB error keeps its existing handling) and the
# column is re-tried every _EXEC_BROKER_RECHECK_S so it appears without a restart.
_EXEC_BROKER_RECHECK_S = 60.0
_exec_broker_missing_until: dict[str, float] = {}


async def _fetch_exec_broker_aware(key: str, build: Callable[[str], str], *args: Any) -> list[Any]:
    """Run build(column_sql) with the real exec_broker column; if the DB lacks it, with
    `NULL::text AS exec_broker`. `key` names the table for the remembered-missing flag."""
    now = time.monotonic()
    if now >= _exec_broker_missing_until.get(key, 0.0):
        try:
            return list(await _db_pool.fetch(build("exec_broker"), *args))
        except Exception as exc:
            if getattr(exc, "sqlstate", None) != "42703":
                raise
            _exec_broker_missing_until[key] = now + _EXEC_BROKER_RECHECK_S
            _log.warning(
                "exec_broker_column_missing table=%s -- apply migration 089; live rows are "
                "reported as 'kite' (the only live broker before it) until then",
                key,
            )
    return list(await _db_pool.fetch(build("NULL::text AS exec_broker"), *args))


def _exec_broker_label(row: Any, is_paper: bool) -> str | None:
    """Broker holding a row: None for paper (nothing was placed), else the stamped broker;
    NULL on a live row is legacy = "kite"."""
    return None if is_paper else (row.get("exec_broker") or "kite")


def _route_if_live(entry: dict[str, Any]) -> BrokerRoute:
    """R1 route of a registry entry evaluated as if it were live (registered, non-paper and
    configured via env credentials). Same function the orchestrator boots its route table with."""
    candidate = {**entry, "execution_mode": "live"}
    return parse_strategy_routes([candidate], list_brokers(), os.environ)[str(entry["name"])]


# Every config writer here is a read-modify-write on a file other requests also write, and sync
# endpoints run in a threadpool: hold this lock across the whole read -> modify -> write (not just
# the write) or concurrent PUTs lose each other's updates.
_CONFIG_WRITE_LOCK = threading.Lock()


def _write_json_atomic(path: Path, content: dict[str, Any]) -> None:
    """Backup, then write through a UNIQUE temp file in the same directory and os.replace it in
    (a shared temp name lets one writer replace another's half-written file). Call with
    _CONFIG_WRITE_LOCK held."""
    _backup_config_file(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(content, indent=2, ensure_ascii=False) + "\n")
        # mkstemp creates 0600; keep the file's existing mode (0644 by default).
        os.chmod(tmp_name, (path.stat().st_mode & 0o777) if path.exists() else 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _registry_live_violations(content: dict[str, Any], current_path: Path) -> list[str]:
    """R1 problems in a registry about to be saved wholesale: a live entry with no usable broker
    that is NEW or whose (execution_mode, broker) differs from the stored entry of that name.
    A legacy-invalid live entry that is left unchanged is not reported, so unrelated edits to a
    registry that already contains one still save."""
    entries = content.get("strategies")
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise HTTPException(status_code=400, detail='"strategies" must be a list of objects')
    try:
        stored = json.loads(current_path.read_text(encoding="utf-8")).get("strategies", [])
        current = {
            e.get("name"): (e.get("execution_mode"), e.get("broker"))
            for e in stored
            if isinstance(e, dict)
        }
    except (OSError, ValueError, AttributeError):
        current = {}
    bad: list[str] = []
    for e in entries:
        if e.get("execution_mode") != "live" or not e.get("name"):
            continue
        route = parse_strategy_routes([e], list_brokers(), os.environ)[e["name"]]
        if not route.ok and current.get(e["name"]) != ("live", e.get("broker")):
            bad.append(f"{e['name']} ({route.reason})")
    return bad


@app.get("/api/settings/files")
def get_settings_files() -> list[dict[str, str]]:
    return [{"name": stem, "path": f"config/{stem}.json"} for stem in sorted(_config_allowlist())]


@app.get("/api/settings/{name}")
def get_setting(name: str) -> dict[str, Any]:
    if name not in _config_allowlist():
        raise HTTPException(status_code=404, detail=f"Unknown config file: {name}")
    config_dir = _config_dir()
    try:
        return json.loads((config_dir / f"{name}.json").read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read {name}.json: {exc}") from exc


@app.put("/api/settings/{name}")
def update_setting(name: str, body: dict[str, Any]) -> dict[str, Any]:
    if name not in _config_allowlist():
        raise HTTPException(status_code=404, detail=f"Unknown config file: {name}")
    content = body.get("content")
    if not isinstance(content, dict):
        raise HTTPException(status_code=400, detail='Body must be {"content": <object>}')
    config_dir = _config_dir()
    path = config_dir / f"{name}.json"
    with _CONFIG_WRITE_LOCK:
        if name == "strategies_registry":
            # Same R1 rule as the dedicated endpoints: never create or keep-by-edit a live
            # strategy without a usable broker.
            bad = _registry_live_violations(content, path)
            if bad:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot save registry: live strategies without a usable broker: "
                    + "; ".join(bad),
                )
        _write_json_atomic(path, content)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Live Kite data
# ---------------------------------------------------------------------------


async def _overlay_live_ltp(data: Any) -> None:
    """Replace Kite positions()' `last_price` with a fresh ltp() quote.

    positions() serves a sticky snapshot price: measured 2026-09-21, a live
    position's last_price sat at 2019.4 for 18s while ltp() moved 2019.3-2020.8
    (the swing table, which uses ltp(), kept updating; Open Positions did not).
    `pnl` is shifted by (fresh - stale) * signed quantity, so Kite's realised
    component is preserved. On any failure the Kite values are left untouched."""
    if not isinstance(data, dict):
        return
    rows = [
        p
        for key in ("net", "day")
        for p in (data.get(key) or [])
        if isinstance(p, dict) and p.get("tradingsymbol")
    ]
    if not rows:
        return
    keys = {f"{p.get('exchange') or 'NSE'}:{p['tradingsymbol']}" for p in rows}
    try:
        quotes = await asyncio.to_thread(_kite().ltp, sorted(keys))
    except Exception:
        return
    for p in rows:
        quote = (quotes or {}).get(f"{p.get('exchange') or 'NSE'}:{p['tradingsymbol']}") or {}
        fresh = float(quote.get("last_price") or 0)
        if fresh <= 0:
            continue
        stale = float(p.get("last_price") or 0)
        p["pnl"] = float(p.get("pnl") or 0) + (fresh - stale) * float(p.get("quantity") or 0)
        p["last_price"] = fresh


@app.get("/api/positions")
async def get_positions(mode: str = "all") -> dict[str, Any]:
    """`mode`: "all" (default) | "paper" | "live" — filters both net/day lists
    by each row's `paper` flag (live Kite rows never carry that key, so they
    read as paper=False)."""
    if not _kite_configured():
        # Broker positions here are Kite's raw payload; without Kite there are none to
        # show, but the paper rows below (DB truth, router LTP) still are.
        cached: Any = {"net": [], "day": []}
    else:
        try:
            # 3s TTL, below the dashboard's 5s poll: each poll still triggers a real
            # read, while concurrent callers share one round-trip instead of racing.
            cached = await asyncio.to_thread(_broker_cached, "positions", 3.0, _kite().positions)
        except Exception as exc:
            return {"error": str(exc), "net": [], "day": []}
    # _broker_cached returns the SAME object to every caller within its TTL. This
    # endpoint mutates rows below (LTP overlay, external/strategy tagging) and
    # appends synthetic paper-trade rows, so it must work on a private copy —
    # otherwise a second poller landing in the same cache window (the dashboard
    # fires two independent pollers on every tick) reprocesses rows the first
    # poller already mutated/appended, duplicating them and mis-tagging the
    # duplicate as `external` (2026-09-22 bug: doubled same-day paper positions).
    data: dict[str, Any] = (
        {
            key: [dict(p) if isinstance(p, dict) else p for p in (cached.get(key) or [])]
            for key in ("net", "day")
        }
        if isinstance(cached, dict)
        else {"net": [], "day": []}
    )
    await _overlay_live_ltp(data)
    # Tag positions the agent did NOT open (symbol not in `trades`) as external →
    # the dashboard shows them read-only; the agent never places orders on them.
    agent_trades: dict[str, Any] = {}
    db_ok = False
    if _db_pool is not None:
        try:
            rows = await _db_pool.fetch(
                "SELECT DISTINCT ON (symbol) symbol, strategy_name, initial_sl, current_sl, "
                "initial_target, current_target, entry_timestamp FROM trades "
                "WHERE order_state NOT IN ('CLOSED','REJECTED','CANCELLED','TIMED_OUT') "
                # A symbol can have more than one open `trades` row (e.g. two strategies
                # signalling the same symbol the same day) — without this, an unfilled
                # duplicate order with a later entry_timestamp than the real filled
                # position wins DISTINCT ON and this endpoint shows ITS strategy/SL/target
                # next to the real broker position (2026-09-07: CGCL showed oneil_canslim's
                # target here while the actual GTT/DB position was minervini_vcp's).
                # /api/swing_positions already requires this same filter for the same reason.
                "AND fill_price IS NOT NULL "
                "ORDER BY symbol, entry_timestamp DESC"
            )
            agent_trades = {r["symbol"]: r for r in rows}
            db_ok = True
        except Exception:
            db_ok = False
    if isinstance(data, dict):
        for _key in ("net", "day"):
            for _p in data.get(_key, []) or []:
                if isinstance(_p, dict):
                    _trade = agent_trades.get(_p.get("tradingsymbol"))
                    _p["external"] = db_ok and _trade is None
                    if _trade is not None:
                        _p["strategy"] = _trade["strategy_name"] or ""
                        _p["initial_sl"] = (
                            float(_trade["initial_sl"])
                            if _trade["initial_sl"] is not None
                            else None
                        )
                        _p["current_sl"] = (
                            float(_trade["current_sl"])
                            if _trade["current_sl"] is not None
                            else None
                        )
                        _p["initial_target"] = (
                            float(_trade["initial_target"])
                            if _trade["initial_target"] is not None
                            else None
                        )
                        _p["current_target"] = (
                            float(_trade["current_target"])
                            if _trade["current_target"] is not None
                            else None
                        )
                        _p["entry_time"] = (
                            str(_trade["entry_timestamp"]) if _trade["entry_timestamp"] else ""
                        )
    # Surface OPEN paper trades in the same view, tagged paper. Read from the
    # paper_trades table (the broker never sees them); LTP from the market-data router.
    if _db_pool is not None and isinstance(data, dict):
        try:
            prows = await _db_pool.fetch(
                "SELECT symbol, exchange, direction, quantity, fill_price, entry_timestamp, "
                "strategy_name, initial_sl, current_sl, initial_target, current_target "
                "FROM paper_trades WHERE order_state NOT IN "
                "('CLOSED','REJECTED','CANCELLED','TIMED_OUT') AND fill_price IS NOT NULL "
                # Intraday (MIS) only — CNC swing holds have their own /api/swing_positions
                # tab and must not double-count here.
                "AND product = 'MIS' "
                "AND (entry_timestamp AT TIME ZONE 'Asia/Kolkata')::date "
                "= (NOW() AT TIME ZONE 'Asia/Kolkata')::date"
            )
            if prows:
                try:
                    _ltp = await _access().ltp([f"NSE:{r['symbol']}" for r in prows])
                except Exception:
                    _ltp = {}
                for r in prows:
                    sym, qty = r["symbol"], int(r["quantity"])
                    is_long = str(r["direction"]).lower() == "long"
                    avg = float(r["fill_price"] or 0)
                    last = float((_ltp.get(f"NSE:{sym}") or {}).get("last_price", avg) or avg)
                    pnl = (last - avg) * qty if is_long else (avg - last) * qty
                    data.setdefault("net", []).append(
                        {
                            "tradingsymbol": sym,
                            "exchange": r["exchange"] or "NSE",
                            "quantity": qty if is_long else -qty,
                            "average_price": avg,
                            "last_price": last,
                            "pnl": round(pnl, 2),
                            "product": "MIS",
                            "external": False,
                            "paper": True,
                            "entry_time": str(r["entry_timestamp"]) if r["entry_timestamp"] else "",
                            "strategy": r["strategy_name"] or "",
                            "initial_sl": float(r["initial_sl"])
                            if r["initial_sl"] is not None
                            else None,
                            "current_sl": float(r["current_sl"])
                            if r["current_sl"] is not None
                            else None,
                            "initial_target": float(r["initial_target"])
                            if r["initial_target"] is not None
                            else None,
                            "current_target": float(r["current_target"])
                            if r["current_target"] is not None
                            else None,
                        }
                    )
        except Exception:
            pass
    if mode != "all" and isinstance(data, dict):
        want_paper = mode == "paper"
        for _key in ("net", "day"):
            data[_key] = [r for r in (data.get(_key) or []) if bool(r.get("paper")) == want_paper]
    return data


@app.get("/api/swing_positions")
async def get_swing_positions(mode: str = "all") -> list[Any]:
    """Open CNC (swing/delivery) positions — DB truth, trades UNION paper_trades.
    `mode`: "all" (default) | "paper" | "live" filters the merged result by `paper`.

    Deliberately has NO same-day filter, unlike /api/positions' paper-surfacing
    block (which is intraday-only by design) — swing positions are meant to be
    held multi-day, so entry_timestamp being yesterday or earlier is normal, not
    stale data.
    """
    if _db_pool is None:
        return []
    try:
        rows = await _fetch_exec_broker_aware(
            "trades",
            lambda col: f"""
            SELECT agent_trade_id, symbol, strategy_name, direction, quantity,
                   fill_price, exit_price, current_sl, current_target, partial_pnl_booked,
                   entry_timestamp, exit_timestamp, false AS is_paper, {col}
              FROM trades
             WHERE product = 'CNC' AND order_state NOT IN
                   ('CLOSED','REJECTED','CANCELLED','TIMED_OUT')
               AND fill_price IS NOT NULL
            UNION ALL
            SELECT agent_trade_id, symbol, strategy_name, direction, quantity,
                   fill_price, exit_price, current_sl, current_target, partial_pnl_booked,
                   entry_timestamp, exit_timestamp, is_paper, NULL::text AS exec_broker
              FROM paper_trades
             WHERE product = 'CNC' AND order_state NOT IN
                   ('CLOSED','REJECTED','CANCELLED','TIMED_OUT')
               AND fill_price IS NOT NULL
             ORDER BY entry_timestamp DESC
            """,  # noqa: S608
        )
    except Exception:
        return []

    ltp_map: dict[str, float] = {}
    prev_close_map: dict[str, float] = {}
    if rows:
        # The two tables store symbols differently and BOTH are correct: trades.symbol
        # is bare ("IFCI") because Kite's order API takes a bare tradingsymbol, while
        # paper_trades.symbol is exchange-qualified ("NSE:IFCI") since it never reaches
        # that API. kite.ltp() answers qualified keys only and silently omits the rest,
        # so assuming "already exchange-qualified" left every LIVE row with no quote —
        # LTP, P&L, %Net Change and To Target all rendered "–" on exactly the real-money
        # positions (2026-08-28). Query qualified, then map back to each row's own key.
        _query_by_row = {
            r["symbol"]: (r["symbol"] if ":" in r["symbol"] else f"NSE:{r['symbol']}") for r in rows
        }
        _qkeys = sorted(set(_query_by_row.values()))
        try:
            # to_thread: this endpoint is `async def`, so a bare kiteconnect call
            # blocks the whole uvicorn event loop for the length of a Zerodha
            # round-trip (~1s), starving every other request behind it.
            # quote() (not ltp()) so we also get ohlc.close = PREVIOUS session's close,
            # which the Day %Chg column needs; falls back to ltp() (no prev close)
            # so a quote() failure can never blank the LTP/P&L columns.
            try:
                quotes = await _access().quote(_qkeys)
            except Exception as _qe:
                _log.warning(
                    "positions_quote_failed err=%s: %s — falling back to ltp() "
                    "(Day %%Chg unavailable this request)",
                    type(_qe).__name__,
                    _qe,
                )
                quotes = await _access().ltp(_qkeys)
            _by_query = {
                sym: float(q.get("last_price", 0) or 0) for sym, q in (quotes or {}).items()
            }
            _prev_by_query = {
                sym: float((q.get("ohlc") or {}).get("close", 0) or 0)
                for sym, q in (quotes or {}).items()
            }
            ltp_map = {row_sym: _by_query.get(q, 0.0) for row_sym, q in _query_by_row.items()}
            prev_close_map = {
                row_sym: _prev_by_query.get(q, 0.0) for row_sym, q in _query_by_row.items()
            }
        except Exception as _le:
            _log.warning(
                "positions_ltp_failed err=%s: %s — LTP/P&L/Day %%Chg blank this request",
                type(_le).__name__,
                _le,
            )
            ltp_map = {}
            prev_close_map = {}

    out = []
    for r in rows:
        entry = float(r["fill_price"] or 0)
        qty = int(r["quantity"] or 0)
        is_long = str(r["direction"] or "").lower() == "long"
        ltp = ltp_map.get(r["symbol"], 0.0)
        prev_close = prev_close_map.get(r["symbol"], 0.0)
        unrealized = (ltp - entry) * qty if is_long else (entry - ltp) * qty if ltp > 0 else 0.0
        booked = float(r["partial_pnl_booked"] or 0)
        out.append(
            {
                "tradeId": r["agent_trade_id"],
                "symbol": r["symbol"],
                "strategyName": r["strategy_name"] or "",
                "direction": str(r["direction"] or "").upper(),
                "quantity": qty,
                "entryPrice": entry,
                "exitPrice": float(r["exit_price"]) if r["exit_price"] is not None else None,
                "stopLoss": float(r["current_sl"]) if r["current_sl"] is not None else None,
                "target": float(r["current_target"]) if r["current_target"] is not None else None,
                "entryTime": str(r["entry_timestamp"]) if r["entry_timestamp"] else "",
                "exitTime": str(r["exit_timestamp"]) if r["exit_timestamp"] else "",
                "paper": bool(r["is_paper"]),
                "exec_broker": _exec_broker_label(r, bool(r["is_paper"])),
                "ltp": ltp if ltp > 0 else None,
                # Stock's own move vs the previous session's close (not signed by
                # position direction); None when either price is unavailable.
                "dayChangePct": round((ltp - prev_close) / prev_close * 100, 2)
                if ltp > 0 and prev_close > 0
                else None,
                # Realized (already booked via partial exit) + unrealized on what's
                # still open — booked profit was previously invisible entirely once
                # a partial fired, since order_state stays open (not CLOSED).
                "partialPnlBooked": round(booked, 2),
                "pnl": round(unrealized + booked, 2) if ltp > 0 else (round(booked, 2) or None),
            }
        )
    if mode != "all":
        want_paper = mode == "paper"
        out = [o for o in out if o["paper"] == want_paper]
    return out


@app.get("/api/orders")
def get_orders() -> list[Any]:
    if not _kite_configured():
        return []  # Kite order book; nothing to show without Kite (never mock)
    try:
        # 4s TTL: orders change only on a fill/placement, and the dashboard polls at 5s.
        return _broker_cached("orders", 4.0, _kite().orders)
    except Exception as exc:
        return [{"error": str(exc)}]


@app.get("/api/holdings")
def get_holdings() -> list[Any]:
    if not _kite_configured():
        return []  # Kite holdings; nothing to show without Kite (never mock)
    try:
        # Holdings settle overnight; a 60s TTL is still far fresher than they change.
        return _broker_cached("holdings", 60.0, _kite().holdings)
    except Exception as exc:
        return [{"error": str(exc)}]


def _funds_block(m: Any) -> dict[str, float]:
    return {
        "available_cash": float(m.available_cash),
        "used_margin": float(m.used_margin),
        "collateral": float(m.collateral),
        "net": float(m.equity),
    }


@app.get("/api/margins")
async def get_margins() -> dict[str, Any]:
    """Funds through each configured broker's get_margins() (no raw broker payload).

    `ashwax` = the funds card's broker (global.json broker.name when configured, else the
    first configured broker; named in `broker`), normalized the way the engine trades on
    (Kite's zero equity.available.cash -> live_balance, see _margin_from_kite). `brokers`
    has one block per configured broker ({"error": ...} when that read failed). On a
    failure of the card's broker there is no `ashwax` block, so the dashboard shows its
    empty state rather than 0 funds."""
    access = _access()
    name = access.primary_account(_legacy_broker_name())
    if name is None:
        return {"error": "no broker configured", "brokers": {}}

    async def _one(b: str) -> dict[str, Any]:
        try:
            # 4s TTL (BrokerAccess.margins) — margins move only on fills. Brokers other than
            # the card's are bounded and their failures reused, so they cannot delay it.
            if b == name:
                return _funds_block(await access.margins(b))
            return _funds_block(
                await access.margins(
                    b, timeout_s=SECONDARY_TIMEOUT_SECONDS, fail_ttl_s=BROKER_FAILURE_TTL_SECONDS
                )
            )
        except Exception as exc:
            return {"error": str(exc) or type(exc).__name__}

    configured = access.configured()
    brokers: dict[str, Any] = dict(
        zip(configured, await asyncio.gather(*(_one(b) for b in configured)), strict=True)
    )
    primary = brokers.get(name) or {"error": f"{name} funds unavailable"}
    if "error" in primary:
        return {"error": primary["error"], "broker": name, "brokers": brokers}
    return {"broker": name, "ashwax": primary, "brokers": brokers}


@app.get("/api/options_funds_margins")
async def get_options_funds_margins() -> dict[str, Any]:
    """Funds & Margins strip for the Options Trading dashboard tab, paper AND
    live. Recomputed independently here rather than read from the orchestrator's
    in-process SellerDesk -- API and orchestrator are separate processes (see
    launcher.py; api_stdout.log/orchestrator_stdout.log are deliberately
    separate files for the same reason), so there is no shared Python object to
    read. Sources: options_settings.json's options_capital_rupees (the paper
    capital sleeve, same figure SellerDesk.available_margin() uses in paper
    mode) for paper; the live broker margins call (same as /api/margins) for
    live; config/capital.json's options.max_share_of_shared_risk (src/risk/
    budget.py), scaled to live equity, for both -- it is one account-wide
    ceiling, not a separate paper/live figure."""
    from src.options import seller_settings as _seller_settings
    from src.risk.budget import get_budget

    paper_capital = float(_seller_settings.load_settings().get("options_capital_rupees", 0))

    paper_committed = 0.0
    if _db_pool is not None:
        try:
            row = await _db_pool.fetchrow(
                "SELECT COALESCE(SUM(entry_margin), 0) AS total FROM options_positions "
                "WHERE status = 'OPEN' AND is_paper = true"
            )
            paper_committed = float(row["total"] or 0.0) if row else 0.0
        except Exception:
            paper_committed = 0.0

    live_fund = 0.0
    live_margin = 0.0
    live_equity = 0.0
    try:
        _live_name = _access().primary_account(_legacy_broker_name())
        if _live_name is None:
            raise RuntimeError("no broker configured")
        live_m = await _access().margins(_live_name)
        # Fund Available = free cash (what you could actually withdraw);
        # Margin Available = total margin capacity for new F&O positions
        # (net equity, incl. collateral) -- operator correction 2026-10-07,
        # these were swapped.
        live_fund = live_m.available_cash
        live_margin = live_m.equity
        live_equity = live_m.equity
    except Exception:
        pass

    budget = get_budget()
    equity_for_ceiling = live_equity or budget.account.fallback_equity_rupees
    ceiling = budget.options.max_share_of_shared_risk_rupees(equity_for_ceiling)

    return {
        "paper": {
            "fundAvailable": paper_capital,
            "marginAvailable": max(0.0, paper_capital - paper_committed),
            "sharedRiskCeiling": ceiling,
        },
        "live": {
            "fundAvailable": live_fund,
            "marginAvailable": live_margin,
            "sharedRiskCeiling": ceiling,
        },
    }


@app.get("/api/gtt")
def get_gtt() -> list[Any]:
    if not _kite_configured():
        return []  # Kite GTTs; nothing to show without Kite (never mock)
    try:
        # GTTs are placed/modified rarely; 10s is well inside useful freshness.
        return _broker_cached("gtt", 10.0, _kite().get_gtts)
    except Exception as exc:
        return [{"error": str(exc)}]


@app.get("/api/ltp")
async def get_ltp(symbols: str = "NSE:NIFTY 50") -> dict[str, Any]:
    """Comma-separated instrument symbols, served by the market-data router. Kite-shaped
    rows; ohlc.close (for change %) only when a provider with quotes (Kite) answered."""
    try:
        sym_list = [s.strip() for s in symbols.split(",")]
        return await _access().quote(sym_list)
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/api/option_chain")
async def get_option_chain(symbol: str, expiry: str | None = None) -> dict[str, Any]:
    """Read-only option chain with live OI/volume and Black-Scholes Greeks
    computed locally from the chain's already-solved IVs (src/options/greeks.py,
    no external I/O). `symbol` accepts a bare root ("NIFTY") or an
    "NSE:"-prefixed form. `expiry` (optional, ISO date) selects the nearest
    qualifying expiry at or after that date; omitted, the nearest expiry
    (min 0 DTE) is used -- same chain-building logic the live seller desk
    uses (src/options/nfo_chain.py NFOChainProvider.get_chain), reused
    read-only here, not re-implemented."""
    from datetime import date as _date
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    from src.options.greeks import DEFAULT_RISK_FREE_RATE, days_to_years
    from src.options.greeks import greeks as _greeks

    root = symbol.strip().upper().removeprefix("NSE:")
    now = _dt.now(_ZI("Asia/Kolkata"))
    min_days = 0
    if expiry:
        try:
            target = _date.fromisoformat(expiry)
            min_days = max(0, (target - now.date()).days)
        except ValueError:
            return {"error": f"invalid expiry date: {expiry!r}"}

    if not _kite_configured() and _access().rate_capped():
        # A chain costs ~120 per-symbol quote calls on a rate-capped provider (Flattrade), on
        # every 5s page poll, out of a per-key budget the orchestrator trades on. Refused.
        return {
            "symbol": root,
            "expiry": None,
            "spot": None,
            "strikes": [],
            "rate_limited": True,
            "error": (
                "Option chain unavailable: the only market-data provider is rate-capped "
                "(Flattrade); its request budget is kept for trading. Add Kite or Angel One "
                "credentials to see the chain here."
            ),
        }
    try:
        provider = _get_option_chain_provider()
        chain = await provider.get_chain(f"NSE:{root}", min_days_to_expiry=min_days, now=now)
    except Exception as exc:
        return {"symbol": root, "expiry": None, "spot": None, "strikes": [], "error": str(exc)}

    if chain is None:
        return {"symbol": root, "expiry": None, "spot": None, "strikes": []}

    t = days_to_years(chain.days_to_expiry)
    rows: list[dict[str, Any]] = []
    for s in chain.strikes:
        row: dict[str, Any] = {"strike": s.strike, "call": None, "put": None}
        for side, iv, quote, tsym in (
            ("call", s.call_iv, s.call, s.call_symbol),
            ("put", s.put_iv, s.put, s.put_symbol),
        ):
            if quote is None:
                continue
            g = (
                _greeks(
                    chain.spot,
                    s.strike,
                    t,
                    DEFAULT_RISK_FREE_RATE,
                    iv,
                    "CE" if side == "call" else "PE",
                )
                if iv
                else None
            )
            row[side] = {
                "symbol": tsym,
                "ltp": quote.ltp,
                "bid": quote.bid,
                "ask": quote.ask,
                "oi": quote.oi,
                "volume": quote.volume,
                "iv": iv,
                "delta": g["delta"] if g else None,
                "gamma": g["gamma"] if g else None,
                "theta": g["theta"] if g else None,
                "vega": g["vega"] if g else None,
            }
        rows.append(row)

    return {
        "symbol": root,
        "spot": chain.spot,
        "expiry": chain.expiry.date().isoformat(),
        "days_to_expiry": chain.days_to_expiry,
        "lot_size": chain.lot_size,
        "strikes": rows,
    }


# ---------------------------------------------------------------------------
# System state (from logs — written by background modules)
# ---------------------------------------------------------------------------


@app.get("/api/regime")
async def get_regime() -> dict[str, Any]:
    if _db_pool is not None:
        try:
            from src.db.repositories.market_data import MarketDataRepository

            row = await MarketDataRepository(_db_pool).latest_regime()
            if row:
                ma50 = float(row["nifty_ma50"] or 0)
                ma200 = float(row["nifty_ma200"] or 0)
                trend = "UP" if ma200 > 0 and ma50 >= ma200 else ("DOWN" if ma200 > 0 else "FLAT")
                breadth_pct = float(row["breadth_pct"] or 0)
                import json as _json

                _suitable_raw = row.get("suitable_strategies", None)
                _avoid_raw = row.get("avoid_strategies", None)
                return {
                    "regime": row["regime"],
                    "confidence": float(row["confidence"] or 0),
                    "vix": float(row["vix"] or 0),
                    "breadth": round(breadth_pct / 100.0, 4),
                    "ma50_trend": trend,
                    "ma200_trend": trend,
                    "adx": float(row["adx"] or 0) if "adx" in row else 0,
                    "atr_pct": float(row["atr_pct"] or 0) if "atr_pct" in row else 0,
                    "suitable_strategies": _json.loads(_suitable_raw) if _suitable_raw else [],
                    "avoid_strategies": _json.loads(_avoid_raw) if _avoid_raw else [],
                    "position_size_multiplier": float(row["position_size_multiplier"] or 1.0)
                    if "position_size_multiplier" in row
                    else 1.0,
                    "historical_accuracy": float(row["historical_accuracy"] or 0)
                    if "historical_accuracy" in row
                    else 0,
                    "since": str(row["created_at"]),
                    "source": "db",
                    # Day-scoped regime from the live intraday loop (5-min refresh);
                    # UNKNOWN off-hours or when the index feed is unavailable.
                    "intraday_regime": _read_log("intraday_signals.json", {}).get(
                        "intraday_regime", "UNKNOWN"
                    ),
                }
        except Exception:
            pass
    _fallback = _read_log("market_regime.json", {"regime": "UNKNOWN", "source": "fallback"})
    if isinstance(_fallback, dict):
        _fallback.setdefault(
            "intraday_regime",
            _read_log("intraday_signals.json", {}).get("intraday_regime", "UNKNOWN"),
        )
    return _fallback


@app.get("/api/strategy_health")
def get_strategy_health() -> dict[str, Any]:
    return _read_log("strategy_health.json", {})


@app.get("/api/llm_health")
async def get_llm_health() -> dict[str, Any]:
    """Latest morning/evening/startup LLM provider + gate-chain probe (src/llm/model_health.py):
    overall OK|DEGRADED|FAIL, per-provider and per-chain rows, and recent history. {} until the
    first probe has run (it runs ~45s after the orchestrator starts)."""
    from src.llm.model_health import load_health

    return load_health()


@app.get("/api/llm_tokens")
def get_llm_tokens() -> dict[str, Any]:
    """Today's LLM token/request spend, per provider and per purpose
    (src/llm/model_health.py:write_token_usage_snapshot, refreshed every ~60s by the
    orchestrator's health loop). {} until the first write.

    Written by the orchestrator, not read live from TokenLedger here: LEDGER is
    Redis-backed but only the orchestrator process calls LEDGER.configure(redis_client)
    — this process's own LEDGER import has no Redis attached and would show all zeros,
    the same reason /api/llm_health reads a file rather than querying providers live."""
    return _read_log("llm_token_usage.json", {"checked_at": None, "providers": {}, "purposes": {}})


@app.get("/api/health_overview")
async def get_health_overview() -> list[dict[str, Any]]:
    """Per-strategy view of what the health/expectancy learning loop is
    CURRENTLY doing — tier, the resulting position-size multiplier applied to
    new entries (orchestrator.py HEALTH_TIER_SIZE_MULTIPLIER / G_HEALTH gate),
    and the open pause/decay incident explaining why, if any. The Learning and
    Intelligence tabs already showed the loop's inputs (attribution,
    expectancy) but not this — the actual live effect on trading — which is
    what made the feedback loop look inert even though it measurably isn't."""
    from src.orchestrator import HEALTH_TIER_SIZE_MULTIPLIER

    tiers: dict[str, Any] = _read_log("strategy_health.json", {})
    incidents_by_strategy: dict[str, dict[str, Any]] = {}
    if _db_pool is not None:
        try:
            from src.db.repositories.supervisor import SupervisorRepository

            rows = await SupervisorRepository(_db_pool).open_incidents()
            for r in rows:
                if r["invariant_id"] not in ("SH_DEGRADED", "SH_SUSPENDED", "STRATEGY_DECAY"):
                    continue
                # dedup_key encodes the strategy name with a known prefix per
                # invariant (health_degraded_/health_suspended_/strategy_decay_) —
                # strip it rather than parsing the free-text message.
                key = str(r["dedup_key"])
                name = (
                    key.removeprefix("health_degraded_")
                    .removeprefix("health_suspended_")
                    .removeprefix("strategy_decay_")
                )
                incidents_by_strategy[name] = {
                    "invariantId": r["invariant_id"],
                    "severity": r["severity"],
                    "message": r["message"],
                    "openedAt": str(r["opened_at"]),
                }
        except Exception as exc:
            _log.warning("health_overview_incidents_fetch_failed err=%s", str(exc)[:160])

    out = [
        {
            "strategy": name,
            "tier": str(tier),
            "sizeMultiplier": HEALTH_TIER_SIZE_MULTIPLIER.get(str(tier), 1.0),
            "activeIncident": incidents_by_strategy.get(name),
        }
        for name, tier in tiers.items()
    ]
    out.sort(key=lambda r: r["sizeMultiplier"])
    return out


@app.get("/api/nightly_setups")
async def get_nightly_setups() -> list[Any]:
    if _db_pool is not None:
        try:
            from src.db.repositories.learning import LearningRepository

            rows = await LearningRepository(_db_pool).recent_setups(50)
            if rows:
                return [dict(r) for r in rows]
        except Exception:
            pass
    return _read_log("nightly_setups.json", [])


@app.get("/api/rs_ranking")
async def get_rs_ranking() -> dict[str, Any]:
    if _db_pool is not None:
        try:
            from src.db.repositories.market_data import MarketDataRepository

            rows = await MarketDataRepository(_db_pool).latest_rs_snapshot()
            if rows:
                leaders = [dict(r) for r in rows if r["rs_score"] >= 0]
                return {"leaders": leaders, "improvers": [], "blacklist": [], "source": "db"}
        except Exception:
            pass
    return _read_log("rs_ranking.json", {})


@app.get("/api/signals")
async def get_signals() -> list[Any]:
    """Recent signals from strategy workers (last 1h, up to 50)."""
    if _db_pool is not None:
        try:
            async with _db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id         AS signal_id,
                           symbol,
                           strategy_name,
                           NULL::text AS regime,
                           llm_confidence,
                           quality_score AS score,
                           created_at,
                           jsonb_build_object(
                               'direction',      direction,
                               'disposition',    disposition,
                               'gate_results',   gate_results,
                               'llm_confidence', llm_confidence,
                               'score',          quality_score
                           ) AS meta
                    FROM signals
                    WHERE created_at >= NOW() - INTERVAL '1 hour'
                    ORDER BY created_at DESC
                    LIMIT 50
                    """
                )
            if rows:
                return [dict(r) for r in rows]
        except Exception:
            pass
    return _read_log("signals_recent.json", [])


@app.get("/api/scanner")
async def get_scanner() -> dict[str, Any]:
    """Nightly setups + RS leaders + real swing-strategy evaluation for the Signals/Scanner tab."""
    raw_setups = _read_log("nightly_setups.json", [])
    # nightly_setups.json stores {"top_setups": [...], "computed_at": "..."}
    if isinstance(raw_setups, dict):
        setups = raw_setups.get("top_setups", [])
    else:
        setups = raw_setups if isinstance(raw_setups, list) else []
    rs = _read_log("rs_ranking.json", {})
    swing = _read_log("swing_signals.json", {})
    return {
        "nightly_setups": setups,
        "rs_leaders": rs.get("leaders", []) if isinstance(rs, dict) else [],
        "swing_signals": swing.get("signals", []) if isinstance(swing, dict) else [],
        "swing_scan_ts": swing.get("ts") if isinstance(swing, dict) else None,
    }


@app.get("/api/alerts")
async def get_alerts() -> list[Any]:
    if _db_pool is not None:
        try:
            from src.db.repositories.supervisor import SupervisorRepository

            rows = await SupervisorRepository(_db_pool).open_incidents()
            if rows:
                return [
                    {
                        "id": str(r["id"]),
                        "tier": r["severity"],
                        "message": r["message"],
                        "acknowledged": False,
                        "ts": str(r["opened_at"]),
                    }
                    for r in rows
                ]
        except Exception:
            pass
    return _read_log("alerts.json", [])


@app.get("/api/strategies")
def get_strategies() -> list[Any]:
    """Strategy list from disk files merged with live health scores."""
    health: dict[str, Any] = _read_log("strategy_health.json", {})
    # Load enabled/disabled flags from the central registry (source of truth)
    _reg_path = _BASE / "config" / "strategies_registry.json"
    _registry_enabled: dict[str, bool] = {}
    _registry_execution_mode: dict[str, str] = {}
    _registry_broker: dict[str, str | None] = {}
    _registry_broker_ready: dict[str, bool | None] = {}
    if _reg_path.exists():
        try:
            for entry in json.loads(_reg_path.read_text(encoding="utf-8")).get("strategies", []):
                _registry_enabled[entry["name"]] = bool(entry.get("enabled", True))
                _registry_execution_mode[entry["name"]] = entry.get("execution_mode", "paper")
                _b = entry.get("broker")
                _registry_broker[entry["name"]] = _b if isinstance(_b, str) and _b else None
                # brokerReady = the R1 route is usable. Live entries: always evaluated (a live
                # entry without a broker is False, its signals would be rejected). Paper entries:
                # None when no broker is chosen yet, else whether the chosen one WOULD be usable
                # if the strategy were switched live.
                if entry.get("execution_mode") == "live" or _registry_broker[entry["name"]]:
                    _registry_broker_ready[entry["name"]] = _route_if_live(entry).ok
        except Exception:
            pass
    strategies: list[dict[str, Any]] = []
    for directory in [_BASE / "strategies" / "builtin", _BASE / "strategies" / "agent_created"]:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                meta = data.get("meta", {})
                name = meta.get("name", path.stem)
                conditions = data.get("entry_conditions", {}).get("conditions", [])
                target = data.get("target", {})
                strategies.append(
                    {
                        "name": name,
                        "display_name": meta.get("display_name") or name.replace("_", " ").title(),
                        "enabled": _registry_enabled.get(name, meta.get("enabled", True)),
                        "executionMode": _registry_execution_mode.get(name, "paper"),
                        "broker": _registry_broker.get(name),
                        "brokerReady": _registry_broker_ready.get(name),
                        "direction": meta.get("direction", "LONG"),
                        "timeframe": meta.get("timeframe", ""),
                        "product": meta.get("product", ""),
                        "condition_count": len(conditions),
                        "target_rr": target.get("ratio"),
                        "source_format": "yaml"
                        if path.suffix in (".yaml", ".yml")
                        else "pine"
                        if path.suffix == ".pine"
                        else "json",
                        "health": health.get(name, "HEALTHY"),
                    }
                )
            except (json.JSONDecodeError, OSError):
                continue
    return strategies


@app.put("/api/strategies/{name}/enabled")
def update_strategy_enabled(name: str, body: dict[str, Any]) -> dict[str, Any]:
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail='Body must be {"enabled": bool}')

    spec_path: str | None = None
    source = "BUILTIN"
    for directory in [_BASE / "strategies" / "builtin", _BASE / "strategies" / "agent_created"]:
        if not directory.exists():
            continue
        for candidate in directory.glob("*.json"):
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("meta", {}).get("name", candidate.stem) == name:
                spec_path = candidate.relative_to(_BASE).as_posix()
                source = "BUILTIN" if directory.name == "builtin" else "AGENT_CREATED"
                break
        if spec_path:
            break
    if spec_path is None:
        raise HTTPException(status_code=404, detail=f"No strategy file found for: {name}")

    reg_path = _BASE / "config" / "strategies_registry.json"
    with _CONFIG_WRITE_LOCK:
        registry: dict[str, Any] = (
            json.loads(reg_path.read_text(encoding="utf-8"))
            if reg_path.exists()
            else {"strategies": []}
        )
        strategies = registry.setdefault("strategies", [])
        for entry in strategies:
            if entry.get("name") == name:
                entry["enabled"] = enabled
                break
        else:
            strategies.append(
                {
                    "name": name,
                    "source": source,
                    "spec_path": spec_path,
                    "enabled": enabled,
                    "min_quality_score": 65,
                }
            )
        _write_json_atomic(reg_path, registry)
    return {"ok": True, "enabled": enabled}


@app.put("/api/strategies/{name}/execution_mode")
def update_strategy_execution_mode(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Per-strategy paper/live decision (strategies_registry.json execution_mode).
    Always explicit: "paper" or "live" — see _resolve_is_paper in orchestrator.py
    for the full precedence (operator.json paper_mode_locked always wins, so
    "live" is refused while the account is locked).
    Going "live" additionally needs a usable broker (spec rule R1: no default, no failover):
    the entry's stored `broker`, or one supplied in the body as {"broker": "<name>"}, must be
    registered, non-paper and configured, else HTTP 400 and nothing is written. "paper" never
    touches the stored broker.
    Takes effect on the next engine restart, same as every other config here."""
    mode = body.get("executionMode")
    if mode not in ("paper", "live"):
        raise HTTPException(
            status_code=400, detail='Body must be {"executionMode": "paper"|"live"}'
        )
    if mode == "live":
        op_path = _BASE / "config" / "operator.json"
        try:
            op_cfg = json.loads(op_path.read_text(encoding="utf-8")) if op_path.exists() else {}
        except (OSError, ValueError):
            op_cfg = {}
        if op_cfg.get("account", {}).get("paper_mode_locked", False):
            raise HTTPException(
                status_code=409, detail="Account is paper_mode_locked; cannot set a strategy live"
            )

    reg_path = _BASE / "config" / "strategies_registry.json"
    if not reg_path.exists():
        raise HTTPException(status_code=404, detail="strategies_registry.json not found")
    with _CONFIG_WRITE_LOCK:
        registry: dict[str, Any] = json.loads(reg_path.read_text(encoding="utf-8"))
        strategies = registry.setdefault("strategies", [])
        for entry in strategies:
            if entry.get("name") == name:
                if mode == "live":
                    broker = body["broker"] if "broker" in body else entry.get("broker")
                    if "broker" in body and (not isinstance(broker, str) or not broker.strip()):
                        raise HTTPException(
                            status_code=400, detail='"broker" must be a non-empty broker name'
                        )
                    route = _route_if_live({**entry, "broker": broker})
                    if not route.ok:
                        raise HTTPException(
                            status_code=400, detail=f"Cannot set {name} live: {route.reason}"
                        )
                    entry["broker"] = route.broker
                entry["execution_mode"] = mode
                break
        else:
            raise HTTPException(status_code=404, detail=f"No registry entry found for: {name}")

        _write_json_atomic(reg_path, registry)
    return {"ok": True, "executionMode": mode}


@app.put("/api/strategies/{name}/broker")
def update_strategy_broker(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Choose the live broker of a registry strategy (strategies_registry.json `broker`).
    The broker must be registered, non-paper and configured (credentials in env), else HTTP 400
    and nothing is written. Does not change execution_mode. Takes effect on the next engine
    restart; open positions keep exiting on the broker stamped on their own row."""
    broker = body.get("broker")
    if not isinstance(broker, str) or not broker.strip():
        raise HTTPException(status_code=400, detail='Body must be {"broker": "<broker name>"}')

    reg_path = _BASE / "config" / "strategies_registry.json"
    if not reg_path.exists():
        raise HTTPException(status_code=404, detail="strategies_registry.json not found")
    with _CONFIG_WRITE_LOCK:
        registry: dict[str, Any] = json.loads(reg_path.read_text(encoding="utf-8"))
        for entry in registry.setdefault("strategies", []):
            if entry.get("name") == name:
                route = _route_if_live({**entry, "broker": broker})
                if not route.ok:
                    raise HTTPException(
                        status_code=400, detail=f"Cannot use broker for {name}: {route.reason}"
                    )
                entry["broker"] = broker
                break
        else:
            raise HTTPException(status_code=404, detail=f"No registry entry found for: {name}")

        _write_json_atomic(reg_path, registry)
    return {"ok": True, "broker": broker}


# ---------------------------------------------------------------------------
# Phase 2.5 survival endpoints
# ---------------------------------------------------------------------------


@app.get("/api/weekly_pnl")
def get_weekly_pnl() -> dict[str, Any]:
    return _read_cache(
        "weekly_pnl.json",
        8 * 86400,
        {"weeks": {}, "consecutive_negative": 0, "halt_threshold": 4, "halted": False},
    )


@app.get("/api/gap_risk")
def get_gap_risk() -> list[Any]:
    return _read_cache("gap_risk.json", 26 * 3600, [])


@app.get("/api/correlation")
def get_correlation() -> dict[str, Any]:
    return _read_log("correlation.json", {"level": "NORMAL", "avg": 0.0, "max": 0.0})


@app.get("/api/tax_report")
async def get_tax_report(mode: str = "live") -> dict[str, Any]:
    """DB truth, not the logs/closed_trades.json cache this used to read: that
    file is written once per close and never re-synced, so a trade deleted
    from `trades`/`paper_trades` (e.g. a duplicate-entry cleanup) kept showing
    up here forever -- the exit_timestamp staleness already fixed for the
    Intelligence/Learning tabs (see _fetch_closed_trades_from_db).

    `mode`: "live" (default) | "paper" -- strictly one table each, never blended
    (this used to UNION both, so the "live" tax figures included paper P&L).
    Equity trades only; options_positions are not part of this report."""
    from src.reporting.tax_report import ClosedTrade, generate_tax_report

    if mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")
    table = "paper_trades" if mode == "paper" else "trades"
    dup_filter = (
        "AND exit_reason IS DISTINCT FROM 'manual_dup_entry_cleanup'" if mode == "paper" else ""
    )
    trades: list[ClosedTrade] = []
    if _db_pool is not None:
        try:
            rows = await _db_pool.fetch(
                f"""
                SELECT symbol, product, entry_timestamp, exit_timestamp, pnl_realized, quantity,
                       partial_qty_booked
                  FROM {table}
                 WHERE order_state = 'CLOSED' AND exit_timestamp IS NOT NULL
                   {dup_filter}
                """  # noqa: S608
            )
        except Exception as exc:
            _log.warning("tax_report_db_fetch_failed err=%s", str(exc)[:160])
            rows = []
        for r in rows:
            entry_ts, exit_ts = r["entry_timestamp"], r["exit_timestamp"]
            trades.append(
                ClosedTrade(
                    symbol=str(r["symbol"] or ""),
                    product=str(r["product"] or "CNC"),
                    entry_date=entry_ts.date() if entry_ts else exit_ts.date(),
                    exit_date=exit_ts.date(),
                    net_pnl=float(r["pnl_realized"] or 0.0),
                    quantity=int(r["quantity"] or 0) + int(r.get("partial_qty_booked") or 0),
                )
            )
    r = generate_tax_report(trades)
    return {
        "fy_label": r.fy_label,
        "stcg_gains": r.stcg_gains,
        "ltcg_gains": r.ltcg_gains,
        "ltcg_gains_taxable": r.ltcg_gains_taxable,
        "speculative_income": r.speculative_income,
        "stcg_tax": r.stcg_tax,
        "ltcg_tax": r.ltcg_tax,
        "total_tax_liability": r.total_tax_liability,
        "trades": [
            {
                "symbol": line.symbol,
                "category": line.category,
                "net_pnl": line.net_pnl,
                "tax_liability": line.tax_liability,
                "holding_days": line.holding_days,
            }
            for line in r.trades
        ],
    }


# ---------------------------------------------------------------------------
# Phase 2.5 — Survival hardening endpoints
# ---------------------------------------------------------------------------


@app.get("/api/edge_decay")
def get_edge_decay() -> dict[str, Any]:
    return _read_cache("edge_decay.json", 36 * 3600, {})


@app.get("/api/cost_erosion")
def get_cost_erosion() -> dict[str, Any]:
    return _read_cache(
        "cost_erosion.json",
        2 * 3600,
        {"level": "GREEN", "cost_ratio": 0.0, "trade_count": 0, "reason": "No data yet"},
    )


@app.get("/api/llm_sanity")
def get_llm_sanity() -> dict[str, Any]:
    return _read_log("llm_sanity.json", {"bypass_llm": False, "pending_signals": 0})


# ---------------------------------------------------------------------------
# Phase 3 — Awareness endpoints
# ---------------------------------------------------------------------------

_SCOPE_TO_CATEGORY = {"COMPANY": "STOCK", "SECTOR": "SECTOR", "MACRO": "MACRO"}
_SENTIMENT_MAP = {
    "BULLISH": "POSITIVE",
    "BEARISH": "NEGATIVE",
    "NEUTRAL": "NEUTRAL",
    "POSITIVE": "POSITIVE",
    "NEGATIVE": "NEGATIVE",
}
_ACTION_MAP = {
    "BOOST": "BUY",
    "HALT": "AVOID",
    "AVOID": "AVOID",
    "DECAY": "SELL",
    "MONITOR": "HOLD",
}


@app.get("/api/news")
async def get_news() -> list[Any]:
    """Classified news — DB-first, falls back to logs/news_status.json."""
    raw: list[dict[str, Any]] = []
    if _db_pool is not None:
        try:
            from src.db.repositories.news import NewsRepository

            db_rows = await NewsRepository(_db_pool).recent(50)
            if db_rows:
                raw = [dict(r) for r in db_rows]
        except Exception:
            pass
    if not raw:
        raw = _read_log("news_status.json", [])
    filtered = [r for r in raw if r.get("headline", "").strip()]
    seen: dict[str, dict[str, Any]] = {}
    for r in filtered:
        key = r.get("symbol", "UNKNOWN") + "_" + str(r.get("created_at", ""))[:10]
        existing = seen.get(key)
        if not existing or float(r.get("confidence", 0)) > float(existing.get("confidence", 0)):
            seen[key] = r
    deduped = sorted(seen.values(), key=lambda x: x.get("created_at", ""), reverse=True)[:30]
    items: list[dict[str, Any]] = []
    for r in deduped:
        sentiment = str(r.get("sentiment", "NEUTRAL"))
        confidence = float(r.get("confidence", 0.5))
        sentiment_score = (
            confidence
            if sentiment in ("BULLISH", "POSITIVE")
            else (-confidence if sentiment in ("BEARISH", "NEGATIVE") else 0.0)
        )
        magnitude = str(r.get("magnitude", "LOW")).upper()
        items.append(
            {
                "id": str(r.get("symbol", "")) + "_" + str(r.get("created_at", ""))[:19],
                # NSE headlines are generic category labels shared by every company
                "headline": (
                    f"{r['symbol']}: {r['headline']}"
                    if r.get("symbol") not in (None, "", "UNKNOWN")
                    and not str(r["headline"]).startswith(f"{r['symbol']}:")
                    else str(r.get("headline", ""))
                ),
                "summary": str(r.get("reasoning", "")),
                "source": str(r.get("source", "")),
                "category": _SCOPE_TO_CATEGORY.get(str(r.get("scope", "MACRO")), "MACRO"),
                "sentiment": _SENTIMENT_MAP.get(sentiment, "NEUTRAL"),
                "sentimentScore": round(sentiment_score, 2),
                "confidence": round(confidence, 2),
                "relevantSymbols": r.get("affected_symbols", [r.get("symbol", "")]),
                "relevantSectors": [],
                "impactLevel": {"HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW"}.get(
                    magnitude, "LOW"
                ),
                "publishedAt": str(r.get("created_at", "")),
                "processedAt": str(r.get("created_at", "")),
                "tradingAction": _ACTION_MAP.get(str(r.get("trading_action", "MONITOR")), "HOLD"),
                "agentAnalysis": str(r.get("reasoning", "")),
            }
        )
    return items


@app.get("/api/sector_rotation")
def get_sector_rotation() -> dict[str, Any]:
    return _read_log("sector_rotation.json", {"sectors": [], "computed_at": ""})


_NIFTY50_SYMBOLS = [
    "NSE:RELIANCE",
    "NSE:TCS",
    "NSE:HDFCBANK",
    "NSE:INFY",
    "NSE:ICICIBANK",
    "NSE:HINDUNILVR",
    "NSE:ITC",
    "NSE:SBIN",
    "NSE:BHARTIARTL",
    "NSE:KOTAKBANK",
    "NSE:LT",
    "NSE:HCLTECH",
    "NSE:AXISBANK",
    "NSE:WIPRO",
    "NSE:ASIANPAINT",
    "NSE:MARUTI",
    "NSE:SUNPHARMA",
    "NSE:TATAMOTORS",
    "NSE:ULTRACEMCO",
    "NSE:TITAN",
    "NSE:BAJFINANCE",
    "NSE:NESTLEIND",
    "NSE:POWERGRID",
    "NSE:NTPC",
    "NSE:TECHM",
    "NSE:TATASTEEL",
    "NSE:JSWSTEEL",
    "NSE:HDFCLIFE",
    "NSE:SBILIFE",
    "NSE:INDUSINDBK",
    "NSE:BAJAJFINSV",
    "NSE:ONGC",
    "NSE:COALINDIA",
    "NSE:GRASIM",
    "NSE:DRREDDY",
    "NSE:CIPLA",
    "NSE:DIVISLAB",
    "NSE:BRITANNIA",
    "NSE:EICHERMOT",
    "NSE:HEROMOTOCO",
    "NSE:BPCL",
    "NSE:TATACONSUM",
    "NSE:APOLLOHOSP",
    "NSE:HINDALCO",
    "NSE:ADANIENT",
    "NSE:ADANIPORTS",
    "NSE:M&M",
    "NSE:BAJAJ-AUTO",
    "NSE:UPL",
    "NSE:SHREECEM",
]

_SECTOR_INDICES = {
    "BANKING": "NSE:NIFTY BANK",
    "IT": "NSE:NIFTY IT",
    "PHARMA": "NSE:NIFTY PHARMA",
    "AUTO": "NSE:NIFTY AUTO",
    "FMCG": "NSE:NIFTY FMCG",
    "METAL": "NSE:NIFTY METAL",
    "REALTY": "NSE:NIFTY REALTY",
    "ENERGY": "NSE:NIFTY ENERGY",
}


@app.get("/api/market_breadth")
async def get_market_breadth() -> dict[str, Any]:
    """Advance/Decline for NIFTY 50 constituents vs the previous close. Needs quotes with a
    previous close (Kite today); without one, or on failure, the "unavailable" payload."""
    try:
        if _kite_configured():
            await asyncio.to_thread(_kite_refreshed)
        ltp_data = await _access().quote(_NIFTY50_SYMBOLS, ltp_fallback=False)
        advances = declines = unchanged = 0
        for _sym, entry in ltp_data.items():
            last = float(entry.get("last_price", 0))
            prev = float((entry.get("ohlc") or {}).get("close", 0))
            if prev <= 0:
                continue
            if last > prev:
                advances += 1
            elif last < prev:
                declines += 1
            else:
                unchanged += 1
        if advances + declines + unchanged == 0:
            raise ValueError("no previous-close data from the market-data provider")
        return {
            "advances": advances,
            "declines": declines,
            "unchanged": unchanged,
            "source": "kite",
        }
    except Exception:
        return {"advances": 0, "declines": 0, "unchanged": 0, "source": "unavailable"}


@app.get("/api/sector_performance")
async def get_sector_performance() -> list[Any]:
    """Live daily sector performance from sector index quotes (market-data router)."""
    try:
        if _kite_configured():
            await asyncio.to_thread(_kite_refreshed)
        symbols = list(_SECTOR_INDICES.values())
        # 30s TTL: this quotes every sector index at once and daily sector
        # performance does not move meaningfully inside one dashboard poll.
        ltp_data = await _access().cached(
            "sector_quotes", 30.0, lambda: _access().quote(symbols, ltp_fallback=False)
        )
        if not any((q or {}).get("ohlc") for q in ltp_data.values()):
            # No provider gave a previous close: every change would read 0% -> show none.
            return []
        result = []
        for sector, sym in _SECTOR_INDICES.items():
            entry = ltp_data.get(sym) or ltp_data.get(sym.replace("NSE:", "")) or {}
            last = float(entry.get("last_price", 0))
            prev = float((entry.get("ohlc") or {}).get("close", 0))
            change_pct = ((last - prev) / prev * 100) if prev > 0 else 0.0
            result.append(
                {
                    "sector": sector,
                    "level": round(last, 2),
                    "change_pct": round(change_pct, 2),
                    "trend": "LEADING"
                    if change_pct > 0.5
                    else "LAGGING"
                    if change_pct < -0.5
                    else "NEUTRAL",
                }
            )
        return result
    except Exception:
        return []


@app.get("/api/fii_dii")
async def get_fii_dii() -> dict[str, Any]:
    """FII and DII cash activity from NSE."""
    try:
        import aiohttp

        _ua = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"}
        async with (
            aiohttp.ClientSession(headers=_ua) as session,
            session.get(
                "https://www.nseindia.com/api/fiidiiTradeReact",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp,
        ):
            data = await resp.json(content_type=None)
        fii = {"buy": 0.0, "sell": 0.0, "net": 0.0}
        dii = {"buy": 0.0, "sell": 0.0, "net": 0.0}
        for row in data if isinstance(data, list) else []:
            cat = str(row.get("category", "")).upper()
            buy = float(row.get("buyValue", 0) or 0)
            sell = float(row.get("sellValue", 0) or 0)
            net = float(row.get("netValue", buy - sell) or 0)
            if "FII" in cat or "FPI" in cat:
                fii = {"buy": buy, "sell": sell, "net": net}
            elif "DII" in cat:
                dii = {"buy": buy, "sell": sell, "net": net}
        return {"fii": fii, "dii": dii, "source": "nse"}
    except Exception:
        return {
            "fii": {"buy": 0.0, "sell": 0.0, "net": 0.0},
            "dii": {"buy": 0.0, "sell": 0.0, "net": 0.0},
            "source": "unavailable",
        }


def _max_drawdown_pct(pnl_r_ordered: list[float]) -> float:
    """Peak-to-trough decline of the cumulative R-multiple curve, normalized by the
    peak R reached at the time of that drawdown (floored at 1.0R to avoid blowups
    when the peak is tiny/negative). There's no per-strategy ₹ equity curve tracked,
    so this is an R-space proxy, not a literal equity drawdown %."""
    if not pnl_r_ordered:
        return 0.0
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    peak_at_max_dd = 1.0
    for r in pnl_r_ordered:
        cum += r
        peak = max(peak, cum)
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
            peak_at_max_dd = max(peak, 1.0)
    return round(max_dd / peak_at_max_dd * 100, 2)


def _sharpe_ratio(pnl_r: list[float]) -> float:
    """Mean/stdev of per-trade R-multiples — a trade-level Sharpe-like ratio, not a
    time-annualized one (no consistent bar interval across intraday/swing trades)."""
    if len(pnl_r) < 2:
        return 0.0
    import statistics as _stats

    mean_r = _stats.mean(pnl_r)
    stdev_r = _stats.stdev(pnl_r)
    return round(mean_r / stdev_r, 2) if stdev_r > 0 else 0.0


def _strategy_recommendations(
    win_rate: float, profit_factor: float, by_regime: dict[str, float]
) -> list[str]:
    recs: list[str] = []
    if profit_factor < 1.0:
        recs.append(f"Negative edge (PF {profit_factor:.2f}) — backtest before further live use.")
    if win_rate < 0.30:
        recs.append(f"Low win rate ({win_rate * 100:.0f}%) — review entry criteria.")
    if by_regime:
        worst_regime, worst_r = min(by_regime.items(), key=lambda kv: kv[1])
        if worst_r < 0:
            recs.append(
                f"Underperforms in {worst_regime} (total {worst_r:.1f}R) — consider a regime filter."
            )
    if not recs and profit_factor >= 1.5 and win_rate >= 0.45:
        recs.append("Healthy edge — no action needed.")
    return recs[:3]


@app.get("/api/attribution")
async def get_attribution() -> list[dict[str, Any]]:
    """Strategy attribution for the Intelligence tab — computed fresh from real DB
    closed-trade history (paper_trades UNION trades) each call, not a daily batch
    table, so it's always in sync with the latest close. Win rate/PF/avg-R/Sharpe/
    max-drawdown are all derived from actual R-multiples; the P&L attribution chart
    breaks each strategy's total R down by regime_at_entry; recommendations are
    simple rule-based flags, not LLM-generated."""
    from src.orchestrator import _fetch_closed_trades_from_db

    trades = await _fetch_closed_trades_from_db(_db_pool)
    if not trades:
        return []
    by_strategy: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        by_strategy.setdefault(str(t.get("strategy_name", "unknown")), []).append(t)

    out: list[dict[str, Any]] = []
    for strat, strat_trades in by_strategy.items():
        strat_trades.sort(key=lambda t: str(t.get("exit_ts", "")))
        pnl_r = [float(t.get("pnl_r", 0.0)) for t in strat_trades]
        n = len(pnl_r)
        wins = [r for r in pnl_r if r > 0]
        losses = [abs(r) for r in pnl_r if r <= 0]
        win_rate = len(wins) / n if n else 0.0
        gwin, gloss = sum(wins), sum(losses)
        profit_factor = (gwin / gloss) if gloss > 0 else (99.0 if gwin > 0 else 0.0)

        by_regime: dict[str, float] = {}
        for t, r in zip(strat_trades, pnl_r, strict=False):
            regime = str(t.get("regime_at_entry", "UNKNOWN"))
            by_regime[regime] = by_regime.get(regime, 0.0) + r
        total_abs_r = sum(abs(v) for v in by_regime.values()) or 1.0
        attributions = sorted(
            (
                {"factor": regime, "contribution": round(abs(v) / total_abs_r * 100, 1)}
                for regime, v in by_regime.items()
            ),
            key=lambda a: -a["contribution"],
        )[:5]

        out.append(
            {
                "strategy": strat,
                "totalTrades": n,
                "winRate": round(win_rate, 4),
                "avgRR": round(sum(pnl_r) / n, 2) if n else 0.0,
                "profitFactor": round(profit_factor, 2),
                "maxDrawdown": _max_drawdown_pct(pnl_r),
                "sharpeRatio": _sharpe_ratio(pnl_r),
                "attributions": attributions,
                "recommendations": _strategy_recommendations(win_rate, profit_factor, by_regime),
            }
        )
    return out


@app.get("/api/slippage")
async def get_slippage() -> list[Any]:
    """Slippage per bucket — DB truth (slippage_stats_log), JSON cache fallback."""
    if _db_pool is not None:
        try:
            async with _db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT ON (order_type, time_of_day, adv_bucket)
                           order_type, time_of_day, adv_bucket, avg_slippage, count
                      FROM slippage_stats_log
                     ORDER BY order_type, time_of_day, adv_bucket, created_at DESC
                    """
                )
            if rows:
                return [
                    {
                        "order_type": r["order_type"],
                        "time_of_day": r["time_of_day"],
                        "adv_bucket": r["adv_bucket"],
                        "avg_slippage_pct": float(r["avg_slippage"]),
                        "count": r["count"],
                    }
                    for r in rows
                ]
        except Exception:
            pass
    return _read_cache("slippage_stats.json", 1800, [])


@app.get("/api/expectancy")
def get_expectancy() -> dict[str, Any]:
    """Learning tab's Strategy Expectancy table wants `expectancy`/`total_trades`;
    ExpectancyEngine's own field names are `current_e`/`trade_count` — adapt here
    rather than rename the domain model to match one specific UI consumer."""
    raw = _read_cache("expectancy_status.json", 1800, {})
    return {
        strat: {**v, "expectancy": v.get("current_e", 0), "total_trades": v.get("trade_count", 0)}
        for strat, v in raw.items()
    }


@app.get("/api/calibration")
async def get_calibration() -> dict[str, Any]:
    """Learning tab's LLM Calibration chart, keyed by strategy -> list of buckets.
    NOTE: LearningRepository.latest_calibration(model_used) filters by ONE exact
    strategy name — calling it with "" (all strategies) can never match a row, so
    that path was always dead code. Query llm_calibration_history directly instead."""
    if _db_pool is not None:
        try:
            rows = await _db_pool.fetch(
                """
                SELECT * FROM llm_calibration_history
                 WHERE (model_used, period_end) IN (
                       SELECT model_used, MAX(period_end)
                         FROM llm_calibration_history GROUP BY model_used)
                 ORDER BY model_used
                """
            )
            if rows:
                result: dict[str, Any] = {}
                for r in rows:
                    m = r["model_used"]
                    result.setdefault(m, []).append(
                        {k: v for k, v in dict(r).items() if k not in ("id", "model_used")}
                    )
                return result
        except Exception:
            pass
    return _read_log("calibration_report.json", {})


@app.get("/api/execution_quality")
async def get_execution_quality() -> list[Any]:
    """Per-symbol execution quality for the Intelligence tab (DB truth: trades UNION paper_trades).
    Only symbol/trade-count/avg-slippage are derivable from the schema; fill-time ms,
    limit-fill rate and SL accuracy have no source column → emitted as 0 (never mock)."""
    if _db_pool is None:
        return []
    try:
        rows = await _db_pool.fetch(
            """
            SELECT symbol, COUNT(*) AS n, AVG(ABS(slippage_pct)) AS avg_slip
              FROM (
                SELECT symbol, slippage_pct FROM trades WHERE slippage_pct IS NOT NULL
                UNION ALL
                SELECT symbol, slippage_pct FROM paper_trades WHERE slippage_pct IS NOT NULL
              ) t
             GROUP BY symbol
             ORDER BY n DESC LIMIT 20
            """
        )
    except Exception:
        return []
    return [
        {
            "symbol": r["symbol"],
            "totalTrades": int(r["n"]),
            "avgSlippagePct": round(float(r["avg_slip"] or 0), 4),
            "avgFillTimeMs": 0,
            "marketImpactPct": 0,
            "bestTimeOfDay": "",
            "worstTimeOfDay": "",
            "limitFillRate": 0,
            "slTriggerAccuracy": 0,
            "recommendation": "",
        }
        for r in rows
    ]


@app.get("/api/monitor_stats")
async def get_monitor_stats() -> dict[str, Any]:
    """Live Order Monitor+ stats (DB truth: order_monitor_stats, written by the
    orchestrator monitor loop). A snapshot older than 30 s → inactive zeros."""
    default = {
        "checkIntervalMs": 2000,
        "checksPerMinute": 0,
        "avgLatencyMs": 0,
        "ordersMonitored": 0,
        "breachesDetected": 0,
        "autoActionsExecuted": 0,
    }
    if _db_pool is None:
        return default
    try:
        row = await _db_pool.fetchrow("SELECT * FROM order_monitor_stats WHERE id = 1")
    except Exception:
        return default
    if not row:
        return default
    from datetime import datetime

    ts = row["updated_at"]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    if (datetime.now(UTC) - ts.astimezone(UTC)).total_seconds() > 30:
        return default
    return {
        "checkIntervalMs": row["check_interval_ms"],
        "checksPerMinute": row["checks_per_minute"],
        "avgLatencyMs": row["avg_latency_ms"],
        "ordersMonitored": row["orders_monitored"],
        "breachesDetected": row["breaches_detected"],
        "autoActionsExecuted": row["auto_actions"],
    }


@app.get("/api/breaches")
async def get_breaches() -> list[Any]:
    """Active + recently-resolved order breaches (DB truth: order_monitor_breaches)."""
    if _db_pool is None:
        return []
    try:
        rows = await _db_pool.fetch(
            """
            SELECT * FROM order_monitor_breaches
             WHERE resolved_at IS NULL OR resolved_at > NOW() - INTERVAL '1 hour'
             ORDER BY detected_at DESC LIMIT 50
            """
        )
    except Exception:
        return []
    return [
        {
            "id": str(r["id"]),
            "positionId": r["agent_trade_id"] or "",
            "symbol": r["symbol"],
            "breachType": r["breach_type"],
            "detectedAt": str(r["detected_at"]),
            "ltp": float(r["ltp"] or 0),
            "expectedPrice": float(r["expected_price"] or 0),
            "gapPercent": float(r["gap_pct"] or 0),
            "orderType": r["order_type"] or "LIMIT",
            "orderStatus": "OPEN",
            "autoAction": r["auto_action"] or "NONE",
            "actionResult": r["action_result"],
            "resolvedAt": str(r["resolved_at"]) if r["resolved_at"] else None,
        }
        for r in rows
    ]


@app.post("/api/orders/{agent_trade_id}/force-exit")
async def post_force_exit(agent_trade_id: str) -> dict[str, Any]:
    """Queue a manual market-exit for the dashboard's "Market Exit" button.

    This process has no reference to the live OrderMonitor — it runs in the
    orchestrator process. manual_order_actions (migration 057) is the command
    channel across that boundary; the orchestrator's _monitor_loop polls it
    every ~2s and executes via OrderMonitor.force_exit(). Previously this
    button only flipped local frontend state and never reached the backend at
    all, so a real breach reappeared on the very next poll.
    """
    if _db_pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    existing = await _db_pool.fetchrow(
        "SELECT id FROM manual_order_actions "
        "WHERE agent_trade_id = $1 AND action = 'MARKET_EXIT' AND status = 'PENDING'",
        agent_trade_id,
    )
    if existing:
        return {"id": str(existing["id"]), "status": "PENDING", "queued": False}
    row = await _db_pool.fetchrow(
        "INSERT INTO manual_order_actions (agent_trade_id, action) "
        "VALUES ($1, 'MARKET_EXIT') RETURNING id",
        agent_trade_id,
    )
    return {"id": str(row["id"]), "status": "PENDING", "queued": True}


@app.get("/api/external_positions")
async def get_external_positions() -> list[dict[str, Any]]:
    """Broker positions this agent did not open (external_positions, migration 074)."""
    if _db_pool is None:
        return []
    from src.db.repositories.external_positions import ExternalPositionRepository

    rows = await ExternalPositionRepository(_db_pool).list_open()
    return [
        {
            **r,
            "avg_price": float(r["avg_price"]) if r["avg_price"] is not None else None,
            "first_seen_at": r["first_seen_at"].isoformat(),
            "last_seen_at": r["last_seen_at"].isoformat(),
        }
        for r in rows
    ]


@app.post("/api/external_positions/{position_id}/ack")
async def post_ack_external_position(
    position_id: int, classification: str, note: str | None = None
) -> dict[str, Any]:
    """Acknowledge an external position (classification: MANUAL | CASH_PARKING | LEGACY |
    OTHER). The reconciler then stops reporting it as a gap; it is auto-closed when the
    broker position disappears."""
    if _db_pool is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    from src.db.repositories.external_positions import ExternalPositionRepository

    try:
        ok = await ExternalPositionRepository(_db_pool).acknowledge(
            position_id, classification.upper(), note
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="no open external position with that id")
    return {"id": position_id, "acknowledged": True}


@app.get("/api/orders/{agent_trade_id}/action-status")
async def get_action_status(agent_trade_id: str) -> dict[str, Any]:
    """Poll target for the dashboard after queuing a force-exit."""
    if _db_pool is None:
        return {"status": "UNKNOWN"}
    row = await _db_pool.fetchrow(
        "SELECT status, result FROM manual_order_actions "
        "WHERE agent_trade_id = $1 ORDER BY requested_at DESC LIMIT 1",
        agent_trade_id,
    )
    if row is None:
        return {"status": "NONE"}
    return {"status": row["status"], "result": row["result"]}


@app.get("/api/partial_fills")
async def get_partial_fills() -> list[Any]:
    """Orders sitting in PARTIAL state (DB truth: trades). The schema does not track
    filled-so-far quantity, so fillPercent is 0 — the row's presence is the signal."""
    if _db_pool is None:
        return []
    try:
        rows = await _db_pool.fetch(
            """
            SELECT agent_trade_id, broker_order_id, symbol, quantity
              FROM trades WHERE order_state = 'PARTIAL'
             ORDER BY updated_at DESC LIMIT 20
            """
        )
    except Exception:
        return []
    return [
        {
            "id": r["agent_trade_id"],
            "orderId": r["broker_order_id"] or "",
            "symbol": r["symbol"],
            "totalQuantity": int(r["quantity"] or 0),
            "filledQuantity": 0,
            "pendingQuantity": int(r["quantity"] or 0),
            "fillPercent": 0,
            "elapsedMs": 0,
            "timeoutMs": 30000,
            "status": "WAITING",
        }
        for r in rows
    ]


@app.get("/api/closed_positions")
async def get_closed_positions(mode: str = "all") -> list[Any]:
    """Trades closed TODAY (live + paper) for the Order Monitor Closed tab. The tab
    reflects process, not mode: a closed position is a closed position whether it
    filled at the broker (`trades`) or in the paper ledger (`paper_trades`); each row
    carries `paper` so the UI can badge/filter it. Detailed paper-only fields (SL,
    exit_reason, regime, etc.) still live on the dedicated Paper Trades tab. Scoped to
    today's exit (IST), mirroring the today-only entry filter open positions use.

    `mode`: "all" (default) | "paper" | "live" — skips querying the table that's
    filtered out entirely rather than fetching then discarding."""
    if _db_pool is None:
        return []
    out: list[dict[str, Any]] = []
    for table, is_paper in (("trades", False), ("paper_trades", True)):
        if mode == "live" and is_paper:
            continue
        if mode == "paper" and not is_paper:
            continue

        def _closed_sql(col: str, table: str = table) -> str:
            return f"""
                SELECT symbol, direction, quantity, fill_price, exit_price,
                       pnl_realized, strategy_name, entry_timestamp, exit_timestamp,
                       partial_qty_booked, {col}
                  FROM {table}
                 WHERE order_state = 'CLOSED'
                   AND (exit_timestamp AT TIME ZONE 'Asia/Kolkata')::date
                     = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                 ORDER BY exit_timestamp DESC NULLS LAST LIMIT 50
                """  # noqa: S608

        try:
            if is_paper:  # paper rows never carry a broker: no need to touch the column
                rows = await _db_pool.fetch(_closed_sql("NULL::text AS exec_broker"))
            else:
                rows = await _fetch_exec_broker_aware("trades", _closed_sql)
        except Exception:
            rows = []
        for r in rows:
            out.append(
                {
                    "symbol": r["symbol"],
                    "direction": r["direction"],
                    # Whole trade: pnl_realized includes what partials banked, so show the
                    # ORIGINAL size (remaining + sold-in-partials), not just the last leg.
                    "quantity": int(r["quantity"] or 0) + int(r.get("partial_qty_booked") or 0),
                    "entryPrice": float(r["fill_price"] or 0),
                    "exitPrice": float(r["exit_price"] or 0),
                    "pnl": float(r["pnl_realized"] or 0),
                    "strategy": r["strategy_name"] or "",
                    "entryAt": str(r["entry_timestamp"]) if r["entry_timestamp"] else "",
                    "closedAt": str(r["exit_timestamp"]) if r["exit_timestamp"] else "",
                    "paper": is_paper,
                    "exec_broker": _exec_broker_label(r, is_paper),
                }
            )
    out.sort(key=lambda x: x["closedAt"], reverse=True)
    return out[:50]


@app.get("/api/executions")
async def get_executions(
    trade_id: str | None = None,
    event: str | None = None,
    instrument: str | None = None,
    mode: str = "all",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """The append-only execution ledger (entry fills, partial exits, final exits, stop/target moves,
    options adjustments, manual edits) for every instrument. `trade_id` returns one trade's full
    history in order; otherwise the newest events first. `mode` = all | paper | live."""
    if _db_pool is None:
        return []
    if mode not in ("all", "paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be all, paper or live")
    from src.db.repositories.trade_executions import TradeExecutionsRepository

    repo = TradeExecutionsRepository(_db_pool)
    if trade_id:
        rows = await repo.for_trade(trade_id)
    else:
        rows = await repo.recent(
            limit, event, instrument, None if mode == "all" else mode == "paper"
        )
    return [
        {k: (str(v) if k == "occurred_at" or hasattr(v, "quantize") else v) for k, v in r.items()}
        for r in rows
    ]


@app.get("/api/paper_trades")
async def get_paper_trades() -> list[Any]:
    """All paper trades (open + closed) from the last 30 days for the Paper Trades
    tab, unified across every segment — intraday, swing AND options — in one
    list. Each row carries `segment` ("INTRADAY" | "SWING" | "OPTIONS") so the
    tab can filter/group. DB truth: `paper_trades` (intraday/swing equity) plus
    `options_positions` where is_paper (options) — neither ever reaches the
    broker. Options rows reuse the same row shape for a consistent table, but
    several equity-only fields (sl/target/regime/llm*) don't apply to a
    multi-leg options structure and are left blank rather than guessed."""
    if _db_pool is None:
        return []
    try:
        rows = await _db_pool.fetch(
            """
            SELECT symbol, direction, quantity, partial_qty_booked, fill_price, exit_price,
                   pnl_realized, partial_pnl_booked, strategy_name, order_state, exit_reason,
                   initial_sl, current_sl, initial_target, current_target,
                   regime_at_entry, setup_quality_score,
                   llm_confidence, llm_flag, llm_reason, product,
                   entry_timestamp, exit_timestamp
              FROM paper_trades
             WHERE entry_timestamp >= NOW() - INTERVAL '30 days'
             ORDER BY entry_timestamp DESC
             LIMIT 500
            """
        )
    except Exception:
        return []

    # Live LTP for EVERY row, open or closed — the current market price of a
    # symbol is fetchable regardless of trade state, and knowing where a closed
    # trade's symbol is trading NOW (vs. where it exited) is useful context, not
    # just a live-position concern. Was previously gated to open trades only,
    # which meant every closed row always showed '—' for LTP even though there
    # was no technical reason it couldn't be fetched.
    _all_syms = {r["symbol"] for r in rows if r["symbol"]}
    _ltp: dict[str, Any] = {}
    if _all_syms:
        try:
            # to_thread — same event-loop-blocking reason as /api/swing_positions.
            _ltp = await _access().ltp([f"NSE:{s}" for s in _all_syms])
        except Exception:
            _ltp = {}

    _CLOSED_STATES = ("CLOSED", "REJECTED", "CANCELLED", "TIMED_OUT")

    def _ltp_value(r: Any) -> float | None:
        val = (_ltp.get(f"NSE:{r['symbol']}") or {}).get("last_price")
        if val is not None:
            return float(val)
        # Kite fetch failed/unavailable — fall back to the trade's own last known
        # price (exit price if closed, fill price if open) rather than blank.
        fallback = r["exit_price"] or r["fill_price"]
        return float(fallback) if fallback else None

    def _unrealized(r: Any) -> float | None:
        if (r["order_state"] or "") in _CLOSED_STATES or not r["fill_price"]:
            return None
        avg = float(r["fill_price"] or 0)
        last = _ltp_value(r) or avg
        qty = int(r["quantity"] or 0)
        is_long = str(r["direction"]).lower() == "long"
        return round((last - avg) * qty if is_long else (avg - last) * qty, 2)

    equity_out: list[dict[str, Any]] = [
        {
            "symbol": r["symbol"],
            "direction": r["direction"],
            # CLOSED rows: pnl_realized is the whole trade, so show the original size too.
            "quantity": int(r["quantity"] or 0)
            + (
                int(r.get("partial_qty_booked") or 0) if (r["order_state"] or "") == "CLOSED" else 0
            ),
            "product": r["product"] or "",
            "segment": "SWING" if (r["product"] or "").upper() == "CNC" else "INTRADAY",
            "entryPrice": float(r["fill_price"] or 0),
            "exitPrice": float(r["exit_price"] or 0),
            "ltp": _ltp_value(r),
            "pnl": float(r["pnl_realized"] or 0),
            "partialPnlBooked": float(r["partial_pnl_booked"] or 0),
            "unrealizedPnl": _unrealized(r),
            "strategy": r["strategy_name"] or "",
            "state": r["order_state"] or "",
            "exitReason": r["exit_reason"] or "",
            # plan (initial_*) kept distinct from the trailed (current_*) values so the
            # dashboard can show the original R:R even after the stop ratchets / on a
            # force-close. "sl"/"target" stay the live (trailed) value for back-compat.
            "sl": float(r["current_sl"] if r["current_sl"] is not None else (r["initial_sl"] or 0)),
            "target": float(
                r["current_target"]
                if r["current_target"] is not None
                else (r["initial_target"] or 0)
            ),
            "initialSl": float(r["initial_sl"]) if r["initial_sl"] is not None else None,
            "initialTarget": float(r["initial_target"])
            if r["initial_target"] is not None
            else None,
            "entryAt": str(r["entry_timestamp"]) if r["entry_timestamp"] else "",
            "closedAt": str(r["exit_timestamp"]) if r["exit_timestamp"] else "",
            "regime": r["regime_at_entry"] or "",
            "setupScore": float(r["setup_quality_score"])
            if r["setup_quality_score"] is not None
            else None,
            "llmConfidence": float(r["llm_confidence"])
            if r["llm_confidence"] is not None
            else None,
            "llmFlag": r["llm_flag"] or "",
            "llmReason": r["llm_reason"] or "",
        }
        for r in rows
    ]

    options_out: list[dict[str, Any]] = []
    try:
        opt_rows = await _db_pool.fetch(
            """
            SELECT position_id, underlying, strategy_type, status, net_credit,
                   realized_pnl, exit_reason, opened_at, closed_at, legs
              FROM options_positions
             WHERE is_paper AND opened_at >= NOW() - INTERVAL '30 days'
             ORDER BY opened_at DESC
             LIMIT 200
            """
        )
        # Live P&L for OPEN structures: same LTP + BUY/SELL sign convention as
        # /api/options_positions and SellerDesk._unrealized_pnl.
        _opt_syms = {
            leg["tradingsymbol"]
            for r in opt_rows
            if r["status"] == "OPEN"
            for leg in _flat_option_legs(r["legs"])
            if leg.get("tradingsymbol")
        }
        _opt_ltp: dict[str, Any] = {}
        if _opt_syms:
            try:
                _opt_ltp = await _access().ltp(list(_opt_syms))
            except Exception:
                _opt_ltp = {}

        def _opt_unrealized(r: Any) -> float | None:
            if r["status"] != "OPEN":
                return None
            total = 0.0
            for leg in _flat_option_legs(r["legs"]):
                tick = _opt_ltp.get(leg.get("tradingsymbol"))
                if not tick:
                    return None  # a missing quote must not read as a flat +0
                sign = 1.0 if leg.get("direction") == "BUY" else -1.0
                qty = int(leg.get("lots") or 0) * int(leg.get("lot_size") or 0)
                total += sign * (float(tick["last_price"]) - float(leg.get("premium") or 0)) * qty
            return round(total, 2)

        def _opt_leg_rows(r: Any) -> tuple[list[dict[str, Any]], float]:
            """Every leg of the structure -- still-open ones (live LTP/P&L) and the ones already stopped
            out (exit price/P&L/reason) -- plus the booked P&L of the closed ones."""
            rows_out: list[dict[str, Any]] = []
            is_open = r["status"] == "OPEN"
            for leg in _flat_option_legs(r["legs"]):
                qty = int(leg.get("lots") or 0) * int(leg.get("lot_size") or 0)
                premium = float(leg.get("premium") or 0)
                sign = 1.0 if leg.get("direction") == "BUY" else -1.0
                exit_px = leg.get("exit_premium")
                tick = _opt_ltp.get(leg.get("tradingsymbol")) if is_open else None
                price = (
                    float(tick["last_price"])
                    if tick
                    else (float(exit_px) if exit_px is not None else None)
                )
                rows_out.append(
                    {
                        "contract": leg.get("tradingsymbol", ""),
                        "direction": leg.get("direction", ""),
                        "quantity": qty,
                        "entry": premium,
                        "price": price,
                        "pnl": (sign * (price - premium) * qty) if price is not None else None,
                        "state": "OPEN" if is_open else "CLOSED",
                        "exitReason": (r["exit_reason"] or "") if not is_open else "",
                        "exitAt": "",
                    }
                )
            closed_rows, closed_total = _closed_leg_rows(r["legs"])
            for c in closed_rows:
                rows_out.append(
                    {
                        "contract": c["tradingsymbol"],
                        "direction": c["direction"],
                        "quantity": int(c["lots"]) * int(c["lotSize"]),
                        "entry": c["premium"],
                        "price": c["exitPremium"],
                        "pnl": c["pnl"],
                        "state": "CLOSED",
                        "exitReason": c["exitReason"],
                        "exitAt": c["exitAt"],
                    }
                )
            return rows_out, closed_total

        def _opt_qty(r: Any) -> int:
            legs = _flat_option_legs(r["legs"])
            return max(
                (int(g.get("lots") or 0) * int(g.get("lot_size") or 0) for g in legs), default=0
            )

        for r in opt_rows:
            _leg_rows, _closed_total = _opt_leg_rows(r)
            options_out.append(
                {
                    "symbol": f"{r['underlying']} {r['strategy_type']}",
                    "direction": "",
                    "quantity": _opt_qty(r),
                    "product": "OPTIONS",
                    "segment": "OPTIONS",
                    "entryPrice": float(r["net_credit"] or 0),
                    "exitPrice": 0.0,
                    "ltp": None,
                    "pnl": float(r["realized_pnl"] or 0),
                    "partialPnlBooked": _closed_total if r["status"] == "OPEN" else 0.0,
                    "unrealizedPnl": _opt_unrealized(r),
                    "legs": _leg_rows,
                    "strategy": r["strategy_type"] or "",
                    "state": r["status"] or "",
                    "exitReason": r["exit_reason"] or "",
                    "sl": 0.0,
                    "target": 0.0,
                    "initialSl": None,
                    "initialTarget": None,
                    "entryAt": str(r["opened_at"]) if r["opened_at"] else "",
                    "closedAt": str(r["closed_at"]) if r["closed_at"] else "",
                    "regime": "",
                    "setupScore": None,
                    "llmConfidence": None,
                    "llmFlag": "",
                    "llmReason": "",
                }
            )
    except Exception as exc:
        _log.warning("paper_trades_options_fetch_failed err=%s", str(exc)[:160])

    return sorted(equity_out + options_out, key=lambda x: x["entryAt"], reverse=True)


def _closed_option_legs(raw_legs: Any) -> list[dict[str, Any]]:
    """Legs already closed one at a time while their position stays OPEN (options_intraday_short_premium's
    per-leg stops), persisted by OptionsRepository.record_leg_exit under legs["closed"] with their
    exit_premium / exit_at / reason. _flat_option_legs only returns the still-open ones."""
    try:
        legs = json.loads(raw_legs) if isinstance(raw_legs, str) else (raw_legs or {})
    except (TypeError, ValueError):
        return []
    return [c for c in legs.get("closed", []) if c.get("exit_premium") is not None]


def _closed_leg_rows(raw_legs: Any) -> tuple[list[dict[str, Any]], float]:
    """(per-leg rows, their realized P&L in rupees) for the legs closed before the position itself."""
    rows: list[dict[str, Any]] = []
    total = 0.0
    for c in _closed_option_legs(raw_legs):
        qty = int(c.get("lots") or 0) * int(c.get("lot_size") or 0)
        premium = float(c.get("premium") or 0)
        exit_px = float(c["exit_premium"])
        sign = 1.0 if c.get("direction") == "BUY" else -1.0
        pnl = sign * (exit_px - premium) * qty
        total += pnl
        rows.append(
            {
                "tradingsymbol": c.get("tradingsymbol", ""),
                "instrumentType": c.get("instrument_type", ""),
                "strike": float(c.get("strike") or 0),
                "direction": c.get("direction", ""),
                "lots": int(c.get("lots") or 0),
                "lotSize": int(c.get("lot_size") or 0),
                "premium": premium,
                "ltp": None,
                "exitPremium": exit_px,
                "pnl": pnl,
                "state": "CLOSED",
                "exitReason": c.get("reason", ""),
                "exitAt": c.get("exit_at", ""),
            }
        )
    return rows, total


def _flat_option_legs(raw_legs: Any) -> list[dict[str, Any]]:
    """`legs` JSONB comes back from asyncpg as a str, same as everywhere else
    this column is read (see SellerDesk.restore_positions) -- not auto-decoded."""
    try:
        legs = json.loads(raw_legs) if isinstance(raw_legs, str) else (raw_legs or {})
    except (TypeError, ValueError):
        return []
    return list(legs.get("protection", [])) + list(legs.get("income", []))


def _option_product_label(raw_legs: Any) -> str:
    """Kite product the structure was placed with: "MIS" or "NRML". Legs saved before they
    carried a product were all NRML."""
    try:
        legs = json.loads(raw_legs) if isinstance(raw_legs, str) else (raw_legs or {})
    except (TypeError, ValueError):
        return "NRML"
    every = [*legs.get("protection", []), *legs.get("income", []), *legs.get("closed", [])]
    return "MIS" if any(leg.get("product") == "INTRADAY" for leg in every) else "NRML"


@app.get("/api/options_positions")
async def get_options_positions(closed: str = "all") -> list[dict[str, Any]]:
    """Every options structure (open + last 30 days closed), paper AND live, for
    the Options Trading tab -- replaces the old bare Option Chain strike-ladder
    viewer with the actual position/strategy view: legs, P&L, target. DB truth:
    options_positions (migration 051, is_paper added by 061).

    `closed`: "all" (default) | "today" -- "today" keeps OPEN rows plus rows
    closed today (IST) only; older closes belong to the Reports tab.

    Per-leg P&L for OPEN rows uses the same live-LTP + BUY/SELL sign convention
    as SellerDesk._unrealized_pnl (seller_desk.py) -- not re-derived differently
    here. No `target` field exists anywhere in this engine (unlike equity's SL/
    target) -- max_profit/max_loss are the real DB-backed numbers, and
    pctOfMaxProfit (current P&L / max_profit) is the closest analog to equity's
    "% to Target"."""
    if _db_pool is None:
        return []
    today_only = (
        "AND (status = 'OPEN' OR (closed_at AT TIME ZONE 'Asia/Kolkata')::date"
        " = (NOW() AT TIME ZONE 'Asia/Kolkata')::date)"
        if closed == "today"
        else ""
    )
    try:
        rows = await _fetch_exec_broker_aware(
            "options_positions",
            lambda col: f"""
            SELECT position_id, underlying, strategy_type, status, legs, expiry,
                   dte_at_entry, lot_size_at_entry, net_credit, max_loss, max_profit,
                   entry_margin, entry_delta, entry_gamma, entry_theta, entry_vega,
                   realized_pnl, exit_reason, opened_at, closed_at, is_paper, {col}
              FROM options_positions
             WHERE opened_at >= NOW() - INTERVAL '30 days'
               {today_only}
             ORDER BY opened_at DESC
             LIMIT 200
            """,  # noqa: S608
        )
    except Exception:
        return []

    from src.options.nfo_chain import index_spot_quote_symbol

    open_rows = [r for r in rows if r["status"] == "OPEN"]
    all_syms = {
        leg["tradingsymbol"]
        for r in open_rows
        for leg in _flat_option_legs(r["legs"])
        if leg.get("tradingsymbol")
    }
    spot_syms = {index_spot_quote_symbol(str(r["underlying"]).split(":")[-1]) for r in open_rows}
    all_syms |= spot_syms
    ltp: dict[str, Any] = {}
    if all_syms:
        try:
            # tradingsymbol already carries its exchange prefix ("NFO:...") --
            # unlike equity's bare symbol, no prefixing needed here.
            ltp = await _access().ltp(list(all_syms))
        except Exception:
            ltp = {}

    from zoneinfo import ZoneInfo as _ZI

    from src.options.position_analytics import (
        compute_breakevens,
        compute_exit_levels,
        compute_pop,
    )
    from src.options.seller_desk import _registry_strategy_for

    today: date = datetime.now(_ZI("Asia/Kolkata")).date()

    open_ids = [r["position_id"] for r in open_rows]
    baselines: dict[str, float] = {}
    if open_ids and _db_pool is not None:
        try:
            baseline_rows = await _db_pool.fetch(
                """
                SELECT position_id, unrealized_pnl FROM options_pnl_daily_snapshot
                WHERE snapshot_date = $1 AND position_id = ANY($2::text[])
                """,
                today,
                open_ids,
            )
            baselines = {
                str(br["position_id"]): float(br["unrealized_pnl"]) for br in baseline_rows
            }
        except Exception:
            baselines = {}

    out: list[dict[str, Any]] = []
    for r in rows:
        is_open = r["status"] == "OPEN"
        legs = _flat_option_legs(r["legs"])
        leg_out: list[dict[str, Any]] = []
        current_pnl = 0.0
        pnl_known = bool(legs)
        for leg in legs:
            premium = float(leg.get("premium") or 0)
            qty = int(leg.get("lots") or 0) * int(leg.get("lot_size") or 0)
            current: float | None = None
            exit_px = leg.get(
                "exit_premium"
            )  # only set on closes after per-leg exits were persisted
            if is_open:
                tick = ltp.get(leg.get("tradingsymbol"))
                current = float(tick["last_price"]) if tick else None
            elif exit_px is not None:
                current = float(exit_px)
            leg_pnl: float | None = None
            if current is not None:
                # BUY (long) leg gains as price rises; SELL (short) leg gains
                # as price falls -- mirrors SellerDesk._unrealized_pnl exactly.
                sign = 1.0 if leg.get("direction") == "BUY" else -1.0
                leg_pnl = sign * (current - premium) * qty
                current_pnl += leg_pnl
            elif is_open:
                pnl_known = False
            leg_out.append(
                {
                    "tradingsymbol": leg.get("tradingsymbol", ""),
                    "instrumentType": leg.get("instrument_type", ""),
                    "strike": float(leg.get("strike") or 0),
                    "direction": leg.get("direction", ""),
                    "lots": int(leg.get("lots") or 0),
                    "premium": premium,
                    "ltp": current if is_open else None,
                    "exitPremium": float(exit_px) if exit_px is not None else None,
                    "pnl": leg_pnl,
                    "state": "OPEN" if is_open else "CLOSED",
                }
            )
        # Legs stopped out earlier while the position stays open: shown (state CLOSED) and their
        # realized P&L counted as BOOKED, so Total = booked + live and nothing disappears.
        closed_leg_rows, closed_legs_pnl = _closed_leg_rows(r["legs"])
        leg_out.extend(closed_leg_rows)

        expiry: date | None = r["expiry"]
        realized = float(r["realized_pnl"]) if r["realized_pnl"] is not None else None
        pnl = realized if not is_open else ((current_pnl + closed_legs_pnl) if pnl_known else None)
        max_profit = float(r["max_profit"] or 0)

        today_pnl: float | None = None
        if is_open and pnl is not None:
            baseline = baselines.get(r["position_id"])
            # No baseline for this position today -> it opened intraday today, so
            # its whole P&L so far IS today's move (see save_daily_pnl_snapshots).
            today_pnl = pnl - baseline if baseline is not None else pnl

        # Booked = realized so far (0 for a normal open position; non-zero only
        # after a partial exit/roll realizes some of it while the structure
        # stays OPEN). Unbooked = live P&L on what's still open (0 once closed
        # -- nothing is left to mark live).
        booked_pnl = realized if realized is not None else closed_legs_pnl
        unbooked_pnl = (current_pnl if pnl_known else None) if is_open else 0.0

        # A position that has already closed a leg: the headline numbers follow what is LEFT --
        # max profit = booked + the open legs' remaining premium, max loss = the open legs' stop
        # less what is already booked (never below 0) -- instead of the original whole-structure figures.
        max_loss = float(r["max_loss"] or 0)
        exit_levels: dict[str, float | None] = {}
        if is_open:
            if closed_leg_rows:
                open_credit = sum(
                    (1.0 if lg.get("direction") != "BUY" else -1.0)
                    * float(lg.get("premium") or 0)
                    * int(lg.get("lots") or 0)
                    * int(lg.get("lot_size") or 0)
                    for lg in legs
                )
                max_profit = closed_legs_pnl + open_credit
            exit_levels = compute_exit_levels(
                r["strategy_type"],
                legs,
                float(r["net_credit"] or 0),
                max_profit,
                float(r["entry_margin"]) if r["entry_margin"] is not None else None,
            )
            if closed_leg_rows and exit_levels.get("stopAmount") is not None:
                max_loss = max(0.0, float(exit_levels["stopAmount"] or 0) - closed_legs_pnl)

        spot_sym = index_spot_quote_symbol(str(r["underlying"]).split(":")[-1])
        spot_tick = ltp.get(spot_sym)
        spot = float(spot_tick["last_price"]) if spot_tick else None

        out.append(
            {
                "breakeven": compute_breakevens(legs, float(r["net_credit"] or 0), spot),
                "pop": compute_pop(r["strategy_type"], legs),
                **exit_levels,
                "todayPnl": today_pnl,
                "bookedPnl": booked_pnl,
                "unbookedPnl": unbooked_pnl,
                "spot": spot,
                "positionId": r["position_id"],
                "underlying": r["underlying"],
                "strategy": r["strategy_type"],
                # The registry strategy (its own paper/live switch) the structure trades under.
                "registryStrategy": _registry_strategy_for(str(r["strategy_type"])),
                "status": r["status"],
                "legs": leg_out,
                "expiry": str(expiry) if expiry else "",
                "dte": (expiry - today).days if expiry else None,
                "netCredit": float(r["net_credit"]) if r["net_credit"] is not None else 0.0,
                "maxLoss": max_loss,
                "maxProfit": max_profit,
                "entryMargin": float(r["entry_margin"]) if r["entry_margin"] is not None else None,
                "entryGreeks": {
                    "delta": float(r["entry_delta"]) if r["entry_delta"] is not None else None,
                    "gamma": float(r["entry_gamma"]) if r["entry_gamma"] is not None else None,
                    "theta": float(r["entry_theta"]) if r["entry_theta"] is not None else None,
                    "vega": float(r["entry_vega"]) if r["entry_vega"] is not None else None,
                },
                "pnl": pnl,
                "pctOfMaxProfit": (pnl / max_profit * 100)
                if (pnl is not None and max_profit)
                else None,
                "exitReason": r["exit_reason"] or "",
                "openedAt": str(r["opened_at"]) if r["opened_at"] else "",
                "closedAt": str(r["closed_at"]) if r["closed_at"] else "",
                "isPaper": bool(r["is_paper"]),
                "product": _option_product_label(r["legs"]),
                "exec_broker": _exec_broker_label(r, bool(r["is_paper"])),
            }
        )
    return out


def _report_leg(lg: dict[str, Any]) -> dict[str, Any]:
    """One closed leg for Reports. exit/pnl are None for closes made before
    per-leg exit prices were persisted -- the total can't be split back."""
    entry = float(lg.get("premium") or 0)
    qty = int(lg.get("lots") or 0) * int(lg.get("lot_size") or 0)
    exit_px = lg.get("exit_premium")
    pnl: float | None = None
    if exit_px is not None:
        sign = 1.0 if lg.get("direction") == "BUY" else -1.0
        pnl = sign * (float(exit_px) - entry) * qty
    return {
        "tradingsymbol": lg.get("tradingsymbol", ""),
        "instrumentType": lg.get("instrument_type", ""),
        "strike": float(lg.get("strike") or 0),
        "direction": lg.get("direction", ""),
        "lots": int(lg.get("lots") or 0),
        "quantity": qty,
        "premium": entry,
        "exitPremium": float(exit_px) if exit_px is not None else None,
        "pnl": pnl,
    }


async def _closed_history_rows(
    mode: str, date_from: str | None, date_to: str | None
) -> list[dict[str, Any]]:
    """Closed trades (equity + options) for ONE mode, exited before today (IST).

    Backs the Reports tab. Today's closes deliberately stay out -- they live in
    Order Monitor+ / Options Trading until the day ends. `mode` is strictly
    "paper" or "live": the two are never blended into one list."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo as _ZI

    if mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")
    if _db_pool is None:
        return []
    is_paper = mode == "paper"
    yesterday = datetime.now(_ZI("Asia/Kolkata")).date() - timedelta(days=1)
    try:
        start = date.fromisoformat(date_from) if date_from else yesterday - timedelta(days=90)
        end = min(date.fromisoformat(date_to), yesterday) if date_to else yesterday
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="dates must be YYYY-MM-DD") from exc

    out: list[dict[str, Any]] = []
    table = "paper_trades" if is_paper else "trades"
    dup_filter = "AND exit_reason IS DISTINCT FROM 'manual_dup_entry_cleanup'" if is_paper else ""
    try:
        rows = await _db_pool.fetch(
            f"""
            SELECT symbol, direction, quantity, partial_qty_booked, fill_price, exit_price, pnl_realized,
                   strategy_name, product, entry_timestamp, exit_timestamp,
                   charges_taxes, charges_breakdown, broker
              FROM {table}
             WHERE order_state = 'CLOSED' AND exit_timestamp IS NOT NULL
               AND (exit_timestamp AT TIME ZONE 'Asia/Kolkata')::date BETWEEN $1 AND $2
               {dup_filter}
             ORDER BY exit_timestamp DESC
             LIMIT 20000
            """,  # noqa: S608
            start,
            end,
        )
    except Exception as exc:
        _log.warning("reports_equity_fetch_failed err=%s", str(exc)[:160])
        rows = []
    for r in rows:
        _qty = int(r["quantity"] or 0) + int(r.get("partial_qty_booked") or 0)
        _c = equity_costs(
            pnl_realized=float(r["pnl_realized"] or 0),
            charges_taxes=float(r["charges_taxes"]) if r["charges_taxes"] is not None else None,
            charges_breakdown=r["charges_breakdown"],
            product=str(r["product"] or ""),
            direction=str(r["direction"] or ""),
            quantity=_qty,
            entry_price=float(r["fill_price"] or 0),
            exit_price=float(r["exit_price"] or 0),
        )
        out.append(
            {
                "segment": "INTRADAY" if str(r["product"] or "").upper() == "MIS" else "SWING",
                "symbol": r["symbol"],
                "side": r["direction"],
                "quantity": _qty,
                "entryPrice": float(r["fill_price"] or 0),
                "exitPrice": float(r["exit_price"] or 0),
                "pnl": _c["net"],
                "gross": _c["gross"],
                "charges": _c["charges"],
                "chargesTotal": _c["chargesTotal"],
                "estimated": _c["estimated"],
                "broker": r["broker"] or "",
                "strategy": r["strategy_name"] or "",
                "exitReason": "",
                "entryAt": str(r["entry_timestamp"]) if r["entry_timestamp"] else "",
                "closedAt": str(r["exit_timestamp"]),
                "legs": [],
            }
        )
    try:
        opt_rows = await _db_pool.fetch(
            """
            SELECT underlying, strategy_type, legs, expiry, net_credit, realized_pnl,
                   exit_reason, opened_at, closed_at, charges_breakdown, broker
              FROM options_positions
             WHERE status <> 'OPEN' AND closed_at IS NOT NULL AND is_paper = $3
               AND (closed_at AT TIME ZONE 'Asia/Kolkata')::date BETWEEN $1 AND $2
             ORDER BY closed_at DESC
             LIMIT 5000
            """,
            start,
            end,
            is_paper,
        )
    except Exception as exc:
        _log.warning("reports_options_fetch_failed err=%s", str(exc)[:160])
        opt_rows = []
    for r in opt_rows:
        _legs = [_report_leg(lg) for lg in _flat_option_legs(r["legs"])]
        _c = options_costs(
            realized_pnl=float(r["realized_pnl"] or 0),
            charges_breakdown=r["charges_breakdown"],
            legs=_legs,
        )
        out.append(
            {
                "segment": "OPTIONS",
                "expiry": str(r["expiry"]) if r["expiry"] else "",
                "symbol": str(r["underlying"] or "").split(":")[-1],
                "side": "",
                "quantity": 0,
                "entryPrice": float(r["net_credit"] or 0),
                "exitPrice": 0.0,
                "pnl": _c["net"],
                "gross": _c["gross"],
                "charges": _c["charges"],
                "chargesTotal": _c["chargesTotal"],
                "estimated": _c["estimated"],
                "broker": r["broker"] or "",
                "strategy": r["strategy_type"] or "",
                "exitReason": r["exit_reason"] or "",
                "entryAt": str(r["opened_at"]) if r["opened_at"] else "",
                "closedAt": str(r["closed_at"]),
                "legs": _legs,
            }
        )
    out.sort(key=lambda x: x["closedAt"], reverse=True)
    return out


@app.get("/api/reports/tradebook")
async def get_reports_tradebook(
    mode: str = "live", date_from: str | None = None, date_to: str | None = None
) -> list[dict[str, Any]]:
    """Reports > Tradebook / Options: closed trades before today, one mode only."""
    return await _closed_history_rows(mode, date_from, date_to)


def _rollup(rows: list[dict[str, Any]], key: Any) -> dict[str, dict[str, Any]]:
    """Group rows by key(row): trades, wins (net > 0), gross, charges, net (+ estimated count)."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        b = out.setdefault(
            key(r),
            {
                "pnl": 0.0,
                "gross": 0.0,
                "charges": 0.0,
                "trades": 0,
                "wins": 0,
                "estimatedTrades": 0,
            },
        )
        b["pnl"] += r["pnl"]
        b["gross"] += r["gross"]
        b["charges"] += r["chargesTotal"]
        b["trades"] += 1
        b["wins"] += 1 if r["pnl"] > 0 else 0
        b["estimatedTrades"] += 1 if r["estimated"] else 0
    return out


@app.get("/api/reports/pnl")
async def get_reports_pnl(
    mode: str = "live", date_from: str | None = None, date_to: str | None = None
) -> dict[str, Any]:
    """Reports > P&L: gross, itemised charges and net (`pnl`) rolled up by day, symbol and
    strategy, plus totals. Rows without a recorded charges breakdown are estimated
    (`estimatedTrades` counts them)."""
    rows = await _closed_history_rows(mode, date_from, date_to)
    by_day = _rollup(rows, lambda r: r["closedAt"][:10])
    by_symbol = _rollup(rows, lambda r: r["symbol"])
    by_strategy = _rollup(rows, lambda r: r["strategy"] or "(none)")
    by_charge: dict[str, float] = {}
    for r in rows:
        for k, v in r["charges"].items():
            by_charge[k] = by_charge.get(k, 0.0) + v
    gross = sum(r["gross"] for r in rows)
    charges = sum(r["chargesTotal"] for r in rows)
    return {
        "totalPnl": sum(r["pnl"] for r in rows),
        "grossPnl": gross,
        "totalCharges": charges,
        "chargesPctOfGross": (charges / abs(gross) * 100) if gross else None,
        "chargesByType": by_charge,
        "estimatedTrades": sum(1 for r in rows if r["estimated"]),
        "trades": len(rows),
        "wins": sum(1 for r in rows if r["pnl"] > 0),
        "byDay": [{"date": k, **v} for k, v in sorted(by_day.items(), reverse=True)],
        "bySymbol": [
            {"symbol": k, **v} for k, v in sorted(by_symbol.items(), key=lambda kv: kv[1]["pnl"])
        ],
        "byStrategy": [
            {"strategy": k, **v}
            for k, v in sorted(by_strategy.items(), key=lambda kv: kv[1]["pnl"])
        ],
    }


@app.get("/api/pnl_breakdown")
async def get_pnl_breakdown(mode: str = "live") -> dict[str, Any]:
    """Closed-trade P&L split into MIS / CNC / OPTIONS / ALL for the Trade & P&L
    tab (spec ask: bifurcate MIS vs CNC vs options, each with cumulative total
    + weekly series). Bypasses the in-memory shared['closed_trades'] list used
    by the legacy weekly_pnl/cost_erosion loops — see _fetch_closed_trades_from_db
    in orchestrator.py for why that list is unreliable across restarts.

    `mode`: "live" (default, preserves the original contract) | "paper" | "all".
    Live and paper are NEVER summed into one figure — that's what caused a real
    bug (found 2026-09-02: a paper swing loss made the real CNC bucket look
    negative when the user's actual live swing trades were fine, because
    paper_trades was UNIONed into the same rows as `trades`). mode="all" returns
    both bucket sets side by side under separate "live"/"paper" keys instead, so
    the two are always visually distinct, never blended.

    Equity uses `trades` (live) or `paper_trades` (paper); unlike `trades`,
    `paper_trades` HAS an exit_reason column, but this query never selects it —
    was a regression once already (2026-09-02) when it briefly did and every
    call failed with "column exit_reason does not exist" on the `trades` side.

    Options uses options_positions.is_paper (added by migration 061) to split
    live vs paper — the previous version of this endpoint queried
    options_positions with no is_paper filter at all, so a paper options trade's
    P&L silently leaked into what was presented as real-money P&L. Fixed here."""
    from src.monitoring.pnl_breakdown import TradeRecord, bucket_pnl

    empty = {"net_pnl": 0.0, "trade_count": 0, "weekly": {}}
    empty_bucket_set = {"MIS": empty, "CNC": empty, "OPTIONS": dict(empty), "ALL": dict(empty)}

    async def _bucket_for(is_paper: bool) -> dict[str, Any]:
        if _db_pool is None:
            return dict(empty_bucket_set)
        records: list[TradeRecord] = []
        equity_table = "paper_trades" if is_paper else "trades"
        try:
            equity_rows = await _db_pool.fetch(
                f"""
                SELECT product, pnl_realized, exit_timestamp FROM {equity_table}
                 WHERE order_state = 'CLOSED' AND exit_timestamp IS NOT NULL
                """  # noqa: S608 — equity_table is one of two fixed literals above
            )
            for r in equity_rows:
                records.append(
                    TradeRecord(
                        product=r["product"] or "CNC",
                        net_pnl=float(r["pnl_realized"] or 0.0),
                        exit_ts=r["exit_timestamp"],
                    )
                )
        except Exception as exc:
            _log.warning(
                "pnl_breakdown_equity_fetch_failed is_paper=%s err=%s", is_paper, str(exc)[:160]
            )

        try:
            options_rows = await _db_pool.fetch(
                "SELECT realized_pnl, closed_at FROM options_positions "
                "WHERE status = 'CLOSED' AND is_paper = $1",
                is_paper,
            )
            for r in options_rows:
                if r["closed_at"] is None:
                    continue
                records.append(
                    TradeRecord(
                        product="OPTIONS",
                        net_pnl=float(r["realized_pnl"] or 0.0),
                        exit_ts=r["closed_at"],
                    )
                )
        except Exception as exc:
            _log.warning(
                "pnl_breakdown_options_fetch_failed is_paper=%s err=%s", is_paper, str(exc)[:160]
            )

        return bucket_pnl(records)

    if mode == "paper":
        return await _bucket_for(True)
    if mode == "all":
        return {"live": await _bucket_for(False), "paper": await _bucket_for(True)}
    return await _bucket_for(False)


@app.get("/api/learning_summary")
def get_learning_summary() -> dict[str, Any]:
    return _read_cache(
        "learning_summary.json",
        36 * 3600,
        {
            "summaries": {},
            "patterns": [],
            "trade_count": 0,
            "computed_at": "",
        },
    )


@app.get("/api/backtest_summary")
def get_backtest_summary() -> dict[str, Any]:
    return _read_log("backtest_summary.json", {})


@app.get("/api/backtest_trades")
def get_backtest_trades() -> dict[str, Any]:
    """Per-trade detail (entry/exit/reason/R per symbol) keyed by strategy name.
    Read-only audit surface — the agent executes these, not a human."""
    return _read_log("backtest_trades.json", {})


@app.get("/api/discovery_scan")
def get_discovery_scan() -> dict[str, Any]:
    return _read_log(
        "discovery_scan.json",
        {
            "accepted": [],
            "rejected": [],
            "unchanged": 0,
            "scanned_at": "",
        },
    )


@app.get("/api/evolution_report")
def get_evolution_report() -> dict[str, Any]:
    return _read_log("evolution_report.json", {"evolutions": [], "run_at": ""})


# ---------------------------------------------------------------------------
# Logs (new Logs tab) — filenames are always resolved against the logger's own
# known-component list, never taken as a raw path from the caller, so nothing
# outside logs/ (or a name the logger never writes) can ever be read.
# ---------------------------------------------------------------------------


def _known_log_filenames() -> set[str]:
    from src.infrastructure.logger import _COMPONENTS

    names = set(_COMPONENTS.values())
    # trading-YYYY-MM-DD.log (master, all components) and audit-YYYY-MM-DD.log are
    # a new file every day — allow any that actually exist on disk today rather
    # than hardcoding a filename pattern here too. trading.log (pre-2026-09-23,
    # single size-rotated file) is kept in the allowlist so an existing file from
    # before the per-day switch stays readable until it ages out.
    names.add("trading.log")
    logs_dir = _BASE / "logs"
    if logs_dir.exists():
        names.update(p.name for p in logs_dir.glob("audit-*.log"))
        names.update(p.name for p in logs_dir.glob("trading-????-??-??.log"))
    return names


@app.get("/api/logs/files")
def list_log_files() -> list[dict[str, Any]]:
    logs_dir = _BASE / "logs"
    out: list[dict[str, Any]] = []
    for name in sorted(_known_log_filenames()):
        path = logs_dir / name
        if not path.exists():
            continue
        stat = path.stat()
        out.append(
            {
                "name": name,
                "sizeBytes": stat.st_size,
                "modifiedAt": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            }
        )
    return out


# Logs tab paging (2026-09-24). The endpoint used to clamp `tail` to 2000 and read only the
# last 1MB, and the UI hard-coded 300 lines, so on a 10MB+ log everything older than a few
# minutes was unreachable from the dashboard. Now the whole file is reachable:
#   ?tail=N                 newest N complete lines
#   ?tail=N&before=OFFSET   the N complete lines that END at byte OFFSET (page backwards)
#   ?after=OFFSET           only lines written since OFFSET (follow mode); reset=true when the
#                           file was rotated/truncated and the client must reload
# Offsets are byte positions of line starts, so pages never split or repeat a line, and a
# half-written last line is held back until its newline arrives.
_LOG_PAGE_MAX = 5000
_LOG_CHUNK = 1024 * 1024


def _decode_lines(chunks: list[bytes]) -> list[str]:
    return [c.rstrip(b"\r\n").decode("utf-8", errors="replace") for c in chunks]


def _read_log_before(path: Path, end: int, tail: int) -> tuple[list[str], int, int]:
    """The `tail` complete lines ending at byte `end`. Returns (lines, start_offset, end_offset)
    where end_offset excludes a trailing half-written line (only possible at end-of-file)."""
    pos = end
    data = b""
    while True:
        new_pos = max(0, pos - _LOG_CHUNK)
        with path.open("rb") as f:
            f.seek(new_pos)
            data = f.read(pos - new_pos) + data
        pos = new_pos
        if pos == 0 or data.count(b"\n") > tail:
            break
    start = pos
    if pos > 0:  # the first line of the window may be cut mid-line: drop it
        cut = data.find(b"\n") + 1
        data, start = data[cut:], pos + cut
    if data and not data.endswith(b"\n"):  # half-written trailing line: hold it back
        keep = data.rfind(b"\n") + 1
        end -= len(data) - keep
        data = data[:keep]
    chunks = data.splitlines(keepends=True)
    if len(chunks) > tail:
        start += sum(len(c) for c in chunks[:-tail])
        chunks = chunks[-tail:]
    return _decode_lines(chunks), start, end


def _read_log_after(path: Path, after: int, tail: int) -> tuple[list[str], int]:
    """Up to `tail` complete lines written at/after byte `after`. Returns (lines, end_offset)."""
    with path.open("rb") as f:
        f.seek(after)
        data = f.read(_LOG_CHUNK * 5)
    keep = data.rfind(b"\n") + 1  # complete lines only
    chunks = data[:keep].splitlines(keepends=True)
    if len(chunks) > tail:
        chunks = chunks[:tail]
    return _decode_lines(chunks), after + sum(len(c) for c in chunks)


@app.get("/api/logs/{name}")
def get_log_contents(
    name: str, tail: int = 200, before: int | None = None, after: int | None = None
) -> dict[str, Any]:
    if name not in _known_log_filenames():
        raise HTTPException(status_code=404, detail="unknown log file")
    tail = max(1, min(tail, _LOG_PAGE_MAX))
    path = _BASE / "logs" / name
    if not path.exists():
        return {
            "name": name,
            "lines": [],
            "startOffset": 0,
            "endOffset": 0,
            "fileSize": 0,
            "hasMore": False,
            "reset": False,
        }
    try:
        size = path.stat().st_size
        reset = False
        if after is not None and after <= size:
            lines, end_off = _read_log_after(path, max(0, after), tail)
            return {
                "name": name,
                "lines": lines,
                "startOffset": after,
                "endOffset": end_off,
                "fileSize": size,
                "hasMore": False,
                "reset": False,
            }
        if after is not None:  # offset is past EOF: the file was rotated/truncated
            reset = True
        end = size if before is None or reset else max(0, min(before, size))
        lines, start, end_off = _read_log_before(path, end, tail)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to read log: {exc}") from exc
    return {
        "name": name,
        "lines": lines,
        "startOffset": start,
        "endOffset": end_off,
        "fileSize": size,
        "hasMore": start > 0,
        "reset": reset,
    }


@app.get("/api/logs/{name}/download")
def download_log(name: str) -> FileResponse:
    """The complete log file, unmodified (the dashboard's "Download full file")."""
    if name not in _known_log_filenames():
        raise HTTPException(status_code=404, detail="unknown log file")
    path = _BASE / "logs" / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="log file not found")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=name)


@app.get("/api/earnings_calendar")
async def get_earnings_calendar() -> dict[str, Any]:
    if _db_pool is not None:
        try:
            from src.db.repositories.market_data import MarketDataRepository

            rows = await MarketDataRepository(_db_pool).upcoming_earnings(60)
            if rows:
                return {"fetched_at": "", "events": [dict(r) for r in rows], "source": "db"}
        except Exception:
            pass
    return _read_log("earnings_calendar.json", {"fetched_at": "", "events": []})


# ---------------------------------------------------------------------------
# WebSocket — real-time LTP every 2 s
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def websocket_ltp(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            try:
                symbols: list[str] = ["NSE:NIFTY 50", "NSE:NIFTY BANK"]
                if _kite_configured():
                    # Open Kite positions join the feed (Kite-account specific). to_thread:
                    # this coroutine runs on the shared uvicorn event loop, so a blocking
                    # call here freezes every HTTP request for the round-trip, every 2s.
                    _pos: dict[str, Any] = await asyncio.to_thread(_kite().positions)
                    net: list[dict[str, Any]] = _pos.get("net", [])
                    for p in net[:8]:
                        if p.get("quantity", 0) != 0:
                            symbols.append(f"{p['exchange']}:{p['tradingsymbol']}")
                ltp = await _access().ltp(list(dict.fromkeys(symbols)))
                await ws.send_json({"type": "ltp", "data": ltp})
            except Exception as exc:
                await ws.send_json({"type": "error", "message": str(exc)})
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
