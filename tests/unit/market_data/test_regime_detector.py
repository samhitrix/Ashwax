"""Tests for RegimeDetector."""

import json

import numpy as np
import pandas as pd
from src.market_data.regime_detector import RegimeDetector, RegimeState


def _make_nifty(n: int = 250, trend: str = "bull") -> pd.DataFrame:
    rng = np.random.default_rng(0)
    drift = 0.003 if trend == "bull" else (-0.003 if trend == "bear" else 0.0)
    closes = [22000.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.normal(drift, 0.01)))
    arr = np.array(closes)
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": arr,
            "high": arr * 1.005,
            "low": arr * 0.995,
            "close": arr,
            "volume": np.ones(n) * 1_000_000,
        },
        index=idx,
    )


def test_bull_trend_normal_vix(tmp_path):
    df = _make_nifty(250, "bull")
    detector = RegimeDetector(log_path=tmp_path / "r.json")
    state = detector.detect(df, vix=18.0, ad_ratio=0.65)
    assert state.trend in ("STRONG_BULL", "BULL", "NEUTRAL")
    assert state.volatility == "NORMAL"
    assert state.breadth == "EXPANDING"


def test_extreme_vix_triggers_unknown(tmp_path):
    df = _make_nifty(250, "bull")
    detector = RegimeDetector(log_path=tmp_path / "r.json")
    state = detector.detect(df, vix=45.0, ad_ratio=0.5)
    assert state.trend == "UNKNOWN"
    assert state.confidence <= 0.4


def test_low_ad_ratio_triggers_unknown(tmp_path):
    df = _make_nifty(250, "bull")
    detector = RegimeDetector(log_path=tmp_path / "r.json")
    state = detector.detect(df, vix=15.0, ad_ratio=0.05)
    assert state.trend == "UNKNOWN"


def test_state_written_to_json(tmp_path):
    df = _make_nifty(250, "bull")
    log_file = tmp_path / "logs" / "regime.json"
    detector = RegimeDetector(log_path=log_file)
    state = detector.detect(df, vix=17.0, ad_ratio=0.60)
    assert log_file.exists()
    data = json.loads(log_file.read_text())
    assert data["trend"] == state.trend
    assert "timestamp" in data


def test_detect_populates_moving_averages(tmp_path):
    df = _make_nifty(250, "bull")
    detector = RegimeDetector(log_path=tmp_path / "r.json")
    state = detector.detect(df, vix=17.0, ad_ratio=0.60)
    close = df["close"]
    assert state.ma50 == float(close.rolling(50).mean().iloc[-1])
    assert state.ma150 == float(close.rolling(150).mean().iloc[-1])
    assert state.ma200 == float(close.rolling(200).mean().iloc[-1])
    assert state.ma50 > 0 and state.ma200 > 0


def test_last_returns_most_recent_state(tmp_path):
    df = _make_nifty(250, "bull")
    detector = RegimeDetector(log_path=tmp_path / "r.json")
    assert detector.last() is None
    state = detector.detect(df, vix=17.0, ad_ratio=0.60)
    assert detector.last() == state


def test_unknown_not_allows_new_entries():
    s = RegimeState(
        trend="UNKNOWN",
        volatility="EXTREME",
        breadth="CONTRACTING",
        phase="UNKNOWN",
        confidence=0.2,
        recommended_posture="CASH",
    )
    assert not s.allows_new_entries()
    assert s.is_unknown()


def test_bull_allows_new_entries():
    s = RegimeState(
        trend="BULL",
        volatility="NORMAL",
        breadth="EXPANDING",
        phase="MARKUP",
        confidence=0.8,
        recommended_posture="NORMAL",
    )
    assert s.allows_new_entries()
    assert not s.is_unknown()


def test_bear_no_new_entries():
    s = RegimeState(
        trend="BEAR",
        volatility="HIGH",
        breadth="CONTRACTING",
        phase="MARKDOWN",
        confidence=0.7,
        recommended_posture="CASH",
    )
    assert not s.allows_new_entries()
