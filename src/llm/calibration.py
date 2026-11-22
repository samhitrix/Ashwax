"""
LLM Calibration Engine â€” Brier score per confidence bucket (spec Â§8.9).

Weekly Sunday 20:00: bucket confidence â†’ actual WR â†’ flag if gap >15%.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.infrastructure.logger import get_logger

logger = get_logger("llm.calibration")
_IST = ZoneInfo("Asia/Kolkata")
_DEFAULT_LOG = Path("logs/llm_calibration.json")

_BUCKET_SIZE = 0.05
_MISCALIBRATION_THRESHOLD = 0.15


@dataclass
class CalibrationRecord:
    llm_confidence: float
    was_win: bool


@dataclass
class BucketStats:
    bucket_label: str
    predicted_confidence: float
    actual_win_rate: float
    count: int
    gap: float
    is_miscalibrated: bool


@dataclass
class CalibrationResult:
    strategy: str
    brier_score: float
    buckets: list[BucketStats]
    overall_miscalibrated: bool
    worst_gap: float


class CalibrationEngine:
    """
    Computes LLM confidence calibration from historical trade records.

    Usage:
        engine = CalibrationEngine()
        engine.add("VCP", llm_confidence=0.75, was_win=True)
        result = engine.evaluate("VCP")
    """

    def __init__(
        self,
        recalibrate_on_brier_above: float = 0.25,
        weekly_calibration_enabled: bool = True,
    ) -> None:
        self._records: dict[str, list[CalibrationRecord]] = defaultdict(list)
        self._recalibrate_on_brier_above = recalibrate_on_brier_above
        self._weekly_calibration_enabled = weekly_calibration_enabled

    def add(self, strategy: str, llm_confidence: float, was_win: bool) -> None:
        self._records[strategy].append(CalibrationRecord(llm_confidence, was_win))

    def save_to_json(
        self,
        strategies: list[str] | None = None,
        log_path: Path = _DEFAULT_LOG,
    ) -> None:
        """Write calibration results for all (or listed) strategies to logs/llm_calibration.json."""
        targets = strategies or list(self._records.keys())
        results = [asdict(self.evaluate(s)) for s in targets]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": datetime.now(_IST).strftime("%d-%m-%Y %H:%M:%S.000"),
            "results": results,
        }
        tmp = log_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(log_path)
        logger.info("llm_calibration saved strategies=%d path=%s", len(results), log_path)

    def evaluate(self, strategy: str) -> CalibrationResult:
        records = self._records.get(strategy, [])
        if not records:
            return CalibrationResult(
                strategy=strategy,
                brier_score=0.0,
                buckets=[],
                overall_miscalibrated=False,
                worst_gap=0.0,
            )
        brier = _brier_score(records)
        buckets = _bucket_stats(records)
        worst_gap = max((b.gap for b in buckets), default=0.0)
        if worst_gap > _MISCALIBRATION_THRESHOLD:
            logger.warning(
                "llm_calibration strategy=%s brier=%.3f worst_gap=%.0f%%",
                strategy,
                brier,
                worst_gap * 100,
            )
        return CalibrationResult(
            strategy=strategy,
            brier_score=round(brier, 4),
            buckets=buckets,
            overall_miscalibrated=worst_gap > _MISCALIBRATION_THRESHOLD,
            worst_gap=round(worst_gap, 4),
        )


def _brier_score(records: list[CalibrationRecord]) -> float:
    return sum((r.llm_confidence - (1.0 if r.was_win else 0.0)) ** 2 for r in records) / len(
        records
    )


def _bucket_stats(records: list[CalibrationRecord]) -> list[BucketStats]:
    buckets: dict[float, list[CalibrationRecord]] = defaultdict(list)
    for r in records:
        key = round(int(r.llm_confidence / _BUCKET_SIZE) * _BUCKET_SIZE, 2)
        buckets[key].append(r)

    stats: list[BucketStats] = []
    for start in sorted(buckets):
        recs = buckets[start]
        predicted = start + _BUCKET_SIZE / 2
        actual_wr = sum(1 for r in recs if r.was_win) / len(recs)
        gap = abs(predicted - actual_wr)
        stats.append(
            BucketStats(
                bucket_label=f"{start:.2f}-{min(1.0, start + _BUCKET_SIZE):.2f}",
                predicted_confidence=round(predicted, 3),
                actual_win_rate=round(actual_wr, 4),
                count=len(recs),
                gap=round(gap, 4),
                is_miscalibrated=gap > _MISCALIBRATION_THRESHOLD,
            )
        )
    return stats
