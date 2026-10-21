"""Launcher boot login: per-broker, credential-gated, isolated (spec 3.2 Kite optional)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import launcher
import pytest
from src.brokers import angel_one_auth, flattrade_auth
from src.brokers.adapters.flattrade.client import FlattradeError
from src.brokers.boot_login import select_boot_logins

_CRED_VARS = (
    "KITE_API_KEY",
    "ANGEL_ONE_API_KEY",
    "ANGEL_ONE_CLIENT_CODE",
    "FLATTRADE_API_KEY",
    "FLATTRADE_USER_ID",
)
_FT = {"FLATTRADE_API_KEY": "k", "FLATTRADE_USER_ID": "u"}
_ANGEL = {"ANGEL_ONE_API_KEY": "k", "ANGEL_ONE_CLIENT_CODE": "c"}
_KITE = {"KITE_API_KEY": "k"}
_ALL_PROVIDERS = {"market_data": {"providers": ["angel_one", "flattrade", "kite"]}}


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict[str, Any]]] = []

    def _rec(self, level: str, msg: str, **kw: Any) -> None:
        self.lines.append((level, msg, (kw.get("extra") or {}).get("ctx", {})))

    def info(self, msg: str, **kw: Any) -> None:
        self._rec("INFO", msg, **kw)

    def warning(self, msg: str, **kw: Any) -> None:
        self._rec("WARNING", msg, **kw)

    def error(self, msg: str, **kw: Any) -> None:
        self._rec("ERROR", msg, **kw)

    def success(self, msg: str, **kw: Any) -> None:
        self._rec("SUCCESS", msg, **kw)

    def msgs(self, level: str | None = None) -> list[str]:
        return [m for lv, m, _ in self.lines if level is None or lv == level]


# ── selection ────────────────────────────────────────────────────────────────


def test_only_flattrade_credentials_selects_only_flattrade() -> None:
    login, skipped, problem = select_boot_logins(_ALL_PROVIDERS, [], _FT)
    assert login == ["flattrade"]
    assert "no credentials" in skipped["kite"]
    assert "no credentials" in skipped["angel_one"]
    assert problem is None


def test_credentials_alone_are_not_enough_when_nothing_uses_the_broker() -> None:
    cfg = {"broker": {"name": "paper"}, "market_data": {"providers": ["angel_one"]}}
    login, skipped, _ = select_boot_logins(cfg, [], {**_ANGEL, **_KITE})
    assert login == ["angel_one"]
    assert "no live strategy" in skipped["kite"]


def test_live_strategy_broker_is_logged_in_even_if_not_a_data_provider() -> None:
    cfg = {"broker": {"name": "paper"}, "market_data": {"providers": ["kite"]}}
    reg = [
        {"name": "s1", "execution_mode": "live", "broker": "angel_one"},
        {"name": "s2", "execution_mode": "paper", "broker": "flattrade"},  # paper: no session
    ]
    login, _, _ = select_boot_logins(cfg, reg, {**_ANGEL, **_KITE, **_FT})
    assert login == ["kite", "angel_one"]


def test_legacy_broker_name_keeps_kite_login_unchanged() -> None:
    # Today's shipped config: broker.name = kite; Kite with credentials is still logged in.
    cfg = {"broker": {"name": "kite"}, "market_data": {"providers": ["angel_one"]}}
    login, _, _ = select_boot_logins(cfg, [], _KITE)
    assert login == ["kite"]


def test_malformed_market_data_reports_problem_and_falls_back_to_credentials() -> None:
    login, _, problem = select_boot_logins({"market_data": {"providers": []}}, [], _FT)
    assert problem is not None
    assert login == ["flattrade"]


_MALFORMED_MARKET_DATA: list[Any] = [
    {"market_data": {"providers": ["kite"], "failover": None}},
    {"market_data": {"providers": ["kite"], "per_use": None}},
    {"market_data": []},
    {"market_data": {"providers": ["kite"], "failover": {"max_consecutive_failures": None}}},
    {"market_data": "kite"},
]


@pytest.mark.parametrize("cfg", _MALFORMED_MARKET_DATA)
def test_any_malformed_market_data_never_raises(cfg: dict[str, Any]) -> None:
    # Hand-edit mistakes raise AttributeError/TypeError in the parser, not ValueError.
    login, _, problem = select_boot_logins(cfg, [], {**_KITE, **_FT})
    assert problem is not None
    assert login == ["kite", "flattrade"]  # every credentialed broker stays a candidate


def test_malformed_registry_entry_never_raises() -> None:
    login, _, problem = select_boot_logins(
        {"broker": ["kite"], "market_data": {"providers": ["kite"]}},
        [{"execution_mode": "live", "broker": "angel_one"}, None],  # type: ignore[list-item]
        {**_KITE, **_ANGEL},
    )
    assert problem is not None
    assert login == ["kite", "angel_one"]


# ── launcher wiring ──────────────────────────────────────────────────────────


@pytest.fixture
def boot_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "global.json").write_text(json.dumps(_ALL_PROVIDERS), encoding="utf-8")
    (tmp_path / "config" / "strategies_registry.json").write_text(
        json.dumps({"strategies": []}), encoding="utf-8"
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    for var in _CRED_VARS:
        # setenv first so teardown restores the original state even after load_dotenv()
        # wrote the var straight into os.environ.
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
    return tmp_path


def _record_calls(monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, Any]) -> list[str]:
    called: list[str] = []
    for broker, fn_name in launcher._BOOT_LOGINS.items():

        def _fake(log: Any, _b: str = broker) -> bool:
            called.append(_b)
            out = outcomes.get(_b, True)
            if isinstance(out, Exception):
                raise out
            return bool(out)

        monkeypatch.setattr(launcher, fn_name, _fake)
    return called


def test_only_flattrade_configured_never_calls_kite(
    boot_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for k, v in _FT.items():
        monkeypatch.setenv(k, v)
    called = _record_calls(monkeypatch, {})
    log = _Log()
    assert launcher._boot_broker_logins(log) == {"flattrade": True}  # type: ignore[arg-type]
    assert called == ["flattrade"]
    skipped = [ctx["broker"] for lv, m, ctx in log.lines if m == "broker_login_skipped"]
    assert set(skipped) == {"kite", "angel_one"}
    assert not log.msgs("ERROR")


def test_kite_failure_does_not_stop_angel_or_flattrade(
    boot_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for k, v in {**_KITE, **_ANGEL, **_FT}.items():
        monkeypatch.setenv(k, v)
    called = _record_calls(monkeypatch, {"kite": RuntimeError("kite down")})
    log = _Log()
    res = launcher._boot_broker_logins(log)  # type: ignore[arg-type]
    assert called == ["kite", "angel_one", "flattrade"]
    assert res == {"kite": False, "angel_one": True, "flattrade": True}


def test_no_credentials_at_all_logs_in_nothing(
    boot_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = _record_calls(monkeypatch, {})
    log = _Log()
    assert launcher._boot_broker_logins(log) == {}  # type: ignore[arg-type]
    assert called == []
    assert not log.msgs("ERROR") and not log.msgs("WARNING")
    assert log.msgs("INFO").count("broker_login_skipped") == 3


def test_env_file_is_loaded_before_credentials_are_checked(
    boot_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (boot_root / ".env").write_text("FLATTRADE_API_KEY=k\nFLATTRADE_USER_ID=u\n", encoding="utf-8")
    called = _record_calls(monkeypatch, {})
    launcher._boot_broker_logins(_Log())  # type: ignore[arg-type]
    assert called == ["flattrade"]


@pytest.mark.parametrize("cfg", _MALFORMED_MARKET_DATA)
def test_malformed_global_json_on_disk_still_logs_in_and_warns(
    boot_root: Path, monkeypatch: pytest.MonkeyPatch, cfg: dict[str, Any]
) -> None:
    (boot_root / "config" / "global.json").write_text(json.dumps(cfg), encoding="utf-8")
    for k, v in _FT.items():
        monkeypatch.setenv(k, v)
    called = _record_calls(monkeypatch, {})
    log = _Log()
    assert launcher._boot_broker_logins(log) == {"flattrade": True}  # type: ignore[arg-type]
    assert called == ["flattrade"]
    assert "boot_login_config_invalid" in log.msgs("WARNING")


def test_safe_boot_logins_swallows_any_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_log: Any) -> dict[str, bool]:
        raise AttributeError("'NoneType' object has no attribute 'get'")

    monkeypatch.setattr(launcher, "_boot_broker_logins", _boom)
    log = _Log()
    assert launcher._safe_boot_broker_logins(log) == {}  # type: ignore[arg-type]
    assert log.msgs("ERROR") == ["boot_login_failed"]


def test_main_starts_services_even_when_boot_login_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """C1: before the fix a raising boot login killed launcher.main() before the API started."""
    log = _Log()
    started: list[Any] = []

    def _boom(_log: Any) -> dict[str, bool]:
        raise TypeError("'<' not supported between instances of 'NoneType' and 'int'")

    def _popen(cmd: Any, **_kw: Any) -> Any:
        started.append(cmd)
        raise KeyboardInterrupt  # stop main() right after the first service launch

    monkeypatch.setattr(launcher, "setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(launcher, "get_logger", lambda *_a, **_k: log)
    monkeypatch.setattr(launcher, "_kill_stale_processes", lambda *_a: None)
    monkeypatch.setattr(launcher, "_free_port", lambda *_a: None)
    monkeypatch.setattr(launcher, "_boot_broker_logins", _boom)
    monkeypatch.setattr(launcher.subprocess, "Popen", _popen)
    monkeypatch.setattr(launcher.time, "sleep", lambda *_a: None)
    launcher.main()
    assert started == [launcher.API_CMD]
    assert "boot_login_failed" in log.msgs("ERROR")


# ── per-broker helpers ───────────────────────────────────────────────────────


def test_flattrade_expired_token_warns_with_the_manual_login_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _expired(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("Flattrade token expired; run python -m src.brokers.flattrade_auth")

    monkeypatch.setattr(flattrade_auth, "auto_refresh", _expired)
    log = _Log()
    assert launcher._flattrade_check_and_report(log) is False  # type: ignore[arg-type]
    warn = [(m, ctx) for lv, m, ctx in log.lines if lv == "WARNING"]
    assert warn and warn[0][0] == "flattrade_token_expired"
    assert "python -m src.brokers.flattrade_auth" in warn[0][1]["action"]


def test_flattrade_api_error_is_an_error_not_an_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    def _down(*_a: Any, **_k: Any) -> str:
        raise FlattradeError("network")

    monkeypatch.setattr(flattrade_auth, "auto_refresh", _down)
    log = _Log()
    assert launcher._flattrade_check_and_report(log) is False  # type: ignore[arg-type]
    assert log.msgs("ERROR") == ["flattrade_token_check_failed"]


def test_flattrade_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flattrade_auth, "auto_refresh", lambda *_a, **_k: "tok")
    assert launcher._flattrade_check_and_report(_Log()) is True  # type: ignore[arg-type]


def test_angel_login_runs_off_the_calling_thread_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    def _uses_asyncio_run(*_a: Any, **_k: Any) -> str:
        return asyncio.run(asyncio.sleep(0, result="tok"))

    async def _from_a_loop() -> bool:
        # asyncio.run inside a running loop's thread would raise; _in_thread avoids it.
        return launcher._angel_one_login_and_report(_Log())  # type: ignore[arg-type]

    monkeypatch.setattr(angel_one_auth, "auto_refresh", _uses_asyncio_run)
    assert asyncio.run(_from_a_loop()) is True

    def _boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("no keyring creds")

    monkeypatch.setattr(angel_one_auth, "auto_refresh", _boom)
    log = _Log()
    assert launcher._angel_one_login_and_report(log) is False  # type: ignore[arg-type]
    assert log.msgs("ERROR") == ["angel_one_login_failed"]


def test_in_thread_gives_up_on_a_stuck_login_instead_of_holding_the_boot(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import concurrent.futures
    import threading

    import launcher

    release = threading.Event()
    monkeypatch.setattr(launcher, "BOOT_LOGIN_TIMEOUT_SECONDS", 0.2)
    try:
        with pytest.raises(concurrent.futures.TimeoutError):
            launcher._in_thread(release.wait)
    finally:
        release.set()
