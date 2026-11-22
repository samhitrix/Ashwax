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
            }
        ]
    }
    (tmp_path / "config" / "strategies_registry.json").write_text(
        json.dumps(registry), encoding="utf-8"
    )
    builtin = tmp_path / "strategies" / "builtin"
    builtin.mkdir(parents=True)
    (builtin / "existing_strat.json").write_text(
        json.dumps({"meta": {"name": "existing_strat"}}), encoding="utf-8"
    )
    (builtin / "new_strat.json").write_text(
        json.dumps({"meta": {"name": "new_strat"}}), encoding="utf-8"
    )
    monkeypatch.setattr(srv, "_BASE", tmp_path)
    return tmp_path


def test_update_strategy_enabled_flips_existing_entry(strategy_env: Path) -> None:
    result = srv.update_strategy_enabled("existing_strat", {"enabled": False})
    assert result == {"ok": True, "enabled": False}
    registry = json.loads(
        (strategy_env / "config" / "strategies_registry.json").read_text(encoding="utf-8")
    )
    entry = next(e for e in registry["strategies"] if e["name"] == "existing_strat")
    assert entry["enabled"] is False


def test_update_strategy_enabled_appends_new_entry(strategy_env: Path) -> None:
    result = srv.update_strategy_enabled("new_strat", {"enabled": True})
    assert result == {"ok": True, "enabled": True}
    registry = json.loads(
        (strategy_env / "config" / "strategies_registry.json").read_text(encoding="utf-8")
    )
    entry = next(e for e in registry["strategies"] if e["name"] == "new_strat")
    assert entry["source"] == "BUILTIN"
    assert entry["spec_path"] == "strategies/builtin/new_strat.json"
    assert entry["min_quality_score"] == 65


def test_update_strategy_enabled_404_for_unknown_strategy(strategy_env: Path) -> None:
    with pytest.raises(HTTPException) as exc_info:
        srv.update_strategy_enabled("ghost_strategy", {"enabled": True})
    assert exc_info.value.status_code == 404


def test_update_strategy_enabled_rejects_non_bool(strategy_env: Path) -> None:
    with pytest.raises(HTTPException) as exc_info:
        srv.update_strategy_enabled("existing_strat", {"enabled": "yes"})
    assert exc_info.value.status_code == 400
