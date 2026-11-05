"""
Strategy Evolution Engine â€” mutates a strategy's numeric parameters within
Â±20% bounds, backtests each candidate, and promotes the best one.

Implements the "parameter tuning" layer of spec Â§9:
  - Never switches strategies, only tunes existing parameters.
  - Mutations are within Â±20% of current values.
  - Constraints enforced: atr_period â‰¥ 5, target ratio â‰¥ 1.5, sl_pct â‰¤ 10.0.
  - The PromotionEngine handles atomic config write + 20-trade rollback guard.
"""

from __future__ import annotations

import random as _random
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

from src.infrastructure.logger import get_logger

from ..learning.simulation_gate import SimulationGate
from .backtest.engine import BacktestEngine, BacktestResult
from .registry import StrategyRegistry
from .uss.compiler import USSCompiler
from .uss.schema import USSSpec

if TYPE_CHECKING:
    from ..learning.promotion import PromotionEngine

logger = get_logger(__name__)

_MUTATION_BOUNDS = 0.20  # Â±20%

# (min, max) constraints per mutable param
_CONSTRAINTS: dict[str, tuple[float, float]] = {
    "sl_atr_period": (5.0, float("inf")),
    "sl_atr_multiplier": (0.5, float("inf")),
    "sl_fixed_pct": (0.1, 10.0),
    "sl_buffer_pct": (0.0, float("inf")),
    "sl_lookback_days": (5.0, float("inf")),
    "target_ratio": (1.5, float("inf")),
    "target_fixed_pct": (0.1, float("inf")),
}

_INT_SL_FIELDS = frozenset({"atr_period", "lookback_days"})


def _apply_param_value(spec: USSSpec, param: str, field_name: str, raw_value: float) -> USSSpec:
    """Clamp `raw_value` to param's _CONSTRAINTS and apply it to `spec`. Shared by
    apply_mutation() (raw_value = current*factor) and apply_param_overrides()
    (raw_value = the override's absolute value) — one constraint table, one
    int/float-field rule, for both the mutation path and the promoted-config-
    override path, so "what's a legal sl_atr_period" is never duplicated."""
    lo, hi = _CONSTRAINTS[param]
    clamped = max(lo, min(hi, raw_value))
    if param.startswith("sl_"):
        new_val: int | float = int(round(clamped)) if field_name in _INT_SL_FIELDS else clamped
        new_sl = spec.stop_loss.model_copy(update={field_name: new_val})
        return spec.model_copy(update={"stop_loss": new_sl})
    new_target = spec.target.model_copy(update={field_name: clamped})
    return spec.model_copy(update={"target": new_target})


def apply_param_overrides(spec: USSSpec, overrides: dict[str, float]) -> USSSpec:
    """Apply a promoted-config override dict (config/strategy_{NAME}.json,
    written by PromotionEngine.promote()) to `spec` — absolute values, same
    _CONSTRAINTS clamping as a mutation, via _apply_param_value(). Unknown
    param keys are skipped (logged), not raised — a stale override file
    referencing a since-removed param must not crash StrategyRegistry.reload().
    """
    for param, value in overrides.items():
        if param not in _CONSTRAINTS:
            logger.warning("apply_param_overrides: unknown param %s, skipped", param)
            continue
        if param.startswith("sl_"):
            spec = _apply_param_value(spec, param, param[3:], float(value))
        elif param.startswith("target_"):
            spec = _apply_param_value(spec, param, param[7:], float(value))
        else:
            logger.warning("apply_param_overrides: unhandled namespace %s, skipped", param)
    return spec


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class MutationCandidate:
    param: str
    factor: float
    mutated_spec: USSSpec
    backtest_result: BacktestResult


