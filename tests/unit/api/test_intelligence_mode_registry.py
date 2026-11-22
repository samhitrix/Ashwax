"""Intelligence/Learning endpoints: one mode per call, registry
strategies only, sample-size reliability labels. Made-up names/values only."""

from __future__ import annotations

from typing import Any

import pytest
import src.api.server as srv


def test_reliability_bands() -> None:
    assert srv._reliability(0) == "insufficient"
    assert srv._reliability(29) == "insufficient"
    assert srv._reliability(30) == "tentative"
    assert srv._reliability(100) == "usable"


def test_norm_mode_defaults_live() -> None:
    assert srv._norm_mode("PAPER") == "paper"
    assert srv._norm_mode("anything") == "live"


def test_in_registry_unreadable_registry_shows_all() -> None:
    assert srv._in_registry("x", None)
    assert not srv._in_registry("old_dead", {"alive"})


def test_small_sample_recommendation_never_claims_edge() -> None:
    recs = srv._strategy_recommendations(0.8, 3.0, {}, n=5)
    assert len(recs) == 1 and "too few" in recs[0]


def _trade(strategy: str, r: float, asset: str = "equity") -> dict[str, Any]:
    return {
        "strategy_name": strategy,
        "pnl_r": r,
        "exit_ts": "2026-01-05T10:00:00",
        "regime_at_entry": "NEUTRAL",
        "asset_class": asset,
    }


@pytest.mark.asyncio
async def test_attribution_filters_registry_and_tags_mode(monkeypatch: Any) -> None:
    import src.orchestrator as orch

    async def _eq(pool: Any, mode: str = "all") -> list[dict[str, Any]]:
        return [_trade("alive_strat", 1.0), _trade("dead_strat", -1.0)]

    async def _opt(pool: Any, mode: str = "all") -> list[dict[str, Any]]:
        return [_trade("alive_opt", 0.2, "options")]

    monkeypatch.setattr(orch, "_fetch_closed_trades_from_db", _eq)
    monkeypatch.setattr(orch, "_fetch_closed_options_from_db", _opt)
    monkeypatch.setattr(srv, "_registry_strategy_names", lambda: {"alive_strat", "alive_opt"})

    out = await srv.get_attribution(mode="paper")
    names = {c["strategy"] for c in out}
    assert names == {"alive_strat", "alive_opt"}
    assert all(c["mode"] == "paper" and c["reliability"] == "insufficient" for c in out)
    assert {c["assetClass"] for c in out} == {"equity", "options"}


@pytest.mark.asyncio
async def test_expectancy_options_never_gated(monkeypatch: Any) -> None:
    async def _fake(mode: str) -> list[dict[str, Any]]:
        return [_trade("alive_opt", -1.0, "options")] * 25

    monkeypatch.setattr(srv, "_closed_trades_for_mode", _fake)
    monkeypatch.setattr(srv, "_read_cache", lambda *a, **k: {})
    out = await srv.get_expectancy(mode="live")
    assert out["alive_opt"]["status"] == "NOT_GATED"
    assert out["alive_opt"]["should_pause"] is False
    assert out["alive_opt"]["total_trades"] == 25


def test_learning_summary_mode_file_and_registry(monkeypatch: Any) -> None:
    seen: list[str] = []

    def _fake_cache(name: str, *a: Any, **k: Any) -> dict[str, Any]:
        seen.append(name)
        return {
            "summaries": {
                "alive_strat": {"total_trades": 40},
                "dead_strat": {"total_trades": 9},
            },
            "patterns": [
                {"strategy": "alive_strat", "observation_count": 9},
                {"strategy": "dead_strat", "observation_count": 3},
            ],
            "trade_count": 49,
        }

    monkeypatch.setattr(srv, "_read_cache", _fake_cache)
    monkeypatch.setattr(srv, "_registry_strategy_names", lambda: {"alive_strat"})
    out = srv.get_learning_summary(mode="paper")
    assert seen == ["learning_summary_paper.json"]
    assert list(out["summaries"]) == ["alive_strat"]
    assert out["summaries"]["alive_strat"]["reliability"] == "tentative"
    assert [p["strategy"] for p in out["patterns"]] == ["alive_strat"]
    assert out["patterns"][0]["reliability"] == "insufficient"
    assert out["trade_count"] == 40
