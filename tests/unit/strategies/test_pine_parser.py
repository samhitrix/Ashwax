"""Tests for PineParser."""

import pytest
from src.strategies.parsers.pine_parser import PineParseError, PineParser
from src.strategies.uss.schema import Direction, SLMethod, TargetMethod, Timeframe

# ---------------------------------------------------------------------------
# Sample Pine Script sources
# ---------------------------------------------------------------------------

_ATR_PINE = """\
//@version=5
strategy("VCP Breakout", overlay=true)
sma200 = ta.sma(close, 200)
sma50  = ta.sma(close, 50)
atr_val = ta.atr(14) * 2.0
stop_price = close - atr_val
// rr=2.5
target_price = close + atr_val * 2.5
long_cond = close > sma200 and close > sma50
if long_cond
    strategy.entry("Long", strategy.long)
strategy.exit("Exit Long", "Long", stop=stop_price, limit=target_price)
"""

_RECENT_LOW_PINE = """\
//@version=5
strategy("Swing Long", overlay=true)
low_stop = ta.lowest(low, 20)
if close > ta.sma(close, 50)
    strategy.entry("Buy", strategy.long)
strategy.exit("Exit", stop=low_stop)
"""

_FIXED_PCT_PINE = """\
//@version=5
strategy("Simple Pct Stop", overlay=true)
stop = close * 0.95
if close > ta.ema(close, 20)
    strategy.entry("Long", strategy.long)
strategy.exit("Exit", stop=stop)
"""

_SHORT_PINE = """\
//@version=5
strategy("Short Mean Rev", overlay=true)
if close < ta.sma(close, 200)
    strategy.entry("Short", strategy.short)
strategy.exit("Cover", stop=close * 1.05)
"""

_CROSSOVER_PINE = """\
//@version=5
strategy("MA Cross", overlay=true)
fast = ta.ema(close, 10)
slow = ta.ema(close, 30)
if ta.crossover(fast, slow)
    strategy.entry("Long", strategy.long)
strategy.exit("Exit", stop=close * 0.97)
"""

_PARSER = PineParser()


# ---------------------------------------------------------------------------
# Normal cases
# ---------------------------------------------------------------------------


def test_atr_stop_extracted() -> None:
    spec = _PARSER.parse(_ATR_PINE)
    assert spec.meta.name == "vcp_breakout"
    assert spec.source_format == "pine"
    assert spec.stop_loss.method == SLMethod.ATR_BASED
    assert spec.stop_loss.atr_period == 14
    assert spec.stop_loss.atr_multiplier == 2.0


def test_rr_ratio_extracted() -> None:
    spec = _PARSER.parse(_ATR_PINE)
    assert spec.target.method == TargetMethod.RISK_REWARD
    assert spec.target.ratio == 2.5


def test_sma_conditions_extracted() -> None:
    spec = _PARSER.parse(_ATR_PINE)
    names = [c.name for c in spec.entry_conditions.conditions]
    assert "sma_200" in names
    assert "sma_50" in names


def test_recent_low_stop_extracted() -> None:
    spec = _PARSER.parse(_RECENT_LOW_PINE)
    assert spec.stop_loss.method == SLMethod.RECENT_LOW
    assert spec.stop_loss.lookback_days == 20


def test_fixed_pct_stop_extracted() -> None:
    spec = _PARSER.parse(_FIXED_PCT_PINE)
    assert spec.stop_loss.method == SLMethod.FIXED_PCT
    assert spec.stop_loss.fixed_pct == pytest.approx(5.0, abs=0.01)


def test_short_direction_detected() -> None:
    spec = _PARSER.parse(_SHORT_PINE)
    assert spec.meta.direction == Direction.SHORT


def test_long_direction_default() -> None:
    spec = _PARSER.parse(_ATR_PINE)
    assert spec.meta.direction == Direction.LONG


def test_crossover_condition_extracted() -> None:
    spec = _PARSER.parse(_CROSSOVER_PINE)
    names = [c.name for c in spec.entry_conditions.conditions]
    assert any("crossover" in n for n in names)


def test_default_timeframe_is_daily() -> None:
    spec = _PARSER.parse(_ATR_PINE)
    assert spec.meta.timeframe == Timeframe.DAILY


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_no_conditions_fallback_to_pine_entry() -> None:
    minimal = 'strategy("Bare", overlay=true)\nstrategy.exit("X", stop=close * 0.95)'
    spec = _PARSER.parse(minimal)
    assert spec.entry_conditions.conditions[0].name == "pine_entry"


def test_limit_exit_without_rr_comment_defaults_2() -> None:
    pine = (
        'strategy("No RR", overlay=true)\n'
        "if close > ta.sma(close, 50)\n"
        '    strategy.entry("Long", strategy.long)\n'
        'strategy.exit("Exit", stop=close*0.95, limit=close*1.10)\n'
    )
    spec = _PARSER.parse(pine)
    assert spec.target.ratio == 2.0


# ---------------------------------------------------------------------------
# Failure cases
# ---------------------------------------------------------------------------


def test_empty_source_raises() -> None:
    with pytest.raises(PineParseError, match="empty"):
        _PARSER.parse("")


def test_whitespace_only_raises() -> None:
    with pytest.raises(PineParseError):
        _PARSER.parse("   \n  ")


def test_no_strategy_declaration_raises() -> None:
    with pytest.raises(PineParseError, match="strategy()"):
        _PARSER.parse("// just a comment\nvar x = 1")
