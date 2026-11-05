"""
Nightly Setup Scanner (spec Â§8.10).

Schedule: 21:00-23:00 IST on trading days.
Output: logs/nightly_setups.json â€” top-20 setups, available at 07:00.

Per symbol: stage, VCP tightness (0-10), volume dryup (0-10),
RS rank (0-100 pct), distance to pivot. Composite score 1-5.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.infrastructure.logger import get_logger

logger = get_logger(__name__)
_IST = ZoneInfo("Asia/Kolkata")
_LOG_PATH = Path("logs/nightly_setups.json")
_TOP_N = 20


@dataclass
class SetupScore:
    symbol: str
    score: float
    stage: str
    vcp_tightness: float
    volume_dryup: float
    rs_rank: float
    distance_to_pivot_pct: float
    base_depth_duration: float  # weeks in base (price within 15% of 52w high)


@dataclass
class NightlyScanResult:
    top_setups: list[SetupScore]
    computed_at: str


class NightlyScanner:
    """
    Scores symbols for next-day setups.

    Usage:
        result = NightlyScanner().scan(prices_df, volumes_df, rs_ranks)
    """

    def __init__(self, log_path: Path = _LOG_PATH) -> None:
        self._log_path = log_path

    def scan(
        self,
        prices_df: pd.DataFrame,
        volumes_df: pd.DataFrame,
        rs_ranks: dict[str, float] | None = None,
    ) -> NightlyScanResult:
        if rs_ranks is None:
            rs_ranks = {}
        scores: list[SetupScore] = []
        for symbol in prices_df.columns:
            prices = prices_df[symbol].dropna()
            vols = (
                volumes_df[symbol].dropna()
                if symbol in volumes_df.columns
                else pd.Series(dtype=float)
            )
            if len(prices) < 50:
                continue
            scores.append(_score_symbol(symbol, prices, vols, rs_ranks.get(symbol, 50.0)))

        top = sorted(scores, key=lambda x: x.score, reverse=True)[:_TOP_N]
        result = NightlyScanResult(top_setups=top, computed_at=datetime.now(_IST).isoformat())
        self._write(result)
        logger.info("nightly_scan symbols=%d top=%d", len(scores), len(top))
        return result

    def _write(self, result: NightlyScanResult) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "top_setups": [asdict(s) for s in result.top_setups],
            "computed_at": result.computed_at,
        }
        tmp = self._log_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._log_path)


def _score_symbol(symbol: str, prices: pd.Series, volumes: pd.Series, rs_rank: float) -> SetupScore:
    stage = _classify_stage(prices)
    vcp = _vcp_tightness(prices)
    vol_dryup = _volume_dryup(volumes)
    pivot_dist = _distance_to_pivot(prices)
    base_dur = _base_depth_duration(prices)

    score = 1.0
    if stage == "STAGE_2":
        score += 1.5
    score += 1.0 if vcp >= 7 else (0.5 if vcp >= 5 else 0.0)
    score += 0.5 if vol_dryup >= 7 else 0.0
    score += 0.5 if rs_rank >= 80 else 0.0
    score += 0.5 if pivot_dist <= 0.03 else 0.0
    # Bonus for mature bases (5+ weeks) with shallow depth
    score += 0.5 if base_dur >= 5 else 0.0

    return SetupScore(
        symbol=symbol,
        score=round(min(5.0, score), 2),
        stage=stage,
        vcp_tightness=round(vcp, 2),
        volume_dryup=round(vol_dryup, 2),
        rs_rank=round(rs_rank, 2),
        distance_to_pivot_pct=round(pivot_dist, 4),
        base_depth_duration=round(base_dur, 2),
    )


def _classify_stage(prices: pd.Series) -> str:
    if len(prices) < 50:
        return "STAGE_1"
    current = float(prices.iloc[-1])
    ma50 = float(prices.rolling(50).mean().iloc[-1])
    ma10 = float(prices.rolling(10).mean().iloc[-1])
    if current > ma50 and ma10 > ma50:
        ma50_prev = float(prices.rolling(50).mean().iloc[-20]) if len(prices) >= 70 else ma50
        return "STAGE_2" if ma50 > ma50_prev else "STAGE_3"
    return "STAGE_4" if current < ma50 else "STAGE_1"


def _vcp_tightness(prices: pd.Series) -> float:
    if len(prices) < 20:
        return 0.0
    recent = prices.iloc[-20:]
    high, low = float(recent.max()), float(recent.min())
    if high == 0:
        return 0.0
    return min(10.0, max(0.0, 10.0 - (high - low) / high * 100))


def _volume_dryup(volumes: pd.Series) -> float:
    if len(volumes) < 10:
        return 5.0
    avg = float(volumes.iloc[-50:].mean() if len(volumes) >= 50 else volumes.mean())
    recent = float(volumes.iloc[-5:].mean())
    if avg == 0:
        return 5.0
    return max(0.0, min(10.0, (1.0 - recent / avg) * 14.3))


def _distance_to_pivot(prices: pd.Series) -> float:
    pivot = float(prices.iloc[-min(252, len(prices)) :].max())
    current = float(prices.iloc[-1])
    return max(0.0, (pivot - current) / pivot) if pivot != 0 else 1.0


def _base_depth_duration(prices: pd.Series) -> float:
    """Count weeks the price has been within 15% of its 52-week high (i.e. in a base)."""
    if len(prices) < 5:
        return 0.0
    lookback = prices.iloc[-min(252, len(prices)) :]
    peak = float(lookback.max())
    if peak == 0:
        return 0.0
    in_base = (peak - prices) / peak <= 0.15
    # Count contiguous bars at the tail that are in-base, convert to weeks (5 bars/week)
    count = 0
    for v in reversed(in_base.values):
        if v:
            count += 1
        else:
            break
    return count / 5.0
