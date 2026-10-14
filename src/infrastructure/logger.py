"""
Single module that owns ALL logging configuration for ashwax.

Every other module:
    from src.infrastructure.logger import get_logger
    log = get_logger("risk_manager")
    log.info("gate_passed", extra={"ctx": {"gate": 1, "symbol": "RELIANCE"}})

Rules:
- All structured fields go under extra={"ctx": {...}} — never top-level extras.
  Top-level extras clobber reserved LogRecord attributes (message, name, etc.).
- setup_logging() is called ONCE per process at orchestrator startup.
- Audit log is a new daily file (audit-YYYY-MM-DD.log), never rotated, never deleted
  by the app. Lifecycle (compress, archive, delete) runs in Windows Task Scheduler.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import logging
import os
import re
import time
from collections.abc import Callable
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TypeVar, cast
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


# ── Config: config/global.json system.log_level is the ONLY level control ────
# .env LOG_LEVEL used to win over this (2026-09-23 and before), so a deployed
# .env silently overrode the deliberate config knob and setting DEBUG in
# global.json did nothing observable unless .env happened to agree. Removed:
# LOG_LEVEL is no longer read from the environment anywhere in this module.
def _global_log_level() -> str | None:
    """Read system.log_level from config/global.json (None if unavailable)."""
    try:
        import json as _json

        p = Path(__file__).resolve().parents[2] / "config" / "global.json"
        level = _json.loads(p.read_text(encoding="utf-8")).get("system", {}).get("log_level")
        return str(level).upper() if level else None
    except Exception:
        return None


LOG_LEVEL: str = (_global_log_level() or "INFO").upper()
LOG_DATE_FORMAT: str = os.getenv("LOG_DATE_FORMAT", "%d-%m-%Y %H:%M:%S.%f")

_IST = ZoneInfo("Asia/Kolkata")

# ── Custom SUCCESS level (25 — between INFO=20 and WARNING=30) ───────────────
SUCCESS: int = 25
logging.addLevelName(SUCCESS, "SUCCESS")


def _success(self: logging.Logger, message: str, *args: object, **kwargs: object) -> None:
    if self.isEnabledFor(SUCCESS):
        self._log(SUCCESS, message, args, **kwargs)  # type: ignore[arg-type]


logging.Logger.success = _success  # type: ignore[attr-defined]

# ── ANSI colours (console only) ───────────────────────────────────────────────
_COLOURS: dict[str, str] = {
    "DEBUG": "\033[90m",  # gray
    "INFO": "\033[97m",  # white
    "SUCCESS": "\033[92m",  # green
    "WARNING": "\033[93m",  # yellow
    "ERROR": "\033[91m",  # red
    "CRITICAL": "\033[41;97m",  # white on red background
}
_RESET = "\033[0m"


# ── Formatters ────────────────────────────────────────────────────────────────


# ── Strategy lead-in ─────────────────────────────────────────────────────────
# Every log line that is about a strategy must START with `strategy=<name> | ` so one
# grep (`strategy=llm_swing_agent`) pulls that strategy's whole story out of the shared
# logs (added 2026-09-24: a G9 line read "... symbol=NSE:CPPLUS strategy=llm_swing_agent ..."
# with the strategy buried mid-line, and lines from the same pass that did not name it at
# all -- "g9_llm_error | symbol=..." -- could not be attributed to anything).
#
# Two sources, applied centrally in the formatters so console, master and per-component
# files all agree: (1) a `strategy=<name>` token already in the message is MOVED to the
# front; (2) otherwise the strategy in the active context (strategy_context /
# @strategy_logged) is prepended. asyncio.to_thread copies the context, so worker-thread
# and SDK logs inherit it. No strategy known -> the line is untouched.
_LOG_STRATEGY: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ashwax_log_strategy", default=None
)
_STRATEGY_TOKEN = re.compile(r"(?:(?<=\s)|^)strategy=([A-Za-z0-9_]+)")


def _lead_with_strategy(msg: str, ctx_strategy: str | None) -> str:
    """Return `msg` starting with `strategy=<name> | ` when a strategy is known.

    Gate-decision records ("GATE | SEGMENT | strategy | SYMBOL | ...", logs/gates.log) are
    left exactly as written: they already carry the strategy as a fixed column, and a second
    copy in front of it broke their agreed format."""
    if msg.startswith("GATE | "):
        return msg
    m = _STRATEGY_TOKEN.search(msg)
    name = m.group(1) if m else ctx_strategy
    if not name:
        return msg
    tag = f"strategy={name}"
    if msg.startswith(tag):
        return msg
    if m:
        msg = (msg[: m.start()] + msg[m.end() :]).replace("  ", " ").strip()
    return f"{tag} | {msg}"


@contextlib.contextmanager
def strategy_context(name: str) -> Any:
    """Attribute every log line emitted inside the block (including worker threads started
    via asyncio.to_thread) to strategy `name`."""
    token = _LOG_STRATEGY.set(name)
    try:
        yield
    finally:
        _LOG_STRATEGY.reset(token)


_F = TypeVar("_F", bound=Callable[..., Any])


def strategy_logged(
    *, fixed: str | None = None, param: str = "strategy_name"
) -> Callable[[_F], _F]:
    """Decorator (sync or async): run the function inside strategy_context. `fixed` names
    the strategy outright; otherwise it is read from the argument called `param`."""

    def deco(fn: _F) -> _F:
        sig = inspect.signature(fn)

        def _name(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
            if fixed is not None:
                return fixed
            try:
                bound = sig.bind_partial(*args, **kwargs).arguments.get(param)
            except TypeError:
                return None
            return str(bound) if bound else None

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                name = _name(args, kwargs)
                if not name:
                    return await fn(*args, **kwargs)
                with strategy_context(name):
                    return await fn(*args, **kwargs)

            return cast(_F, awrapper)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            name = _name(args, kwargs)
            if not name:
                return fn(*args, **kwargs)
            with strategy_context(name):
                return fn(*args, **kwargs)

        return cast(_F, wrapper)

    return deco


class _ISTFormatter(logging.Formatter):
    """Formats timestamps in IST regardless of machine timezone."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        ist_dt = datetime.fromtimestamp(record.created, tz=_IST)
        ts = ist_dt.strftime(datefmt or LOG_DATE_FORMAT)
        # Truncate microseconds → milliseconds (3 digits)
        if "." in ts:
            head, frac = ts.rsplit(".", 1)
            ts = f"{head}.{frac[:3]}"
        return ts


