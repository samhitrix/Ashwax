"""
TradeMetadata — typed contract for the meta dict that flows from signal → DB.

Every field that the orchestrator packs into CRMSignal.meta and that
_persist_entry writes to trades/paper_trades is declared here.  Using this
TypedDict (with total=False so all fields are optional) gives:

  - mypy errors at the construction site if a key is misspelled
  - a single source-of-truth for what the DB row expects
  - grep-ability: searching for TradeMetadata instantly shows every call site

Usage:
    from src.execution.trade_meta import TradeMetadata

    meta: TradeMetadata = {
        "autonomy_mode": "assisted",
        "strategy_name": strategy_name,
        "regime_at_entry": shared.get("regime", "UNKNOWN"),
        "llm_confidence": 0.90,
        "llm_flag": "CONFIRM",
        "llm_reason": "ok",
    }
"""

from __future__ import annotations

from typing import Any


class TradeMetadata(dict[str, Any]):  # noqa: UP006
    """Typed wrapper for the signal-level meta dict.

    Inherits from dict so it can be passed anywhere a dict is accepted.
    Keys below are the only ones _persist_entry reads; extras are ignored.
    """

    # ── orchestrator-level ────────────────────────────────────────────────
    # strategy_name:  name from strategy registry (e.g. "minervini_vcp")
    # regime_at_entry: regime snapshot at signal time (e.g. "BULL")
    # autonomy_mode:  "assisted" | "autonomous"

    # ── G9 LLM verdict ────────────────────────────────────────────────────
    # llm_confidence:  float 0–1
    # llm_flag:        "CONFIRM" | "NEUTRAL" | "REJECT"
    # llm_reason:      short human-readable string
    # llm_evaluated:   bool — False when LLM was unavailable (fail-open)

    REQUIRED_FOR_PAPER_TRADES = frozenset({"strategy_name", "regime_at_entry"})

    def missing_fields(self) -> set[str]:
        """Return any fields that are required but absent or empty."""
        return {k for k in self.REQUIRED_FOR_PAPER_TRADES if not self.get(k)}


def build_trade_meta(
    *,
    autonomy_mode: str = "assisted",
    strategy_name: str,
    regime_at_entry: str,
    llm_confidence: float | None = None,
    llm_flag: str | None = None,
    llm_reason: str | None = None,
    llm_evaluated: bool = False,
) -> TradeMetadata:
    """Construct a fully-typed meta dict — the single construction site.

    Call this instead of building raw dicts so required fields can never
    be silently omitted.
    """
    m: TradeMetadata = TradeMetadata(
        autonomy_mode=autonomy_mode,
        strategy_name=strategy_name,
        regime_at_entry=regime_at_entry,
    )
    if llm_confidence is not None:
        m["llm_confidence"] = llm_confidence
    if llm_flag is not None:
        m["llm_flag"] = llm_flag
    if llm_reason is not None:
        m["llm_reason"] = llm_reason
    m["llm_evaluated"] = llm_evaluated
    return m
