"""Shape test for GET /api/paper_trades — global _db_pool monkeypatched."""

from __future__ import annotations

from datetime import datetime

import pytest
import src.api.server as srv


class _FakePool:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def fetch(self, *_a: object, **_k: object) -> list[dict]:
        return self._rows


async def test_paper_trades_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "symbol": "INFY",
            "direction": "long",
            "quantity": 10,
            "product": "MIS",
            "fill_price": 100.0,
            "exit_price": 110.0,
            "pnl_realized": 100.0,
            "partial_pnl_booked": 0,
            "strategy_name": "orb_intraday",
            "order_state": "CLOSED",
            "exit_reason": "TARGET_HIT",
            "initial_sl": 98.0,
            "current_sl": 99.0,
            "initial_target": 105.0,
            "current_target": 110.0,
            "regime_at_entry": "BULL",
            "setup_quality_score": 82.5,
            "llm_confidence": 0.78,
            "llm_flag": "CONFIRM",
            "llm_reason": "trend + RS aligned",
            "entry_timestamp": datetime(2026, 6, 17, 9, 30),
            "exit_timestamp": datetime(2026, 6, 17, 15, 25),
        }
    ]
    monkeypatch.setattr(srv, "_db_pool", _FakePool(rows))
    out = await srv.get_paper_trades()
    assert len(out) == 1
    row = out[0]
    assert row["symbol"] == "INFY"
    assert row["product"] == "MIS"
    assert row["entryPrice"] == 100.0
    assert row["exitPrice"] == 110.0
    assert row["pnl"] == 100.0
    assert row["state"] == "CLOSED"
    assert row["strategy"] == "orb_intraday"
    assert row["sl"] == 99.0  # current_sl wins over initial_sl (trailed value)
    assert row["target"] == 110.0  # current_target wins over initial_target
    assert row["initialSl"] == 98.0  # plan SL surfaced distinctly
    assert row["initialTarget"] == 105.0  # plan target surfaced distinctly
    assert row["exitReason"] == "TARGET_HIT"
    assert row["regime"] == "BULL"
    assert row["setupScore"] == 82.5
    assert row["llmConfidence"] == 0.78
    assert row["llmFlag"] == "CONFIRM"
    assert row["llmReason"] == "trend + RS aligned"
    assert row["entryAt"] and row["closedAt"]


async def test_paper_trades_empty_when_no_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "_db_pool", None)
    assert await srv.get_paper_trades() == []
