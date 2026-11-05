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
    # Deterministic broker credentials regardless of the developer's .env.
    for var in ("KITE_API_KEY", "ANGEL_ONE_API_KEY", "ANGEL_ONE_CLIENT_CODE"):
        monkeypatch.delenv(var, raising=False)
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


def _set_entry_field(strategy_env: Path, name: str, key: str, value: str) -> None:
    reg = _registry(strategy_env)
    next(e for e in reg["strategies"] if e["name"] == name)[key] = value
    (strategy_env / "config" / "strategies_registry.json").write_text(
        json.dumps(reg), encoding="utf-8"
    )


def test_sets_live_override_with_a_configured_broker(
    strategy_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KITE_API_KEY", "k")
    result = srv.update_strategy_execution_mode(
        "existing_strat", {"executionMode": "live", "broker": "kite"}
    )
    assert result["ok"] is True
    entry = next(e for e in _registry(strategy_env)["strategies"] if e["name"] == "existing_strat")
    assert entry["execution_mode"] == "live"
    assert entry["broker"] == "kite"


def test_live_uses_the_stored_broker_when_none_supplied(
    strategy_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KITE_API_KEY", "k")
    _set_entry_field(strategy_env, "existing_strat", "broker", "kite")
    srv.update_strategy_execution_mode("existing_strat", {"executionMode": "live"})
    entry = next(e for e in _registry(strategy_env)["strategies"] if e["name"] == "existing_strat")
    assert entry["execution_mode"] == "live"


def _assert_refused_and_untouched(strategy_env: Path, body: dict, *, reason: str) -> None:
    path = strategy_env / "config" / "strategies_registry.json"
    before = path.read_bytes()
    with pytest.raises(HTTPException) as exc_info:
        srv.update_strategy_execution_mode("existing_strat", body)
    assert exc_info.value.status_code == 400
    assert "existing_strat" in exc_info.value.detail
    assert reason in exc_info.value.detail
    assert path.read_bytes() == before


def test_live_without_a_broker_is_refused(strategy_env: Path) -> None:
    _assert_refused_and_untouched(
        strategy_env, {"executionMode": "live"}, reason="no 'broker' field"
    )


def test_live_with_an_unconfigured_broker_is_refused(strategy_env: Path) -> None:
    _assert_refused_and_untouched(
        strategy_env, {"executionMode": "live", "broker": "angel_one"}, reason="not configured"
    )


def test_live_with_an_unregistered_or_paper_broker_is_refused(strategy_env: Path) -> None:
    _assert_refused_and_untouched(
        strategy_env, {"executionMode": "live", "broker": "nope"}, reason="not registered"
    )
    _assert_refused_and_untouched(
        strategy_env, {"executionMode": "live", "broker": "paper"}, reason="not a live broker"
    )


def test_live_with_a_malformed_broker_value_is_refused(strategy_env: Path) -> None:
    with pytest.raises(HTTPException) as exc_info:
        srv.update_strategy_execution_mode("existing_strat", {"executionMode": "live", "broker": 5})
    assert exc_info.value.status_code == 400


def test_paper_keeps_the_stored_broker(strategy_env: Path) -> None:
    _set_entry_field(strategy_env, "already_live", "broker", "kite")
    srv.update_strategy_execution_mode("already_live", {"executionMode": "paper"})
    entry = next(e for e in _registry(strategy_env)["strategies"] if e["name"] == "already_live")
    assert entry["execution_mode"] == "paper"
    assert entry["broker"] == "kite"


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


def test_get_strategies_reports_broker_and_broker_ready(
    strategy_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KITE_API_KEY", "k")
    _set_entry_field(strategy_env, "already_live", "broker", "kite")
    _set_entry_field(strategy_env, "existing_strat", "broker", "angel_one")  # paper, unconfigured
    builtin = strategy_env / "strategies" / "builtin"
    builtin.mkdir(parents=True)
    for n in ("existing_strat", "already_live", "unregistered"):
        (builtin / f"{n}.json").write_text(json.dumps({"meta": {"name": n}}), encoding="utf-8")
    monkeypatch.setattr(srv, "_read_log", lambda *a, **k: {})

    out = {s["name"]: s for s in srv.get_strategies()}

    assert out["already_live"]["broker"] == "kite"
    assert out["already_live"]["brokerReady"] is True
    assert out["existing_strat"]["broker"] == "angel_one"
    assert out["existing_strat"]["brokerReady"] is False  # would be refused if set live
    assert out["unregistered"]["broker"] is None
    assert out["unregistered"]["brokerReady"] is None


def test_get_strategies_live_without_broker_is_not_ready(
    strategy_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builtin = strategy_env / "strategies" / "builtin"
    builtin.mkdir(parents=True)
    (builtin / "already_live.json").write_text(
        json.dumps({"meta": {"name": "already_live"}}), encoding="utf-8"
    )
    monkeypatch.setattr(srv, "_read_log", lambda *a, **k: {})
    out = {s["name"]: s for s in srv.get_strategies()}
    assert out["already_live"]["broker"] is None
    assert out["already_live"]["brokerReady"] is False
