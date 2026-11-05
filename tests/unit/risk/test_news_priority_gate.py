"""G5 applies the +0.10 news-priority boost only when aligned (src/risk/crm.py)."""

from __future__ import annotations

from src.risk.crm import CentralRiskManager

from tests.unit.risk.test_crm import _ctx, _sig

crm = CentralRiskManager()


def test_g5_boosts_when_priority_aligned() -> None:
    sig = _sig(meta={"llm_confidence": 0.50, "news_priority_aligned": True})
    crm._g5_news(sig, _ctx(), sig.intended_quantity)
    assert sig.meta["llm_confidence"] == 0.60


def test_g5_no_boost_when_priority_absent() -> None:
    sig = _sig(meta={"llm_confidence": 0.50})
    crm._g5_news(sig, _ctx(), sig.intended_quantity)
    assert sig.meta["llm_confidence"] == 0.50


def test_g5_no_boost_when_priority_false() -> None:
    sig = _sig(meta={"llm_confidence": 0.50, "news_priority_aligned": False})
    crm._g5_news(sig, _ctx(), sig.intended_quantity)
    assert sig.meta["llm_confidence"] == 0.50
