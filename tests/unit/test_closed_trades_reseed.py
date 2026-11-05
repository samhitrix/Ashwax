"""Tests for _load_closed_trades_log — rehydrates shared['closed_trades'] from
logs/closed_trades.json on startup so a restart no longer wipes closed-trade
history that feeds attribution/expectancy/learning (see orchestrator.py comment)."""

import json

from src import orchestrator


def test_loads_existing_log(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "closed_trades.json").write_text(
        json.dumps([{"agent_trade_id": "T1", "pnl_r": 1.5, "strategy": "orb_intraday_smart"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "_LOGS", log_dir)

    result = orchestrator._load_closed_trades_log()

    assert result == [{"agent_trade_id": "T1", "pnl_r": 1.5, "strategy": "orb_intraday_smart"}]


def test_missing_log_returns_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "_LOGS", tmp_path / "logs_missing")

    assert orchestrator._load_closed_trades_log() == []


def test_malformed_log_returns_empty_list(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "closed_trades.json").write_text("not valid json", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_LOGS", log_dir)

    assert orchestrator._load_closed_trades_log() == []


def test_non_list_json_returns_empty_list(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "closed_trades.json").write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_LOGS", log_dir)

    assert orchestrator._load_closed_trades_log() == []