class _ConsoleFormatter(_ISTFormatter):
    """
    Colored human-readable console output:
    29-05-2026 09:15:01.234 | INFO     | risk_manager         | gate_passed | gate=1 symbol=RELIANCE
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record)
        level = record.levelname.ljust(8)
        colour = _COLOURS.get(record.levelname, "")
        module = getattr(record, "script_name", record.name)[:20].ljust(20)
        msg = _lead_with_strategy(record.getMessage(), _LOG_STRATEGY.get())
        ctx: dict[str, object] | None = getattr(record, "ctx", None)
        detail = " | " + " ".join(f"{k}={v}" for k, v in ctx.items()) if ctx else ""
        return f"{ts} | {colour}{level}{_RESET} | {module} | {msg}{detail}"


class _FileFormatter(_ISTFormatter):
    """
    Plain text for file logs — same layout as the console, no colour codes:
    01-06-2026 23:27:15.744 | INFO     | launcher             | waiting_for_api_ready | k=v
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record)
        level = record.levelname.ljust(8)
        script = getattr(record, "script_name", record.name)[:20].ljust(20)
        msg = _lead_with_strategy(record.getMessage(), _LOG_STRATEGY.get())
        ctx: dict[str, object] | None = getattr(record, "ctx", None)
        detail = " | " + " ".join(f"{k}={v}" for k, v in ctx.items()) if ctx else ""
        return f"{ts} | {level} | {script} | {msg}{detail}"


