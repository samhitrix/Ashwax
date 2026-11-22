"""Tests for StrategyEvolutionEngine."""

import random
from dataclasses import dataclass, field

import pandas as pd
import pytest
from src.strategies.backtest.engine import BacktestResult
from src.strategies.evolution import StrategyEvolutionEngine
from src.strategies.registry import StrategyRegistry
from src.strategies.uss.compiler import CompiledStrategy
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

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _TrackingPromotion:
    calls: list[tuple[str, dict[str, object], float]] = field(default_factory=list)

    def promote(
        self,
        strategy: str,
        params: dict[str, object],
        baseline_wr: float,
        prior_params: dict[str, object] | None = None,
    ) -> None:
        self.calls.append((strategy, params, baseline_wr))

    def record_trade(self, strategy: str, pnl_r: float) -> None:
        pass

    def check_rollback(self, strategy: str) -> bool:
        return False


class _PassBacktest:
    def __init__(self, expectancy: float = 0.35) -> None:
        self._expectancy = expectancy

    def run(self, compiled: CompiledStrategy, df: pd.DataFrame) -> BacktestResult:
        return BacktestResult(
            strategy_name=compiled.spec.meta.name,
            passed=True,
            total_trades=25,
            win_rate=0.52,
            profit_factor=1.5,
            expectancy_r=self._expectancy,
            avg_win_r=2.0,
            avg_loss_r=1.0,
            max_drawdown_r=3.0,
            signals=[],
        )


