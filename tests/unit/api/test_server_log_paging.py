"""Logs tab: the WHOLE file must be reachable (2026-09-24).

Before: the API clamped a request to 2000 lines and only ever read the last 1MB, and the UI
hard-coded 300 lines -- so on a 10MB+ log everything older than a few minutes was
unreachable. Now: byte-offset paging backwards, follow-mode forwards, and a full download.
"""

from __future__ import annotations

from fastapi import HTTPException
from src.api import server

_RUPEE, _MINUS, _DASH = chr(0x20B9), chr(0x2212), chr(0x2014)


def _big_log(tmp_path, n: int = 40000) -> list[str]:
    """~3MB of lines containing multi-byte characters (must survive chunk boundaries)."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    lines = [
        f"24-09-2026 12:00:00.{i % 1000:03d} | INFO | x | line-{i} {_RUPEE} {_MINUS} {_DASH} "
        + "y" * 60
        for i in range(n)
    ]
    (logs_dir / "orchestrator.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def test_can_page_back_through_a_file_much_larger_than_the_old_1mb_window(monkeypatch, tmp_path):
    all_lines = _big_log(tmp_path)
    monkeypatch.setattr(server, "_BASE", tmp_path)

    page = server.get_log_contents("orchestrator.log", tail=2000)
    collected = list(page["lines"])
    pages = 1
    while page["hasMore"]:
        page = server.get_log_contents("orchestrator.log", tail=2000, before=page["startOffset"])
        collected = page["lines"] + collected
        pages += 1
    assert pages > 5  # the old cap could only ever reach the last ~1MB
    assert collected == all_lines  # nothing lost, nothing duplicated, order preserved
    assert page["startOffset"] == 0


def test_page_size_is_no_longer_clamped_to_2000(monkeypatch, tmp_path):
    _big_log(tmp_path, n=3500)
    monkeypatch.setattr(server, "_BASE", tmp_path)
    out = server.get_log_contents("orchestrator.log", tail=3000)
    assert len(out["lines"]) == 3000


def test_follow_mode_returns_only_lines_written_since_the_offset(monkeypatch, tmp_path):
    _big_log(tmp_path, n=50)
    monkeypatch.setattr(server, "_BASE", tmp_path)
    first = server.get_log_contents("orchestrator.log", tail=10)
    with (tmp_path / "logs" / "orchestrator.log").open("a", encoding="utf-8") as f:
        f.write("NEW-1\nNEW-2\nPARTIAL-NO-NEWLINE")
    nxt = server.get_log_contents("orchestrator.log", after=first["endOffset"])
    assert nxt["lines"] == ["NEW-1", "NEW-2"]  # the half-written last line is held back
    assert nxt["reset"] is False
    with (tmp_path / "logs" / "orchestrator.log").open("a", encoding="utf-8") as f:
        f.write(" -done\n")
    again = server.get_log_contents("orchestrator.log", after=nxt["endOffset"])
    assert again["lines"] == ["PARTIAL-NO-NEWLINE -done"]


def test_initial_page_never_ends_on_a_half_written_line(monkeypatch, tmp_path):
    _big_log(tmp_path, n=20)
    monkeypatch.setattr(server, "_BASE", tmp_path)
    with (tmp_path / "logs" / "orchestrator.log").open("a", encoding="utf-8") as f:
        f.write("HALF-WRITTEN")
    page = server.get_log_contents("orchestrator.log", tail=5)
    assert all("HALF-WRITTEN" not in ln for ln in page["lines"])
    nxt = server.get_log_contents("orchestrator.log", after=page["endOffset"])
    assert nxt["lines"] == []  # still incomplete, so nothing is emitted yet


def test_follow_mode_detects_rotation_and_asks_the_client_to_reset(monkeypatch, tmp_path):
    _big_log(tmp_path, n=100)
    monkeypatch.setattr(server, "_BASE", tmp_path)
    out = server.get_log_contents("orchestrator.log", tail=5, after=10_000_000)
    assert out["reset"] is True and len(out["lines"]) == 5


def test_download_serves_the_entire_file_for_known_names_only(monkeypatch, tmp_path):
    _big_log(tmp_path, n=100)
    monkeypatch.setattr(server, "_BASE", tmp_path)
    resp = server.download_log("orchestrator.log")
    assert str(resp.path).endswith("orchestrator.log")
    try:
        server.download_log("../../etc/passwd")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("expected 404")
