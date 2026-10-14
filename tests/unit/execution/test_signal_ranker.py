"""Unit tests for the shadow signal ranker."""

from __future__ import annotations

from src.execution.signal_ranker import rank_candidates, score_candidate


def _long(**kw):
    base = dict(bar_high=110.0, bar_low=100.0, bar_close=110.0, is_short=False)
    base.update(kw)
    return score_candidate(**base)


def test_strong_breakout_outranks_weak_one():
    strong = _long(breakout_volume=200, avg_volume=100, or_width_pct=0.9, nifty_aligned=True)
    weak = _long(
        bar_close=100.0, breakout_volume=10, avg_volume=100, or_width_pct=0.05, nifty_aligned=False
    )
    assert strong.score > weak.score
    assert strong.score == 1.0


def test_missing_inputs_score_neutral_not_zero():
    """A candidate must not rank last merely because data was unavailable."""
    c = _long(bar_close=105.0)
    assert c.score == 0.5
    assert all(v == 0.5 for v in c.components.values())


def test_follow_through_is_direction_aware():
    """Closing at the low is strong for a short and weak for a long."""
    at_low_long = _long(bar_close=100.0)
    at_low_short = score_candidate(bar_high=110.0, bar_low=100.0, bar_close=100.0, is_short=True)
    assert at_low_short.components["follow_through"] == 1.0
    assert at_low_long.components["follow_through"] == 0.0


def test_width_band_rejects_dead_and_oversized_ranges():
    assert _long(or_width_pct=0.05).components["width"] == 0.0  # noise
    assert _long(or_width_pct=5.0).components["width"] == 0.0  # uncontrollable risk
    assert _long(or_width_pct=0.9).components["width"] == 1.0  # mid-band


def test_zero_range_bar_does_not_divide_by_zero():
    c = score_candidate(bar_high=100.0, bar_low=100.0, bar_close=100.0, is_short=True)
    assert c.score == 0.5


def test_volume_saturates_and_never_exceeds_one():
    c = _long(breakout_volume=10_000, avg_volume=100)
    assert c.components["volume"] == 1.0
    assert c.score <= 1.0


def test_ranking_breaks_ties_deterministically_by_symbol():
    ranked = rank_candidates(
        [
            {"symbol": "B", "score": 0.5},
            {"symbol": "A", "score": 0.5},
            {"symbol": "C", "score": 0.9},
        ]
    )
    assert [c["symbol"] for c in ranked] == ["C", "A", "B"]


def test_ranking_empty_list_is_safe():
    assert rank_candidates([]) == []


# ── Global cross-strategy selection (2026-08-27) ───────────────────────────────


def _c(sym: str, strategy: str, score: float) -> dict:
    return {"symbol": sym, "strategy_name": strategy, "score": score}


def test_select_top_candidates_ranks_across_strategies_not_within() -> None:
    """The whole point: a better setup from a later strategy must beat a weaker one
    from the strategy that happened to be evaluated first."""
    from src.execution.signal_ranker import select_top_candidates

    accepted, rejected = select_top_candidates(
        [_c("WEAK", "minervini_vcp", 0.40), _c("STRONG", "oneil_canslim", 0.95)],
        max_total=1,
        max_per_strategy=2,
    )

    assert [c["symbol"] for c in accepted] == ["STRONG"]
    assert [c["symbol"] for c in rejected] == ["WEAK"]


def test_select_top_candidates_honours_daily_cap() -> None:
    from src.execution.signal_ranker import select_top_candidates

    accepted, rejected = select_top_candidates(
        [_c("A", "s1", 0.9), _c("B", "s2", 0.8), _c("C", "s1", 0.7)],
        max_total=2,
        max_per_strategy=5,
    )

    assert [c["symbol"] for c in accepted] == ["A", "B"]
    assert len(rejected) == 1


def test_select_top_candidates_honours_per_strategy_cap() -> None:
    from src.execution.signal_ranker import select_top_candidates

    accepted, _ = select_top_candidates(
        [_c("A", "s1", 0.9), _c("B", "s1", 0.8), _c("C", "s2", 0.7)],
        max_total=3,
        max_per_strategy=1,
    )

    assert [c["symbol"] for c in accepted] == ["A", "C"]


def test_select_top_candidates_respects_trades_already_placed_today() -> None:
    """A strategy already at its cap from an earlier scan must not take a slot."""
    from src.execution.signal_ranker import select_top_candidates

    accepted, _ = select_top_candidates(
        [_c("A", "s1", 0.9), _c("B", "s2", 0.5)],
        max_total=2,
        max_per_strategy=1,
        per_strategy_counts={"s1": 1},
    )

    assert [c["symbol"] for c in accepted] == ["B"]


def test_select_top_candidates_never_accepts_the_same_symbol_twice() -> None:
    """2026-09-07: CGCL got two same-day CNC orders — minervini_vcp AND
    oneil_canslim both signalled it, both fit under max_total/max_per_strategy,
    and the only other same-symbol guard (order_monitor's FILLED positions)
    couldn't see the first one yet because its AMO hadn't filled. The best-ranked
    candidate for a symbol must win and the rest must be rejected, regardless of
    strategy, cap headroom, or fill timing."""
    from src.execution.signal_ranker import select_top_candidates

    accepted, rejected = select_top_candidates(
        [_c("CGCL", "minervini_vcp", 0.789), _c("CGCL", "oneil_canslim", 0.789)],
        max_total=5,
        max_per_strategy=5,
    )

    assert len(accepted) == 1
    assert (
        accepted[0]["strategy_name"] == "minervini_vcp"
    )  # ties break on symbol, but only one wins
    assert len(rejected) == 1
    assert rejected[0]["strategy_name"] == "oneil_canslim"


