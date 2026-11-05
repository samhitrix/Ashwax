"""Layer 4 Failure Scorer — dominant failure detection + mutation-bias mapping."""

from __future__ import annotations

from src.learning.failure_scorer import FailureScorer, param_bias_for


def test_score_picks_the_most_common_loss_attribution() -> None:
    trades = [
        {"pnl_r": -1.0, "attribution": "TIMING_FAILURE"},
        {"pnl_r": -0.5, "attribution": "TIMING_FAILURE"},
        {"pnl_r": -2.0, "attribution": "REGIME_MISMATCH"},
        {"pnl_r": 1.5, "attribution": "WIN_TIMING_OPTIMAL"},  # win, excluded
    ]
    report = FailureScorer().score("vcp", trades)
    assert report.dominant_failure == "TIMING_FAILURE"
    assert report.failure_modes[0].count == 2


def test_score_no_losses_reports_none() -> None:
    report = FailureScorer().score("vcp", [{"pnl_r": 1.0, "attribution": "WIN_TIMING_OPTIMAL"}])
    assert report.dominant_failure == "NONE"


def test_param_bias_for_timing_failure_weights_stop_params() -> None:
    bias = param_bias_for("TIMING_FAILURE", ["sl_atr_multiplier", "target_ratio"])
    assert bias is not None
    assert bias["sl_atr_multiplier"] == 5.0
    assert bias["target_ratio"] == 1.0


def test_param_bias_for_unmapped_failure_mode_returns_none() -> None:
    """REGIME_MISMATCH/SIGNAL_QUALITY/NEWS_ADVERSE are deliberately unmapped —
    no honest SL/target correlate — so mutation stays uniform for them."""
    assert param_bias_for("REGIME_MISMATCH", ["sl_atr_multiplier"]) is None
    assert param_bias_for("SIGNAL_QUALITY", ["sl_atr_multiplier"]) is None
    assert param_bias_for("NONE", ["sl_atr_multiplier"]) is None


def test_param_bias_for_timing_failure_with_no_applicable_param_returns_none() -> None:
    """Strategy has no SL param at all (e.g. fixed-pct only, no atr fields) — the
    mapped params don't apply, so there's nothing to bias."""
    assert param_bias_for("TIMING_FAILURE", ["target_ratio"]) is None
