"""Outcome memory shared by the LLM agents: paper and live closed
trades are tagged and aggregated separately, and the swing prompt now carries them.
Made-up symbols/values only."""

from __future__ import annotations

from datetime import date
from typing import Any

from src.strategies.llm_intraday.pipeline import _summarise_outcomes
from src.strategies.llm_swing.pipeline import build_prompt


def _r(mode: str | None, r: float | None, sym: str = "TESTSYM") -> dict[str, Any]:
    row = {
        "entry_date": date(2026, 1, 5),
        "symbol": sym,
        "direction": "long",
        "exit_reason": "target",
        "r": r,
    }
    if mode is not None:
        row["mode"] = mode
    return row


def test_empty() -> None:
    assert _summarise_outcomes([]) == "(no closed trades yet)"


def test_modes_aggregated_separately_and_tagged() -> None:
    text = _summarise_outcomes([_r("LIVE", 1.0), _r("PAPER", -1.0), _r("PAPER", -0.5)])
    assert "LIVE last 1 trades: wins=1/1 avg_R=+1.00" in text
    assert "PAPER last 2 trades: wins=0/2 avg_R=-0.75" in text
    assert "[LIVE] 2026-01-05 TESTSYM LONG +1.00R" in text
    assert "small sample" in text


def test_untagged_rows_keep_old_format() -> None:
    text = _summarise_outcomes([_r(None, 0.5)])
    assert "  last 1 trades: wins=1/1 avg_R=+0.50" in text
    assert "[" not in text


def test_missing_r_does_not_crash() -> None:
    assert "?R" in _summarise_outcomes([_r("LIVE", None)])


def test_swing_prompt_includes_outcomes_block() -> None:
    p = build_prompt({"regime": "BULL"}, [], 14, [_r("PAPER", 0.8, "FAKECO")])
    assert "YOUR_RECENT_CLOSED_TRADES" in p
    assert "[PAPER] 2026-01-05 FAKECO LONG +0.80R" in p


def test_swing_prompt_without_outcomes() -> None:
    assert "(no closed trades yet)" in build_prompt({}, [], 14)