class _FailBacktest:
    def run(self, compiled: CompiledStrategy, df: pd.DataFrame) -> BacktestResult:
        return BacktestResult(
            strategy_name=compiled.spec.meta.name,
            passed=False,
            total_trades=5,
            win_rate=0.20,
            profit_factor=0.8,
            expectancy_r=-0.1,
            avg_win_r=1.0,
            avg_loss_r=1.5,
            max_drawdown_r=5.0,
            signals=[],
            reject_reason="insufficient trades",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ATR_SPEC = USSSpec(
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

_FIXED_SPEC = USSSpec(
    meta=USSMeta(
        name="fixed_strat",
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
    stop_loss=StopLoss(method=SLMethod.FIXED_PCT, fixed_pct=5.0, max_sl_pct=8.0),
    target=Target(method=TargetMethod.RISK_REWARD, ratio=2.0),
    exit_conditions=ExitConditions(),
)


def _registered_registry(spec: USSSpec) -> StrategyRegistry:
    reg = StrategyRegistry()
    reg.register_spec(spec, skip_backtest=True)
    return reg


_DF = pd.DataFrame()  # sentinel — real backtest not run (mocked)


# ---------------------------------------------------------------------------
# mutable_params tests
# ---------------------------------------------------------------------------


def test_mutable_params_atr_spec() -> None:
    engine = StrategyEvolutionEngine(_registered_registry(_ATR_SPEC), _TrackingPromotion())
    params = engine.mutable_params(_ATR_SPEC)
    assert "sl_atr_period" in params
    assert "sl_atr_multiplier" in params
    assert "target_ratio" in params
    assert params["sl_atr_period"] == 14.0
    assert params["sl_atr_multiplier"] == 2.0


def test_mutable_params_fixed_pct_excludes_atr() -> None:
    engine = StrategyEvolutionEngine(_registered_registry(_FIXED_SPEC), _TrackingPromotion())
    params = engine.mutable_params(_FIXED_SPEC)
    assert "sl_fixed_pct" in params
    assert "sl_atr_period" not in params
    assert "sl_atr_multiplier" not in params


# ---------------------------------------------------------------------------
# apply_mutation tests
# ---------------------------------------------------------------------------


def test_apply_mutation_atr_multiplier() -> None:
    engine = StrategyEvolutionEngine(_registered_registry(_ATR_SPEC), _TrackingPromotion())
    mutated = engine.apply_mutation(_ATR_SPEC, "sl_atr_multiplier", 1.10)
    assert mutated.stop_loss.atr_multiplier == pytest.approx(2.2, rel=1e-4)
    assert _ATR_SPEC.stop_loss.atr_multiplier == 2.0  # original unchanged


def test_apply_mutation_target_ratio() -> None:
    engine = StrategyEvolutionEngine(_registered_registry(_ATR_SPEC), _TrackingPromotion())
    mutated = engine.apply_mutation(_ATR_SPEC, "target_ratio", 1.20)
    assert mutated.target.ratio == pytest.approx(3.0, rel=1e-4)


def test_apply_mutation_atr_period_stays_int() -> None:
    engine = StrategyEvolutionEngine(_registered_registry(_ATR_SPEC), _TrackingPromotion())
    mutated = engine.apply_mutation(_ATR_SPEC, "sl_atr_period", 1.20)
    assert isinstance(mutated.stop_loss.atr_period, int)
    assert mutated.stop_loss.atr_period == 17  # round(14 * 1.20) = 17


def test_apply_mutation_ratio_clamped_to_min_rr() -> None:
    engine = StrategyEvolutionEngine(_registered_registry(_ATR_SPEC), _TrackingPromotion())
    mutated = engine.apply_mutation(_ATR_SPEC, "target_ratio", 0.5)  # 2.5 * 0.5 = 1.25 → clamped
    assert mutated.target.ratio == pytest.approx(1.5, rel=1e-4)


def test_apply_mutation_sl_fixed_pct_clamped_to_max() -> None:
    engine = StrategyEvolutionEngine(_registered_registry(_FIXED_SPEC), _TrackingPromotion())
    mutated = engine.apply_mutation(_FIXED_SPEC, "sl_fixed_pct", 3.0)  # 5.0 * 3.0 → clamped
    assert mutated.stop_loss.fixed_pct == pytest.approx(10.0, rel=1e-4)


# ---------------------------------------------------------------------------
# evolve tests
# ---------------------------------------------------------------------------


def test_evolve_unknown_strategy_returns_not_promoted() -> None:
    engine = StrategyEvolutionEngine(StrategyRegistry(), _TrackingPromotion())
    result = engine.evolve("nonexistent", _DF, 0.50)
    assert result.promoted is False
    assert "not found" in result.reason


def test_evolve_promotes_when_candidate_passes() -> None:
    promo = _TrackingPromotion()
    engine = StrategyEvolutionEngine(
        _registered_registry(_ATR_SPEC),
        promo,
        backtest_engine=_PassBacktest(expectancy=0.4),
        rng=random.Random(42),  # noqa: S311
    )
    result = engine.evolve("atr_strat", _DF, baseline_wr=0.50, n_candidates=3)
    assert result.promoted is True
    assert result.winner is not None
    assert len(promo.calls) == 1
    assert promo.calls[0][0] == "atr_strat"


def test_evolve_not_promoted_when_all_fail_backtest() -> None:
    promo = _TrackingPromotion()
    engine = StrategyEvolutionEngine(
        _registered_registry(_ATR_SPEC),
        promo,
        backtest_engine=_FailBacktest(),
        rng=random.Random(0),  # noqa: S311
    )
    result = engine.evolve("atr_strat", _DF, baseline_wr=0.50, n_candidates=5)
    assert result.promoted is False
    assert "no candidate" in result.reason
    assert len(promo.calls) == 0


def test_evolve_winner_has_best_expectancy() -> None:
    class _SequenceBacktest:
        def __init__(self) -> None:
            self._call = 0
            self._expectancies = [0.25, 0.45, 0.30]

        def run(self, compiled: CompiledStrategy, df: pd.DataFrame) -> BacktestResult:
            e = self._expectancies[self._call % len(self._expectancies)]
            self._call += 1
            return BacktestResult(
                strategy_name=compiled.spec.meta.name,
                passed=True,
                total_trades=25,
                win_rate=0.5,
                profit_factor=1.4,
                expectancy_r=e,
                avg_win_r=2.0,
                avg_loss_r=1.0,
                max_drawdown_r=3.0,
                signals=[],
            )

    engine = StrategyEvolutionEngine(
        _registered_registry(_ATR_SPEC),
        _TrackingPromotion(),
        backtest_engine=_SequenceBacktest(),
        rng=random.Random(1),  # noqa: S311
    )
    result = engine.evolve("atr_strat", _DF, baseline_wr=0.50, n_candidates=3)
    assert result.promoted is True
    assert result.winner is not None
    assert result.winner.backtest_result.expectancy_r == pytest.approx(0.45, rel=1e-4)


# ---------------------------------------------------------------------------
# dry_run / use_simulation_gate / param_bias (equity learning-engine PR4/PR5)
# ---------------------------------------------------------------------------

_TRAIN_TEST_DF = pd.DataFrame({"x": range(30)})  # non-empty, so the 70/30 split is real


def test_evolve_dry_run_skips_promote_but_reports_would_promote() -> None:
    promo = _TrackingPromotion()
    engine = StrategyEvolutionEngine(
        _registered_registry(_ATR_SPEC),
        promo,
        backtest_engine=_PassBacktest(expectancy=0.4),
        rng=random.Random(42),  # noqa: S311
    )
    result = engine.evolve("atr_strat", _DF, baseline_wr=0.50, n_candidates=3, dry_run=True)
    assert result.promoted is False
    assert "would_promote" in result.reason
    assert len(promo.calls) == 0  # config file never written


class _GoodOutOfSampleBacktest:
    """Passes on both train and test with metrics that clear WalkForwardValidator's bar."""

    def run(self, compiled: CompiledStrategy, df: pd.DataFrame) -> BacktestResult:
        return BacktestResult(
            strategy_name=compiled.spec.meta.name,
            passed=True,
            total_trades=25,
            win_rate=0.55,
            profit_factor=1.6,
            expectancy_r=0.35,
            avg_win_r=2.0,
            avg_loss_r=1.0,
            max_drawdown_r=0.10,
            signals=[],
        )


class _OverfitBacktest:
    """Looks great in-sample, fails the out-of-sample slice — the exact case
    use_simulation_gate exists to catch that a single full-sample backtest can't."""

    def run(self, compiled: CompiledStrategy, df: pd.DataFrame) -> BacktestResult:
        is_train = len(df) >= 21  # _TRAIN_TEST_DF has 30 rows; 70% split -> train=21, test=9
        return BacktestResult(
            strategy_name=compiled.spec.meta.name,
            passed=True,
            total_trades=25,
            win_rate=0.60 if is_train else 0.10,
            profit_factor=1.8 if is_train else 0.5,
            expectancy_r=0.50 if is_train else -0.30,
            avg_win_r=2.0,
            avg_loss_r=1.0,
            max_drawdown_r=0.10 if is_train else 0.50,
            signals=[],
        )


def test_use_simulation_gate_promotes_when_out_of_sample_also_passes() -> None:
    promo = _TrackingPromotion()
    engine = StrategyEvolutionEngine(
        _registered_registry(_ATR_SPEC),
        promo,
        backtest_engine=_GoodOutOfSampleBacktest(),
        rng=random.Random(42),  # noqa: S311
    )
    result = engine.evolve(
        "atr_strat", _TRAIN_TEST_DF, baseline_wr=0.50, n_candidates=3, use_simulation_gate=True
    )
    assert result.promoted is True
    assert len(promo.calls) == 1


def test_use_simulation_gate_rejects_candidate_that_fails_out_of_sample() -> None:
    """A single full-sample backtest (use_simulation_gate=False) would have
    promoted this candidate — the train slice alone looks like a clear pass.
    The gate must catch that the test slice does not hold up."""
    promo = _TrackingPromotion()
    engine = StrategyEvolutionEngine(
        _registered_registry(_ATR_SPEC),
        promo,
        backtest_engine=_OverfitBacktest(),
        rng=random.Random(42),  # noqa: S311
    )
    result = engine.evolve(
        "atr_strat", _TRAIN_TEST_DF, baseline_wr=0.50, n_candidates=3, use_simulation_gate=True
    )
    assert result.promoted is False
    assert "no candidate" in result.reason
    assert len(promo.calls) == 0


def test_param_bias_skews_mutation_selection_toward_the_biased_param() -> None:
    """With sl_atr_multiplier weighted far above the other mutable params, most
    (not necessarily all, since choices() is still random) mutations should
    target it rather than being spread uniformly."""
    engine = StrategyEvolutionEngine(
        _registered_registry(_ATR_SPEC),
        _TrackingPromotion(),
        backtest_engine=_PassBacktest(expectancy=0.4),
        rng=random.Random(7),  # noqa: S311
    )
    params = engine.mutable_params(_ATR_SPEC)
    bias = dict.fromkeys(params, 0.0001)
    bias["sl_atr_multiplier"] = 100.0

    result = engine.evolve("atr_strat", _DF, baseline_wr=0.50, n_candidates=10, param_bias=bias)
    assert result.promoted is True
    assert result.winner.param == "sl_atr_multiplier"
