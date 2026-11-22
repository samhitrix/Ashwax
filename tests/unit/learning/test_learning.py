"""Unit tests for src/learning/ — all 5 layers."""

from __future__ import annotations

import json

import pytest
from src.learning.attribution import AttributionEngine
from src.learning.causal_memory import CausalMemory, TradeInsight
from src.learning.pattern_detector import PatternConfidence, PatternDetector
from src.learning.promotion import PromotionEngine
from src.learning.trade_autopsy import FailureMode, TradeAutopsyEngine, TradeOutcome, WinDriver


def _ae() -> TradeAutopsyEngine:
    return TradeAutopsyEngine()


# ---------------------------------------------------------------------------
# Layer 1 — Trade Autopsy
# ---------------------------------------------------------------------------


def test_autopsy_win():
    a = _ae().analyse("T1", "VCP", 2.0, "BULL")
    assert a.outcome == TradeOutcome.WIN
    assert a.primary_failure_mode is None
    assert a.primary_win_driver is not None


def test_autopsy_loss():
    a = _ae().analyse("T2", "VCP", -1.0, "BULL")
    assert a.outcome == TradeOutcome.LOSS
    assert a.primary_win_driver is None


def test_autopsy_breakeven():
    a = _ae().analyse("T3", "VCP", 0.0, "NEUTRAL")
    assert a.outcome == TradeOutcome.BREAKEVEN


def test_autopsy_bear_regime_mismatch():
    a = _ae().analyse("T4", "VCP", -1.0, "BEAR")
    assert a.primary_failure_mode == FailureMode.REGIME_MISMATCH
    assert a.failure_was_environmental is True


def test_autopsy_adverse_news():
    a = _ae().analyse("T5", "VCP", -1.0, "BULL", had_adverse_news=True)
    assert a.primary_failure_mode == FailureMode.NEWS_ADVERSE


def test_autopsy_extended_entry_timing():
    a = _ae().analyse("T6", "VCP", -1.0, "BULL", entry_vs_pivot_pct=0.05)
    assert a.primary_failure_mode == FailureMode.TIMING_FAILURE


def test_autopsy_sl_quality():
    assert (
        _ae().analyse("T7", "VCP", -1.0, "BULL", sl_atr_ratio=0.3).sl_placement_quality == "TIGHT"
    )
    assert _ae().analyse("T8", "VCP", 2.0, "BULL", sl_atr_ratio=1.0).sl_placement_quality == "GOOD"
    assert _ae().analyse("T9", "VCP", 2.0, "BULL", sl_atr_ratio=2.5).sl_placement_quality == "WIDE"


def test_autopsy_entry_quality():
    assert (
        _ae().analyse("T10", "VCP", 2.0, "BULL", entry_vs_pivot_pct=0.001).entry_quality
        == "OPTIMAL"
    )
    assert _ae().analyse("T11", "VCP", 2.0, "BULL", entry_vs_pivot_pct=0.05).entry_quality == "POOR"


def test_autopsy_bull_optimal_win_driver():
    a = _ae().analyse("T12", "VCP", 2.0, "BULL", entry_vs_pivot_pct=0.005)
    assert a.primary_win_driver == WinDriver.TIMING_OPTIMAL
    assert a.was_win_skill_based is True


# ---------------------------------------------------------------------------
# Layer 2 — Attribution Engine
# ---------------------------------------------------------------------------


def test_attribution_empty():
    assert AttributionEngine().summary("VCP").total_trades == 0


def test_attribution_counts():
    e = AttributionEngine()
    ae = _ae()
    e.add(ae.analyse("T1", "VCP", 2.0, "BULL"))
    e.add(ae.analyse("T2", "VCP", -1.0, "BULL"))
    e.add(ae.analyse("T3", "VCP", 2.0, "BULL"))
    s = e.summary("VCP")
    assert s.total_trades == 3
    assert s.wins == 2
    assert s.losses == 1


def test_attribution_regime_mismatch():
    e = AttributionEngine()
    ae = _ae()
    e.add(ae.analyse("T1", "VCP", -1.0, "BEAR"))
    e.add(ae.analyse("T2", "VCP", -1.0, "STRONG_BEAR"))
    assert e.summary("VCP").regime_mismatch_count == 2


def test_attribution_dominant_failure():
    e = AttributionEngine()
    ae = _ae()
    for i in range(3):
        e.add(ae.analyse(f"T{i}", "VCP", -1.0, "BEAR"))
    e.add(ae.analyse("T9", "VCP", -1.0, "BULL", entry_vs_pivot_pct=0.05))
    assert e.dominant_failure_mode("VCP") == "REGIME_MISMATCH"


def test_attribution_no_losses_dominant_none():
    e = AttributionEngine()
    e.add(_ae().analyse("T1", "VCP", 2.0, "BULL"))
    assert e.dominant_failure_mode("VCP") is None


def test_attribution_all_summaries():
    e = AttributionEngine()
    ae = _ae()
    e.add(ae.analyse("T1", "VCP", 2.0, "BULL"))
    e.add(ae.analyse("T2", "ORB", -1.0, "BULL"))
    assert len(e.all_summaries()) == 2


# ---------------------------------------------------------------------------
# Layer 3 — Pattern Detector
# ---------------------------------------------------------------------------


def test_pattern_below_threshold():
    det = PatternDetector()
    ae = _ae()
    for i in range(2):
        det.add(ae.analyse(f"T{i}", "VCP", -1.0, "BEAR"))
    assert det.detect() == []


def test_pattern_weak_at_3():
    det = PatternDetector()
    ae = _ae()
    for i in range(3):
        det.add(ae.analyse(f"T{i}", "VCP", -1.0, "BEAR"))
    p = det.detect()
    assert len(p) == 1
    assert p[0].confidence == PatternConfidence.WEAK


