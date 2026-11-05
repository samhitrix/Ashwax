"""
Market Regime Detector.

Inputs: NIFTY50 OHLCV DataFrame (200 days), VIX float, advance-decline ratio float.
Output: RegimeState written to logs/market_regime.json + returned to callers.

Detection logic (from architecture spec):
  NIFTY50 vs MA50 / MA150 / MA200:
    all above  â†’ STRONG_BULL
    two above  â†’ BULL
    mixed      â†’ NEUTRAL
    two below  â†’ BEAR
    all below  â†’ STRONG_BEAR

  Confidence < 0.5 OR VIX > 40 OR A-D ratio < 0.10 â†’ UNKNOWN

Writes atomically to logs/market_regime.json on every update.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.infrastructure.logger import get_logger

_IST = ZoneInfo("Asia/Kolkata")
_LOG_PATH = Path("logs/market_regime.json")
logger = get_logger(__name__)


@dataclass
class RegimeState:
    trend: str  # STRONG_BULL | BULL | NEUTRAL | BEAR | STRONG_BEAR | UNKNOWN
    volatility: str  # LOW | NORMAL | HIGH | EXTREME
    breadth: str  # EXPANDING | CONTRACTING | NEUTRAL
    phase: str  # ACCUMULATION | MARKUP | DISTRIBUTION | MARKDOWN | UNKNOWN
    confidence: float
    recommended_posture: str  # AGGRESSIVE | NORMAL | DEFENSIVE | CASH
    timestamp: str = ""
    ma50: float = 0.0
    ma150: float = 0.0
    ma200: float = 0.0
    adx: float = 0.0
    atr_pct: float = 0.0

    def is_unknown(self) -> bool:
        return self.trend == "UNKNOWN"

    def allows_new_entries(self) -> bool:
        return self.trend not in ("BEAR", "STRONG_BEAR", "UNKNOWN")


def _classify_volatility(vix: float) -> str:
    if vix < 15:
        return "LOW"
    if vix < 20:
        return "NORMAL"
    if vix < 30:
        return "HIGH"
    return "EXTREME"


def _classify_breadth(ad_ratio: float) -> str:
    """ad_ratio = advancing / (advancing + declining), 0.0â€“1.0."""
    if ad_ratio > 0.60:
        return "EXPANDING"
    if ad_ratio < 0.40:
        return "CONTRACTING"
    return "NEUTRAL"


def _classify_phase(trend: str, breadth: str) -> str:
    if trend == "STRONG_BULL" and breadth == "EXPANDING":
        return "MARKUP"
    if trend in ("BULL", "NEUTRAL") and breadth == "EXPANDING":
        return "ACCUMULATION"
    if trend in ("BEAR", "STRONG_BEAR") and breadth == "CONTRACTING":
        return "MARKDOWN"
    if trend in ("BULL", "NEUTRAL") and breadth == "CONTRACTING":
        return "DISTRIBUTION"
    return "UNKNOWN"


def _recommended_posture(trend: str, volatility: str) -> str:
    if trend == "STRONG_BULL" and volatility in ("LOW", "NORMAL"):
        return "AGGRESSIVE"
    if trend in ("BULL", "STRONG_BULL"):
        return "NORMAL"
    if trend == "NEUTRAL":
        return "DEFENSIVE"
    return "CASH"


def _compute_atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    """ATR as a % of current close (14-period, simple rolling mean of True Range)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(
        axis=1
    )
    atr = tr.rolling(period).mean().iloc[-1]
    current_close = float(close.iloc[-1])
    if not current_close or pd.isna(atr):
        return 0.0
    return round(float(atr) / current_close * 100, 3)


