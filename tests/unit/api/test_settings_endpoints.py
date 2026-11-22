from __future__ import annotations

import json
from pathlib import Path

import pytest
import src.api.server as srv
from fastapi import HTTPException


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sample.json").write_text(
        json.dumps({"_note": "a comment", "enabled": True, "count": 3}),
        encoding="utf-8",
    )
    monkeypatch.setattr(srv, "_BASE", tmp_path)
    return cfg


def test_get_settings_files_lists_allowlist(config_dir: Path) -> None:
    files = srv.get_settings_files()
    assert {"name": "sample", "path": "config/sample.json"} in files


def test_get_setting_returns_content(config_dir: Path) -> None:
    data = srv.get_setting("sample")
    assert data["enabled"] is True
    assert data["_note"] == "a comment"


def test_get_setting_rejects_unknown_name(config_dir: Path) -> None:
    with pytest.raises(HTTPException) as exc_info:
        srv.get_setting("does_not_exist")
    assert exc_info.value.status_code == 404


def test_update_setting_writes_file_and_backup(config_dir: Path) -> None:
    new_content = {"_note": "a comment", "enabled": False, "count": 3}
    result = srv.update_setting("sample", {"content": new_content})
    assert result == {"ok": True}
    on_disk = json.loads((config_dir / "sample.json").read_text(encoding="utf-8"))
    assert on_disk["enabled"] is False
    assert on_disk["_note"] == "a comment"
    backups = list((config_dir / ".backups").glob("sample.*.json"))
    assert len(backups) == 1


def test_update_setting_rejects_unknown_name(config_dir: Path) -> None:
    with pytest.raises(HTTPException) as exc_info:
        srv.update_setting("does_not_exist", {"content": {}})
    assert exc_info.value.status_code == 404


def test_update_setting_rejects_non_object_content(config_dir: Path) -> None:
    with pytest.raises(HTTPException) as exc_info:
        srv.update_setting("sample", {"content": "not an object"})
    assert exc_info.value.status_code == 400
