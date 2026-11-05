"""
Layer 6 — Promotion Engine + Rollback (spec §9).

Promotes parameter changes atomically; 20-trade rollback if WR drops >10%.
Writes config/strategy_{NAME}.json atomically via tmp rename.

DB persistence (optional `repo`, injected by orchestrator._evolution_loop) makes
promotions and rollback-tracking survive a restart — previously this class was
constructed fresh, in-memory only, every time the evolution loop started, so a
promotion's rollback tracking was silently lost on every launcher restart.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from src.infrastructure.logger import get_logger

logger = get_logger(__name__)

_ROLLBACK_WINDOW = 20
_ROLLBACK_WR_DROP = 0.10


@dataclass
class PromotionRecord:
    strategy: str
    promoted_at: str
    param_snapshot: dict[str, object]
    baseline_wr: float
    prior_param_snapshot: dict[str, object] = field(default_factory=dict)
    post_promotion_pnl: list[float] = field(default_factory=list)
    rolled_back: bool = False


class PromotionEngine:
    """
    Manages strategy parameter promotions with automatic rollback.

    Usage:
        engine = PromotionEngine(config_dir=Path("config"), repo=learning_repo)
        engine.promote("VCP", new_params, baseline_wr=0.55, prior_params={...})
        rolled_back = await engine.record_and_check_rollback("VCP", pnl_r=1.5)
    """

    def __init__(self, config_dir: Path = Path("config"), repo: Any | None = None) -> None:
        self._config_dir = config_dir
        self._repo = repo
        self._records: dict[str, PromotionRecord] = {}

    def promote(
        self,
        strategy: str,
        params: dict[str, object],
        baseline_wr: float,
        prior_params: Mapping[str, object] | None = None,
    ) -> None:
        self._records[strategy] = PromotionRecord(
            strategy=strategy,
            promoted_at=datetime.now().isoformat(),
            param_snapshot=params,
            baseline_wr=baseline_wr,
            prior_param_snapshot=dict(prior_params or {}),
        )
        self._write_config(strategy, params)
        logger.info("promoted strategy=%s baseline_wr=%.2f", strategy, baseline_wr)

    async def persist_promotion(self, strategy: str) -> None:
        """DB-persist the most recent promote() call for `strategy`. Separate from
        promote() itself (which stays synchronous — evolution.py's evolve() calls
        it inline, non-async) — the orchestrator's evolution loop awaits this right
        after a synchronous evolve() call returns a promotion."""
        if self._repo is None:
            return
        rec = self._records.get(strategy)
        if rec is None:
            return
        await self._repo.insert_promotion(
            strategy=rec.strategy,
            promoted_at=rec.promoted_at,
            param_snapshot=rec.param_snapshot,
            prior_param_snapshot=rec.prior_param_snapshot,
            baseline_wr=rec.baseline_wr,
        )

    def record_trade(self, strategy: str, pnl_r: float) -> None:
        if strategy in self._records:
            self._records[strategy].post_promotion_pnl.append(pnl_r)

    def check_rollback(self, strategy: str) -> bool:
        rec = self._records.get(strategy)
        if not rec or rec.rolled_back:
            return False
        pnl = rec.post_promotion_pnl
        if len(pnl) < _ROLLBACK_WINDOW:
            return False
        recent = pnl[-_ROLLBACK_WINDOW:]
        current_wr = sum(1 for r in recent if r > 0) / _ROLLBACK_WINDOW
        if current_wr < rec.baseline_wr - _ROLLBACK_WR_DROP:
            rec.rolled_back = True
            logger.warning(
                "rollback strategy=%s current_wr=%.2f vs baseline=%.2f",
                strategy,
                current_wr,
                rec.baseline_wr,
            )
            return True
        return False

    def active_strategies(self) -> list[str]:
        """Strategies with a promotion still being rollback-evaluated (not yet
        rolled back) — the evolution loop's per-tick sweep only needs to fetch
        new trades for these."""
        return [s for s, r in self._records.items() if not r.rolled_back]

    async def restore_from_repo(self) -> None:
        """Repopulate in-memory promotion records from the DB at boot, so
        rollback tracking (trade count so far, baseline WR) survives a restart
        instead of starting over from zero every time the process restarts."""
        if self._repo is None:
            return
        rows = await self._repo.load_active_promotions()
        for row in rows:
            import json as _json

            strategy = row["strategy_name"]
            self._records[strategy] = PromotionRecord(
                strategy=strategy,
                promoted_at=str(row["promoted_at"]),
                param_snapshot=_json.loads(row["param_snapshot"])
                if isinstance(row["param_snapshot"], str)
                else dict(row["param_snapshot"]),
                baseline_wr=float(row["baseline_wr"]),
                prior_param_snapshot=_json.loads(row["prior_param_snapshot"])
                if isinstance(row["prior_param_snapshot"], str)
                else dict(row["prior_param_snapshot"]),
            )
            try:
                self._records[strategy].post_promotion_pnl = list(
                    await self._repo.load_promotion_trades(strategy)
                )
            except Exception:
                logger.debug("promotion_trade_restore_failed strategy=%s", strategy)

    def prior_params(self, strategy: str) -> dict[str, object] | None:
        """The pre-promotion param snapshot, for the caller to write back to
        config/strategy_{NAME}.json on a check_rollback()==True."""
        rec = self._records.get(strategy)
        return dict(rec.prior_param_snapshot) if rec else None

    async def record_and_check_rollback(self, strategy: str, pnl_r: float) -> bool:
        """record_trade + check_rollback + (if a repo is injected) persist both the
        trade and any resulting rollback — single entry point for the orchestrator's
        per-tick sweep of promoted strategies' new closed trades."""
        self.record_trade(strategy, pnl_r)
        rolled_back = self.check_rollback(strategy)
        if self._repo is not None and strategy in self._records:
            try:
                await self._repo.record_promotion_trade(strategy, pnl_r)
                if rolled_back:
                    await self._repo.mark_rolled_back(strategy)
            except Exception:
                logger.debug("promotion_trade_persist_failed strategy=%s", strategy)
        return rolled_back

    @staticmethod
    async def adjust_confidence_threshold(repo: Any, strategy: str, new_threshold: float) -> None:
        """Persist a G9 confidence-threshold adjustment — routed through
        PromotionEngine per spec §8.9 ("adjust minimum confidence thresholds via
        promotion engine"), but reuses the exact same DB-persisted, restart-safe
        write equity calibration already had (learning_repo.upsert_calibration_threshold)
        rather than inventing new persistence. A staticmethod, not an instance method:
        the calibration loop and the evolution loop are separate long-running loops
        with their own repo references — this avoids forcing them to share one
        PromotionEngine instance just to route one write through this class.
        """
        if repo is None:
            return
        await repo.upsert_calibration_threshold(strategy, new_threshold)

    def _write_config(self, strategy: str, params: dict[str, object]) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        path = self._config_dir / f"strategy_{strategy}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(params, indent=2), encoding="utf-8")
        tmp.replace(path)