# ── Components ────────────────────────────────────────────────────────────────
# One DEBUG .log file per component. Module loggers use get_logger(__name__) so
# their names are dotted package paths (e.g. "src.learning.attribution"); a handler
# attached to the package logger ("src.learning") captures every submodule beneath
# it via the standard logging hierarchy. Entry-point/label loggers (orchestrator,
# launcher, api, dashboard, websocket) are listed explicitly.
_COMPONENTS: dict[str, str] = {
    # top-level src packages
    "src.api": "api.log",
    "src.brokers": "brokers.log",
    "src.circuit_breaker": "circuit_breaker.log",
    "src.db": "db.log",
    "src.execution": "execution.log",
    "src.infrastructure": "infrastructure.log",
    "src.ingestion": "ingestion.log",
    "src.intelligence": "intelligence.log",
    "src.learning": "learning.log",
    # Flat "llm" hierarchy (llm, llm.market_scanner, llm.supervisor_brain, llm.calibration):
    # trade_evaluator/providers log under get_logger("llm"), which a "src.llm" handler never
    # captured - llm.log sat empty for weeks (found 2026-09-21).
    "llm": "llm.log",
    "src.market_data": "market_data.log",
    "src.monitoring": "monitoring.log",
    "src.news": "news.log",
    "src.notifications": "notifications.log",
    "src.paper_trading": "paper_trading.log",
    "src.reporting": "reporting.log",
    "src.risk": "risk.log",
    "src.screener": "screener.log",
    "src.shared": "shared.log",
    "src.strategies": "strategies.log",
    "src.supervisor": "supervisor.log",
    "src.workers": "workers.log",
    # entry-point / short-label loggers
    "orchestrator": "orchestrator.log",
    "launcher": "launcher.log",
    "dashboard": "dashboard.log",
    # Launcher stdout-mirror labels. These MUST NOT share a file with the child's
    # own logger: the launcher and the child are separate processes, and two
    # processes holding one file open makes the rename in RotatingFileHandler's
    # rollover fail forever on Windows. api.log was previously written by both the
    # launcher ("api") and the uvicorn worker ("src.api") and so never rotated once
    # — it reached 189MB against a 10MB threshold with zero backups.
    "api_stdout": "api_stdout.log",
    "orchestrator_stdout": "orchestrator_stdout.log",
    "websocket": "websocket.log",
    "backtest": "backtest.log",
    # Every swing strategy's per-bar USS scan AND the LLM-originated swing
    # agent's daily reasoning pass (src/strategies/llm_swing/, driven by
    # orchestrator's _llm_swing_scan_loop) share this one logger/file.
    "swing": "swing.log",
    # Same pattern as "swing" above, added 2026-09-23: the intraday 5-min DSL
    # scan loop (_intraday_signal_scan_loop and the functions it calls —
    # _evaluate_llm_intraday_signal, _llm_intraday_signal_tick,
    # _run_llm_intraday_pass, _llm_intraday_agent_loop) AND the LLM intraday
    # agent's own reasoning pass (src/strategies/llm_intraday/) all log here
    # instead of being split across orchestrator.log and strategies.log —
    # previously the only way to see "everything the intraday engine did"
    # meant reading two files and mentally interleaving them by timestamp.
    "intraday": "intraday.log",
    # Per-gate CRM decision trail (src/risk/gate_log.py). Deliberately its own
    # file and deliberately UNSCOPED: every strategy, every segment, every signal
    # that reaches crm.evaluate(), approved or blocked. Before 2026-09-11 nothing
    # logged a per-gate line anywhere -- a block recorded only the FIRST blocking
    # gate and an approved trade recorded no gate detail at all.
    "gates": "gates.log",
    # src/options/*.py modules all log under logging.getLogger("options.<module>")
    # (a flat "options.*" hierarchy, not "src.options.*") — this entry is what
    # actually captures them, same short-label pattern as "swing"/"vcp_swing".
    "options": "options.log",
}

_COMPONENT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
_COMPONENT_BACKUPS = 5
# Master log holds every component's output, so it needs more headroom per day;
# this is a same-day safety net against a runaway, not the normal case.
_MASTER_LOG_MAX_BYTES = 50 * 1024 * 1024  # 50 MB per day
_MASTER_LOG_SAME_DAY_BACKUPS = 2


# ── Setup ─────────────────────────────────────────────────────────────────────

_setup_done = False