def _compute_adx(df: pd.DataFrame, period: int = 14) -> float:
    """Standard 14-period ADX (Wilder's smoothing via an EMA with alpha=1/period)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_high, prev_low, prev_close = high.shift(1), low.shift(1), close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = ((up_move > down_move) & (up_move > 0)).astype(float) * up_move.clip(lower=0)
    minus_dm = ((down_move > up_move) & (down_move > 0)).astype(float) * down_move.clip(lower=0)

    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(
        axis=1
    )

    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr

    di_sum = (plus_di + minus_di).replace(0, float("nan"))
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()

    value = adx.iloc[-1]
    return round(float(value), 2) if pd.notna(value) else 0.0


def _confidence_score(
    trend: str,
    vix: float,
    ad_ratio: float,
    above_count: int,
) -> float:
    """Rough confidence: MA agreement + VIX stability + breadth alignment."""
    ma_conf = above_count / 3.0
    if trend in ("BEAR", "STRONG_BEAR"):
        ma_conf = 1.0 - ma_conf

    vix_penalty = 0.0
    if vix > 30:
        vix_penalty = 0.3
    elif vix > 20:
        vix_penalty = 0.1

    breadth_bonus = 0.1 if ad_ratio > 0.6 or ad_ratio < 0.4 else 0.0
    return max(0.0, min(1.0, ma_conf - vix_penalty + breadth_bonus))


class RegimeDetector:
    """Detects market regime from NIFTY50 OHLCV, VIX, and advance-decline data."""

    def __init__(
        self,
        log_path: Path = _LOG_PATH,
        vix_bull_max: float = 18.0,
        vix_bear_min: float = 25.0,
        breadth_bull_min_pct: float = 60.0,
        unknown_regime_go_cash: bool = True,
    ) -> None:
        self._log_path = log_path
        self._lock = threading.Lock()
        self._last: RegimeState | None = None
        self._vix_bull_max = vix_bull_max
        self._vix_bear_min = vix_bear_min
        self._breadth_bull_min_pct = breadth_bull_min_pct
        self._unknown_regime_go_cash = unknown_regime_go_cash

    def detect(
        self,
        nifty_df: pd.DataFrame,
        vix: float,
        ad_ratio: float,
    ) -> RegimeState:
        """
        nifty_df: OHLCV DataFrame with DatetimeIndex, at least 200 rows.
        vix:      India VIX current value.
        ad_ratio: NSE advance / (advance + decline), 0.0â€“1.0.
        """
        close = nifty_df["close"]
        current = float(close.iloc[-1])

        ma50 = float(close.rolling(50).mean().iloc[-1])
        ma150 = float(close.rolling(150).mean().iloc[-1])
        ma200 = float(close.rolling(200).mean().iloc[-1])

        above = sum([current > ma50, current > ma150, current > ma200])
        below = 3 - above

        if above == 3:
            trend = "STRONG_BULL"
        elif above == 2:
            trend = "BULL"
        elif below == 3:
            trend = "STRONG_BEAR"
        elif below == 2:
            trend = "BEAR"
        else:
            trend = "NEUTRAL"

        volatility = _classify_volatility(vix)
        breadth = _classify_breadth(ad_ratio)
        confidence = _confidence_score(trend, vix, ad_ratio, above)

        unknown_trigger = confidence < 0.5 or vix > 40 or ad_ratio < 0.10
        if unknown_trigger:
            trend = "UNKNOWN"
            confidence = min(confidence, 0.4)

        phase = _classify_phase(trend, breadth)
        posture = _recommended_posture(trend, volatility)

        state = RegimeState(
            trend=trend,
            volatility=volatility,
            breadth=breadth,
            phase=phase,
            confidence=round(confidence, 3),
            recommended_posture=posture,
            timestamp=datetime.now(_IST).isoformat(),
            ma50=ma50,
            ma150=ma150,
            ma200=ma200,
            adx=_compute_adx(nifty_df),
            atr_pct=_compute_atr_pct(nifty_df),
        )

        with self._lock:
            self._last = state
            self._write(state)

        if trend == "UNKNOWN":
            logger.critical(
                "REGIME=UNKNOWN vix=%.1f ad_ratio=%.2f confidence=%.2f â€” "
                "zero new entries, tighten all SLs",
                vix,
                ad_ratio,
                confidence,
            )
        else:
            logger.info(
                "regime=%s vol=%s breadth=%s confidence=%.2f posture=%s",
                trend,
                volatility,
                breadth,
                confidence,
                posture,
            )

        return state

    def last(self) -> RegimeState | None:
        with self._lock:
            return self._last

    def _write(self, state: RegimeState) -> None:
        """Atomic write via temp file rename."""
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._log_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
        tmp.replace(self._log_path)
