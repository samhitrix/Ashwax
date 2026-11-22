"""StrategyRegistry's promoted-config override gating (equity learning-engine
PR4, config/learning.json apply_promoted_overrides.mode)."""

from __future__ import annotations

import json

import pandas as pd
from src.strategies.registry import StrategyRegistry
from src.strategies.uss.schema import (
    Condition,
    ConditionType,
    Direction,
    EntryConditions,
    ExitConditions,
    LogicOperator,
    Product,
    SLMethod,
    StopLoss,
    Target,
    TargetMethod,
    Timeframe,
    UniverseFilter,
    USSMeta,
    USSSpec,
)

_SPEC = USSSpec(
    meta=USSMeta(
        name="atr_strat",
        product=Product.DELIVERY,
        direction=Direction.LONG,
        timeframe=Timeframe.DAILY,
    ),
    universe_filter=UniverseFilter(),
    entry_conditions=EntryConditions(
        logic=LogicOperator.AND,
        conditions=[
            Condition(
                name="c",
                type=ConditionType.PRICE,
                params={
                    "condition": "price_above",
                    "reference": "consolidation_high",
                    "lookback_days": 21,
                },
            )
        ],
    ),
    stop_loss=StopLoss(
        method=SLMethod.ATR_BASED, atr_period=14, atr_multiplier=2.0, max_sl_pct=8.0
    ),
    target=Target(method=TargetMethod.RISK_REWARD, ratio=2.5),
    exit_conditions=ExitConditions(),
)


def _write_override(tmp_path, overrides: dict) -> None:
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "strategy_atr_strat.json").write_text(json.dumps(overrides))


def _write_learning_mode(tmp_path, mode: str) -> None:
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "learning.json").write_text(
        json.dumps({"apply_promoted_overrides": {"mode": mode}})
    )


def test_off_mode_ignores_override_file_even_when_present(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_override(tmp_path, {"sl_atr_multiplier": 3.0})
    _write_learning_mode(tmp_path, "off")

    reg = StrategyRegistry()
    compiled = reg.register_spec(_SPEC, df=pd.DataFrame(), skip_backtest=True)

    assert compiled.spec.stop_loss.atr_multiplier == 2.0  # unchanged


def test_shadow_mode_logs_but_does_not_apply(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_override(tmp_path, {"sl_atr_multiplier": 3.0})
    _write_learning_mode(tmp_path, "shadow")

    reg = StrategyRegistry()
    compiled = reg.register_spec(_SPEC, df=pd.DataFrame(), skip_backtest=True)

    assert compiled.spec.stop_loss.atr_multiplier == 2.0  # still unchanged


def test_live_mode_applies_the_override(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_override(tmp_path, {"sl_atr_multiplier": 3.0})
    _write_learning_mode(tmp_path, "live")

    reg = StrategyRegistry()
    compiled = reg.register_spec(_SPEC, df=pd.DataFrame(), skip_backtest=True)

    assert compiled.spec.stop_loss.atr_multiplier == 3.0


def test_live_mode_with_no_override_file_is_a_noop(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_learning_mode(tmp_path, "live")

    reg = StrategyRegistry()
    compiled = reg.register_spec(_SPEC, df=pd.DataFrame(), skip_backtest=True)

    assert compiled.spec.stop_loss.atr_multiplier == 2.0


def test_live_mode_skips_unknown_override_param_without_raising(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_override(tmp_path, {"sl_atr_multiplier": 3.0, "not_a_real_param": 9.0})
    _write_learning_mode(tmp_path, "live")

    reg = StrategyRegistry()
    compiled = reg.register_spec(_SPEC, df=pd.DataFrame(), skip_backtest=True)

    assert compiled.spec.stop_loss.atr_multiplier == 3.0


def test_no_learning_json_defaults_to_off(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_override(tmp_path, {"sl_atr_multiplier": 3.0})

    reg = StrategyRegistry()
    compiled = reg.register_spec(_SPEC, df=pd.DataFrame(), skip_backtest=True)

    assert compiled.spec.stop_loss.atr_multiplier == 2.0
