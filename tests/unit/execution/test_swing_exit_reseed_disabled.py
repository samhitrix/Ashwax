"""Swing exit management for positions whose strategy is DISABLED, and the time-stop clock
across restarts (found 2026-10-06: RADICO, oneil_canslim disabled 09-11, held 69 days with
no trail/time stop because the registry returns None for disabled strategies and a rebuilt
tracker restarted its bar count at 0)."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from src.orchestrator import _daily_bars_after_entry, _reseed_swing_exit_state
from src.strategies.registry import StrategyRegistry

_ROOT = Path(__file__).resolve().parents[3]


def _disabled_registry(tmp_path: Path, monkeypatch) -> StrategyRegistry:  # type: ignore[no-untyped-def]
    # Real oneil_canslim spec, disabled in a temp registry file.
    sdir = tmp_path / "strategies"
    sdir.mkdir()
    (sdir / "oneil_canslim.json").write_text(
        (_ROOT / "strategies/builtin/oneil_canslim.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "strategies_registry.json").write_text(
        json.dumps({"strategies": [{"name": "oneil_canslim", "enabled": False}]})
    )
    monkeypatch.chdir(tmp_path)  # registry reads config/strategies_registry.json relatively
    reg = StrategyRegistry(strategy_dirs=[sdir])
    reg.reload(df=None)
    return reg


def test_disabled_strategy_spec_is_available_for_open_positions_only(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    reg = _disabled_registry(tmp_path, monkeypatch)
    assert reg.get("oneil_canslim") is None  # cannot originate new signals
    assert "oneil_canslim" not in [c.spec.meta.name for c in reg.all_enabled()]
    compiled = reg.spec_for_open_positions("oneil_canslim")
    assert compiled is not None
    shipped = json.loads((_ROOT / "strategies/builtin/oneil_canslim.json").read_text())
    assert (
        compiled.spec.exit_conditions.time_stop_days == shipped["exit_conditions"]["time_stop_days"]
    )
    assert reg.spec_for_open_positions("no_such_strategy") is None


def test_daily_bars_after_entry_excludes_entry_day() -> None:
    days = [date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31), date(2026, 8, 3)]
    assert _daily_bars_after_entry(days, date(2026, 7, 29)) == 3
    assert _daily_bars_after_entry(days, date(2026, 8, 3)) == 0


class _Pool:
    def __init__(self, rows) -> None:  # type: ignore[no-untyped-def]
        self._rows = rows

    async def fetch(self, sql, *args):  # type: ignore[no-untyped-def]
        return self._rows if "paper_trades" in sql else []


class _Ohlcv:
    def __init__(self, n_days: int) -> None:
        idx = pd.bdate_range("2026-07-29", periods=n_days, tz="Asia/Kolkata")
        self.df = pd.DataFrame(
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6}, index=idx
        )

    async def fetch_dataframe(self, sym, tf, n):  # type: ignore[no-untyped-def]
        return self.df


def test_reseed_manages_disabled_strategy_and_keeps_the_entry_clock(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    reg = _disabled_registry(tmp_path, monkeypatch)
    row = {
        "agent_trade_id": "TRD-RADICO",
        "symbol": "NSE:RADICO",
        "direction": "LONG",
        "fill_price": 100.0,
        "initial_sl": 92.0,
        "partial_pnl_booked": None,
        "current_sl": 92.0,
        "current_target": 120.0,
        "strategy_name": "oneil_canslim",
        "entry_timestamp": datetime(2026, 7, 29, 10, 0),
    }
    shared = {"db_pool": _Pool([row]), "strategy_registry": reg, "ohlcv_repo": _Ohlcv(70)}
    added = asyncio.run(_reseed_swing_exit_state(shared))
    assert added == 1  # old code: 0 (swing_exit_reseed_no_spec, silently skipped)
    tracker = shared["swing_exit_state"]["TRD-RADICO"]["tracker"]
    assert tracker._bars == 69  # 70 daily bars incl. the entry day; old code: 0
    # One more daily step crosses 60 -> the time stop must fire.
    exit_ = tracker.step(_Ohlcv(71).df)
    assert exit_ is not None and "time" in str(exit_).lower()


def test_reseed_without_any_spec_warns_once_and_alerts(tmp_path, monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    reg = _disabled_registry(tmp_path, monkeypatch)
    alerts = []
    monkeypatch.setattr(
        "src.infrastructure.operator_alerts.raise_alert",
        lambda msg, **kw: alerts.append((msg, kw)) or True,
    )
    row = {
        "agent_trade_id": "TRD-X",
        "symbol": "NSE:X",
        "direction": "LONG",
        "fill_price": 100.0,
        "initial_sl": 92.0,
        "partial_pnl_booked": None,
        "current_sl": 92.0,
        "current_target": 120.0,
        "strategy_name": "deleted_strategy",
        "entry_timestamp": datetime(2026, 7, 29, 10, 0),
    }
    shared = {"db_pool": _Pool([row]), "strategy_registry": reg, "ohlcv_repo": _Ohlcv(10)}
    asyncio.run(_reseed_swing_exit_state(shared))
    asyncio.run(_reseed_swing_exit_state(shared))  # second 30s cycle
    assert len(alerts) == 1
    assert sum("swing_exit_reseed_no_spec" in r.message for r in caplog.records) == 1
    assert SimpleNamespace  # keep import used
