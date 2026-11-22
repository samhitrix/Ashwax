"""
Slippage Tracker â€” per-order slippage analysis with auto-adjustment.

Per spec Â§8.11:
  Per order: expected_price vs actual_fill_price â†’ slippage_pct
  Bucketed by: order_type, time_of_day, ADV bucket
  Auto-adjust: if 10-day rolling LIMIT slippage > 0.3% â†’ widen limit buffer by 0.1%
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from src.infrastructure.logger import get_logger

logger = get_logger(__name__)

_LIMIT_SLIPPAGE_THRESHOLD = 0.003  # 0.3%
_LIMIT_BUFFER_ADJUST = 0.001  # 0.1%
_ROLLING_WINDOW = 10


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL_M"


class TimeOfDay(StrEnum):
    MORNING = "morning"
    MID_MORNING = "mid_morning"
    MIDDAY = "midday"
    AFTERNOON = "afternoon"
    PRE_CLOSE = "pre_close"


class ADVBucket(StrEnum):
    LOW = "low"  # < 50k shares/day
    MID = "mid"  # 50k-500k
    HIGH = "high"  # > 500k


@dataclass
class SlippageRecord:
    order_id: str
    symbol: str
    order_type: OrderType
    expected_price: float
    fill_price: float
    slippage_pct: float
    time_of_day: TimeOfDay
    adv_bucket: ADVBucket
    recorded_at: datetime


@dataclass
class SlippageStats:
    order_type: str
    time_of_day: str
    adv_bucket: str
    count: int
    avg_slippage_pct: float
    rolling_10d_avg: float
    auto_adjust_triggered: bool
    suggested_buffer_pct: float


class SlippageTracker:
    """
    Tracks per-order slippage and detects when LIMIT buffer needs widening.

    Usage:
        tracker = SlippageTracker()
        tracker.record_fill(...)
        stats = tracker.stats_for("LIMIT", "morning", "mid", current_buffer_pct=0.002)
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], list[float]] = defaultdict(list)

    def record_fill(
        self,
        order_id: str,
        symbol: str,
        order_type: OrderType,
        expected_price: float,
        fill_price: float,
        time_of_day: TimeOfDay,
        adv_bucket: ADVBucket,
    ) -> SlippageRecord:
        slippage_pct = _compute_slippage(expected_price, fill_price)
        key = (order_type.value, time_of_day.value, adv_bucket.value)
        self._records[key].append(slippage_pct)

        if abs(slippage_pct) > 0.005:
            logger.warning(
                "slippage_high order_id=%s symbol=%s type=%s slip=%.3f%%",
                order_id,
                symbol,
                order_type,
                slippage_pct * 100,
            )

        return SlippageRecord(
            order_id=order_id,
            symbol=symbol,
            order_type=order_type,
            expected_price=expected_price,
            fill_price=fill_price,
            slippage_pct=slippage_pct,
            time_of_day=time_of_day,
            adv_bucket=adv_bucket,
            recorded_at=datetime.now(),
        )

    def stats_for(
        self,
        order_type: str,
        time_of_day: str,
        adv_bucket: str,
        current_buffer_pct: float = 0.0,
    ) -> SlippageStats:
        key = (order_type, time_of_day, adv_bucket)
        all_vals = self._records.get(key, [])
        if not all_vals:
            return SlippageStats(
                order_type=order_type,
                time_of_day=time_of_day,
                adv_bucket=adv_bucket,
                count=0,
                avg_slippage_pct=0.0,
                rolling_10d_avg=0.0,
                auto_adjust_triggered=False,
                suggested_buffer_pct=current_buffer_pct,
            )

        avg = sum(all_vals) / len(all_vals)
        rolling = all_vals[-_ROLLING_WINDOW:]
        rolling_avg = sum(rolling) / len(rolling)

        auto_adjust = (
            order_type == OrderType.LIMIT.value and rolling_avg > _LIMIT_SLIPPAGE_THRESHOLD
        )
        suggested = current_buffer_pct + _LIMIT_BUFFER_ADJUST if auto_adjust else current_buffer_pct

        if auto_adjust:
            logger.warning(
                "slippage_auto_adjust LIMIT time=%s adv=%s rolling=%.3f%% â†’ +0.1%% buffer",
                time_of_day,
                adv_bucket,
                rolling_avg * 100,
            )

        return SlippageStats(
            order_type=order_type,
            time_of_day=time_of_day,
            adv_bucket=adv_bucket,
            count=len(all_vals),
            avg_slippage_pct=round(avg, 6),
            rolling_10d_avg=round(rolling_avg, 6),
            auto_adjust_triggered=auto_adjust,
            suggested_buffer_pct=round(suggested, 4),
        )

    def check_limit_auto_adjust(
        self,
        time_of_day: str,
        adv_bucket: str,
        current_buffer_pct: float,
    ) -> bool:
        return self.stats_for(
            OrderType.LIMIT.value, time_of_day, adv_bucket, current_buffer_pct
        ).auto_adjust_triggered

    def all_stats(self) -> list[SlippageStats]:
        """Return SlippageStats for every tracked (order_type, time_of_day, adv_bucket) bucket."""
        return [self.stats_for(ot, tod, adv) for (ot, tod, adv) in self._records]


def _compute_slippage(expected: float, fill: float) -> float:
    if expected == 0:
        return 0.0
    return (fill - expected) / expected
