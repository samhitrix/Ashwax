"""_CausalInsightRepo.find_similar — Layer 7 regime-conditional retrieval fix
(equity learning-engine PR6). Previously find_similar ignored regime entirely,
returning the strategy's k most recent insights regardless of relevance."""

from __future__ import annotations

import asyncio

from src.orchestrator import _CausalInsightRepo


class _FakeLearningRepo:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def recent_insights(self, strategy: str, limit: int) -> list[dict]:
        return [r for r in self._rows if r.get("strategy") == strategy][:limit]


def _row(trade_id: str, regime: str, insight: str) -> dict:
    return {
        "trade_id": trade_id,
        "strategy": "vcp",
        "regime_at_trade": regime,
        "generalizable_insight": insight,
        "was_signal_valid": True,
        "failure_was_environmental": False,
        "what_i_would_do_differently": "",
        "confidence_in_insight": 0.7,
        "applicable_when": "",
        "sector_at_trade": "IT",
    }


def test_find_similar_prioritizes_regime_matches_over_pure_recency() -> None:
    rows = [
        _row("1", "BULL", "most recent, wrong regime"),
        _row("2", "BEAR", "second most recent, matching regime"),
        _row("3", "BEAR", "third, matching regime"),
    ]
    repo = _CausalInsightRepo(_FakeLearningRepo(rows))

    result = asyncio.run(repo.find_similar("vcp", "BEAR", k=2))

    assert [i.trade_id for i in result] == ["2", "3"]


def test_find_similar_falls_back_to_recency_when_regime_sample_is_thin() -> None:
    """Only one BEAR-regime insight exists — the remaining slot is filled from
    strategy-only recency rather than returning fewer than k results."""
    rows = [
        _row("1", "BULL", "recent, wrong regime"),
        _row("2", "BEAR", "the only regime match"),
    ]
    repo = _CausalInsightRepo(_FakeLearningRepo(rows))

    result = asyncio.run(repo.find_similar("vcp", "BEAR", k=2))

    assert {i.trade_id for i in result} == {"1", "2"}


def test_find_similar_returns_empty_on_repo_error() -> None:
    class _BrokenRepo:
        async def recent_insights(self, strategy, limit):
            raise RuntimeError("db down")

    repo = _CausalInsightRepo(_BrokenRepo())
    assert asyncio.run(repo.find_similar("vcp", "BEAR", k=10)) == []
