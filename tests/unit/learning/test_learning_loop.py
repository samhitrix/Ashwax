"""Tests for learning stack (autopsy + attribution + pattern) — Phase 4 Item 4."""

from __future__ import annotations

from src.learning.attribution import AttributionEngine
from src.learning.pattern_detector import PatternConfidence, PatternDetector
from src.learning.trade_autopsy import TradeAutopsyEngine, TradeOutcome

# ── Autopsy ──────────────────────────────────────────────────────────────────


def test_win_has_no_failure_mode() -> None:
    engine = TradeAutopsyEngine()
    autopsy = engine.analyse("T1", "VCP", pnl_r=2.0, regime_at_entry="BULL")
    assert autopsy.outcome == TradeOutcome.WIN
    assert autopsy.primary_failure_mode is None
    assert autopsy.primary_win_driver is not None


def test_loss_has_failure_mode() -> None:
    engine = TradeAutopsyEngine()
    autopsy = engine.analyse("T2", "VCP", pnl_r=-1.0, regime_at_entry="BULL")
    assert autopsy.outcome == TradeOutcome.LOSS
    assert autopsy.primary_failure_mode is not None
    assert autopsy.primary_win_driver is None


def test_bear_regime_loss_classified_regime_mismatch() -> None:
    engine = TradeAutopsyEngine()
    autopsy = engine.analyse("T3", "VCP", pnl_r=-1.0, regime_at_entry="BEAR")
    assert str(autopsy.primary_failure_mode) == "REGIME_MISMATCH"
    assert autopsy.failure_was_environmental is True


def test_adverse_news_loss_classified_news_adverse() -> None:
    engine = TradeAutopsyEngine()
    autopsy = engine.analyse("T4", "VCP", pnl_r=-1.0, regime_at_entry="BULL", had_adverse_news=True)
    assert str(autopsy.primary_failure_mode) == "NEWS_ADVERSE"


def test_breakeven_outcome() -> None:
    engine = TradeAutopsyEngine()
    autopsy = engine.analyse("T5", "VCP", pnl_r=0.01, regime_at_entry="BULL")
    assert autopsy.outcome == TradeOutcome.BREAKEVEN


# ── Attribution ───────────────────────────────────────────────────────────────


def test_attribution_counts_wins_and_losses() -> None:
    tae = TradeAutopsyEngine()
    ae = AttributionEngine()
    for i in range(5):
        ae.add(tae.analyse(f"W{i}", "VCP", pnl_r=2.0, regime_at_entry="BULL"))
    for i in range(3):
        ae.add(tae.analyse(f"L{i}", "VCP", pnl_r=-1.0, regime_at_entry="BULL"))

    summary = ae.summary("VCP")
    assert summary.total_trades == 8
    assert summary.wins == 5
    assert summary.losses == 3


def test_attribution_tracks_multiple_strategies() -> None:
    tae = TradeAutopsyEngine()
    ae = AttributionEngine()
    ae.add(tae.analyse("A1", "VCP", pnl_r=2.0, regime_at_entry="BULL"))
    ae.add(tae.analyse("B1", "EMA", pnl_r=-1.0, regime_at_entry="BULL"))

    assert set(ae._autopsies.keys()) == {"VCP", "EMA"}
    assert ae.summary("VCP").wins == 1
    assert ae.summary("EMA").losses == 1


def test_attribution_all_summaries() -> None:
    tae = TradeAutopsyEngine()
    ae = AttributionEngine()
    for s in ["VCP", "EMA", "RS"]:
        ae.add(tae.analyse(f"{s}1", s, pnl_r=1.0, regime_at_entry="BULL"))
    summaries = ae.all_summaries()
    assert len(summaries) == 3


# ── Pattern Detector ──────────────────────────────────────────────────────────


def test_pattern_needs_3_trades_minimum() -> None:
    tae = TradeAutopsyEngine()
    det = PatternDetector()
    for i in range(2):
        det.add(tae.analyse(f"T{i}", "VCP", pnl_r=2.0, regime_at_entry="BULL"))
    assert det.detect() == []


def test_pattern_weak_confidence_at_3_trades() -> None:
    tae = TradeAutopsyEngine()
    det = PatternDetector()
    for i in range(3):
        det.add(tae.analyse(f"T{i}", "VCP", pnl_r=2.0, regime_at_entry="BULL"))
    patterns = det.detect()
    assert len(patterns) == 1
    assert patterns[0].confidence == PatternConfidence.WEAK


def test_pattern_strong_confidence_at_7_trades() -> None:
    tae = TradeAutopsyEngine()
    det = PatternDetector()
    for i in range(7):
        det.add(tae.analyse(f"T{i}", "VCP", pnl_r=2.0, regime_at_entry="BULL"))
    patterns = det.detect()
    assert patterns[0].confidence == PatternConfidence.STRONG


def test_pattern_for_known_strategy_regime() -> None:
    tae = TradeAutopsyEngine()
    det = PatternDetector()
    for i in range(5):
        det.add(tae.analyse(f"T{i}", "VCP", pnl_r=1.0, regime_at_entry="BULL"))
    p = det.pattern_for("VCP", "BULL")
    assert p is not None
    assert p.strategy == "VCP"
    assert p.regime == "BULL"


# ── Dedup guard ───────────────────────────────────────────────────────────────


def test_seen_ids_dedup_in_learning_loop() -> None:
    """Simulate _seen_ids guard: same trade_id never processed twice."""
    tae = TradeAutopsyEngine()
    ae = AttributionEngine()
    seen: set[str] = set()
    trades = [
        {"trade_id": "T1", "strategy_name": "VCP", "pnl_r": 1.5, "regime_at_entry": "BULL"},
        {"trade_id": "T1", "strategy_name": "VCP", "pnl_r": 1.5, "regime_at_entry": "BULL"},  # dup
        {"trade_id": "T2", "strategy_name": "VCP", "pnl_r": -0.8, "regime_at_entry": "BULL"},
    ]
    for t in trades:
        tid = str(t.get("trade_id", ""))
        if not tid or tid in seen:
            continue
        seen.add(tid)
        ae.add(
            tae.analyse(tid, str(t["strategy_name"]), float(t["pnl_r"]), str(t["regime_at_entry"]))
        )

    summary = ae.summary("VCP")
    assert summary.total_trades == 2  # T1 + T2, not 3