@dataclass
class EvolutionResult:
    strategy_name: str
    promoted: bool
    winner: MutationCandidate | None
    reason: str


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class StrategyEvolutionEngine:
    """
    Generates parameter mutations, backtests them, and promotes the best.

    Inject rng=random.Random(seed) for deterministic tests.
    Inject backtest_engine to replace BacktestEngine in tests.
    """

    def __init__(
        self,
        registry: StrategyRegistry,
        promotion: PromotionEngine,
        backtest_engine: BacktestEngine | None = None,
        rng: _random.Random | None = None,  # noqa: S311
    ) -> None:
        self._registry = registry
        self._promotion = promotion
        self._backtest = backtest_engine or BacktestEngine()
        self._rng = rng or _random.Random()  # noqa: S311
        self._compiler = USSCompiler()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mutable_params(self, spec: USSSpec) -> dict[str, float]:
        """Return flat dict of param_nameâ†’value for all non-None mutable params."""
        result: dict[str, float] = {}
        sl = spec.stop_loss
        if sl.atr_period is not None:
            result["sl_atr_period"] = float(sl.atr_period)
        if sl.atr_multiplier is not None:
            result["sl_atr_multiplier"] = sl.atr_multiplier
        if sl.fixed_pct is not None:
            result["sl_fixed_pct"] = sl.fixed_pct
        if sl.buffer_pct:
            result["sl_buffer_pct"] = sl.buffer_pct
        if sl.lookback_days is not None:
            result["sl_lookback_days"] = float(sl.lookback_days)
        t = spec.target
        if t.ratio is not None:
            result["target_ratio"] = t.ratio
        if t.fixed_pct is not None:
            result["target_fixed_pct"] = t.fixed_pct
        return result

    def apply_mutation(self, spec: USSSpec, param: str, factor: float) -> USSSpec:
        """Return new USSSpec with param scaled by factor, constraints enforced."""
        if param.startswith("sl_"):
            field_name = param[3:]
            current = float(getattr(spec.stop_loss, field_name) or 0.0)
            return _apply_param_value(spec, param, field_name, current * factor)

        if param.startswith("target_"):
            field_name = param[7:]
            current = float(getattr(spec.target, field_name) or 0.0)
            return _apply_param_value(spec, param, field_name, current * factor)

        raise ValueError(f"Unknown param namespace: {param}")

    def evolve(
        self,
        name: str,
        df: pd.DataFrame,
        baseline_wr: float,
        n_candidates: int = 5,
        dry_run: bool = False,
        use_simulation_gate: bool = False,
        param_bias: dict[str, float] | None = None,
    ) -> EvolutionResult:
        """
        Generate n_candidates mutations, backtest each, promote the best.

        Returns EvolutionResult. Calls PromotionEngine.promote() only if a
        candidate passes backtest and improves on baseline, AND dry_run is False
        (config/learning.json promotion.mode != "live" — see orchestrator.py's
        _evolution_loop). dry_run still computes and reports the winning
        candidate (EvolutionResult.reason says "would promote..."), it just
        withholds the actual config/strategy_{NAME}.json write.

        use_simulation_gate (Layer 5, spec §9 / config/learning.json
        expectancy_resume.mode... see orchestrator.py's _evolution_loop for the
        actual gate key): when True, each candidate is backtested on a 70/30
        train/test split of `df` via src.learning.simulation_gate.SimulationGate
        instead of a single full-sample backtest — a candidate only survives if
        it passes OUT-OF-SAMPLE, closing the "one in-sample backtest decides
        promotion" gap. When False (default — today's behavior, unchanged),
        the full-sample single backtest is used as before.

        param_bias (Layer 4 Failure Scorer): optional weights over param_keys —
        when given, mutation parameter selection is weighted by this dict
        (default weight 1.0 for any param not listed) instead of uniform random,
        so a strategy failing predominantly one way (e.g. "stopped too tight")
        is more likely to have ITS stop-loss param mutated, not a random one.
        """
        compiled = self._registry.get(name)
        if compiled is None:
            return EvolutionResult(
                strategy_name=name,
                promoted=False,
                winner=None,
                reason=f"strategy not found: {name}",
            )

        params = self.mutable_params(compiled.spec)
        if not params:
            return EvolutionResult(
                strategy_name=name,
                promoted=False,
                winner=None,
                reason="no mutable params",
            )

        param_keys = list(params.keys())
        weights = (
            [float((param_bias or {}).get(k, 1.0)) for k in param_keys] if param_bias else None
        )
        candidates: list[MutationCandidate] = []

        for _ in range(n_candidates):
            param = self._rng.choices(param_keys, weights=weights, k=1)[0]
            factor = 1.0 + self._rng.uniform(-_MUTATION_BOUNDS, _MUTATION_BOUNDS)
            try:
                mutated = self.apply_mutation(compiled.spec, param, factor)
                compiled_mut = self._compiler.compile(mutated)
                if use_simulation_gate:
                    split_idx = max(1, int(len(df) * 0.7))
                    train_df, test_df = df.iloc[:split_idx], df.iloc[split_idx:]
                    if len(test_df) == 0:
                        continue
                    bt_train = self._backtest.run(compiled_mut, train_df)
                    bt_test = self._backtest.run(compiled_mut, test_df)
                    gate_result = SimulationGate().evaluate(
                        name,
                        {},
                        {
                            "win_rate": bt_train.win_rate,
                            "profit_factor": bt_train.profit_factor,
                            "expectancy_r": bt_train.expectancy_r,
                            "max_drawdown": bt_train.max_drawdown_r,
                        },
                        {
                            "win_rate": bt_test.win_rate,
                            "profit_factor": bt_test.profit_factor,
                            "expectancy_r": bt_test.expectancy_r,
                            "max_drawdown": bt_test.max_drawdown_r,
                        },
                    )
                    if gate_result.passed:
                        candidates.append(
                            MutationCandidate(
                                param=param,
                                factor=factor,
                                mutated_spec=mutated,
                                backtest_result=bt_test,
                            )
                        )
                else:
                    bt = self._backtest.run(compiled_mut, df)
                    if bt.passed:
                        candidates.append(
                            MutationCandidate(
                                param=param,
                                factor=factor,
                                mutated_spec=mutated,
                                backtest_result=bt,
                            )
                        )
            except Exception:
                logger.debug("mutation failed param=%s factor=%.3f", param, factor)

        if not candidates:
            return EvolutionResult(
                strategy_name=name,
                promoted=False,
                winner=None,
                reason="no candidate passed backtest",
            )

        winner = max(candidates, key=lambda c: c.backtest_result.expectancy_r)
        winner_params: dict[str, object] = dict(self.mutable_params(winner.mutated_spec))
        if not dry_run:
            self._promotion.promote(name, winner_params, baseline_wr, prior_params=params)

        logger.info(
            "evolved strategy=%s param=%s factor=%.3f expectancy=%.3fR dry_run=%s",
            name,
            winner.param,
            winner.factor,
            winner.backtest_result.expectancy_r,
            dry_run,
        )
        return EvolutionResult(
            strategy_name=name,
            promoted=not dry_run,
            winner=winner,
            reason=(
                f"would_promote {winner.param} factor={winner.factor:.3f}"
                if dry_run
                else f"promoted {winner.param} factor={winner.factor:.3f}"
            ),
        )
