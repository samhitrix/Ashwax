"""Tests for StrategyDiscoveryEngine."""

import json
from pathlib import Path

from src.strategies.discovery import StrategyDiscoveryEngine
from src.strategies.registry import StrategyRegistry

# ---------------------------------------------------------------------------
# Shared spec fixtures
# ---------------------------------------------------------------------------

_VALID_SPEC: dict[str, object] = {
    "meta": {"name": "vcp_test", "product": "DELIVERY", "direction": "LONG", "timeframe": "DAILY"},
    "universe_filter": {"min_price": 50, "min_adv_crores": 5, "exchanges": ["NSE"]},
    "entry_conditions": {
        "logic": "AND",
        "conditions": [
            {
                "name": "breakout",
                "type": "PRICE",
                "condition": "price_above",
                "reference": "consolidation_high",
                "lookback_days": 21,
            }
        ],
    },
    "stop_loss": {
        "method": "recent_low",
        "lookback_days": 21,
        "buffer_pct": 0.5,
        "max_sl_pct": 8.0,
    },
    "target": {"method": "risk_reward", "ratio": 2.0},
    "exit_conditions": {},
}

_VALID_YAML = """\
meta:
  name: yaml_strategy
  product: DELIVERY
  direction: LONG
  timeframe: DAILY
universe_filter:
  min_price: 50
  min_adv_crores: 5
  exchanges: [NSE]
entry_conditions:
  logic: AND
  conditions:
    - name: breakout
      type: PRICE
      condition: price_above
      reference: consolidation_high
      lookback_days: 21
stop_loss:
  method: recent_low
  lookback_days: 21
  buffer_pct: 0.5
  max_sl_pct: 8.0
target:
  method: risk_reward
  ratio: 2.0
exit_conditions: {}
"""

_VALID_PINE = """\
//@version=5
strategy("Pine Strategy", overlay=true)
sma200 = ta.sma(close, 200)
stop = close * 0.95
if close > sma200
    strategy.entry("Long", strategy.long)
strategy.exit("Exit", stop=stop)
"""


def _make_registry() -> StrategyRegistry:
    return StrategyRegistry()


# ---------------------------------------------------------------------------
# Normal cases
# ---------------------------------------------------------------------------


def test_json_file_accepted(tmp_path: Path) -> None:
    (tmp_path / "vcp_test.json").write_text(json.dumps(_VALID_SPEC), encoding="utf-8")
    engine = StrategyDiscoveryEngine(_make_registry(), dirs=[tmp_path])
    result = engine.scan(skip_backtest=True)
    assert len(result.accepted) == 1
    assert result.accepted[0].strategy_name == "vcp_test"
    assert result.accepted[0].status == "accepted"


def test_yaml_file_accepted(tmp_path: Path) -> None:
    (tmp_path / "yaml_strategy.yaml").write_text(_VALID_YAML, encoding="utf-8")
    engine = StrategyDiscoveryEngine(_make_registry(), dirs=[tmp_path])
    result = engine.scan(skip_backtest=True)
    assert len(result.accepted) == 1
    assert result.accepted[0].strategy_name == "yaml_strategy"


def test_pine_file_accepted(tmp_path: Path) -> None:
    (tmp_path / "pine_strategy.pine").write_text(_VALID_PINE, encoding="utf-8")
    engine = StrategyDiscoveryEngine(_make_registry(), dirs=[tmp_path])
    result = engine.scan(skip_backtest=True)
    assert len(result.accepted) == 1
    assert result.accepted[0].strategy_name == "pine_strategy"


def test_result_summary_format(tmp_path: Path) -> None:
    (tmp_path / "vcp_test.json").write_text(json.dumps(_VALID_SPEC), encoding="utf-8")
    engine = StrategyDiscoveryEngine(_make_registry(), dirs=[tmp_path])
    result = engine.scan(skip_backtest=True)
    assert "accepted=1" in result.summary()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_nonexistent_dir_returns_empty_result() -> None:
    engine = StrategyDiscoveryEngine(_make_registry(), dirs=[Path("/nonexistent/path")])
    result = engine.scan(skip_backtest=True)
    assert result.accepted == []
    assert result.rejected == []
    assert result.unchanged == []