def setup_logging(engine_name: str, log_dir: str | Path = "logs") -> None:
    """
    Call ONCE per process at startup.

    Args:
        engine_name: identifies which engine is starting (written to startup log line).
        log_dir: directory for log files. Created if absent.
    """
    global _setup_done
    if _setup_done:
        return
    _setup_done = True

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # handlers filter by their own level

    # Console — human-readable, coloured, level = config/global.json system.log_level
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    console.setFormatter(_ConsoleFormatter())
    with contextlib.suppress(AttributeError):
        console.stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    root.addHandler(console)

    # Master log — trading-YYYY-MM-DD.log, ALL components, ALL levels down to
    # DEBUG when system.log_level is DEBUG (not hardcoded INFO as before
    # 2026-09-23 — setting DEBUG in config previously never reached this file).
    # This is the single place to read "everything that happened on this day"
    # across every engine; the per-component files below stay the place to
    # isolate one engine's own trail for the CURRENT day. Rolls to a new dated
    # file at IST midnight (_DailyBoundaryHandler, dated=True) — that day's file
    # is never deleted by this rollover, only cleaned up later by
    # log_retention_days.master via cleanup_old_logs().
    master = _DailyBoundaryHandler(
        log_path, "trading", _MASTER_LOG_MAX_BYTES, _MASTER_LOG_SAME_DAY_BACKUPS, dated=True
    )
    master.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    master.setFormatter(_FileFormatter())
    root.addHandler(master)

    # Per-component files — every component gets its OWN file at DEBUG (full debug
    # always captured regardless of system.log_level, which only gates the master
    # log + console). propagate=True (default): every record also reaches the
    # master log + console, filtered by THEIR level. Each file holds only the
    # CURRENT day (_DailyBoundaryHandler, dated=False): at IST midnight it is
    # deleted and reopens empty — the full record for prior days lives in that
    # day's master log instead, so nothing is lost, and this file never
    # accumulates across days.
    for name, filename in _COMPONENTS.items():
        sub = logging.getLogger(name)
        sub.setLevel(logging.DEBUG)
        _add_rotating(
            sub, log_path / filename, logging.DEBUG, _COMPONENT_MAX_BYTES, _COMPONENT_BACKUPS
        )

    # Audit log — daily file, append-only, own lifecycle (Windows Task Scheduler,
    # per this module's docstring) — deliberately NOT subject to the per-component
    # daily deletion above (it is compliance-grade, kept longer than a day on
    # purpose). It now ALSO propagates into the master log (previously it did
    # not), so "the master log has everything" is actually true without
    # weakening audit's own separate retention.
    _setup_audit_logger(log_path)

    # Best-effort: service_std{out,err}-*.log (NSSM restart artifacts) is not a
    # Python-logging-managed file family and is not touched by the day-boundary
    # rollover above -- it is cleaned up by age AND by count, see cleanup_old_logs().
    cleanup_old_logs(log_path)

    logging.getLogger(engine_name).info(
        "logging_initialised",
        extra={"ctx": {"engine": engine_name, "log_dir": str(log_path), "level": LOG_LEVEL}},
    )


_DEFAULT_SERVICE_LOG_RETENTION_DAYS = 14
_DEFAULT_AUDIT_LOG_RETENTION_DAYS = 30
_DEFAULT_MASTER_LOG_RETENTION_DAYS = 30
# Belt-and-braces cap on service_std{out,err} FILE COUNT, independent of age.
# NSSM (AppRotateOnline=1) starts a fresh timestamped file on every service
# restart, and this engine restarts many times a day during active development
# -- 70 of the 143 files ever seen in logs/ on 2026-09-23 were this one family,
# because they were being CREATED faster than the 14-day age cutoff removed
# them. A day-based cutoff alone cannot bound a restart-rate problem; a count
# cap does, without needing to guess a shorter (and possibly still wrong) day
# number or touch ops/service_setup.ps1's rotation policy itself.
_SERVICE_LOG_MAX_COUNT = 20


