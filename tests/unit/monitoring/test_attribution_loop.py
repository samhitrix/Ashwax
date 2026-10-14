"""Tests for _attribution_loop closed_trades → TradeRecord mapping logic."""

from __future__ import annotations

from datetime import datetime

import pytest
from src.monitoring.performance_attribution import PerformanceAttributor, TradeRecord


def _map_trade(t: dict, fallback_regime: str = "BULL") -> TradeRecord:
    """Replicate the mapping logic from _attribution_loop."""
    try:
        entry_ts = str(t.get("entry_ts", "") or t.get("exit_ts", ""))
        entry_time = datetime.fromisoformat(entry_ts) if entry_ts else datetime.now()
        exit_ts = str(t.get("exit_ts", ""))
        exit_time = datetime.fromisoformat(exit_ts) if exit_ts else entry_time
        hold_hours = max(0.0, (exit_time - entry_time).total_seconds() / 3600.0)
    except Exception:
        hold_hours, entry_time = 0.0, datetime.now()
    return TradeRecord(
        strategy=str(t.get("strategy_name", t.get("strategy", "unknown"))),
        pnl_r=float(t.get("pnl_r", 0.0)),
        entry_time=entry_time,
        regime_at_entry=str(t.get("regime_at_entry", fallback_regime)),
        sector=str(t.get("sector", "UNKNOWN")),
        llm_confidence=float(t.get("llm_confidence", 0.5)),
        hold_hours=hold_hours,
        volume_at_entry=str(t.get("volume_at_entry", "AVG")),
    )


class TestClosedTradeMapping:
    def test_strategy_name_mapped(self) -> None:
        t = _map_trade(
            {
                "strategy_name": "minervini_vcp",
                "pnl_r": 1.5,
                "entry_ts": "2026-01-15T10:00:00",
                "exit_ts": "2026-01-15T14:00:00",
            }
        )
        assert t.strategy == "minervini_vcp"

    def test_falls_back_to_strategy_key(self) -> None:
        t = _map_trade({"strategy": "ema_pullback", "pnl_r": 1.0})
        assert t.strategy == "ema_pullback"

    def test_hold_hours_computed(self) -> None:
        t = _map_trade(
            {"entry_ts": "2026-01-15T09:30:00", "exit_ts": "2026-01-15T15:30:00", "pnl_r": 1.0}
        )
        assert t.hold_hours == pytest.approx(6.0)

    def test_hold_hours_zero_on_same_timestamps(self) -> None:
        t = _map_trade(
            {"entry_ts": "2026-01-15T10:00:00", "exit_ts": "2026-01-15T10:00:00", "pnl_r": 1.0}
        )
        assert t.hold_hours == 0.0

    def test_bad_timestamps_do_not_raise(self) -> None:
        t = _map_trade({"entry_ts": "BAD", "exit_ts": "ALSO_BAD", "pnl_r": 0.5})
        assert t.hold_hours == 0.0

    def test_regime_falls_back_to_shared(self) -> None:
        t = _map_trade({"pnl_r": 0.5}, fallback_regime="BEAR")
        assert t.regime_at_entry == "BEAR"

    def test_attribution_runs_on_mapped_trades(self) -> None:
        trades = [
            _map_trade(
                {
                    "strategy_name": "vcp",
                    "pnl_r": r,
                    "entry_ts": "2026-01-15T10:00:00",
                    "exit_ts": "2026-01-15T11:00:00",
                }
            )
            for r in [1.5, -0.5, 2.0, -1.0]
        ]
        result = PerformanceAttributor().attribute(trades)
        assert "vcp" in result.by_strategy
        assert result.by_strategy["vcp"].count == 4
        assert result.by_strategy["vcp"].win_rate == pytest.approx(0.5)

    def test_empty_closed_trades_gives_empty_result(self) -> None:
        result = PerformanceAttributor().attribute([])
        assert result.by_strategy == {}
