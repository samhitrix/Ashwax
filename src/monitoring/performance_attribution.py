"""
Performance Attribution Engine â€” decomposes P&L across 7 dimensions.

Schedule: daily 15:45 IST (spec Â§8.8).

Dimensions:
  1. By Strategy (WR, avg R, profit factor, expectancy)
  2. By Time Window (morning/midday/afternoon)
  3. By Market Regime at entry
  4. By Sector
  5. By LLM Confidence Bucket (0.60-0.65, ..., 0.90-1.00)
  6. By Hold Duration (intraday/<3d/3-7d/>7d)
  7. By Volume at Entry (below_avg/avg/above_avg/surge)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.infrastructure.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TradeRecord:
    strategy: str
    pnl_r: float
    entry_time: datetime
    regime_at_entry: str
    sector: str
    llm_confidence: float
    hold_hours: float
    volume_at_entry: str  # BELOW_AVG | AVG | ABOVE_AVG | SURGE


@dataclass
class DimensionStats:
    count: int
    win_rate: float
    avg_r: float
    profit_factor: float
    expectancy: float
    total_r: float


@dataclass
class AttributionResult:
    by_strategy: dict[str, DimensionStats] = field(default_factory=dict)
    by_time_window: dict[str, DimensionStats] = field(default_factory=dict)
    by_regime: dict[str, DimensionStats] = field(default_factory=dict)
    by_sector: dict[str, DimensionStats] = field(default_factory=dict)
    by_confidence_bucket: dict[str, DimensionStats] = field(default_factory=dict)
    by_hold_duration: dict[str, DimensionStats] = field(default_factory=dict)
    by_volume: dict[str, DimensionStats] = field(default_factory=dict)
    computed_at: str = ""


class PerformanceAttributor:
    """
    Computes attribution across 7 dimensions from a list of TradeRecord.

    Usage:
        result = PerformanceAttributor().attribute(trades)
    """

    def attribute(self, trades: list[TradeRecord]) -> AttributionResult:
        if not trades:
            logger.info("attribution: no trades")
            return AttributionResult(computed_at=datetime.now().isoformat())

        groups: dict[str, dict[str, list[TradeRecord]]] = {
            d: {}
            for d in (
                "strategy",
                "time_window",
                "regime",
                "sector",
                "confidence_bucket",
                "hold_duration",
                "volume",
            )
        }
        for t in trades:
            _add(groups["strategy"], t.strategy, t)
            _add(groups["time_window"], _time_window(t.entry_time), t)
            _add(groups["regime"], t.regime_at_entry, t)
            _add(groups["sector"], t.sector, t)
            _add(groups["confidence_bucket"], _confidence_bucket(t.llm_confidence), t)
            _add(groups["hold_duration"], _hold_duration(t.hold_hours), t)
            _add(groups["volume"], t.volume_at_entry, t)

        result = AttributionResult(
            by_strategy={k: _stats(v) for k, v in groups["strategy"].items()},
            by_time_window={k: _stats(v) for k, v in groups["time_window"].items()},
            by_regime={k: _stats(v) for k, v in groups["regime"].items()},
            by_sector={k: _stats(v) for k, v in groups["sector"].items()},
            by_confidence_bucket={k: _stats(v) for k, v in groups["confidence_bucket"].items()},
            by_hold_duration={k: _stats(v) for k, v in groups["hold_duration"].items()},
            by_volume={k: _stats(v) for k, v in groups["volume"].items()},
            computed_at=datetime.now().isoformat(),
        )
        logger.info(
            "attribution computed trades=%d strategies=%d sectors=%d",
            len(trades),
            len(result.by_strategy),
            len(result.by_sector),
        )
        return result


def _add(groups: dict[str, list[TradeRecord]], key: str, trade: TradeRecord) -> None:
    if key not in groups:
        groups[key] = []
    groups[key].append(trade)


def _stats(trades: list[TradeRecord]) -> DimensionStats:
    n = len(trades)
    if n == 0:
        return DimensionStats(0, 0.0, 0.0, 0.0, 0.0, 0.0)

    wins = [t.pnl_r for t in trades if t.pnl_r > 0]
    losses = [abs(t.pnl_r) for t in trades if t.pnl_r <= 0]

    win_rate = len(wins) / n
    total_r = sum(t.pnl_r for t in trades)
    avg_r = total_r / n
    gross_win = sum(wins)
    gross_loss = sum(losses)
    profit_factor: float = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    return DimensionStats(
        count=n,
        win_rate=round(win_rate, 4),
        avg_r=round(avg_r, 4),
        profit_factor=round(profit_factor, 4) if profit_factor != float("inf") else profit_factor,
        expectancy=round(expectancy, 4),
        total_r=round(total_r, 4),
    )


def _time_window(entry: datetime) -> str:
    t = entry.hour * 60 + entry.minute
    if t < 9 * 60 + 30:
        return "pre_open"
    if t < 10 * 60 + 30:
        return "morning"
    if t < 12 * 60:
        return "mid_morning"
    if t < 13 * 60 + 30:
        return "midday"
    if t < 14 * 60 + 30:
        return "afternoon"
    return "pre_close"


def _confidence_bucket(confidence: float) -> str:
    if confidence < 0.60:
        return "below_0.60"
    bucket = int(confidence * 20) / 20  # floor to nearest 0.05
    high = min(1.0, bucket + 0.05)
    return f"{bucket:.2f}-{high:.2f}"


def _hold_duration(hours: float) -> str:
    if hours < 8:
        return "intraday"
    if hours < 3 * 24:
        return "lt_3_days"
    if hours < 7 * 24:
        return "3_to_7_days"
    return "gt_7_days"
