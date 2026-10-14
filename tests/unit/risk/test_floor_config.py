"""Absolute-floor percentage comes from config/capital.json with a safe fallback."""

from __future__ import annotations

import json

import pytest
from src.risk import floor_config

_VALID_CAPITAL = {
    "account": {
        "fallback_equity_if_broker_unavailable": "₹20,00,000",
        "max_loss_if_everything_open_hits_stop": "2.5%",
        "stop_all_trading_if_equity_falls_below": "40%",
        "drawdown_steps": [{"drawdown_from_start": "3%", "position_size_becomes": "75%"}],
    },
    "options": {
        "max_structure_loss_per_strategy": "0.75%",
        "max_loss_per_day_per_strategy": "0.25%",
        "max_loss_per_day_all_options_strategies": "0.5%",
        "max_share_of_shared_risk": "1.5%",
        "max_open_strategies": 3,
        "max_open_strategies_per_index": 2,
        "max_open_same_direction_spreads": 2,
    },
    "swing": {
        "max_loss_per_trade": "0.2%",
        "max_share_of_shared_risk": "1%",
        "max_open_trades": 12,
        "max_new_trades_per_day": 2,
        "max_new_trades_per_day_per_strategy": 2,
        "keep_unused_cash_buffer": "10%",
    },
    "intraday": {
        "max_loss_per_trade": "0.05%",
        "max_share_of_shared_risk": "0.1%",
        "max_loss_per_day": "0.15%",
        "close_all_positions_when_daily_loss_hit": True,
        "max_open_trades": 6,
        "max_new_trades_per_day": 6,
        "max_new_trades_per_day_per_strategy": 3,
    },
}


def _load(tmp_path, payload, monkeypatch):  # type: ignore[no-untyped-def]
    p = tmp_path / "capital.json"
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    from src.risk import budget as budget_module

    monkeypatch.setattr(budget_module, "_PATH", p)
    floor_config.absolute_floor_pct.cache_clear()
    return floor_config.absolute_floor_pct()


def test_reads_configured_value(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = json.loads(json.dumps(_VALID_CAPITAL))
    payload["account"]["stop_all_trading_if_equity_falls_below"] = "40%"
    assert _load(tmp_path, payload, monkeypatch) == 0.40


def test_missing_or_corrupt_config_uses_default(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert _load(tmp_path, "{not json", monkeypatch) == 0.25
    assert _load(tmp_path, {}, monkeypatch) == 0.25


def test_out_of_range_value_is_rejected(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for bad in ("0%", "100%"):
        payload = json.loads(json.dumps(_VALID_CAPITAL))
        payload["account"]["stop_all_trading_if_equity_falls_below"] = bad
        assert _load(tmp_path, payload, monkeypatch) == 0.25


def test_shipped_config_is_valid_and_all_three_checks_share_it() -> None:
    floor_config.absolute_floor_pct.cache_clear()
    pct = floor_config.absolute_floor_pct()
    from src.risk import absolute_floor, crm
    from src.supervisor import floor_check

    assert absolute_floor._FLOOR_PCT == crm._ABSOLUTE_FLOOR_PCT == floor_check._FLOOR_PCT == pct


@pytest.fixture(autouse=True)
def _reset_floor_cache():
    yield
    floor_config.absolute_floor_pct.cache_clear()
