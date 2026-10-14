"""
Layer 6 â€” Promotion Engine + Rollback (spec Â§9).

Promotes parameter changes atomically; 20-trade rollback if WR drops >10%.
Writes config/strategy_{NAME}.json atomically via tmp rename.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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
    post_promotion_pnl: list[float] = field(default_factory=list)
    rolled_back: bool = False


class PromotionEngine:
    """
    Manages strategy parameter promotions with automatic rollback.

    Usage:
        engine = PromotionEngine(config_dir=Path("config"))
        engine.promote("VCP", new_params, baseline_wr=0.55)
        engine.record_trade("VCP", pnl_r=1.5)
        did_rollback = engine.check_rollback("VCP")
    """

    def __init__(
        self,
        config_dir: Path = Path("config"),
        promotion_min_paper_trades: int = 20,
        promotion_min_win_rate: float = 0.55,
        promotion_min_profit_factor: float = 1.5,
    ) -> None:
        self._config_dir = config_dir
        self._records: dict[str, PromotionRecord] = {}
        self._promotion_min_paper_trades = promotion_min_paper_trades
        self._promotion_min_win_rate = promotion_min_win_rate
        self._promotion_min_profit_factor = promotion_min_profit_factor

    def promote(self, strategy: str, params: dict[str, object], baseline_wr: float) -> None:
        self._records[strategy] = PromotionRecord(
            strategy=strategy,
            promoted_at=datetime.now().isoformat(),
            param_snapshot=params,
            baseline_wr=baseline_wr,
        )
        self._write_config(strategy, params)
        logger.info("promoted strategy=%s baseline_wr=%.2f", strategy, baseline_wr)

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

    def _write_config(self, strategy: str, params: dict[str, object]) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        path = self._config_dir / f"strategy_{strategy}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(params, indent=2), encoding="utf-8")
        tmp.replace(path)
