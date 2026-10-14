"""Tests for the swing cash-backlog helpers in src/orchestrator.py.

A candidate that passes CRM/ranking but is blocked only by insufficient_cash is
carried forward (up to _SWING_CASH_BACKLOG_MAX_RETRIES extra scans) so it gets
another shot once cash frees up, instead of being silently dropped forever.
"""

from __future__ import annotations

from pathlib import Path

import src.orchestrator as orch_mod
from src.strategies.uss.compiler import Signal
from src.strategies.uss.schema import Direction


def test_load_returns_empty_list_when_file_missing(tmp_path: Path) -> None:
    assert orch_mod._load_swing_cash_backlog(tmp_path / "missing.json") == []


def test_load_returns_empty_list_on_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "backlog.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert orch_mod._load_swing_cash_backlog(path) == []


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "backlog.json"
    entries = [
        {
            "bare_sym": "IFCI",
            "strategy_name": "minervini_vcp",
            "direction": "LONG",
            "entry_price": 45.5,
            "sl": 43.0,
            "target": 52.0,
            "score": 0.724,
            "rr": 2.5,
            "retries_used": 0,
        }
    ]

    orch_mod._save_swing_cash_backlog(path, entries)

    assert orch_mod._load_swing_cash_backlog(path) == entries


def test_backlog_entry_built_from_candidate_dict() -> None:
    sig = Signal("oneil_canslim", Direction.LONG, 100.0, 95.0, 115.0)
    candidate = {
        "bare_sym": "JSL",
        "strategy_name": "oneil_canslim",
        "sig": sig,
        "score": 0.771,
        "rr": 2.0,
    }

    entry = orch_mod._swing_cash_backlog_entry(candidate, retries_used=1)

    assert entry == {
        "bare_sym": "JSL",
        "strategy_name": "oneil_canslim",
        "direction": "LONG",
        "entry_price": 100.0,
        "sl": 95.0,
        "target": 115.0,
        "score": 0.771,
        "rr": 2.0,
        "retries_used": 1,
    }


def test_backlog_entry_json_round_trips_into_a_reconstructable_signal(tmp_path: Path) -> None:
    """The saved dict must carry everything needed to rebuild a Signal on the next
    scan (see the merge block in _swing_signal_scan_loop)."""
    sig = Signal("minervini_vcp", Direction.LONG, 250.0, 235.0, 290.0)
    candidate = {"bare_sym": "SAIL", "strategy_name": "minervini_vcp", "sig": sig}
    path = tmp_path / "backlog.json"

    orch_mod._save_swing_cash_backlog(path, [orch_mod._swing_cash_backlog_entry(candidate, 0)])
    loaded = orch_mod._load_swing_cash_backlog(path)[0]
    rebuilt = Signal(
        loaded["strategy_name"],
        Direction(loaded["direction"]),
        float(loaded["entry_price"]),
        float(loaded["sl"]),
        float(loaded["target"]),
    )

    assert rebuilt == sig
