"""Layer 4A (StrategySelector) / Layer 5A (UniverseAdapter) paper/live
separation — a strategy's simulated paper outcomes must never blend into or
substitute for its real-money live outcomes, and vice versa."""

from __future__ import annotations

from src.learning.strategy_selector import StrategySelector
from src.learning.universe_adapter import UniverseAdapter


def test_strategy_selector_tracks_paper_and_live_independently() -> None:
    sel = StrategySelector()
    sel.update("vcp", "BULL", expectancy_r=1.5, mode="live")
    sel.update("vcp", "BULL", expectancy_r=-2.0, mode="paper")

    live = sel.get_priorities("BULL", ["vcp"], mode="live")
    paper = sel.get_priorities("BULL", ["vcp"], mode="paper")

    assert live[0].expectancy_r == 1.5
    assert paper[0].expectancy_r == -2.0


def test_strategy_selector_defaults_to_live_mode() -> None:
    sel = StrategySelector()
    sel.update("vcp", "BULL", expectancy_r=1.5)  # no mode kwarg
    assert sel.get_priorities("BULL", ["vcp"])[0].expectancy_r == 1.5
    assert sel.get_priorities("BULL", ["vcp"], mode="paper")[0].expectancy_r == 0.3  # no data yet


def test_strategy_selector_unknown_mode_has_no_data() -> None:
    sel = StrategySelector()
    sel.update("vcp", "BULL", expectancy_r=1.5, mode="live")
    paper_priorities = sel.get_priorities("BULL", ["vcp"], mode="paper")
    assert paper_priorities[0].expectancy_r == 0.3  # neutral placeholder, not live's 1.5


def test_universe_adapter_tracks_paper_and_live_independently() -> None:
    ua = UniverseAdapter()
    live_wins = [{"pnl_r": 1.0, "sector": "IT"} for _ in range(10)]
    paper_wins = [{"pnl_r": 1.0, "sector": "AUTO"} for _ in range(10)]
    ua.update("vcp", live_wins, mode="live")
    ua.update("vcp", paper_wins, mode="paper")

    meta = {"A": {"sector": "IT", "rs_rank": 70}, "B": {"sector": "AUTO", "rs_rank": 70}}
    live_kept = ua.filter_universe("vcp", ["A", "B"], meta, mode="live")
    paper_kept = ua.filter_universe("vcp", ["A", "B"], meta, mode="paper")

    assert live_kept == ["A"]
    assert paper_kept == ["B"]


def test_universe_adapter_no_data_for_mode_returns_symbols_unfiltered() -> None:
    ua = UniverseAdapter()
    ua.update("vcp", [{"pnl_r": 1.0, "sector": "IT"} for _ in range(10)], mode="live")
    meta = {"A": {"sector": "AUTO", "rs_rank": 10}}
    # No paper data recorded -> paper mode must not apply live's adaptation.
    assert ua.filter_universe("vcp", ["A"], meta, mode="paper") == ["A"]
