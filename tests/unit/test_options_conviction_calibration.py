"""Options learning-engine PR6 — _adjust_conviction_bounds() directional
correctness (the adjustment RULE, not just "did it run")."""

from __future__ import annotations

from dataclasses import dataclass

from src.orchestrator import (
    _OPTIONS_CONVICTION_CLAMP_DEFAULT,
    _adjust_conviction_bounds,
)


@dataclass
class _FakeBucket:
    predicted_confidence: float
    actual_win_rate: float
    gap: float


@dataclass
class _FakeResult:
    overall_miscalibrated: bool
    buckets: list


def test_not_miscalibrated_decays_toward_defaults() -> None:
    result = _FakeResult(overall_miscalibrated=False, buckets=[])
    lo, hi = _adjust_conviction_bounds((0.55, 0.60), result)
    default_lo, default_hi = _OPTIONS_CONVICTION_CLAMP_DEFAULT
    assert lo < 0.55 and lo >= default_lo  # decayed down toward 0.45, not past it
    assert hi > 0.60 and hi <= default_hi  # decayed up toward 0.70, not past it


def test_decay_stops_exactly_at_defaults_not_below() -> None:
    result = _FakeResult(overall_miscalibrated=False, buckets=[])
    # Already AT the defaults -> decay must not move past them.
    lo, hi = _adjust_conviction_bounds(_OPTIONS_CONVICTION_CLAMP_DEFAULT, result)
    assert lo == _OPTIONS_CONVICTION_CLAMP_DEFAULT[0]
    assert hi == _OPTIONS_CONVICTION_CLAMP_DEFAULT[1]


def test_miscalibrated_below_predicted_tightens_ceiling() -> None:
    """Structure wins LESS often than conviction implied -> the ceiling (upper
    win-probability bound) should come down, not the floor."""
    result = _FakeResult(
        overall_miscalibrated=True,
        buckets=[_FakeBucket(predicted_confidence=0.65, actual_win_rate=0.40, gap=0.25)],
    )
    lo, hi = _adjust_conviction_bounds((0.45, 0.70), result)
    assert hi < 0.70
    assert lo == 0.45  # floor untouched


def test_miscalibrated_above_predicted_raises_floor() -> None:
    """Structure wins MORE often than conviction implied -> the floor should
    rise, not the ceiling."""
    result = _FakeResult(
        overall_miscalibrated=True,
        buckets=[_FakeBucket(predicted_confidence=0.50, actual_win_rate=0.80, gap=0.30)],
    )
    lo, hi = _adjust_conviction_bounds((0.45, 0.70), result)
    assert lo > 0.45
    assert hi == 0.70  # ceiling untouched


def test_bounds_never_invert() -> None:
    """Repeated tightening from both directions must never push ceiling <= floor."""
    result_low = _FakeResult(
        overall_miscalibrated=True,
        buckets=[_FakeBucket(predicted_confidence=0.65, actual_win_rate=0.10, gap=0.55)],
    )
    lo, hi = 0.59, 0.60
    for _ in range(5):
        lo, hi = _adjust_conviction_bounds((lo, hi), result_low)
        assert hi > lo


def test_bounds_stay_within_absolute_ranges() -> None:
    result = _FakeResult(
        overall_miscalibrated=True,
        buckets=[_FakeBucket(predicted_confidence=0.65, actual_win_rate=0.10, gap=0.55)],
    )
    lo, hi = (0.45, 0.70)
    for _ in range(20):  # many cycles of the same miscalibration
        lo, hi = _adjust_conviction_bounds((lo, hi), result)
    assert 0.30 <= lo <= 0.60
    assert 0.60 <= hi <= 0.85


def test_no_buckets_with_miscalibrated_flag_is_a_noop() -> None:
    """Defensive: overall_miscalibrated=True with an empty buckets list (should
    not happen from the real CalibrationEngine, but must not crash or move
    bounds arbitrarily)."""
    result = _FakeResult(overall_miscalibrated=True, buckets=[])
    lo, hi = _adjust_conviction_bounds((0.45, 0.70), result)
    assert (lo, hi) == (0.45, 0.70)
