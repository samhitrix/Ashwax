"""
Sector Rotation Tracker â€” classifies NIFTY sectors as LEADING/EMERGING/LAGGING/DECLINING.

Schedule: weekly Sunday 18:00 + daily update (spec Â§8.7).

Scoring per sector:
  - 4-week RS vs NIFTY50 (primary signal)
  - Volume trend (rising = bullish)
  - Stage analysis of sector ETF (above/below MA50)

Classification:
  LEADING   â€” RS > 0 AND volume_trend >= 0 AND above MA50
  EMERGING  â€” RS > 0 AND (volume or stage negative)
  LAGGING   â€” RS <= 0 AND (volume or stage positive)
  DECLINING â€” RS <= 0 AND volume_trend < 0 AND below MA50

Actions (applied by CRM):
  LEADING   â†’ confidence +0.05 for stocks in sector
  DECLINING â†’ block new entries
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.infrastructure.logger import get_logger

logger = get_logger(__name__)
_IST = ZoneInfo("Asia/Kolkata")
_LOG_PATH = Path("logs/sector_rotation.json")
_CONFIDENCE_BOOST = 0.05
_MA_PERIOD = 50


class SectorStatus(StrEnum):
    LEADING = "LEADING"
    EMERGING = "EMERGING"
    LAGGING = "LAGGING"
    DECLINING = "DECLINING"


@dataclass
class SectorRecord:
    name: str
    status: SectorStatus
    rs_4w: float
    volume_trend: float
    above_ma50: bool
    confidence_delta: float
    blocks_entries: bool


@dataclass
class SectorRotationResult:
    sectors: list[SectorRecord]
    computed_at: str


class SectorRotationTracker:
    """
    Tracks sector rotation across NIFTY sectors.

    Usage:
        tracker = SectorRotationTracker()
        result = tracker.update(sector_prices, nifty, sector_volumes)
    """

    def __init__(self, log_path: Path = _LOG_PATH) -> None:
        self._log_path = log_path

    def update(
        self,
        sector_prices: pd.DataFrame,
        nifty: pd.Series,
        sector_volumes: pd.DataFrame | None = None,
    ) -> SectorRotationResult:
        records: list[SectorRecord] = []
        for sector in sector_prices.columns:
            prices = sector_prices[sector].dropna()
            if len(prices) < _MA_PERIOD + 1:
                continue
            volumes = (
                sector_volumes[sector].dropna()
                if sector_volumes is not None and sector in sector_volumes.columns
                else None
            )
            records.append(_score_sector(sector, prices, nifty, volumes))

        result = SectorRotationResult(
            sectors=records,
            computed_at=datetime.now(_IST).isoformat(),
        )
        self._write(result)
        leading = [r.name for r in records if r.status == SectorStatus.LEADING]
        declining = [r.name for r in records if r.status == SectorStatus.DECLINING]
        logger.info("sector_rotation leading=%s declining=%s", leading[:5], declining[:5])
        summary = "  ".join(f"{r.name}:{r.status.value}(rs={r.rs_4w:.4f})" for r in records)
        logger.info("sector_rotation_computed %s", summary)
        return result

    def _write(self, result: SectorRotationResult) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sectors": [{**asdict(r), "status": r.status.value} for r in result.sectors],
            "computed_at": result.computed_at,
        }
        tmp = self._log_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._log_path)


def _score_sector(
    name: str,
    prices: pd.Series,
    nifty: pd.Series,
    volumes: pd.Series | None,
) -> SectorRecord:
    rs_4w = _compute_rs_4w(prices, nifty)
    above_ma50 = _is_above_ma(prices, _MA_PERIOD)
    volume_trend = _compute_volume_trend(volumes) if volumes is not None else 0.0
    status = _classify(rs_4w, volume_trend, above_ma50)
    return SectorRecord(
        name=name,
        status=status,
        rs_4w=round(rs_4w, 4),
        volume_trend=round(volume_trend, 4),
        above_ma50=above_ma50,
        confidence_delta=_CONFIDENCE_BOOST if status == SectorStatus.LEADING else 0.0,
        blocks_entries=(status == SectorStatus.DECLINING),
    )


def _classify(rs_4w: float, volume_trend: float, above_ma50: bool) -> SectorStatus:
    if rs_4w > 0 and volume_trend >= 0 and above_ma50:
        return SectorStatus.LEADING
    if rs_4w > 0:
        return SectorStatus.EMERGING
    if rs_4w <= 0 and not above_ma50 and volume_trend < 0:
        return SectorStatus.DECLINING
    return SectorStatus.LAGGING


def _compute_rs_4w(prices: pd.Series, nifty: pd.Series) -> float:
    lookback = 20
    aligned, nifty_aligned = prices.align(nifty, join="inner")
    if len(aligned) < lookback + 1:
        return 0.0
    sector_ret = float(aligned.iloc[-1]) / float(aligned.iloc[-lookback]) - 1
    nifty_ret = float(nifty_aligned.iloc[-1]) / float(nifty_aligned.iloc[-lookback]) - 1
    return sector_ret - nifty_ret


def _is_above_ma(prices: pd.Series, period: int) -> bool:
    if len(prices) < period:
        return False
    ma = float(prices.rolling(period).mean().iloc[-1])
    return float(prices.iloc[-1]) > ma


def _compute_volume_trend(volumes: pd.Series) -> float:
    """Linear slope of last 20 bars of volume."""
    lookback = 20
    if len(volumes) < lookback + 1:
        return 0.0
    recent = list(volumes.iloc[-lookback:].dropna())
    if len(recent) < 2:
        return 0.0
    n = len(recent)
    mean_x = (n - 1) / 2
    mean_y = sum(recent) / n
    num = sum((i - mean_x) * (recent[i] - mean_y) for i in range(n))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return float(num / den) if den != 0 else 0.0
