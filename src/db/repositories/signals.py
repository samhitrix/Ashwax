"""SignalRepository -- the `signals` decision journal (migration 084).

One row per crm.evaluate() call: what was proposed, every gate's verdict, and whether it was
approved. What became of an approved signal is `signal_outcomes` (joins trades / paper_trades on
signal_ref)."""

from __future__ import annotations

import json
from typing import Any


class SignalRepository:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def record(self, row: dict[str, Any]) -> int:
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO signals
                        (symbol, strategy_name, direction, entry_price, sl_price, target_price,
                         quality_score, gate_results, llm_confidence, llm_reasoning, disposition,
                         signal_id, is_paper, segment, asset_class, intended_quantity,
                         final_quantity, blocking_gate, blocking_reason, regime, risk_rupees,
                         stop_loss_pct, target_pct)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,
                            $19,$20,$21,$22,$23)
                    RETURNING id
                    """,
                    row["symbol"],
                    row["strategy_name"],
                    row["direction"],
                    row.get("entry_price"),
                    row.get("sl_price"),
                    row.get("target_price"),
                    row.get("quality_score"),
                    json.dumps(row.get("gate_results") or []),
                    row.get("llm_confidence"),
                    row.get("llm_reasoning"),
                    row["disposition"],
                    row.get("signal_id"),
                    row.get("is_paper"),
                    row.get("segment"),
                    row.get("asset_class"),
                    row.get("intended_quantity"),
                    row.get("final_quantity"),
                    row.get("blocking_gate"),
                    row.get("blocking_reason"),
                    row.get("regime"),
                    row.get("risk_rupees"),
                    row.get("stop_loss_pct"),
                    row.get("target_pct"),
                )
            )

    async def outcomes(self, outcome: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            "SELECT * FROM signal_outcomes WHERE ($1::text IS NULL OR outcome = $1) "
            "ORDER BY created_at DESC, id DESC LIMIT $2",
            outcome,
            max(1, min(int(limit), 2000)),
        )
        return [dict(r) for r in rows]
