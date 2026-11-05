"""
USSValidator — structural + safety validation for USSSpec.

Safety invariants are INVIOLABLE — failure = hard REJECT, no override.
  1. stop_loss section present (schema enforces this)
  2. target section present (schema enforces this)
  3. stop_loss.max_sl_pct <= operator hard_limits.max_stop_loss_pct
  4. implied R:R >= operator hard_limits.min_risk_reward_ratio (when computable)
  5. entry_conditions non-empty
  6. known condition types only
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..indicators.library import _DISPATCH
from .schema import ConditionType, SLMethod, TargetMethod, USSSpec

# Derived from library dispatch — always in sync, never stale
_KNOWN_INDICATORS: frozenset[str] = frozenset(_DISPATCH.keys())


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class USSValidator:
    """Validates a USSSpec against structural rules and operator hard limits."""

    def __init__(self, max_stop_loss_pct: float = 10.0, min_rr_ratio: float = 1.5) -> None:
        self._max_sl_pct = max_stop_loss_pct
        self._min_rr = min_rr_ratio

    def validate(self, spec: USSSpec) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []

        self._check_entry_conditions(spec, errors)
        self._check_stop_loss(spec, errors)
        self._check_target(spec, errors, warnings)
        self._check_rr(spec, errors)
        self._check_exit_conditions(spec, warnings)

        return ValidationResult(ok=len(errors) == 0, errors=errors, warnings=warnings)

    # ------------------------------------------------------------------
    def _check_entry_conditions(self, spec: USSSpec, errors: list[str]) -> None:
        if not spec.entry_conditions.conditions:
            errors.append("entry_conditions.conditions is empty")
            return
        for cond in spec.entry_conditions.conditions:
            _p = cond.params.get("params", cond.params)  # unwrap JSON-parser double-wrap
            if cond.type == ConditionType.INDICATOR:
                ind = _p.get("indicator") or _p.get("name", "")
                if ind.upper() not in _KNOWN_INDICATORS:
                    errors.append(
                        f"Condition '{cond.name}': unknown indicator '{ind}'. "
                        f"Known: {sorted(_KNOWN_INDICATORS)}"
                    )
            if (
                cond.type == ConditionType.PATTERN
                and _p.get("pattern") == "pullback_trigger"
                and spec.regime_filter is None
            ):
                errors.append(
                    f"Condition '{cond.name}': pattern=pullback_trigger requires "
                    "spec.regime_filter to be set"
                )

    def _check_stop_loss(self, spec: USSSpec, errors: list[str]) -> None:
        sl = spec.stop_loss
        if sl.max_sl_pct > self._max_sl_pct:
            errors.append(
                f"stop_loss.max_sl_pct={sl.max_sl_pct}% exceeds operator limit "
                f"of {self._max_sl_pct}% — SAFETY REJECT"
            )
        if sl.method == SLMethod.ATR_BASED and (sl.atr_period is None or sl.atr_multiplier is None):
            errors.append("stop_loss method=atr_based requires atr_period and atr_multiplier")
        if sl.method == SLMethod.FIXED_PCT and sl.fixed_pct is None:
            errors.append("stop_loss method=fixed_pct requires fixed_pct")
        if sl.method == SLMethod.FIXED_POINTS and sl.fixed_points is None:
            errors.append("stop_loss method=fixed_points requires fixed_points")
        if sl.method == SLMethod.ORB_RANGE and sl.range_fraction is None:
            errors.append("stop_loss method=orb_range requires range_fraction")
        if sl.method == SLMethod.PULLBACK_LOW:
            if sl.atr_period is None or sl.atr_multiplier is None:
                errors.append(
                    "stop_loss method=pullback_low requires atr_period and atr_multiplier"
                )
            has_pullback_pattern = any(
                c.type == ConditionType.PATTERN
                and c.params.get("params", c.params).get("pattern") == "pullback_trigger"
                for c in spec.entry_conditions.conditions
            )
            if not has_pullback_pattern:
                errors.append(
                    "stop_loss method=pullback_low requires an entry condition with "
                    "pattern=pullback_trigger"
                )

    def _check_target(self, spec: USSSpec, errors: list[str], warnings: list[str]) -> None:
        t = spec.target
        if t.method == TargetMethod.RISK_REWARD and t.ratio is None:
            errors.append("target method=risk_reward requires ratio")
        if t.method == TargetMethod.FIXED_PCT and t.fixed_pct is None:
            errors.append("target method=fixed_pct requires fixed_pct")
        if t.method == TargetMethod.FIXED_POINTS and t.fixed_points is None:
            errors.append("target method=fixed_points requires fixed_points")
        if t.method == TargetMethod.INDICATOR and (t.indicator is None or t.period is None):
            errors.append("target method=indicator requires indicator and period")

    def _check_rr(self, spec: USSSpec, errors: list[str]) -> None:
        """Check R:R only when it's directly computable (risk_reward method)."""
        if (
            spec.target.method == TargetMethod.RISK_REWARD
            and spec.target.ratio is not None
            and spec.target.ratio < self._min_rr
        ):
            errors.append(
                f"target.ratio={spec.target.ratio} is below minimum R:R of "
                f"{self._min_rr} — SAFETY REJECT"
            )

    def _check_exit_conditions(self, spec: USSSpec, warnings: list[str]) -> None:
        ec = spec.exit_conditions
        if ec.trailing_stop is None and ec.time_stop_days is None and not ec.additional:
            warnings.append(
                "No exit conditions defined beyond target/SL — "
                "consider adding a trailing stop or time stop"
            )
