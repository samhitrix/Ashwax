"""Unit tests for src/market_data/corporate_actions.py"""

from __future__ import annotations

import pytest
from src.market_data.corporate_actions import ActionType, CorporateAction, adjust_position


def _action(atype: ActionType, num: int, den: int) -> CorporateAction:
    return CorporateAction(
        symbol="X",
        action_type=atype,
        ratio_numerator=num,
        ratio_denominator=den,
        ex_date="2026-06-01",
    )


def test_split_2_for_1():
    adj = adjust_position("RELIANCE", 10, 2500.0, 2400.0, 2600.0, _action(ActionType.SPLIT, 2, 1))
    assert adj.new_quantity == 20
    assert adj.new_avg_price == pytest.approx(1250.0)
    assert adj.new_stop_loss == pytest.approx(1200.0)
    assert adj.new_target == pytest.approx(1300.0)
    assert adj.adjustment_factor == pytest.approx(2.0)


def test_split_5_for_1():
    adj = adjust_position("X", 5, 1000.0, 950.0, 1050.0, _action(ActionType.SPLIT, 5, 1))
    assert adj.new_quantity == 25
    assert adj.new_avg_price == pytest.approx(200.0)
    assert adj.new_stop_loss == pytest.approx(190.0)


def test_bonus_1_for_1():
    adj = adjust_position("X", 10, 500.0, 490.0, 520.0, _action(ActionType.BONUS, 1, 1))
    assert adj.new_quantity == 20
    assert adj.new_avg_price == pytest.approx(250.0)
    assert adj.new_stop_loss == pytest.approx(245.0)
    assert adj.adjustment_factor == pytest.approx(2.0)


def test_dividend_no_change():
    adj = adjust_position("X", 10, 500.0, 490.0, 520.0, _action(ActionType.DIVIDEND, 0, 1))
    assert adj.new_quantity == 10
    assert adj.new_stop_loss == pytest.approx(490.0)
    assert adj.adjustment_factor == pytest.approx(1.0)


def test_note_contains_action_info():
    adj = adjust_position("TCS", 10, 3000.0, 2900.0, 3100.0, _action(ActionType.SPLIT, 2, 1))
    assert "SPLIT" in adj.note
    assert "2:1" in adj.note


# ── CorporateActionWatcher ─────────────────────────────────────────────────────

from src.market_data.corporate_actions import CorporateActionWatcher  # noqa: E402


def _watcher(*actions: CorporateAction) -> CorporateActionWatcher:
    return CorporateActionWatcher(actions=list(actions))


def test_watcher_empty_returns_nothing() -> None:
    assert _watcher().pending_for_today({"INFY"}, today="2026-06-15") == []


def test_watcher_pending_for_today_match() -> None:
    a = CorporateAction("RELIANCE", ActionType.SPLIT, 2, 1, "2026-06-15")
    result = _watcher(a).pending_for_today({"RELIANCE"}, today="2026-06-15")
    assert len(result) == 1 and result[0].symbol == "RELIANCE"


def test_watcher_pending_ignores_wrong_date() -> None:
    a = CorporateAction("RELIANCE", ActionType.SPLIT, 2, 1, "2026-06-20")
    assert _watcher(a).pending_for_today({"RELIANCE"}, today="2026-06-15") == []


def test_watcher_pending_ignores_unmonitored_symbol() -> None:
    a = CorporateAction("TCS", ActionType.SPLIT, 2, 1, "2026-06-15")
    assert _watcher(a).pending_for_today({"INFY"}, today="2026-06-15") == []


def test_watcher_adjustments_for_positions() -> None:
    a = CorporateAction("INFY", ActionType.SPLIT, 2, 1, "2026-06-15")
    adjs = _watcher(a).adjustments_for_positions(
        [("INFY", 10, 1500.0, 1450.0, 1600.0)], today="2026-06-15"
    )
    assert len(adjs) == 1
    assert adjs[0].new_quantity == 20
    assert adjs[0].new_stop_loss == pytest.approx(725.0)


def test_watcher_adjustments_skips_no_action_symbols() -> None:
    a = CorporateAction("INFY", ActionType.SPLIT, 2, 1, "2026-06-15")
    adjs = _watcher(a).adjustments_for_positions(
        [("TCS", 5, 3000.0, 2900.0, 3200.0)], today="2026-06-15"
    )
    assert adjs == []