def test_select_top_candidates_keeps_the_better_ranked_duplicate_symbol() -> None:
    from src.execution.signal_ranker import select_top_candidates

    accepted, rejected = select_top_candidates(
        [_c("CGCL", "weak_strategy", 0.30), _c("CGCL", "strong_strategy", 0.95)],
        max_total=5,
        max_per_strategy=5,
    )

    assert [c["strategy_name"] for c in accepted] == ["strong_strategy"]
    assert [c["strategy_name"] for c in rejected] == ["weak_strategy"]


def test_select_top_candidates_different_symbols_both_accepted() -> None:
    """The dedup is per-symbol, not a blanket one-trade-per-cycle limit."""
    from src.execution.signal_ranker import select_top_candidates

    accepted, _ = select_top_candidates(
        [_c("CGCL", "minervini_vcp", 0.9), _c("IFCI", "oneil_canslim", 0.8)],
        max_total=5,
        max_per_strategy=5,
    )

    assert {c["symbol"] for c in accepted} == {"CGCL", "IFCI"}


# ── Signal Quality Score ranking (PR3, config/learning.json signal_ranker.mode) ──


def _c_sqs(sym: str, score: float, sqs_total: float | None) -> dict:
    d = _c(sym, "s1", score)
    if sqs_total is not None:
        d["sqs"] = {"total": sqs_total}
    return d


def test_rank_candidates_by_sqs_sorts_on_sqs_total_not_legacy_score() -> None:
    from src.execution.signal_ranker import rank_candidates_by_sqs

    ranked = rank_candidates_by_sqs(
        [_c_sqs("HIGH_LEGACY_LOW_SQS", 0.95, 20.0), _c_sqs("LOW_LEGACY_HIGH_SQS", 0.10, 80.0)]
    )
    assert [c["symbol"] for c in ranked] == ["LOW_LEGACY_HIGH_SQS", "HIGH_LEGACY_LOW_SQS"]


def test_rank_candidates_by_sqs_falls_back_to_legacy_score_when_sqs_missing() -> None:
    """A candidate built before the mode flipped live (no "sqs" key) must not be
    silently dropped to the bottom — it degrades to its legacy score instead."""
    from src.execution.signal_ranker import rank_candidates_by_sqs

    ranked = rank_candidates_by_sqs([_c_sqs("NO_SQS", 0.99, None), _c_sqs("HAS_SQS", 0.10, 50.0)])
    assert [c["symbol"] for c in ranked] == ["NO_SQS", "HAS_SQS"]


def test_select_top_candidates_use_sqs_ranks_by_signal_quality_score() -> None:
    from src.execution.signal_ranker import select_top_candidates

    accepted, _ = select_top_candidates(
        [_c_sqs("LEGACY_WINNER", 0.99, 10.0), _c_sqs("SQS_WINNER", 0.01, 90.0)],
        max_total=1,
        max_per_strategy=5,
        use_sqs=True,
    )
    assert [c["symbol"] for c in accepted] == ["SQS_WINNER"]


def test_rank_candidates_by_sqs_applies_priority_weight_tie_break() -> None:
    """Layer 4A: a lower-SQS candidate with a much higher regime priority weight
    can overtake a higher-SQS candidate with a low weight."""
    from src.execution.signal_ranker import rank_candidates_by_sqs

    low_sqs_high_priority = _c_sqs("A", 0.5, 60.0)
    low_sqs_high_priority["priority_weight"] = 1.0  # multiplier 1.0 -> 60.0
    high_sqs_low_priority = _c_sqs("B", 0.5, 65.0)
    high_sqs_low_priority["priority_weight"] = 0.0  # multiplier 0.5 -> 32.5

    ranked = rank_candidates_by_sqs([high_sqs_low_priority, low_sqs_high_priority])
    assert [c["symbol"] for c in ranked] == ["A", "B"]


def test_rank_candidates_by_sqs_ignores_priority_weight_when_absent() -> None:
    """off/shadow mode: callers never attach priority_weight, ranking is pure SQS."""
    from src.execution.signal_ranker import rank_candidates_by_sqs

    ranked = rank_candidates_by_sqs([_c_sqs("LOW", 0.5, 60.0), _c_sqs("HIGH", 0.5, 65.0)])
    assert [c["symbol"] for c in ranked] == ["HIGH", "LOW"]


def test_select_top_candidates_default_use_sqs_false_matches_legacy_ranking() -> None:
    """use_sqs defaults to False — off-mode behavior must stay byte-identical."""
    from src.execution.signal_ranker import select_top_candidates

    accepted, _ = select_top_candidates(
        [_c_sqs("LEGACY_WINNER", 0.99, 10.0), _c_sqs("SQS_WINNER", 0.01, 90.0)],
        max_total=1,
        max_per_strategy=5,
    )
    assert [c["symbol"] for c in accepted] == ["LEGACY_WINNER"]