def test_pattern_strong_at_7():
    det = PatternDetector()
    ae = _ae()
    for i in range(7):
        det.add(ae.analyse(f"T{i}", "VCP", -1.0, "BEAR"))
    assert det.detect()[0].confidence == PatternConfidence.STRONG


def test_pattern_low_wr_avoid():
    det = PatternDetector()
    ae = _ae()
    for i in range(7):
        det.add(ae.analyse(f"T{i}", "VCP", -1.0, "BEAR"))
    assert det.detect()[0].recommendation == "AVOID"


def test_pattern_high_wr_normal():
    det = PatternDetector()
    ae = _ae()
    for i in range(7):
        det.add(ae.analyse(f"T{i}", "VCP", 2.0, "BULL"))
    assert det.detect()[0].recommendation == "NORMAL"


def test_pattern_for_match():
    det = PatternDetector()
    ae = _ae()
    for i in range(5):
        det.add(ae.analyse(f"T{i}", "VCP", -1.0, "BEAR"))
    assert det.pattern_for("VCP", "BEAR") is not None


def test_pattern_for_no_match():
    assert PatternDetector().pattern_for("VCP", "BULL") is None


# ---------------------------------------------------------------------------
# Layer 6 — Promotion Engine
# ---------------------------------------------------------------------------


def test_promotion_writes_config(tmp_path):
    e = PromotionEngine(config_dir=tmp_path)
    e.promote("VCP", {"max_sl_pct": 0.07}, baseline_wr=0.55)
    data = json.loads((tmp_path / "strategy_VCP.json").read_text())
    assert data["max_sl_pct"] == pytest.approx(0.07)


def test_promotion_no_rollback_below_window(tmp_path):
    e = PromotionEngine(config_dir=tmp_path)
    e.promote("VCP", {}, baseline_wr=0.55)
    for _ in range(10):
        e.record_trade("VCP", -1.0)
    assert e.check_rollback("VCP") is False


def test_promotion_rollback_triggers(tmp_path):
    e = PromotionEngine(config_dir=tmp_path)
    e.promote("VCP", {}, baseline_wr=0.55)
    for _ in range(20):
        e.record_trade("VCP", -1.0)
    assert e.check_rollback("VCP") is True


def test_promotion_no_rollback_good_wr(tmp_path):
    e = PromotionEngine(config_dir=tmp_path)
    e.promote("VCP", {}, baseline_wr=0.50)
    for i in range(20):
        e.record_trade("VCP", 2.0 if i % 2 == 0 else -1.0)
    assert e.check_rollback("VCP") is False


def test_promotion_rollback_only_once(tmp_path):
    e = PromotionEngine(config_dir=tmp_path)
    e.promote("VCP", {}, baseline_wr=0.55)
    for _ in range(20):
        e.record_trade("VCP", -1.0)
    e.check_rollback("VCP")
    assert e.check_rollback("VCP") is False


def test_promotion_unknown_no_rollback(tmp_path):
    assert PromotionEngine(config_dir=tmp_path).check_rollback("UNKNOWN") is False


# ---------------------------------------------------------------------------
# Layer 7 — Causal Memory
# ---------------------------------------------------------------------------


class _FakeLLM:
    def __init__(self, response: str) -> None:
        self._response = response

    async def complete(self, prompt: str) -> str:
        return self._response


class _FakeRepo:
    def __init__(self) -> None:
        self.saved: list[TradeInsight] = []

    async def save(self, insight: TradeInsight) -> None:
        self.saved.append(insight)

    async def find_similar(self, strategy: str, regime: str, k: int) -> list[TradeInsight]:
        return [i for i in self.saved if i.strategy == strategy][:k]


def _good_llm_response() -> str:
    return json.dumps(
        {
            "was_signal_valid": True,
            "failure_was_environmental": False,
            "what_i_would_do_differently": "tighter SL",
            "generalizable_insight": "VCP fails in BEAR",
            "confidence_in_insight": 0.8,
            "applicable_when": "BEAR regime",
        }
    )


async def test_causal_memory_stores():
    repo = _FakeRepo()
    insight = await CausalMemory(_FakeLLM(_good_llm_response()), repo).analyse_and_store(
        "T1", "VCP", -1.0, "BEAR", "BANKING"
    )
    assert len(repo.saved) == 1
    assert insight.strategy == "VCP"
    assert insight.generalizable_insight == "VCP fails in BEAR"


async def test_causal_memory_fallback_on_llm_error():
    class _BrokenLLM:
        async def complete(self, prompt: str) -> str:
            raise RuntimeError("down")

    repo = _FakeRepo()
    insight = await CausalMemory(_BrokenLLM(), repo).analyse_and_store(
        "T1", "VCP", -1.0, "BEAR", "IT"
    )
    assert insight.confidence_in_insight == pytest.approx(0.3)
    assert len(repo.saved) == 1


async def test_causal_memory_retrieve():
    repo = _FakeRepo()
    memory = CausalMemory(_FakeLLM(_good_llm_response()), repo)
    for i in range(5):
        await memory.analyse_and_store(f"T{i}", "VCP", 1.0, "BULL", "BANKING")
    results = await memory.retrieve("VCP", "BULL", k=3)
    assert len(results) <= 3


async def test_causal_memory_retrieve_repo_failure_returns_empty():
    class _BrokenRepo:
        async def save(self, insight: TradeInsight) -> None:
            pass

        async def find_similar(self, strategy: str, regime: str, k: int) -> list[TradeInsight]:
            raise RuntimeError("DB down")

    results = await CausalMemory(_FakeLLM("{}"), _BrokenRepo()).retrieve("VCP", "BULL")
    assert results == []
