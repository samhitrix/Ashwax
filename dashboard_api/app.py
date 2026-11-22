"""
dashboard_api/app.py — FastAPI backend for ashwax dashboard.

Endpoints are split across route modules:
  - market_context_api  : positions, holdings, margins, LTP, WebSocket
  - order_lifecycle_api : orders, GTT
  - settings_api        : config, operator
  - supervisor_api      : session, regime, strategy_health, nightly_setups

Run:
  uvicorn dashboard_api.app:app --port 3000 --reload
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from kiteconnect import KiteConnect
from src.brokers.kite_auth import CredentialsMissingError, auto_refresh
from src.infrastructure.logger import get_logger, setup_logging

_ENV = Path(__file__).parent.parent / ".env"
load_dotenv(_ENV, override=True)

_API_KEY = os.environ.get("KITE_API_KEY", "")
_ACCESS_TOKEN = os.environ.get("KITE_ACCESS_TOKEN", "")
_BASE = Path(__file__).parent.parent

setup_logging("dashboard", log_dir=str(_BASE / "logs"))
_log = get_logger("dashboard")

app = FastAPI(title="ashwax API", version="3.0.0")


@app.on_event("startup")
async def _startup() -> None:
    global _ACCESS_TOKEN
    try:
        _ACCESS_TOKEN = auto_refresh(_ENV)
        _log.info("kite_auth_ok")
    except CredentialsMissingError as exc:
        _log.warning("kite_auth_skipped", extra={"ctx": {"reason": str(exc)}})
    except Exception as exc:
        _log.error("kite_auth_failed", extra={"ctx": {"error": str(exc)}})


app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://localhost:\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _kite() -> KiteConnect:
    k = KiteConnect(api_key=_API_KEY)
    k.set_access_token(_ACCESS_TOKEN)
    return k


def _read_log(name: str, default: Any = None) -> Any:
    path = _BASE / "logs" / name
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@app.get("/api/session")
def session_status() -> dict[str, Any]:
    try:
        k = _kite()
        profile: dict[str, Any] = k.profile()
        return {
            "ok": True,
            "user_id": profile.get("user_id"),
            "user_name": profile.get("user_name"),
            "email": profile.get("email"),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Market context
# ---------------------------------------------------------------------------


@app.get("/api/positions")
def get_positions() -> dict[str, Any]:
    try:
        return _kite().positions()  # type: ignore[return-value]
    except Exception as exc:
        return {"error": str(exc), "net": [], "day": []}


@app.get("/api/holdings")
def get_holdings() -> list[Any]:
    try:
        return _kite().holdings()  # type: ignore[return-value]
    except Exception as exc:
        return [{"error": str(exc)}]


@app.get("/api/margins")
def get_margins() -> dict[str, Any]:
    try:
        return _kite().margins()  # type: ignore[return-value]
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/api/ltp")
def get_ltp(symbols: str = "NSE:NIFTY 50") -> dict[str, Any]:
    """Comma-separated instrument symbols: NSE:RELIANCE,NSE:NIFTY 50"""
    try:
        sym_list = [s.strip() for s in symbols.split(",")]
        return _kite().ltp(sym_list)  # type: ignore[return-value]
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@app.get("/api/orders")
def get_orders() -> list[Any]:
    try:
        return _kite().orders()  # type: ignore[return-value]
    except Exception as exc:
        return [{"error": str(exc)}]


@app.get("/api/gtt")
def get_gtt() -> list[Any]:
    try:
        return _kite().get_gtts()  # type: ignore[return-value]
    except Exception as exc:
        return [{"error": str(exc)}]


# ---------------------------------------------------------------------------
# System state (from logs)
# ---------------------------------------------------------------------------


@app.get("/api/regime")
def get_regime() -> dict[str, Any]:
    return _read_log("regime.json", {"regime": "UNKNOWN", "source": "no data"})


@app.get("/api/strategy_health")
def get_strategy_health() -> dict[str, Any]:
    return _read_log("strategy_health.json", {})


@app.get("/api/nightly_setups")
def get_nightly_setups() -> list[Any]:
    return _read_log("nightly_setups.json", [])


@app.get("/api/rs_ranking")
def get_rs_ranking() -> dict[str, Any]:
    return _read_log("rs_ranking.json", {})


@app.get("/api/components")
def get_components() -> list[Any]:
    """
    Returns live component/process status.
    Written by the orchestrator to logs/processes.json.
    Returns [] when the trading engine is not running.
    """
    return _read_log("processes.json", [])


# ---------------------------------------------------------------------------
# Config / operator
# ---------------------------------------------------------------------------


@app.get("/api/config/{name}")
def get_config(name: str) -> dict[str, Any]:
    # TODO: load from config/{name}.json with schema validation
    path = _BASE / "config" / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"error": f"config '{name}' not found"}


@app.get("/api/operator")
def get_operator() -> dict[str, Any]:
    return get_config("operator")


# ---------------------------------------------------------------------------
# WebSocket — real-time LTP every 2 s
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def websocket_ltp(ws: WebSocket) -> None:
    await ws.accept()
    k = _kite()
    try:
        while True:
            try:
                symbols: list[str] = ["NSE:NIFTY 50", "NSE:NIFTY BANK"]
                net: list[dict[str, Any]] = k.positions().get("net", [])
                for p in net[:8]:
                    if p.get("quantity", 0) != 0:
                        symbols.append(f"{p['exchange']}:{p['tradingsymbol']}")
                ltp = k.ltp(list(dict.fromkeys(symbols)))
                await ws.send_json({"type": "ltp", "data": ltp})
            except Exception as exc:
                await ws.send_json({"type": "error", "message": str(exc)})
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