def _log_retention_days() -> tuple[int, int, int]:
    """(service_log_days, audit_log_days, master_log_days) from config/global.json
    system.log_retention_days, falling back to the defaults above on any
    read/parse failure or missing key."""
    try:
        import json as _json

        p = Path(__file__).resolve().parents[2] / "config" / "global.json"
        cfg = (
            _json.loads(p.read_text(encoding="utf-8"))
            .get("system", {})
            .get("log_retention_days", {})
        )
        return (
            int(cfg.get("service_stdout_stderr", _DEFAULT_SERVICE_LOG_RETENTION_DAYS)),
            int(cfg.get("audit", _DEFAULT_AUDIT_LOG_RETENTION_DAYS)),
            int(cfg.get("master", _DEFAULT_MASTER_LOG_RETENTION_DAYS)),
        )
    except Exception:
        return (
            _DEFAULT_SERVICE_LOG_RETENTION_DAYS,
            _DEFAULT_AUDIT_LOG_RETENTION_DAYS,
            _DEFAULT_MASTER_LOG_RETENTION_DAYS,
        )


def cleanup_old_logs(log_dir: Path) -> None:
    """Delete rotated/dated log files past their retention window.

    Three file families here grow WITHOUT BOUND on their own, unlike the
    per-component files (now day-bounded by _DailyBoundaryHandler itself — see
    setup_logging()) and the master log's same-day byte cap:

    - service_std{out,err}-<timestamp>.log — ops/service_setup.ps1 sets
      AppRotateOnline 1, so NSSM renames service_stdout.log/service_stderr.log
      to a fresh timestamped file on every restart (or at AppRotateBytes) and
      never deletes old ones itself. This engine restarts often (multiple times
      a day during active development), so these accumulate quickly — 70 of the
      143 files ever seen in logs/ on 2026-09-23 were this one family, created
      faster than the day-based cutoff alone removed them. Also capped by
      COUNT (_SERVICE_LOG_MAX_COUNT), not just age, for that reason.
    - audit-YYYY-MM-DD.log — _setup_audit_logger opens a brand new file every
      calendar day and never rotates or deletes it (by design, append-only,
      own lifecycle per the module docstring — this is its only cleanup).
    - trading-YYYY-MM-DD.log — the master log; _DailyBoundaryHandler rolls to
      a new dated file at IST midnight but never deletes the old one (that IS
      the daily archive), so retention has to happen here instead.

    Called once per process start (not on a sleep-based daily loop): a process
    that restarts more often than once a day would otherwise starve a
    once-a-day sleep loop of ever completing, the same class of bug found in
    the swing/intraday LLM reasoning loops (an in-memory "already ran today"
    gate reset by every restart). Startup is cheap and idempotent to re-run.

    Best-effort: never touches the live, currently-open files (service_stdout.log,
    service_stderr.log, today's audit-<today>.log / trading-<today>.log are
    excluded by pattern/date), and a permission error or missing directory must
    never crash the caller.
    """
    service_days, audit_days, master_days = _log_retention_days()
    now = time.time()
    today_suffix = datetime.now(tz=_IST).strftime("%Y-%m-%d")
    today_audit = f"audit-{today_suffix}.log"
    today_master = f"trading-{today_suffix}.log"

    deleted = 0
    freed_bytes = 0
    for pattern, cutoff_secs, exclude in (
        ("service_std*-*.log", service_days * 86400, None),
        ("audit-????-??-??.log", audit_days * 86400, today_audit),
        ("trading-????-??-??.log", master_days * 86400, today_master),
    ):
        cutoff = now - cutoff_secs
        try:
            candidates = list(log_dir.glob(pattern))
        except OSError:
            continue
        for f in candidates:
            if exclude is not None and f.name == exclude:
                continue
            try:
                st = f.stat()
                if st.st_mtime < cutoff:
                    freed_bytes += st.st_size
                    f.unlink()
                    deleted += 1
            except OSError:
                continue

    # Count cap on service_std{out,err}: age alone cannot bound a restart-rate
    # problem (see docstring). Keep the newest _SERVICE_LOG_MAX_COUNT of each,
    # oldest-by-mtime first to go, after the age-based pass above has already run.
    for prefix in ("service_stdout-", "service_stderr-"):
        try:
            files = sorted(
                log_dir.glob(f"{prefix}*.log"), key=lambda f: f.stat().st_mtime, reverse=True
            )
        except OSError:
            continue
        for f in files[_SERVICE_LOG_MAX_COUNT:]:
            try:
                freed_bytes += f.stat().st_size
                f.unlink()
                deleted += 1
            except OSError:
                continue

    if deleted:
        logging.getLogger(__name__).info(
            "log_cleanup_done deleted=%d freed_mb=%.1f service_retention_days=%d "
            "audit_retention_days=%d master_retention_days=%d service_max_count=%d",
            deleted,
            freed_bytes / (1024 * 1024),
            service_days,
            audit_days,
            master_days,
            _SERVICE_LOG_MAX_COUNT,
        )


