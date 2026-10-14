"""Dashboard API: GET /api/llm_tokens (D5, 2026-09-23).

Written by the orchestrator (src.llm.model_health.write_token_usage_snapshot, the
only process with a Redis-backed TokenLedger) and read here via the existing
_read_log convention -- the same bridge /api/llm_health already uses for the same
reason (see that endpoint's docstring).
"""

from __future__ import annotations

import json

from src.api import server


def test_llm_tokens_returns_default_when_file_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_BASE", tmp_path)
    out = server.get_llm_tokens()
    assert out == {"checked_at": None, "providers": {}, "purposes": {}}


def test_llm_tokens_reads_written_snapshot(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    payload = {
        "checked_at": "2026-09-23T12:00:00+05:30",
        "providers": {"gemini": {"tokens": 500, "requests": 12}},
        "purposes": {"swing": {"spent": 300, "limit": 40000}},
    }
    (logs_dir / "llm_token_usage.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(server, "_BASE", tmp_path)

    out = server.get_llm_tokens()

    assert out["providers"]["gemini"]["requests"] == 12
    assert out["purposes"]["swing"]["limit"] == 40000
