"""Dashboard API: PUT /api/strategies/{name}/execution_mode — per-strategy
paper/live override (strategies_registry.json execution_mode field). Mirrors
the existing update_strategy_enabled endpoint's read-modify-atomic-write
pattern (test_strategy_enabled_endpoint.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import src.api.server as srv
from fastapi import HTTPException


@pytest.fixture
def strategy_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "config").mkdir()
    registry = {
        "strategies": [
            {
                "name": "existing_strat",
                "source": "BUILTIN",
                "spec_path": "strategies/builtin/existing_strat.json",
                "enabled": True,
                "min_quality_score": 65,
            },
            {
                "name": "already_live",
                "source": "BUILTIN",
                "spec_path": "strategies/builtin/already_live.json",
                "enabled": True,
                "min_quality_score": 65,
                "execution_mode": "live",
            },
        ]
    }
    (tmp_path / "config" / "strategies_registry.json").write_text(
        json.dumps(registry), encoding="utf-8"
    )
    monkeypatch.setattr(srv, "_BASE", tmp_path)
    return tmp_path


def _registry(strategy_env: Path) -> dict:
    return json.loads(
        (strategy_env / "config" / "strategies_registry.json").read_text(encoding="utf-8")
    )


def test_sets_paper_override(strategy_env: Path) -> None:
    result = srv.update_strategy_execution_mode("existing_strat", {"executionMode": "paper"})
    assert result == {"ok": True, "executionMode": "paper"}
    entry = next(e for e in _registry(strategy_env)["strategies"] if e["name"] == "existing_strat")
    assert entry["execution_mode"] == "paper"


def test_sets_live_override(strategy_env: Path) -> None:
    srv.update_strategy_execution_mode("existing_strat", {"executionMode": "live"})
    entry = next(e for e in _registry(strategy_env)["strategies"] if e["name"] == "existing_strat")
    assert entry["execution_mode"] == "live"


def test_inherit_is_rejected(strategy_env: Path) -> None:
    """Every strategy carries an explicit paper/live value now."""
    with pytest.raises(HTTPException) as exc_info:
        srv.update_strategy_execution_mode("already_live", {"executionMode": "inherit"})
    assert exc_info.value.status_code == 400


def test_live_rejected_when_account_paper_locked(strategy_env: Path) -> None:
    (strategy_env / "config" / "operator.json").write_text(
        json.dumps({"account": {"paper_mode_locked": True}}), encoding="utf-8"
    )
    with pytest.raises(HTTPException) as exc_info:
        srv.update_strategy_execution_mode("existing_strat", {"executionMode": "live"})
    assert exc_info.value.status_code == 409
    # Going to paper is always allowed, even while locked.
    assert srv.update_strategy_execution_mode("already_live", {"executionMode": "paper"})["ok"]


def test_404_for_unknown_strategy(strategy_env: Path) -> None:
    with pytest.raises(HTTPException) as exc_info:
        srv.update_strategy_execution_mode("ghost_strategy", {"executionMode": "paper"})
    assert exc_info.value.status_code == 404


def test_400_for_invalid_mode(strategy_env: Path) -> None:
    with pytest.raises(HTTPException) as exc_info:
        srv.update_strategy_execution_mode("existing_strat", {"executionMode": "sometimes"})
    assert exc_info.value.status_code == 400


def test_get_strategies_reports_execution_mode(
    strategy_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builtin = strategy_env / "strategies" / "builtin"
    builtin.mkdir(parents=True)
    (builtin / "existing_strat.json").write_text(
        json.dumps({"meta": {"name": "existing_strat"}}), encoding="utf-8"
    )
    (builtin / "already_live.json").write_text(
        json.dumps({"meta": {"name": "already_live"}}), encoding="utf-8"
    )
    monkeypatch.setattr(srv, "_read_log", lambda *a, **k: {})

    out = srv.get_strategies()

    by_name = {s["name"]: s for s in out}
    assert by_name["existing_strat"]["executionMode"] == "paper"
    assert by_name["already_live"]["executionMode"] == "live"
