"""LearningRepository — trade_insights, autopsy_attributions, llm_calibration_history."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


class LearningRepository:
    """Data access for learning and calibration tables."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def insert_insight(
        self,
        trade_id: int,
        was_signal_valid: bool | None,
        failure_was_environmental: bool | None,
        what_i_would_do_differently: str | None,
        generalizable_insight: str | None,
        confidence_in_insight: Decimal | None,
        applicable_when: str | None,
        regime_at_trade: str | None,
        sector_at_trade: str | None,
        llm_model_used: str | None,
        raw_llm_response: str | None,
    ) -> int:
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO trade_insights
                        (trade_id, was_signal_valid, failure_was_environmental,
                         what_i_would_do_differently, generalizable_insight,
                         confidence_in_insight, applicable_when,
                         regime_at_trade, sector_at_trade,
                         llm_model_used, raw_llm_response)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    RETURNING id
                    """,
                    trade_id,
                    was_signal_valid,
                    failure_was_environmental,
                    what_i_would_do_differently,
                    generalizable_insight,
                    confidence_in_insight,
                    applicable_when,
                    regime_at_trade,
                    sector_at_trade,
                    llm_model_used,
                    raw_llm_response,
                )
            )

    async def insights_for_regime(self, regime: str, limit: int = 10) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    """
                    SELECT * FROM trade_insights
                     WHERE regime_at_trade = $1
                     ORDER BY created_at DESC LIMIT $2
                    """,
                    regime,
                    limit,
                )
            )

    async def insert_attribution(
        self,
        trade_id: int,
        axis: str,
        attribution: str,
        confidence: Decimal | None = None,
    ) -> int:
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO autopsy_attributions (trade_id, axis, attribution, confidence)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id
                    """,
                    trade_id,
                    axis,
                    attribution,
                    confidence,
                )
            )

    async def insert_calibration(
        self,
        period_start: date,
        period_end: date,
        model_used: str,
        confidence_bucket: str,
        predicted_wr: Decimal | None,
        actual_wr: Decimal | None,
        trade_count: int | None,
        brier_score: Decimal | None,
    ) -> int:
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO llm_calibration_history
                        (period_start, period_end, model_used, confidence_bucket,
                         predicted_wr, actual_wr, trade_count, brier_score)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                    RETURNING id
                    """,
                    period_start,
                    period_end,
                    model_used,
                    confidence_bucket,
                    predicted_wr,
                    actual_wr,
                    trade_count,
                    brier_score,
                )
            )

    async def latest_calibration(self, model_used: str) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    """
                    SELECT * FROM llm_calibration_history
                     WHERE model_used = $1
                     ORDER BY period_end DESC LIMIT 10
                    """,
                    model_used,
                )
            )

    async def insert_setup(
        self,
        symbol: str,
        strategy_name: str,
        quality_score: float,
        key_level: float,
        sl_level: float,
        target_level: float,
        notes: str = "",
    ) -> int:
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO nightly_setups
                        (symbol, strategy_name, quality_score,
                         key_level, sl_level, target_level, notes)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                    RETURNING id
                    """,
                    symbol,
                    strategy_name,
                    quality_score,
                    key_level,
                    sl_level,
                    target_level,
                    notes,
                )
            )

    async def recent_setups(self, limit: int = 50) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    "SELECT * FROM nightly_setups ORDER BY created_at DESC LIMIT $1",
                    limit,
                )
            )

    async def recent_insights(self, strategy: str, limit: int = 10) -> list[asyncpg.Record]:
        """Return most recent trade_insights for a strategy (used by CausalMemory retrieval)."""
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    """
                    SELECT ti.*, t.strategy_name
                      FROM trade_insights ti
                      JOIN trades t ON ti.trade_id = t.id
                     WHERE t.strategy_name = $1
                     ORDER BY ti.created_at DESC LIMIT $2
                    """,
                    strategy,
                    limit,
                )
            )

    async def upsert_calibration_threshold(self, strategy: str, min_confidence: float) -> None:
        """Persist a raised calibration threshold so it survives restarts."""
        from decimal import Decimal as _D

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO calibration_thresholds (strategy, min_confidence, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (strategy) DO UPDATE
                  SET min_confidence = EXCLUDED.min_confidence,
                      updated_at     = NOW()
                """,
                strategy,
                _D(str(round(min_confidence, 4))),
            )

    async def load_calibration_thresholds(self) -> dict[str, float]:
        """Load all persisted calibration thresholds at startup."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT strategy, min_confidence FROM calibration_thresholds")
            return {r["strategy"]: float(r["min_confidence"]) for r in rows}

    async def insert_promotion(
        self,
        strategy: str,
        promoted_at: str,
        param_snapshot: dict[str, object],
        prior_param_snapshot: dict[str, object],
        baseline_wr: float,
    ) -> None:
        """Persist a Layer 6 promotion (migration 068) — makes PromotionEngine's
        rollback tracking survive a restart, instead of resetting every time
        _evolution_loop is constructed fresh."""
        import json as _json
        from decimal import Decimal as _D

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO promotion_records
                    (strategy_name, promoted_at, param_snapshot, prior_param_snapshot,
                     baseline_wr, rolled_back, rolled_back_at)
                VALUES ($1, $2, $3, $4, $5, FALSE, NULL)
                ON CONFLICT (strategy_name) DO UPDATE
                  SET promoted_at          = EXCLUDED.promoted_at,
                      param_snapshot        = EXCLUDED.param_snapshot,
                      prior_param_snapshot  = EXCLUDED.prior_param_snapshot,
                      baseline_wr           = EXCLUDED.baseline_wr,
                      rolled_back           = FALSE,
                      rolled_back_at        = NULL
                """,
                strategy,
                promoted_at,
                _json.dumps(param_snapshot),
                _json.dumps(prior_param_snapshot),
                _D(str(round(baseline_wr, 4))),
            )

    async def record_promotion_trade(self, strategy: str, pnl_r: float) -> None:
        from decimal import Decimal as _D

        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO promotion_trade_log (strategy_name, pnl_r) VALUES ($1, $2)",
                strategy,
                _D(str(round(pnl_r, 4))),
            )

    async def mark_rolled_back(self, strategy: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE promotion_records
                   SET rolled_back = TRUE, rolled_back_at = NOW()
                 WHERE strategy_name = $1
                """,
                strategy,
            )

    async def load_active_promotions(self) -> list[asyncpg.Record]:
        """Non-rolled-back promotions, for the evolution loop to resume rollback
        tracking after a restart."""
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch("SELECT * FROM promotion_records WHERE rolled_back = FALSE")
            )

    async def load_promotion_trades(self, strategy: str) -> list[float]:
        """pnl_r values already recorded for `strategy` since its promotion —
        lets PromotionEngine.restore_from_repo() rebuild its rollback-evaluation
        window (trade count so far) after a restart instead of starting at 0."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT pnl_r FROM promotion_trade_log WHERE strategy_name = $1 "
                "ORDER BY recorded_at ASC",
                strategy,
            )
            return [float(r["pnl_r"]) for r in rows]
