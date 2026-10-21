"""
launcher.py — start the ashwax v3.0 system (API + Dashboard).

Usage:
    python launcher.py

Starts:
    1. uvicorn src.api.server:app --port 3000  (FastAPI backend)
    2. npm run dev  (Vite dashboard, dashboard_react/)

Press Ctrl+C to stop both.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from src.infrastructure.logger import _AshwaxAdapter, get_logger, setup_logging

ROOT = Path(__file__).parent.resolve()
DASHBOARD_DIR = ROOT / "dashboard_react"

# NOTE: deliberately no `--reload`. uvicorn's file watcher raises a console control
# event when it restarts its worker; every child here shares the launcher's console
# process group, so that event reached the launcher too and tore the whole stack —
# orchestrator included — down. 32/32 reloads in logs/api.log are followed by a
# launcher "stopping" within 4-600ms. Reloading a live trading engine on file save
# is not a behaviour this launcher should have.
# --no-access-log: the dashboard polls this API continuously, and one access-log
# line per poll was 1,466,628 of api.log's 1,467,573 lines (99.94%). Uvicorn still
# logs startup, shutdown and unhandled exceptions; only the "200 OK" noise is gone.
API_CMD = [
    sys.executable,
    "-m",
    "uvicorn",
    "src.api.server:app",
    "--port",
    "3000",
    "--no-access-log",
]
DASHBOARD_CMD = ["npm", "run", "dev"]
ORCHESTRATOR_CMD = [sys.executable, "-m", "src.orchestrator", "--paper"]

# Windows: start each child in its OWN console process group so a control event
# raised by (or sent to) one child can never propagate to the launcher or to its
# siblings. Shutdown is then explicit, via the finally block below.
_NEW_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

# Lines that are already in structured format (produced by get_logger / _ConsoleFormatter)
_STRUCTURED = re.compile(r"^\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2}")
# ANSI colour/escape codes emitted by tools like Vite — stripped before logging
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _level_of(line: str) -> int:
    """Guess the log level of a raw subprocess line so the console colours it correctly."""
    upper = line.upper()
    if any(k in upper for k in ("ERROR", "TRACEBACK", "EXCEPTION", "FATAL", "FAILED")):
        return logging.ERROR
    if any(k in upper for k in ("WARNING", "WARN")):
        return logging.WARNING
    if "DEBUG" in upper:
        return logging.DEBUG
    # Uvicorn HTTP access log lines — chatty, demote to DEBUG so they only
    # appear when LOG_LEVEL=DEBUG.  Pattern: '"GET /api/..." 200 OK'
    if re.search(r'"(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) /', line):
        return logging.DEBUG
    return logging.INFO


def _safe_print(line: str) -> None:
    """print() that cannot raise on characters the launcher's stdout cannot encode.

    ROOT CAUSE of the ~30 orchestrator freezes on 2026-09-24: under the service the
    launcher's stdout is a cp1252 pipe, and a single line containing e.g. U+2212 (minus
    sign, from an LLM prompt) or the rupee sign raised UnicodeEncodeError inside print().
    That killed the reader thread silently, nothing drained the orchestrator's stdout
    pipe, the orchestrator blocked on its next log write, its event loop stalled, the
    heartbeat stopped and the watchdog killed it two minutes later -- on every boot."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(line.encode(enc, "replace").decode(enc), flush=True)


def _stream(proc: subprocess.Popen[bytes], log: logging.LoggerAdapter[logging.Logger]) -> None:
    """
    Read subprocess stdout line by line.
    Lines already in structured format are printed as-is.
    Raw lines (e.g. Vite output) get ANSI stripped, then routed through the logger
    at their detected level so the console colours ERROR/WARNING/DEBUG/INFO correctly.

    MUST NEVER DIE while the child runs: if this thread stops reading, the child blocks
    on a full stdout pipe and freezes (see _safe_print). So each line is handled inside its
    own try/except, and the reader's end is always logged.
    """
    if proc.stdout is None:
        return
    try:
        for raw in proc.stdout:
            try:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                if _STRUCTURED.match(line):
                    # Already formatted by the subprocess's own logger — print unchanged
                    _safe_print(line)
                else:
                    clean = _ANSI.sub("", line).rstrip()
                    if clean:
                        log.log(_level_of(clean), clean)
            except Exception as exc:  # noqa: BLE001 — one bad line must not stop the drain
                try:
                    log.error(
                        "stream_line_failed",
                        extra={"ctx": {"pid": proc.pid, "error": repr(exc)[:160]}},
                    )
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001
        log.error("stream_reader_died", extra={"ctx": {"pid": proc.pid, "error": repr(exc)[:160]}})
    else:
        log.info("stream_reader_ended", extra={"ctx": {"pid": proc.pid}})


