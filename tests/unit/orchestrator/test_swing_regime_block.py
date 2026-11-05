"""llm_swing_agent must not open positions in a blocked regime (config llm_swing.block_regimes).

2026-09-28 (user-approved): the agent proposed delivery longs every day while the regime was
STRONG_BEAR. Enforced twice: the LLM is not called at all, and CRM G4 blocks any entry that arrives."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from src import orchestrator as orch


def test_helper_reads_the_configured_regimes() -> None:
    cfg = {"block_regimes": ["STRONG_BEAR"]}
    assert orch._swing_regime_blocked(cfg, "STRONG_BEAR") is True
    assert orch._swing_regime_blocked(cfg, "BEAR") is False
    assert orch._swing_regime_blocked(cfg, "UNKNOWN") is False  # unknown regime is not a block


def test_empty_or_missing_config_blocks_nothing() -> None:
    assert orch._swing_regime_blocked({}, "STRONG_BEAR") is False
    assert orch._swing_regime_blocked({"block_regimes": []}, "STRONG_BEAR") is False
    assert orch._swing_regime_blocked({"block_regimes": None}, "STRONG_BEAR") is False


def test_shipped_block_regimes_agree_between_working_and_example_files() -> None:
    # 2026-10-06 operator decision: STRONG_BEAR block removed (paper and live). What must
    # hold regardless of the chosen list: both shipped files carry the SAME list.
    root = Path(__file__).parents[3]
    lists = [
        json.loads((root / rel).read_text(encoding="utf-8"))["llm_swing"]["block_regimes"]
        for rel in ("config/global.json", "config/examples/global.json")
    ]
    assert lists[0] == lists[1]


def test_the_swing_loop_skips_the_llm_call_before_booking_anything() -> None:
    src = inspect.getsource(orch._llm_swing_scan_loop)
    skip = src.index("_swing_regime_blocked(gcfg, _regime_now)")
    assert skip < src.index('_call_log_repo.record("swing"')  # nothing booked when blocked
    assert skip < src.index("run_daily_reasoning(")
    assert "regime_{_regime_now}_blocked" in src  # the reason is logged


def test_the_swing_execute_path_hands_the_block_to_the_crm() -> None:
    src = inspect.getsource(orch._execute_swing_signal)
    assert 'crm_ctx.blocked_regimes["llm_swing_agent"]' in src
