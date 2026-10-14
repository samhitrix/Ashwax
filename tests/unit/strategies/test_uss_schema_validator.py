"""Tests for USS schema and validator."""

from src.strategies.uss.schema import (
    Condition,
    ConditionType,
    Direction,
    EntryConditions,
    ExitConditions,
    Product,
    SLMethod,
    StopLoss,
    Target,
    TargetMethod,
    Timeframe,
    TrailingStop,
    TrailMethod,
    UniverseFilter,
    USSMeta,
    USSSpec,
)
from src.strategies.uss.validator import USSValidator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_spec(rr: float = 2.0, max_sl_pct: float = 8.0) -> USSSpec:
    return USSSpec(
        meta=USSMeta(name="test_strategy"),
        entry_conditions=EntryConditions(
            conditions=[
                Condition(
                    name="trend",
                    type=ConditionType.INDICATOR,
                    params={"indicator": "SMA", "period": 200, "condition": "price_above"},
                )
            ]
        ),
        stop_loss=StopLoss(method=SLMethod.FIXED_PCT, fixed_pct=5.0, max_sl_pct=max_sl_pct),
        target=Target(method=TargetMethod.RISK_REWARD, ratio=rr),
    )


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_meta_display_name_auto_filled():
    meta = USSMeta(name="vcp_breakout")
    assert meta.display_name == "Vcp Breakout"


def test_meta_display_name_explicit():
    meta = USSMeta(name="vcp_breakout", display_name="VCP Breakout Strategy")
    assert meta.display_name == "VCP Breakout Strategy"


def test_uss_spec_defaults():
    spec = _minimal_spec()
    assert spec.meta.product == Product.DELIVERY
    assert spec.meta.direction == Direction.LONG
    assert spec.meta.timeframe == Timeframe.DAILY
    assert spec.enabled is True
    assert spec.source_format == "json"


def test_universe_filter_defaults():
    uf = UniverseFilter()
    assert uf.exchanges == ["NSE"]
    assert uf.min_price == 0.0
    assert uf.max_price is None


# ---------------------------------------------------------------------------
# Validator — PASS cases
# ---------------------------------------------------------------------------


def test_validator_passes_valid_spec():
    validator = USSValidator(max_stop_loss_pct=10.0, min_rr_ratio=1.5)
    result = validator.validate(_minimal_spec(rr=2.0))
    assert result.ok
    assert result.errors == []


def test_validator_passes_atr_sl():
    spec = _minimal_spec()
    spec.stop_loss = StopLoss(method=SLMethod.ATR_BASED, atr_period=14, atr_multiplier=2.0)
    result = USSValidator().validate(spec)
    assert result.ok


def test_validator_warns_no_exit_conditions():
    spec = _minimal_spec()
    result = USSValidator().validate(spec)
    assert result.ok
    assert any("exit" in w.lower() for w in result.warnings)


# ---------------------------------------------------------------------------
# Validator — FAIL cases (safety invariants)
# ---------------------------------------------------------------------------


def test_validator_rejects_sl_pct_over_limit():
    validator = USSValidator(max_stop_loss_pct=10.0)
    spec = _minimal_spec(max_sl_pct=15.0)
    result = validator.validate(spec)
    assert not result.ok
    assert any("SAFETY REJECT" in e for e in result.errors)


def test_validator_rejects_low_rr():
    validator = USSValidator(min_rr_ratio=2.0)
    spec = _minimal_spec(rr=1.2)
    result = validator.validate(spec)
    assert not result.ok
    assert any("SAFETY REJECT" in e for e in result.errors)


def test_validator_rejects_empty_entry_conditions():
    spec = _minimal_spec()
    spec.entry_conditions.conditions = []
    result = USSValidator().validate(spec)
    assert not result.ok
    assert any("empty" in e for e in result.errors)


def test_validator_rejects_unknown_indicator():
    spec = _minimal_spec()
    spec.entry_conditions.conditions = [
        Condition(
            name="bad",
            type=ConditionType.INDICATOR,
            params={"indicator": "NONEXISTENT_IND", "period": 10, "condition": "price_above"},
        )
    ]
    result = USSValidator().validate(spec)
    assert not result.ok
    assert any("unknown indicator" in e for e in result.errors)


def test_validator_rejects_atr_sl_missing_params():
    spec = _minimal_spec()
    spec.stop_loss = StopLoss(method=SLMethod.ATR_BASED)
    result = USSValidator().validate(spec)
    assert not result.ok


def test_validator_rejects_rr_target_missing_ratio():
    spec = _minimal_spec()
    spec.target = Target(method=TargetMethod.RISK_REWARD, ratio=None)
    result = USSValidator().validate(spec)
    assert not result.ok


def test_validator_passes_exit_with_trail():
    spec = _minimal_spec()
    spec.exit_conditions = ExitConditions(
        trailing_stop=TrailingStop(method=TrailMethod.INDICATOR_CROSS, indicator="EMA", period=10)
    )
    result = USSValidator().validate(spec)
    assert result.ok
    assert result.warnings == []
