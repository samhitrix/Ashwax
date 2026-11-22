"""Dashboard API: PUT /api/strategies/{name}/broker -- sets the per-strategy live broker
(strategies_registry.json `broker`). The route must be usable (registered, non-paper,
configured) or nothing is written (spec rule R1: no default broker, no failover)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import src.api.server as srv
from fastapi import HTTPException

_REG = "strategies_registry.json"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "config").mkdir()
    registry = {
        "_note": "keep me",
        "strategies": [
            {"name": "paper_strat", "enabled": True, "execution_mode": "paper"},
            {"name": "live_strat", "enabled": True, "execution_mode": "live", "broker": "kite"},
        ],
    }
    (tmp_path / "config" / _REG).write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(srv, "_BASE", tmp_path)
    for var in ("KITE_API_KEY", "ANGEL_ONE_API_KEY", "ANGEL_ONE_CLIENT_CODE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _reg(env: Path) -> dict:
    return json.loads((env / "config" / _REG).read_text(encoding="utf-8"))


def _entry(env: Path, name: str) -> dict:
    return next(e for e in _reg(env)["strategies"] if e["name"] == name)


def test_sets_a_configured_broker_and_changes_nothing_else(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANGEL_ONE_API_KEY", "a")
    monkeypatch.setenv("ANGEL_ONE_CLIENT_CODE", "c")
    result = srv.update_strategy_broker("live_strat", {"broker": "angel_one"})
    assert result == {"ok": True, "broker": "angel_one"}
    assert _entry(env, "live_strat") == {
        "name": "live_strat",
        "enabled": True,
        "execution_mode": "live",
        "broker": "angel_one",
    }
    assert _reg(env)["_note"] == "keep me"


def test_paper_strategy_may_be_given_a_broker_without_going_live(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KITE_API_KEY", "k")
    srv.update_strategy_broker("paper_strat", {"broker": "kite"})
    entry = _entry(env, "paper_strat")
    assert entry["broker"] == "kite"
    assert entry["execution_mode"] == "paper"


@pytest.mark.parametrize(
    ("broker", "reason"),
    [
        ("angel_one", "not configured"),  # registered but no credentials in env
        ("nope", "not registered"),
        ("paper", "not a live broker"),
    ],
)
def test_unusable_broker_is_400_and_writes_nothing(env: Path, broker: str, reason: str) -> None:
    before = (env / "config" / _REG).read_bytes()
    with pytest.raises(HTTPException) as exc_info:
        srv.update_strategy_broker("live_strat", {"broker": broker})
    assert exc_info.value.status_code == 400
    assert "live_strat" in exc_info.value.detail
    assert reason in exc_info.value.detail
    assert (env / "config" / _REG).read_bytes() == before


@pytest.mark.parametrize(
    "body", [{}, {"broker": ""}, {"broker": "  "}, {"broker": 3}, {"broker": None}]
)
def test_malformed_body_is_400(env: Path, body: dict) -> None:
    with pytest.raises(HTTPException) as exc_info:
        srv.update_strategy_broker("live_strat", body)
    assert exc_info.value.status_code == 400


def test_unknown_strategy_is_404(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KITE_API_KEY", "k")
    with pytest.raises(HTTPException) as exc_info:
        srv.update_strategy_broker("ghost", {"broker": "kite"})
    assert exc_info.value.status_code == 404


def test_missing_registry_file_is_404(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KITE_API_KEY", "k")
    (env / "config" / _REG).unlink()
    with pytest.raises(HTTPException) as exc_info:
        srv.update_strategy_broker("live_strat", {"broker": "kite"})
    assert exc_info.value.status_code == 404
