"""Dashboard API: new Logs tab endpoints (/api/logs/files, /api/logs/{name}).

Filenames are always resolved against the logger's own known-component list
(never a raw caller-supplied path), so an unknown name — including a path
traversal attempt — must be rejected rather than read.
"""

from __future__ import annotations

from fastapi import HTTPException
from src.api import server


def test_known_log_filenames_includes_components_and_trading_log():
    names = server._known_log_filenames()
    assert "execution.log" in names
    assert "orchestrator.log" in names
    assert "trading.log" in names


def test_list_log_files_only_lists_existing_files(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "execution.log").write_text("line1\nline2\n", encoding="utf-8")
    monkeypatch.setattr(server, "_BASE", tmp_path)  # "risk.log" etc. never created → absent

    out = server.list_log_files()

    names = {f["name"] for f in out}
    assert "execution.log" in names
    assert "risk.log" not in names
    entry = next(f for f in out if f["name"] == "execution.log")
    assert entry["sizeBytes"] > 0
    assert entry["modifiedAt"]


def test_get_log_contents_rejects_unknown_filename():
    try:
        server.get_log_contents("../../etc/passwd")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("expected HTTPException for unknown log filename")


def test_get_log_contents_rejects_name_outside_known_list():
    try:
        server.get_log_contents("not_a_real_component.log")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("expected HTTPException for unknown log filename")


def test_get_log_contents_tails_known_file(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    lines = [f"line-{i}" for i in range(10)]
    (logs_dir / "execution.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(server, "_BASE", tmp_path)

    out = server.get_log_contents("execution.log", tail=3)

    assert out["name"] == "execution.log"
    assert out["lines"] == ["line-7", "line-8", "line-9"]


def test_get_log_contents_missing_file_returns_empty_lines(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_BASE", tmp_path)  # tmp_path/logs doesn't exist

    out = server.get_log_contents("execution.log")

    assert out["name"] == "execution.log" and out["lines"] == []
    assert out["hasMore"] is False and out["fileSize"] == 0


def test_get_log_contents_tail_is_clamped_to_at_least_one(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    lines = [f"line-{i}" for i in range(5)]
    (logs_dir / "execution.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(server, "_BASE", tmp_path)

    out = server.get_log_contents("execution.log", tail=0)

    assert out["lines"] == ["line-4"]  # tail=0 clamps up to 1, not "return everything"


def test_get_log_contents_tail_is_clamped_to_at_most_2000(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    lines = [f"line-{i}" for i in range(5)]
    (logs_dir / "execution.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(server, "_BASE", tmp_path)

    out = server.get_log_contents("execution.log", tail=99999)

    assert out["lines"] == lines  # file only has 5 lines regardless of the huge request
