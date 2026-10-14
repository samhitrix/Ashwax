"""Unit tests for PromotionEngine (Layer 6, spec §9)."""

from __future__ import annotations

import asyncio

import pytest
from src.learning.promotion import PromotionEngine


class _FakeRepo:
    def __init__(self) -> None:
        self.promotions: list[tuple] = []
        self.trades: list[tuple[str, float]] = []
        self.rolled_back: list[str] = []
        self.thresholds: dict[str, float] = {}
        self.active_rows: list[dict] = []
        self.trade_history: dict[str, list[float]] = {}

    async def insert_promotion(
        self, strategy, promoted_at, param_snapshot, prior_param_snapshot, baseline_wr
    ):
        self.promotions.append((strategy, param_snapshot, prior_param_snapshot, baseline_wr))

    async def record_promotion_trade(self, strategy, pnl_r):
        self.trades.append((strategy, pnl_r))

    async def mark_rolled_back(self, strategy):
        self.rolled_back.append(strategy)

    async def upsert_calibration_threshold(self, strategy, min_confidence):
        self.thresholds[strategy] = min_confidence

    async def load_active_promotions(self):
        return self.active_rows

    async def load_promotion_trades(self, strategy):
        return self.trade_history.get(strategy, [])


def test_promote_writes_config_and_tracks_prior_params(tmp_path) -> None:
    engine = PromotionEngine(config_dir=tmp_path)
    engine.promote(
        "vcp", {"sl_atr_multiplier": 2.2}, baseline_wr=0.5, prior_params={"sl_atr_multiplier": 2.0}
    )

    written = (tmp_path / "strategy_vcp.json").read_text()
    assert "2.2" in written
    assert engine.prior_params("vcp") == {"sl_atr_multiplier": 2.0}


def test_check_rollback_false_before_evaluation_window(tmp_path) -> None:
    engine = PromotionEngine(config_dir=tmp_path)
    engine.promote("vcp", {"a": 1}, baseline_wr=0.60)
    for _ in range(19):
        engine.record_trade("vcp", 1.0)  # all wins, but under the 20-trade window
    assert engine.check_rollback("vcp") is False


def test_check_rollback_true_when_win_rate_drops(tmp_path) -> None:
    engine = PromotionEngine(config_dir=tmp_path)
    engine.promote("vcp", {"a": 1}, baseline_wr=0.60)
    for _ in range(20):
        engine.record_trade("vcp", -1.0)  # all losses -> wr=0, drop of 0.60 > 0.10 threshold
    assert engine.check_rollback("vcp") is True
    assert engine.check_rollback("vcp") is False  # already rolled back, no double-trigger


def test_record_and_check_rollback_persists_via_repo(tmp_path) -> None:
    engine = PromotionEngine(config_dir=tmp_path, repo=_FakeRepo())
    engine.promote("vcp", {"a": 1}, baseline_wr=0.60)

    async def _run():
        for _ in range(19):
            await engine.record_and_check_rollback("vcp", -1.0)
        return await engine.record_and_check_rollback("vcp", -1.0)

    rolled_back = asyncio.run(_run())
    assert rolled_back is True
    assert engine._repo.rolled_back == ["vcp"]
    assert len(engine._repo.trades) == 20


def test_persist_promotion_calls_repo_insert(tmp_path) -> None:
    repo = _FakeRepo()
    engine = PromotionEngine(config_dir=tmp_path, repo=repo)
    engine.promote("vcp", {"a": 1}, baseline_wr=0.5, prior_params={"a": 0.9})

    asyncio.run(engine.persist_promotion("vcp"))

    assert len(repo.promotions) == 1
    assert repo.promotions[0][0] == "vcp"
    assert repo.promotions[0][2] == {"a": 0.9}


def test_persist_promotion_noop_without_repo(tmp_path) -> None:
    engine = PromotionEngine(config_dir=tmp_path)
    engine.promote("vcp", {"a": 1}, baseline_wr=0.5)
    asyncio.run(engine.persist_promotion("vcp"))  # must not raise


@pytest.mark.parametrize("repo", [None, _FakeRepo()])
def test_adjust_confidence_threshold_is_a_static_helper(repo) -> None:
    asyncio.run(PromotionEngine.adjust_confidence_threshold(repo, "oneil_canslim", 0.75))
    if repo is not None:
        assert repo.thresholds["oneil_canslim"] == 0.75


def test_restore_from_repo_rebuilds_records_and_trade_history(tmp_path) -> None:
    repo = _FakeRepo()
    repo.active_rows = [
        {
            "strategy_name": "vcp",
            "promoted_at": "2026-09-17T00:00:00",
            "param_snapshot": {"a": 1.1},
            "prior_param_snapshot": {"a": 1.0},
            "baseline_wr": 0.55,
        }
    ]
    repo.trade_history["vcp"] = [1.0, -1.0, 1.0]
    engine = PromotionEngine(config_dir=tmp_path, repo=repo)

    asyncio.run(engine.restore_from_repo())

    assert engine.active_strategies() == ["vcp"]
    assert engine.prior_params("vcp") == {"a": 1.0}
    assert engine._records["vcp"].post_promotion_pnl == [1.0, -1.0, 1.0]
