"""
Per-strategy LLM (G9) gate policy — read from config/strategies_registry.json.

  llm_gate_required : bool  — when true the strategy may NOT trade (paper or live)
                              without a genuine LLM verdict. Missing key -> True
                              (a new strategy fails closed).
  llm_originated    : bool  — the strategy's ideas come from an LLM (llm_swing_agent,
                              llm_intraday_agent). Always gated; setting
                              llm_gate_required:false on one is a config error.

The result populates CRMContext.llm_gate_required, which crm.py's G9 enforces for
every path (swing, intraday, options) in both paper and live.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REGISTRY = Path(__file__).parent.parent.parent / "config" / "strategies_registry.json"


class GatePolicyError(ValueError):
    """Contradictory llm_gate_required / llm_originated settings."""


def parse_gate_policy(entries: list[dict[str, Any]]) -> tuple[dict[str, bool], set[str]]:
    """(required-by-strategy, llm-originated strategy names) from registry entries."""
    required: dict[str, bool] = {}
    originated: set[str] = set()
    for e in entries:
        name = e.get("name")
        if not name:
            continue
        if e.get("llm_originated", False):
            if e.get("llm_gate_required", True) is False:
                raise GatePolicyError(
                    f"{name}: llm_originated strategies cannot set llm_gate_required=false"
                )
            originated.add(name)
            required[name] = True
        else:
            required[name] = bool(e.get("llm_gate_required", True))
    return required, originated


def load_gate_policy(path: Path | None = None) -> tuple[dict[str, bool], set[str]]:
    raw = json.loads((path or _REGISTRY).read_text(encoding="utf-8"))
    return parse_gate_policy(list(raw.get("strategies", [])))
