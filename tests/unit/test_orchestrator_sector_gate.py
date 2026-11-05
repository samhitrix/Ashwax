"""Tests for the sector-rotation -> CRMSignal.sector_declining wiring."""

import json

import pytest


@pytest.fixture
def sector_rotation_log(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_path = log_dir / "sector_rotation.json"
    log_path.write_text(
        json.dumps(
            {
                "sectors": [
                    {"name": "IT", "status": "DECLINING", "rs_4w": -0.05},
                    {"name": "BANKING", "status": "LEADING", "rs_4w": 0.03},
                ],
                "computed_at": "2026-08-12T09:00:00+05:30",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "operator.json").write_text(
        json.dumps({"sector_gate_enabled": True}), encoding="utf-8"
    )
    return tmp_path, log_path


def test_declining_sector_symbol_blocks(sector_rotation_log, monkeypatch):
    tmp_path, _ = sector_rotation_log
    from src import orchestrator

    monkeypatch.setattr(orchestrator, "_LOGS", tmp_path / "logs")
    monkeypatch.setattr(orchestrator, "_CONFIG", tmp_path / "config")
    orchestrator._sector_gate_cache.clear()
    assert orchestrator._resolve_sector_declining("TCS") is True  # TCS is in _SECTOR_STOCKS["IT"]


def test_leading_sector_symbol_passes(sector_rotation_log, monkeypatch):
    tmp_path, _ = sector_rotation_log
    from src import orchestrator

    monkeypatch.setattr(orchestrator, "_LOGS", tmp_path / "logs")
    monkeypatch.setattr(orchestrator, "_CONFIG", tmp_path / "config")
    orchestrator._sector_gate_cache.clear()
    assert orchestrator._resolve_sector_declining("HDFCBANK") is False


def test_unmapped_symbol_fails_open(sector_rotation_log, monkeypatch):
    tmp_path, _ = sector_rotation_log
    from src import orchestrator

    monkeypatch.setattr(orchestrator, "_LOGS", tmp_path / "logs")
    monkeypatch.setattr(orchestrator, "_CONFIG", tmp_path / "config")
    orchestrator._sector_gate_cache.clear()
    assert orchestrator._resolve_sector_declining("SOMERANDOMSYMBOL") is False


def test_gate_disabled_always_false(sector_rotation_log, monkeypatch):
    tmp_path, _ = sector_rotation_log
    (tmp_path / "config" / "operator.json").write_text(
        json.dumps({"sector_gate_enabled": False}), encoding="utf-8"
    )
    from src import orchestrator

    monkeypatch.setattr(orchestrator, "_LOGS", tmp_path / "logs")
    monkeypatch.setattr(orchestrator, "_CONFIG", tmp_path / "config")
    orchestrator._sector_gate_cache.clear()
    assert orchestrator._resolve_sector_declining("TCS") is False


def test_missing_log_file_fails_open(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "operator.json").write_text(
        json.dumps({"sector_gate_enabled": True}), encoding="utf-8"
    )
    from src import orchestrator

    monkeypatch.setattr(orchestrator, "_LOGS", tmp_path / "logs_missing")
    monkeypatch.setattr(orchestrator, "_CONFIG", tmp_path / "config")
    orchestrator._sector_gate_cache.clear()
    assert orchestrator._resolve_sector_declining("TCS") is False
