"""Registry write paths: the generic PUT /api/settings/strategies_registry must obey R1 (no new
invalid live state), and every registry writer is serialized (no lost updates, no shared temp)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
import src.api.server as srv
from fastapi import HTTPException

_REG = "strategies_registry.json"


def _entries() -> list[dict[str, Any]]:
    return [
        {"name": "p1", "enabled": True, "execution_mode": "paper"},
        {"name": "ok_live", "enabled": True, "execution_mode": "live", "broker": "kite"},
        {"name": "legacy_bad", "enabled": True, "execution_mode": "live"},  # live, no broker
    ]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / _REG).write_text(
        json.dumps({"_note": "n", "strategies": _entries()}), encoding="utf-8"
    )
    monkeypatch.setattr(srv, "_BASE", tmp_path)
    for var in ("KITE_API_KEY", "ANGEL_ONE_API_KEY", "ANGEL_ONE_CLIENT_CODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KITE_API_KEY", "k")
    return tmp_path


def _content(env: Path) -> dict[str, Any]:
    return json.loads((env / "config" / _REG).read_text(encoding="utf-8"))


def _put(env: Path, mutate) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    content = _content(env)
    mutate(content["strategies"])
    return srv.update_setting("strategies_registry", {"content": content})


def _by(entries: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(e for e in entries if e["name"] == name)


def _refused(env: Path, mutate, *needles: str) -> None:  # type: ignore[no-untyped-def]
    before = (env / "config" / _REG).read_bytes()
    with pytest.raises(HTTPException) as exc_info:
        _put(env, mutate)
    assert exc_info.value.status_code == 400
    for needle in needles:
        assert needle in exc_info.value.detail
    assert (env / "config" / _REG).read_bytes() == before


def test_settings_refuses_a_newly_live_strategy_without_broker(env: Path) -> None:
    _refused(
        env,
        lambda es: _by(es, "p1").update(execution_mode="live"),
        "p1",
        "no 'broker' field",
    )


def test_settings_refuses_live_with_unconfigured_or_unregistered_broker(env: Path) -> None:
    _refused(
        env,
        lambda es: _by(es, "p1").update(execution_mode="live", broker="angel_one"),
        "p1",
        "not configured",
    )
    _refused(
        env,
        lambda es: _by(es, "p1").update(execution_mode="live", broker="nope"),
        "p1",
        "not registered",
    )


def test_settings_refuses_a_broker_change_to_an_invalid_one_on_a_live_entry(env: Path) -> None:
    _refused(env, lambda es: _by(es, "ok_live").update(broker="angel_one"), "ok_live")


def test_settings_names_every_offender(env: Path) -> None:
    def both(es: list[dict[str, Any]]) -> None:
        _by(es, "p1").update(execution_mode="live")
        es.append({"name": "new_live", "execution_mode": "live"})

    _refused(env, both, "p1", "new_live")


def test_settings_accepts_valid_live_with_broker(env: Path) -> None:
    result = _put(env, lambda es: _by(es, "p1").update(execution_mode="live", broker="kite"))
    assert result == {"ok": True}
    assert _by(_content(env)["strategies"], "p1")["broker"] == "kite"


def test_settings_unrelated_edit_with_a_legacy_invalid_live_entry_is_allowed(env: Path) -> None:
    _put(env, lambda es: _by(es, "p1").update(enabled=False))
    entries = _content(env)["strategies"]
    assert _by(entries, "p1")["enabled"] is False
    assert _by(entries, "legacy_bad")["execution_mode"] == "live"  # kept, untouched


def test_settings_paper_edits_are_unaffected(env: Path) -> None:
    _put(env, lambda es: _by(es, "legacy_bad").update(execution_mode="paper"))
    assert _by(_content(env)["strategies"], "legacy_bad")["execution_mode"] == "paper"


def test_settings_malformed_strategies_list_is_400(env: Path) -> None:
    before = (env / "config" / _REG).read_bytes()
    with pytest.raises(HTTPException) as exc_info:
        srv.update_setting("strategies_registry", {"content": {"strategies": "nope"}})
    assert exc_info.value.status_code == 400
    assert (env / "config" / _REG).read_bytes() == before


def test_other_settings_files_are_not_registry_checked(env: Path) -> None:
    (env / "config" / "sample.json").write_text("{}", encoding="utf-8")
    assert srv.update_setting("sample", {"content": {"strategies": "whatever"}}) == {"ok": True}


def test_concurrent_registry_writers_lose_no_update(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANGEL_ONE_API_KEY", "a")
    monkeypatch.setenv("ANGEL_ONE_CLIENT_CODE", "c")
    n = 20
    names = [f"s{i}" for i in range(n)]
    reg = {"strategies": [{"name": x, "enabled": False, "execution_mode": "paper"} for x in names]}
    (env / "config" / _REG).write_text(json.dumps(reg), encoding="utf-8")
    builtin = env / "strategies" / "builtin"
    builtin.mkdir(parents=True)
    for x in names:
        (builtin / f"{x}.json").write_text(json.dumps({"meta": {"name": x}}), encoding="utf-8")
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            barrier.wait()
            name = names[i]
            if i % 3 == 0:
                srv.update_strategy_broker(name, {"broker": "angel_one"})
            elif i % 3 == 1:
                srv.update_strategy_execution_mode(
                    name, {"executionMode": "live", "broker": "kite"}
                )
            else:
                srv.update_strategy_enabled(name, {"enabled": True})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    entries = _content(env)["strategies"]  # valid JSON
    assert len(entries) == n
    for i, name in enumerate(names):
        e = _by(entries, name)
        if i % 3 == 0:
            assert e["broker"] == "angel_one", name
        elif i % 3 == 1:
            assert (e["execution_mode"], e["broker"]) == ("live", "kite"), name
        else:
            assert e["enabled"] is True, name
    leftovers = [p.name for p in (env / "config").iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