def test_second_scan_unchanged_for_same_mtime(tmp_path: Path) -> None:
    (tmp_path / "vcp_test.json").write_text(json.dumps(_VALID_SPEC), encoding="utf-8")
    registry = _make_registry()
    engine = StrategyDiscoveryEngine(registry, dirs=[tmp_path])
    engine.scan(skip_backtest=True)  # first scan: accepted
    result2 = engine.scan(skip_backtest=True)  # second scan: unchanged
    assert len(result2.unchanged) == 1
    assert len(result2.accepted) == 0


def test_txt_file_ignored(tmp_path: Path) -> None:
    (tmp_path / "readme.txt").write_text("this is not a strategy", encoding="utf-8")
    engine = StrategyDiscoveryEngine(_make_registry(), dirs=[tmp_path])
    result = engine.scan(skip_backtest=True)
    assert result.accepted == []
    assert result.rejected == []
    assert result.unchanged == []


def test_mixed_accepted_and_rejected(tmp_path: Path) -> None:
    (tmp_path / "vcp_test.json").write_text(json.dumps(_VALID_SPEC), encoding="utf-8")
    (tmp_path / "bad.yaml").write_text("this: is: bad: yaml: [unclosed", encoding="utf-8")
    engine = StrategyDiscoveryEngine(_make_registry(), dirs=[tmp_path])
    result = engine.scan(skip_backtest=True)
    assert len(result.accepted) == 1
    assert len(result.rejected) == 1


# ---------------------------------------------------------------------------
# Failure cases
# ---------------------------------------------------------------------------


def test_malformed_json_rejected(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{ not valid json", encoding="utf-8")
    engine = StrategyDiscoveryEngine(_make_registry(), dirs=[tmp_path])
    result = engine.scan(skip_backtest=True)
    assert len(result.rejected) == 1
    assert "Parse error" in result.rejected[0].reason


def test_validation_failure_rejected(tmp_path: Path) -> None:
    bad_spec = dict(_VALID_SPEC)
    bad_spec["stop_loss"] = {"method": "fixed_pct", "fixed_pct": 5.0, "max_sl_pct": 50.0}
    (tmp_path / "bad_sl.json").write_text(json.dumps(bad_spec), encoding="utf-8")
    engine = StrategyDiscoveryEngine(_make_registry(), dirs=[tmp_path])
    result = engine.scan(skip_backtest=True)
    assert len(result.rejected) == 1
    assert "Validation failed" in result.rejected[0].reason


# ---------------------------------------------------------------------------
# Backtest-quality override (registry.py no longer hard-deregisters a
# strategy on a failing backtest — see src/risk/crm.py G6 for the live gate
# that takes over instead)
# ---------------------------------------------------------------------------


def test_register_soft_registers_on_failing_backtest() -> None:
    import pandas as pd
    from src.strategies.backtest.engine import BacktestResult

    registry = _make_registry()
    failing = BacktestResult(
        strategy_name="vcp_test",
        passed=False,
        total_trades=50,
        win_rate=0.3,
        profit_factor=0.8,
        expectancy_r=0.05,
        avg_win_r=1.5,
        avg_loss_r=-0.9,
        max_drawdown_r=10.0,
        signals=[],
        reject_reason="expectancy below +0.20R",
    )
    registry._backtest.run = lambda compiled, df: failing  # type: ignore[method-assign]

    compiled = registry.register(_VALID_SPEC, df=pd.DataFrame({"close": [1.0]}))

    assert compiled is not None
    assert registry.get("vcp_test") is compiled
    result = registry.backtest_result("vcp_test")
    assert result is not None
    assert result.passed is False
    assert result.reject_reason == "expectancy below +0.20R"
