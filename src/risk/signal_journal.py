"""Writes every crm.evaluate() decision to the `signals` table (migration 084).

crm.evaluate() is synchronous and sits on the hot path of every entry, so the sink only builds the
row and schedules the insert; a failed insert is logged and raised as a deduped CRITICAL alert
(a silent failure is exactly how the table stayed empty), and never affects the decision."""

from __future__ import annotations

import asyncio
from typing import Any

from src.infrastructure.logger import get_logger

_log = get_logger("crm")


def build_row(signal: Any, ctx: Any, decision: Any) -> dict[str, Any]:
    from src.risk.crm import mode_of, segment_of

    entry = float(getattr(signal, "entry_price", 0.0) or 0.0)
    sl_pct = float(signal.stop_loss_pct or 0.0)
    tgt_pct = float(signal.target_pct or 0.0)
    is_buy = str(signal.direction).upper() == "BUY"
    sl = entry * (1 - sl_pct if is_buy else 1 + sl_pct) if entry else None
    tgt = entry * (1 + tgt_pct if is_buy else 1 - tgt_pct) if entry else None
    meta = signal.meta or {}
    return {
        "symbol": signal.symbol,
        "strategy_name": signal.strategy_name,
        "direction": "long" if is_buy else "short",  # signals.direction CHECK domain
        "entry_price": round(entry, 4) if entry else None,
        "sl_price": round(sl, 4) if sl else None,
        "target_price": round(tgt, 4) if tgt else None,
        "quality_score": meta.get("setup_quality_score", meta.get("quality_score")),
        "gate_results": [
            {
                "gate": g.gate_id,
                "decision": str(g.decision.value if hasattr(g.decision, "value") else g.decision),
                "reason": g.reason,
                "modified_quantity": g.modified_quantity,
            }
            for g in decision.gate_results
        ],
        "llm_confidence": meta.get("llm_confidence"),
        "llm_reasoning": meta.get("llm_reason"),
        "disposition": "APPROVED" if decision.approved else "BLOCKED",
        "signal_id": signal.signal_id,
        "is_paper": mode_of(signal, ctx) == "paper",
        "segment": segment_of(signal),
        "asset_class": signal.asset_class,
        "intended_quantity": signal.intended_quantity,
        "final_quantity": decision.final_quantity,
        "blocking_gate": decision.blocking_gate or None,
        "blocking_reason": decision.blocking_reason or None,
        "regime": signal.regime,
        "risk_rupees": signal.risk_rupees,
        "stop_loss_pct": sl_pct,
        "target_pct": tgt_pct,
    }


class SignalJournal:
    def __init__(self, repo: Any) -> None:
        self._repo = repo
        self._tasks: set[asyncio.Task[None]] = set()

    def __call__(self, signal: Any, ctx: Any, decision: Any) -> None:
        row = build_row(signal, ctx, decision)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log.error(
                "signal_journal_no_event_loop signal=%s -- decision NOT recorded", row["signal_id"]
            )
            return
        task = loop.create_task(self._write(row))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write(self, row: dict[str, Any]) -> None:
        try:
            await self._repo.record(row)
        except Exception as exc:  # noqa: BLE001
            _log.error("signal_journal_write_failed signal=%s err=%s", row.get("signal_id"), exc)
            try:
                from src.infrastructure import operator_alerts

                operator_alerts.raise_alert(
                    f"signals INSERT is failing ({str(exc)[:120]}) -- CRM decisions are not "
                    "being saved",
                    source="db:signals",
                    dedupe_seconds=3600,
                )
            except Exception:  # noqa: BLE001
                pass