class _SafeRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that tolerates Windows file locks during rollover.

    The same log file (e.g. api.log) is held open by more than one process — the
    launcher and the uvicorn `--reload` worker both write it — and by the `api` and
    `src.api` loggers within a process. On Windows os.rename then raises
    PermissionError (WinError 32). Swallow it: skip this rollover and keep appending
    to the current file rather than crashing the logging thread on every attempt."""

    # Seconds to wait before re-attempting a rollover that failed on a file lock.
    _RETRY_COOLDOWN_S = 60.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rotate_blocked_until = 0.0

    def rotate(self, source: str, dest: str) -> None:
        try:
            super().rotate(source, dest)
            self._rotate_blocked_until = 0.0
        except OSError:
            # Another handle holds the file — leave it in place; doRollover() reopens
            # baseFilename and logging continues. Back off before retrying: retrying
            # on the very next emit (the previous behaviour) meant that once a file
            # passed maxBytes with a lock held, EVERY subsequent log line ran a full
            # close/rename/reopen cycle — measured 1.19ms vs 0.033ms, 36x — while the
            # file still grew without bound. api.log reached 189MB, 18.9x its 10MB
            # threshold, with zero backups ever produced.
            self._rotate_blocked_until = time.time() + self._RETRY_COOLDOWN_S

    def shouldRollover(self, record: logging.LogRecord) -> int:
        # Suppress the size check entirely while backing off, so the expensive
        # doRollover() path is not entered at all.
        if time.time() < self._rotate_blocked_until:
            return 0
        return super().shouldRollover(record)


def _today_ist() -> str:
    return datetime.now(tz=_IST).strftime("%Y-%m-%d")


class _DailyBoundaryHandler(_SafeRotatingFileHandler):
    """Resets to a clean file the moment the IST calendar day changes (2026-09-23
    design). Checked on every emit — no separate thread/loop, so it works the
    same in every process (launcher/api/orchestrator/dashboard) without
    coordination, and a process that happens to run past midnight without
    restarting still rolls over correctly rather than needing one.

    Two modes:
    - dated=True (the master log): filename carries the date
      (trading-YYYY-MM-DD.log). On rollover, nothing is deleted -- yesterday's
      file is simply left alone under its own name, cleaned up later by
      log_retention_days.master via cleanup_old_logs(). This IS the daily
      "rename" the master log needs: it is born with the right name so there
      is nothing to rename.
    - dated=False (every per-component file, e.g. api.log): filename is fixed.
      On rollover the old file (and any same-day size-rotation backups
      _SafeRotatingFileHandler made, e.g. api.log.1) is DELETED, not kept --
      its full content already reached yesterday's dated master log via
      propagation, so nothing is lost, and logs/ does not accumulate 24
      component files x N days x backups forever. The file reopens empty for
      the new day.

    Same-day byte-size rotation (inherited from _SafeRotatingFileHandler) is
    kept as a safety net against a single day's runaway volume; it is not the
    primary lifecycle mechanism any more.
    """

    def __init__(
        self,
        directory: Path,
        base_name: str,
        max_bytes: int,
        backup_count: int,
        dated: bool,
    ) -> None:
        self._directory = directory
        self._base_name = base_name
        self._dated = dated
        self._ist_day = _today_ist()
        super().__init__(
            str(self._path_for(self._ist_day)),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )

    def _path_for(self, day: str) -> Path:
        if self._dated:
            return self._directory / f"{self._base_name}-{day}.log"
        return self._directory / self._base_name

    def emit(self, record: logging.LogRecord) -> None:
        today = _today_ist()
        if today != self._ist_day:
            self._roll_to_new_day(today)
        super().emit(record)

    def _roll_to_new_day(self, today: str) -> None:
        with contextlib.suppress(Exception):
            self.close()  # sets self.stream = None; next emit() reopens lazily
        self._ist_day = today
        self.baseFilename = str(self._path_for(today))
        if not self._dated:
            # Component file: delete today's-now-yesterday's file AND any same-day
            # size-rotation backups (api.log.1, .2, ...) — its content already
            # reached the dated master log via propagation, so nothing is lost.
            # A Windows lock (PermissionError, a subclass of OSError) is tolerated
            # the same way _SafeRotatingFileHandler tolerates one: skip it, the
            # stale file just sits there an extra day until the next process
            # restart's cleanup_old_logs() or the next successful rollover removes it.
            for stale in self._directory.glob(f"{self._base_name}*"):
                with contextlib.suppress(OSError):
                    stale.unlink()


def _add_rotating(
    logger: logging.Logger,
    path: Path,
    level: int,
    max_bytes: int,
    backup_count: int,
) -> None:
    # delay=True (inside _DailyBoundaryHandler): don't open/create the file until
    # the first record is actually emitted to it. A component that never logs
    # anything (or a day nothing happens on) leaves no empty file in logs/.
    handler = _DailyBoundaryHandler(path.parent, path.name, max_bytes, backup_count, dated=False)
    handler.setLevel(level)
    handler.setFormatter(_FileFormatter())
    logger.addHandler(handler)


def _setup_audit_logger(log_path: Path) -> None:
    """Daily audit file. Append-only, own lifecycle (never rotated/deleted by
    this app -- see module docstring). Propagates into the master log (changed
    2026-09-23: used to be isolated) so the master log genuinely holds
    everything; audit's own file and retention are unaffected by that."""
    audit = logging.getLogger("audit")
    audit.setLevel(logging.DEBUG)

    today = datetime.now(tz=_IST).strftime("%Y-%m-%d")
    handler = logging.FileHandler(
        str(log_path / f"audit-{today}.log"), mode="a", encoding="utf-8", delay=True
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_FileFormatter())
    audit.addHandler(handler)


