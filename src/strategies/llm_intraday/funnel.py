"""One-line-per-stage audit trail (and terminal alerts) for LLM-originated trade ideas.

Every step an idea takes -- call sent, response, idea published, validation, entry check,
regime/breadth/CRM/G9 gates, execution -- emits ONE greppable line:

    llm_funnel agent=<a> stage=<s> outcome=<o> symbol=<SYM|-> reason=<why|-> k=v ...

so `grep llm_funnel logs/intraday.log` (or swing.log) reconstructs the whole path of every
call and idea, and a step that went missing is visible as a gap. Alert-worthy outcomes (proposed, where an
idea died, or that it was placed) are also pushed as an LLM_FUNNEL alert, once per
(day, agent, symbol, stage, outcome, reason). Added 2026-09-24: a day of LLM calls produced
ideas that vanished with no record of what happened to them.
"""

from __future__ import annotations

import logging
from typing import Any

# Outcomes pushed to the operator: the idea was proposed, or its journey ended (killed,
# expired, refused, blocked, failed, placed). Intermediate steps ("sent", "signal",
# "waiting", "resting", "live_ideas") are logged only -- they would be noise on a phone.
# The subset that means "the idea did NOT go through" - these also land on the Alerts tab.
FAILURE_OUTCOMES = frozenset({"rejected", "expired", "refused", "blocked", "failed"})
TERMINAL_OUTCOMES = frozenset(
    {"proposed", "rejected", "expired", "refused", "blocked", "failed", "placed"}
)


def funnel_log(
    log: Any,
    *,
    agent: str,
    stage: str,
    outcome: str,
    symbol: str = "",
    reason: str = "",
    level: int = logging.INFO,
    **detail: Any,
) -> None:
    """Emit the one canonical line for this step. Never raises."""
    try:
        fields = [
            f"agent={agent}",
            f"stage={stage}",
            f"outcome={outcome}",
            f"symbol={symbol or '-'}",
            f"reason={reason or '-'}",
            *(f"{k}={v}" for k, v in detail.items()),
        ]
        log.log(level, "llm_funnel %s", " ".join(fields))
    except Exception:  # noqa: BLE001 — logging must never break trading
        pass


def funnel_alert(
    shared: dict[str, Any],
    *,
    day: str,
    agent: str,
    stage: str,
    outcome: str,
    symbol: str = "",
    reason: str = "",
    detail: str = "",
    is_paper: bool = True,
) -> bool:
    """Push a terminal-step alert, once per (day, agent, symbol, stage, outcome, reason).
    Returns True if a push was submitted. Never raises."""
    if outcome not in TERMINAL_OUTCOMES:
        return False
    try:
        seen: set[tuple[str, ...]] = shared.setdefault("llm_funnel_alerted", set())
        key = (day, agent, symbol, stage, outcome, reason)
        if key in seen:
            return False
        seen.add(key)
        from src.notifications import EventKind, notify

        notify(
            EventKind.LLM_FUNNEL,
            is_paper=is_paper,
            symbol=symbol,
            agent=agent,
            stage=stage,
            outcome=outcome,
            reason=reason,
            detail=detail,
        )
        return True
    except Exception:  # noqa: BLE001 — notify() must never break the caller
        return False
