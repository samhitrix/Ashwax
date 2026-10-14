"""Tests for the news-catalyst-momentum entry gate (_evaluate_news_catalyst_signal).

This is the strategy built 2026-08-05 as a deliberate pivot away from a 20th
ORB/mean-reversion variant (both categories already proven to lose to costs
across 25+ backtested variants). Entry here is gated on a live NewsOpportunity
(already pre-filtered by OpportunityScanner to HIGH magnitude + BULLISH +
BOOST + confidence>=0.65) plus price-above-session-VWAP and a 2x volume surge
— never on the news_catalyst_momentum.json entry_conditions, which are
deliberately inert (RSI can never exceed 100).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from src.orchestrator import _evaluate_news_catalyst_signal
from src.strategies.registry import StrategyRegistry

_IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(scope="module")
def compiled():
    reg = StrategyRegistry()
    reg.reload(df=None)
    c = reg.get("news_catalyst_momentum")
    assert c is not None
    return c


def _make_df(now: datetime, *, surge: bool, rising: bool) -> pd.DataFrame:
    """20 bars of today's session, tz-aware, ending at `now`. Last bar has a
    volume surge (3x) and a price pop when surge=True/rising=True."""
    n = 20
    start = now - timedelta(minutes=5 * (n - 1))
    idx = pd.date_range(start, periods=n, freq="5min")
    base = 100.0
    closes = [base + i * 0.05 for i in range(n)]  # mild drift
    if rising:
        closes[-1] = closes[-2] * 1.015  # last bar pops 1.5% above session VWAP
    else:
        closes[-1] = base * 0.97  # last bar drops well below session VWAP
    volumes = [1000] * n
    if surge:
        volumes[-1] = 3500  # ~3.5x the 20-bar average
    highs = [c * 1.002 for c in closes]
    lows = [c * 0.998 for c in closes]
    opens = [closes[i - 1] if i > 0 else closes[0] for i in range(n)]
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )


def _now() -> datetime:
    return datetime(2026, 8, 6, 10, 30, tzinfo=_IST)


def test_no_opportunity_returns_none(compiled) -> None:
    now = _now()
    df = _make_df(now, surge=True, rising=True)
    shared: dict = {"news_opportunities": {}}
    assert _evaluate_news_catalyst_signal(compiled, shared, "RELIANCE", df, now) is None


def test_stale_opportunity_returns_none(compiled) -> None:
    now = _now()
    df = _make_df(now, surge=True, rising=True)
    shared = {
        "news_opportunities": {
            "RELIANCE": {"confidence_boost": 0.8, "ts": time.time() - 1201}  # 20m1s old
        }
    }
    assert _evaluate_news_catalyst_signal(compiled, shared, "RELIANCE", df, now) is None


def test_daily_cap_blocks_third_trade(compiled) -> None:
    now = _now()
    df = _make_df(now, surge=True, rising=True)
    shared = {
        "news_opportunities": {"RELIANCE": {"confidence_boost": 0.8, "ts": time.time()}},
        "news_catalyst_traded": {now.date().isoformat(): 2},
    }
    assert _evaluate_news_catalyst_signal(compiled, shared, "RELIANCE", df, now) is None


def test_no_volume_surge_returns_none(compiled) -> None:
    now = _now()
    df = _make_df(now, surge=False, rising=True)
    shared = {"news_opportunities": {"RELIANCE": {"confidence_boost": 0.8, "ts": time.time()}}}
    assert _evaluate_news_catalyst_signal(compiled, shared, "RELIANCE", df, now) is None


def test_price_below_vwap_returns_none(compiled) -> None:
    now = _now()
    df = _make_df(now, surge=True, rising=False)
    shared = {"news_opportunities": {"RELIANCE": {"confidence_boost": 0.8, "ts": time.time()}}}
    assert _evaluate_news_catalyst_signal(compiled, shared, "RELIANCE", df, now) is None


def test_qualifying_setup_returns_long_signal(compiled) -> None:
    now = _now()
    df = _make_df(now, surge=True, rising=True)
    shared = {"news_opportunities": {"RELIANCE": {"confidence_boost": 0.8, "ts": time.time()}}}
    sig = _evaluate_news_catalyst_signal(compiled, shared, "RELIANCE", df, now)
    assert sig is not None
    assert sig.direction.value == "LONG"
    assert sig.sl < sig.entry_price < sig.target


def test_symbol_stripped_of_exchange_prefix(compiled) -> None:
    now = _now()
    df = _make_df(now, surge=True, rising=True)
    shared = {"news_opportunities": {"RELIANCE": {"confidence_boost": 0.8, "ts": time.time()}}}
    sig = _evaluate_news_catalyst_signal(compiled, shared, "NSE:RELIANCE", df, now)
    assert sig is not None


def test_bearish_catalyst_returns_short_signal(compiled) -> None:
    """AVOID/HALT + HIGH + BEARISH opportunities (from the 2026-08-05 scanner
    extension) drive a SHORT, not a LONG — the stock must be trading below
    session VWAP with a volume surge, mirroring the long-side confirmation."""
    now = _now()
    df = _make_df(now, surge=True, rising=False)  # price drops below VWAP
    shared = {
        "news_opportunities": {
            "RELIANCE": {"confidence_boost": 0.8, "ts": time.time(), "direction": "SHORT"}
        }
    }
    sig = _evaluate_news_catalyst_signal(compiled, shared, "RELIANCE", df, now)
    assert sig is not None
    assert sig.direction.value == "SHORT"
    assert sig.target < sig.entry_price < sig.sl


def test_bearish_catalyst_blocked_when_price_above_vwap(compiled) -> None:
    now = _now()
    df = _make_df(now, surge=True, rising=True)  # price still above VWAP
    shared = {
        "news_opportunities": {
            "RELIANCE": {"confidence_boost": 0.8, "ts": time.time(), "direction": "SHORT"}
        }
    }
    assert _evaluate_news_catalyst_signal(compiled, shared, "RELIANCE", df, now) is None