class _AshwaxAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    """
    LoggerAdapter that merges caller's extra with our own script_name.

    Python's default LoggerAdapter.process() *overwrites* kwargs["extra"] with
    self.extra — it does not merge. That would silently drop any ctx dict the
    caller passes. This subclass merges instead.
    """

    def process(  # type: ignore[override]
        self, msg: str, kwargs: dict[str, object]
    ) -> tuple[str, dict[str, object]]:
        raw_extra = kwargs.get("extra")
        caller_extra: dict[str, object] = raw_extra if isinstance(raw_extra, dict) else {}
        base: dict[str, object] = dict(self.extra) if self.extra else {}
        kwargs["extra"] = {**base, **caller_extra}
        return msg, kwargs

    def success(self, msg: str, *args: object, **kwargs: object) -> None:
        """SUCCESS-level log (25). LoggerAdapter does not proxy custom Logger methods."""
        self.log(SUCCESS, msg, *args, **kwargs)  # type: ignore[arg-type]


def get_logger(script_name: str) -> _AshwaxAdapter:
    """
    Return a LoggerAdapter that stamps every record with script_name.

    Usage:
        log = get_logger("order_monitor")
        log.warning("sl_missing", extra={"ctx": {"trade_id": "T-001", "symbol": "INFY"}})
    """
    return _AshwaxAdapter(
        logging.getLogger(script_name),
        {"script_name": script_name},
    )


def _reset_for_testing() -> None:
    """Reset setup guard and remove all handlers. TEST USE ONLY."""
    global _setup_done
    _setup_done = False
    for name in ("", "audit", *_COMPONENTS.keys()):
        lg = logging.getLogger(name)
        for h in lg.handlers[:]:
            lg.removeHandler(h)
            h.close()