def _kite_login_and_report(log: _AshwaxAdapter) -> bool:
    """
    Auto-login to Kite from .env credentials and log real profile + funds.

    Returns True if login succeeded and live data was fetched, else False.
    Never raises — logs the problem and lets the launcher continue.
    """
    try:
        from kiteconnect import KiteConnect
        from src.brokers.kite_auth import CredentialsMissingError, auto_refresh
    except ImportError as exc:
        log.error("kite_import_failed", extra={"ctx": {"error": str(exc)}})
        return False

    log.info("kite_login_start")
    try:
        token = auto_refresh(ROOT / ".env")
    except CredentialsMissingError as exc:
        log.error("kite_credentials_missing", extra={"ctx": {"detail": str(exc)}})
        return False
    except Exception as exc:
        log.error("kite_login_failed", extra={"ctx": {"error": str(exc)}})
        return False

    api_key = os.environ.get("KITE_API_KEY", "")
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)

    try:
        profile = kite.profile()
        log.success(
            "kite_login_ok",
            extra={
                "ctx": {
                    "user_id": profile.get("user_id"),
                    "user_name": profile.get("user_name"),
                    "email": profile.get("email"),
                }
            },
        )
    except Exception as exc:
        log.error("kite_profile_failed", extra={"ctx": {"error": str(exc)}})
        return False

    try:
        # Local import: pulls pandas via the adapter, not worth paying at launcher start.
        from src.brokers.adapters.kite.adapter import _margin_from_kite

        # Raw equity.available.cash reads 0 when the free cash sits in live_balance,
        # so normalize here instead of logging a misleading 0.
        m = _margin_from_kite(kite.margins())
        log.info(
            "kite_funds",
            extra={
                "ctx": {
                    "available_cash": m.available_cash,
                    "collateral": m.collateral,
                    "used_margin": m.used_margin,
                    "net": m.equity,
                }
            },
        )
    except Exception as exc:
        log.error("kite_margins_failed", extra={"ctx": {"error": str(exc)}})
        return False

    return True


BOOT_LOGIN_TIMEOUT_SECONDS = 90


def _in_thread(fn: Any, *args: Any) -> Any:
    """Run fn on a worker thread and wait: the Angel/Flattrade helpers call asyncio.run(),
    which must never run on a thread that already has an event loop.

    Waits at most BOOT_LOGIN_TIMEOUT_SECONDS so a stuck SDK can never hold the boot; the
    abandoned thread is not waited for (shutdown(wait=False))."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn, *args).result(timeout=BOOT_LOGIN_TIMEOUT_SECONDS)
    finally:
        pool.shutdown(wait=False)


def _angel_one_login_and_report(log: _AshwaxAdapter) -> bool:
    """Refresh/renew the Angel One session (refresh token first, TOTP login as fallback).

    Returns True on a valid session. Never raises."""
    log.info("angel_one_login_start")
    try:
        from src.brokers import angel_one_auth

        _in_thread(angel_one_auth.auto_refresh, ROOT / ".env")
    except Exception as exc:  # noqa: BLE001 — one broker's failure must not stop boot
        log.error("angel_one_login_failed", extra={"ctx": {"error": str(exc)[:300]}})
        return False
    log.success("angel_one_login_ok")
    return True


def _flattrade_check_and_report(log: _AshwaxAdapter) -> bool:
    """Validate the stored Flattrade token. It NEVER logs in: Flattrade's daily login is
    manual-assisted (python -m src.brokers.flattrade_auth). An expired token is a WARNING
    telling the operator what to run; the orchestrator's _flattrade_token_refresh_loop raises
    the operator alert (this process has no alert surface). Never raises."""
    log.info("flattrade_token_check_start")
    try:
        from src.brokers import flattrade_auth
        from src.brokers.adapters.flattrade.client import FlattradeError

        _in_thread(flattrade_auth.auto_refresh, ROOT / ".env")
    except Exception as exc:  # noqa: BLE001 — one broker's failure must not stop boot
        expired = isinstance(exc, RuntimeError) and not isinstance(exc, FlattradeError)
        if expired:
            log.warning(
                "flattrade_token_expired",
                extra={
                    "ctx": {
                        "action": "run: python -m src.brokers.flattrade_auth",
                        "detail": str(exc)[:200],
                    }
                },
            )
        else:
            log.error("flattrade_token_check_failed", extra={"ctx": {"error": str(exc)[:300]}})
        return False
    log.success("flattrade_token_ok")
    return True


# Looked up by name at call time so each login can be replaced on its own (tests).
_BOOT_LOGINS: dict[str, str] = {
    "kite": "_kite_login_and_report",
    "angel_one": "_angel_one_login_and_report",
    "flattrade": "_flattrade_check_and_report",
}


def _read_json(path: Path) -> dict[str, Any]:
    import json

    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 — a missing/broken file just means "nothing configured"
        return {}


def _boot_broker_logins(log: _AshwaxAdapter) -> dict[str, bool]:
    """Log in every broker this run can use (credentials present AND a market-data provider,
    the legacy broker.name, or a live strategy's broker); see src/brokers/boot_login.py.

    A broker with no credentials is skipped with an INFO line. Each login is isolated: one
    broker failing never affects another, and nothing here can block startup.
    Returns {broker: logged_in} for the brokers that were attempted."""
    from dotenv import load_dotenv
    from src.brokers.boot_login import select_boot_logins

    # The children inherit this environment; Kite's auto_refresh used to be the thing that
    # loaded .env here, and it no longer always runs.
    load_dotenv(ROOT / ".env", override=True)
    registry = _read_json(ROOT / "config" / "strategies_registry.json").get("strategies", [])
    to_login, skipped, problem = select_boot_logins(
        _read_json(ROOT / "config" / "global.json"),
        [e for e in registry if isinstance(e, dict)],
        os.environ,
    )
    if problem:
        log.warning("boot_login_config_invalid", extra={"ctx": {"error": problem[:300]}})
    for name, reason in skipped.items():
        log.info("broker_login_skipped", extra={"ctx": {"broker": name, "reason": reason}})
    results: dict[str, bool] = {}
    for name in to_login:
        try:
            results[name] = bool(globals()[_BOOT_LOGINS[name]](log))
        except Exception as exc:  # noqa: BLE001 — defensive: the helpers never raise
            log.error("broker_login_failed", extra={"ctx": {"broker": name, "error": str(exc)}})
            results[name] = False
    return results


def _safe_boot_broker_logins(log: _AshwaxAdapter) -> dict[str, bool]:
    """_boot_broker_logins that can never stop the launcher: boot logins are best-effort, and
    a bad config or a broken import here must not keep the API/dashboard/orchestrator down."""
    try:
        return _boot_broker_logins(log)
    except Exception as exc:  # noqa: BLE001 — services must start whatever happens here
        log.error(
            "boot_login_failed",
            extra={
                "ctx": {
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                    "action": "starting services anyway",
                }
            },
        )
        return {}


def _kill_stale_processes(log: logging.LoggerAdapter[logging.Logger]) -> None:
    """Kill every other python.exe on this machine before starting fresh.

    2026-08-06: a launcher instance kept running all day on stale orchestrator.py
    code — code edits don't hot-reload into an already-running process, and
    nobody re-ran launcher.py, so the day's strategy changes never took effect.
    Explicitly requested as an unconditional full-machine kill (not scoped to
    this project's own entrypoints) so every `python launcher.py` start is
    guaranteed clean, at the cost of also killing unrelated Python processes on
    this machine (other scripts, notebooks, tooling) — that tradeoff was raised
    and this scope was chosen deliberately. Never touches the current process.
    """
    try:
        import psutil
    except ImportError:
        log.debug("psutil_unavailable_skip_stale_kill")
        return

    self_pid = os.getpid()
    victims = [
        proc
        for proc in psutil.process_iter(["pid", "name"])
        if proc.pid != self_pid and (proc.info["name"] or "").lower().startswith("python")
    ]

    if not victims:
        return
    for p in victims:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(victims, timeout=3)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    log.warning("stale_python_processes_killed", extra={"ctx": {"pids": [p.pid for p in victims]}})


def _probe_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.5)
    return False


# ── Fixed ports ───────────────────────────────────────────────────────────────
# The dashboard hardcodes localhost:3000 and users bookmark :5173, so the ports are
# FIXED, never "whatever is free". If a leftover process of ours holds one, free it;
# if something unrelated holds it, stop with a clear message instead of drifting to
# another port (Vite would otherwise silently move to 5174 — see strictPort).
API_PORT = 3000
DASHBOARD_PORT = 5173
_OURS = ("python", "node", "npm", "uvicorn", "esbuild")


def _free_port(port: int, log: _AshwaxAdapter) -> None:
    """Free `port` if a stale process of ours holds it; exit loudly otherwise."""
    try:
        import psutil
    except ImportError:
        return
    me = os.getpid()
    try:
        conns = psutil.net_connections(kind="tcp")
    except psutil.Error:
        return
    for c in conns:
        if c.status != psutil.CONN_LISTEN or c.laddr.port != port or not c.pid or c.pid == me:
            continue
        try:
            proc = psutil.Process(c.pid)
            name = (proc.name() or "").lower()
        except psutil.Error:
            continue
        if not name.startswith(_OURS):
            log.error(
                "port_held_by_other_process",
                extra={"ctx": {"port": port, "pid": c.pid, "name": name}},
            )
            raise SystemExit(f"port {port} is held by {name} (pid {c.pid}) — free it and restart")
        for victim in [*proc.children(recursive=True), proc]:
            try:
                victim.kill()
            except psutil.Error:
                pass
        log.warning("port_freed", extra={"ctx": {"port": port, "pid": c.pid, "name": name}})
        time.sleep(1)


# ── Orchestrator liveness watchdog ───────────────────────────────────────────
# The launcher only noticed an orchestrator that EXITED. On 2026-09-21 it froze
# twice (alive, 0 CPU, no log lines) and the dashboard sat on STOPPED for 37 min.
# 120 s is the API's own IDLE→STOPPED boundary (server._engine_heartbeats), so
# the watchdog acts exactly when the dashboard would show STOPPED, and is
# deliberately looser than the 45 s "active" line so a long-but-live blocking
# stretch (the 181 s pool-starvation incident) is not killed prematurely.
HEARTBEAT_DEAD_S = 120.0
_WATCHDOG_POLL_S = 10.0
_MAX_RESTARTS = 3  # per rolling window, then stop restarting (boot-loop guard)
# 1h, not 15 min: the 2026-09-24 hang recurred every ~430s, so a 900s window never held
# more than 2 restarts and this guard never tripped (30 kills). Must span >_MAX_RESTARTS
# hang periods to be able to trip at all.
_RESTART_WINDOW_S = 3600.0


def _heartbeat_hung(age_s: float | None, uptime_s: float) -> bool:
    """True when the orchestrator has run long enough that a fresh heartbeat must
    exist, and the newest one is still older than HEARTBEAT_DEAD_S. `age_s` None
    (API unreachable / 503) means we cannot judge, so never act."""
    return age_s is not None and uptime_s > HEARTBEAT_DEAD_S and age_s > HEARTBEAT_DEAD_S


def _heartbeat_age() -> float | None:
    import json as _json
    import urllib.request

    try:
        with urllib.request.urlopen(  # noqa: S310 — fixed localhost URL
            f"http://127.0.0.1:{API_PORT}/api/agent_status", timeout=5
        ) as r:
            age = _json.loads(r.read()).get("heartbeat_age_s")
        return float(age) if age is not None else None
    except Exception:
        return None


def _find_pyspy() -> str | None:
    """Locate py-spy. The launcher runs as the `svc_ashwax` service account, whose %APPDATA%
    is not the interactive user's -- the old %APPDATA%-only lookup silently found nothing
    in all 30 hang kills of 2026-09-24, so no stack was ever captured. Order: explicit
    ASHWAX_PYSPY, PATH, the project's own tools/bin copy, then the current user's APPDATA."""
    import shutil

    candidates = [
        os.environ.get("ASHWAX_PYSPY", ""),
        shutil.which("py-spy") or "",
        str(ROOT / "tools" / "bin" / "py-spy.exe"),
        str(
            Path(os.environ.get("APPDATA", "")) / "Python" / "Python313" / "Scripts" / "py-spy.exe"
        ),
    ]
    return next((c for c in candidates if c and Path(c).is_file()), None)


def _dump_stack(pid: int, log: _AshwaxAdapter) -> None:
    """py-spy stack dump of the hung process, so the root cause of the freeze is captured
    before it is killed. Never silent: a missing py-spy is logged, because a diagnostic
    that quietly does nothing is how the same hang went undiagnosed all day."""
    exe = _find_pyspy()
    if exe is None:
        log.warning("hang_stack_dump_unavailable", extra={"ctx": {"detail": "py-spy not found"}})
        return
    out = ROOT / "logs" / f"hang_dump_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    try:
        res = subprocess.run(  # noqa: S603
            [exe, "dump", "--pid", str(pid)], capture_output=True, text=True, timeout=30
        )
        out.write_text(res.stdout + res.stderr, encoding="utf-8")
        log.warning("hang_stack_dumped", extra={"ctx": {"file": str(out)}})
    except Exception as exc:  # noqa: BLE001
        log.warning("hang_stack_dump_failed", extra={"ctx": {"error": str(exc)}})


def _push_alert(text: str, log: _AshwaxAdapter) -> bool:
    """Push `text` to the operator's Telegram from the LAUNCHER process. The launcher is the
    only component that still works when the orchestrator is frozen, so it must be able to
    raise the alarm itself: on 2026-09-24 the orchestrator was killed ~30 times and only
    launcher.log knew. Never raises; a failed or unconfigured push is logged, not silent."""
    import json as _json
    import urllib.request

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        try:  # read-only lookup; never modifies .env
            from dotenv import dotenv_values

            vals = dotenv_values(ROOT / ".env")
            token = token or str(vals.get("TELEGRAM_BOT_TOKEN") or "").strip()
            chat = chat or str(vals.get("TELEGRAM_CHAT_ID") or "").strip()
        except Exception:  # noqa: BLE001
            pass
    if not (token and chat):
        log.warning("alert_push_unconfigured", extra={"ctx": {"detail": "no telegram creds"}})
        return False
    try:
        req = urllib.request.Request(  # noqa: S310 — fixed https Telegram API URL
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=_json.dumps({"chat_id": chat, "text": text[:3500]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return getattr(resp, "status", 200) == 200
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "alert_push_failed", extra={"ctx": {"error": str(exc).replace(token, "***")[:160]}}
        )
        return False


def _spawn_orchestrator(
    procs: list[subprocess.Popen[bytes]], threads: list[threading.Thread], log: _AshwaxAdapter
) -> subprocess.Popen[bytes]:
    orch = subprocess.Popen(  # noqa: S603
        ORCHESTRATOR_CMD,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=_NEW_GROUP,
    )
    procs.append(orch)
    t = threading.Thread(
        target=_stream, args=(orch, get_logger("orchestrator_stdout")), daemon=True
    )
    t.start()
    threads.append(t)
    log.info("orchestrator_started", extra={"ctx": {"mode": "paper", "pid": orch.pid}})
    return orch


def main() -> None:
    # Full-fidelity UTF-8 on the launcher's own stdout (the service pipe defaults to cp1252);
    # errors="replace" so nothing can ever raise here. _safe_print covers a stream that
    # cannot be reconfigured.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    setup_logging("launcher")
    log = get_logger("launcher")

    procs: list[subprocess.Popen[bytes]] = []
    threads: list[threading.Thread] = []

    log.info("ashwax_starting", extra={"ctx": {"version": "3.0"}})

    # Reap any leftover API/orchestrator processes from a prior unclean run so they
    # don't keep api.log open (Windows rotation lock) or hold port 3000.
    _kill_stale_processes(log)
    _free_port(API_PORT, log)
    _free_port(DASHBOARD_PORT, log)

    # Log in each usable broker (Kite reports real funds) before bringing services up
    if _safe_boot_broker_logins(log).get("kite") is False:
        log.warning("kite_login_unavailable", extra={"ctx": {"action": "starting services anyway"}})

    try:
        # ── API server ────────────────────────────────────────────────────────
        api = subprocess.Popen(  # noqa: S603
            API_CMD,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=_NEW_GROUP,
        )
        procs.append(api)
        log_api = get_logger("api_stdout")
        t_api = threading.Thread(target=_stream, args=(api, log_api), daemon=True)
        t_api.start()
        threads.append(t_api)
        log.info(
            "api_server_started", extra={"ctx": {"url": "http://localhost:3000", "pid": api.pid}}
        )

        log.info("waiting_for_api_ready")
        if _probe_port(3000, timeout=30.0):
            log.info("api_ready")
        else:
            log.warning(
                "api_probe_timeout", extra={"ctx": {"action": "starting other services anyway"}}
            )

        # ── Trading orchestrator (all loops: news, RS, sector, attribution, slippage, CRM…) ──
        orch = _spawn_orchestrator(procs, threads, log)

        # ── React dashboard ────────────────────────────────────────────────────
        dash = subprocess.Popen(  # noqa: S603 S602
            DASHBOARD_CMD,
            cwd=str(DASHBOARD_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # Windows needs the shell to resolve npm.cmd; with a list argument on POSIX
            # shell=True would run a bare `npm` and drop "run dev".
            shell=sys.platform == "win32",
            creationflags=_NEW_GROUP,
        )
        procs.append(dash)
        log_dash = get_logger("dashboard")
        t_dash = threading.Thread(target=_stream, args=(dash, log_dash), daemon=True)
        t_dash.start()
        threads.append(t_dash)
        log.info(
            "dashboard_started", extra={"ctx": {"url": "http://localhost:5173", "pid": dash.pid}}
        )
        log.info("press_ctrl_c_to_stop")

        # p.poll() keeps returning the same cached exit code forever once a
        # process has died — without de-duping, a dead child logs the same
        # "process_exited" warning every 2s indefinitely while the launcher
        # carries on as if nothing happened. A core component being down is
        # not a state to run in silently, so log it once and stop the stack.
        reported_exits: set[int] = set()
        orch_started = time.monotonic()
        restarts: list[float] = []
        next_watchdog = time.monotonic() + _WATCHDOG_POLL_S
        while True:
            time.sleep(2)
            if orch.poll() is None and time.monotonic() >= next_watchdog:
                next_watchdog = time.monotonic() + _WATCHDOG_POLL_S
                age = _heartbeat_age()
                if _heartbeat_hung(age, time.monotonic() - orch_started):
                    now = time.monotonic()
                    restarts = [t for t in restarts if now - t < _RESTART_WINDOW_S]
                    if len(restarts) >= _MAX_RESTARTS:
                        log.error(
                            "orchestrator_hang_restart_limit_stopping",
                            extra={"ctx": {"restarts": len(restarts)}},
                        )
                        _push_alert(
                            f"ASHWAX STOPPED: orchestrator froze {len(restarts)}x in "
                            f"{_RESTART_WINDOW_S / 60:.0f} min - restart limit hit, stack stopped. "
                            "Open positions rely on broker-side SL/GTT only. Needs manual attention.",
                            log,
                        )
                        break
                    restarts.append(now)
                    log.error(
                        "orchestrator_hung_restarting",
                        extra={"ctx": {"pid": orch.pid, "heartbeat_age_s": age}},
                    )
                    _dump_stack(orch.pid, log)
                    _push_alert(
                        f"ASHWAX: orchestrator FROZE (no heartbeat {age:.0f}s) and was killed + "
                        f"restarted ({len(restarts)} in the last {_RESTART_WINDOW_S / 60:.0f} min). "
                        "Stack dump in logs/hang_dump_*.txt.",
                        log,
                    )
                    procs.remove(orch)  # intentional kill must not trip core_process_down
                    orch.kill()
                    orch.wait(timeout=10)
                    orch = _spawn_orchestrator(procs, threads, log)
                    orch_started = time.monotonic()
                    continue
            dead = [p for p in procs if p.poll() is not None]
            for p in dead:
                if p.pid not in reported_exits:
                    reported_exits.add(p.pid)
                    log.warning(
                        "process_exited", extra={"ctx": {"pid": p.pid, "code": p.returncode}}
                    )
            if dead:
                log.error(
                    "core_process_down_stopping",
                    extra={"ctx": {"dead_pids": [p.pid for p in dead]}},
                )
                break

    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        for p in procs:
            if p.poll() is None:
                if sys.platform == "win32":
                    # CTRL_BREAK, not CTRL_C: Windows ignores CTRL_C_EVENT for a
                    # process started with CREATE_NEW_PROCESS_GROUP, so the old
                    # call would have been a silent no-op that left every child
                    # to the hard kill() below.
                    try:
                        p.send_signal(signal.CTRL_BREAK_EVENT)
                    except (OSError, ValueError):
                        p.terminate()
                else:
                    p.terminate()
        time.sleep(2)
        for p in procs:
            if p.poll() is None:
                p.kill()
        log.info("done")


if __name__ == "__main__":
    main()
