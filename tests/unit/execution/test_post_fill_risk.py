"""Tests for post-fill risk verification + SL tightening."""

from __future__ import annotations

import pytest
from src.execution.post_fill_risk import (
    INCIDENT_SLIPPAGE_RISK_BREACH,
    reanchor_protection,
    verify_post_fill_risk,
)


def test_within_cap_not_breached() -> None:
    r = verify_post_fill_risk(
        fill_price=100.0, current_sl=98.0, quantity=10, direction="BUY", max_risk_rupees=30.0
    )
    assert r.breached is False
    assert r.actual_risk_rupees == pytest.approx(20.0)
    assert r.suggested_sl == 98.0  # unchanged
    assert r.incident_code == ""


def test_long_breach_tightens_sl_up() -> None:
    # risk = |105 - 100| * 10 = 50 > cap 30 → tighten
    r = verify_post_fill_risk(
        fill_price=105.0, current_sl=100.0, quantity=10, direction="BUY", max_risk_rupees=30.0
    )
    assert r.breached is True
    assert r.actual_risk_rupees == pytest.approx(50.0)
    # max distance = 30/10 = 3 → new SL = 105 - 3 = 102 (raised toward entry)
    assert r.suggested_sl == pytest.approx(102.0)
    # risk at tightened SL equals the cap exactly
    assert abs(105.0 - r.suggested_sl) * 10 == pytest.approx(30.0)
    assert r.incident_code == INCIDENT_SLIPPAGE_RISK_BREACH


def test_short_breach_tightens_sl_down() -> None:
    # short: SL above entry. risk = |100 - 105| * 10 = 50 > cap 30
    r = verify_post_fill_risk(
        fill_price=100.0, current_sl=105.0, quantity=10, direction="SELL", max_risk_rupees=30.0
    )
    assert r.breached is True
    # new SL = 100 + 3 = 103 (lowered toward entry)
    assert r.suggested_sl == pytest.approx(103.0)
    assert abs(100.0 - r.suggested_sl) * 10 == pytest.approx(30.0)


def test_exactly_at_cap_not_breached() -> None:
    r = verify_post_fill_risk(
        fill_price=103.0, current_sl=100.0, quantity=10, direction="BUY", max_risk_rupees=30.0
    )
    assert r.breached is False  # 30 == cap, not over


@pytest.mark.parametrize("qty,cap", [(0, 30.0), (-1, 30.0), (10, 0.0), (10, -5.0)])
def test_invalid_inputs_raise(qty: int, cap: float) -> None:
    with pytest.raises(ValueError):
        verify_post_fill_risk(
            fill_price=100.0, current_sl=98.0, quantity=qty, direction="BUY", max_risk_rupees=cap
        )


# --- reanchor_protection -------------------------------------------------


def test_reanchor_long_inband_preserves_rr() -> None:
    # signal: entry 100, sl 98 (risk 2), target 105 (R:R 2.5). Fill drifts to 101
    # (unfavorable for a long buy — paid more than signalled).
    r = reanchor_protection(
        fill_price=101.0,
        signal_entry=100.0,
        signal_sl=98.0,
        signal_target=105.0,
        direction="long",
    )
    assert r.degenerate is False
    # Naive fill-risk would be 101-2=99, TIGHTER than the signal's own SL (98) —
    # capped back to 98 so ordinary slippage never ratchets the stop past where
    # the signal placed it (2026-07-30 APOLLOHOSP: this exact shift turned a
    # normal pullback into an instant stop-out).
    assert r.sl == pytest.approx(98.0)
    assert r.target == pytest.approx(106.0)  # 101 + 2.5*2
    assert r.sl < 101.0 < r.target  # valid geometry by construction


def test_reanchor_eichermot_fill_past_old_target_yields_valid_target() -> None:
    # EICHERMOT repro: signal entry ~7600, target 7608.88, fill 7615.61 (past target).
    # Old code armed target 7608.88 below fill → instant TARGET_HIT loss.
    r = reanchor_protection(
        fill_price=7615.61,
        signal_entry=7600.0,
        signal_sl=7596.45,
        signal_target=7608.88,  # below fill — the bug
        direction="BUY",
    )
    assert r.degenerate is False
    assert r.target > 7615.61  # fresh target above the fill → no instant exit
    # Naive fill-risk (7615.61-3.55=7612.06) is far TIGHTER than the signal's own
    # SL — capped back to the signal SL. No quantity/cap supplied here, so nothing
    # re-tightens it for the (now much larger) risk; see
    # test_reanchor_large_gap_cap_still_protects_risk for that half of the story.
    assert r.sl == pytest.approx(7596.45)


def test_reanchor_large_gap_cap_still_protects_risk() -> None:
    # Same large gap as the EICHERMOT case, but with a real per-trade rupee cap
    # supplied (as the live order_lifecycle path always does). The SL-tighten cap
    # freezes the stop at the signal SL, which would leave risk/share = 19.16 —
    # well over a small cap — so verify_post_fill_risk must still re-tighten it.
    r = reanchor_protection(
        fill_price=7615.61,
        signal_entry=7600.0,
        signal_sl=7596.45,
        signal_target=7608.88,
        direction="BUY",
        quantity=10,
        max_risk_rupees=100.0,  # 10/share max
    )
    assert r.cap_breached is True
    assert r.sl == pytest.approx(7615.61 - 10.0)
    assert abs(7615.61 - r.sl) * 10 == pytest.approx(100.0)


def test_reanchor_short_mirrors() -> None:
    # short: entry 100, sl 102 (risk 2), target 95 (R:R 2.5). Fill at 99
    # (favorable for a short sell — but that still makes the raw fill-risk SL
    # tighter than the signal's, so it must be capped the same way as longs).
    r = reanchor_protection(
        fill_price=99.0,
        signal_entry=100.0,
        signal_sl=102.0,
        signal_target=95.0,
        direction="short",
    )
    # Naive fill-risk would be 99+2=101, TIGHTER than signal_sl (102) — capped.
    assert r.sl == pytest.approx(102.0)
    assert r.target == pytest.approx(94.0)  # 99 - 2.5*2
    assert r.target < 99.0 < r.sl


def test_reanchor_cap_breach_tightens_sl() -> None:
    # risk per share 2, qty 20 → 40 > cap 30 → SL tightened to 30/20 = 1.5 distance.
    r = reanchor_protection(
        fill_price=101.0,
        signal_entry=100.0,
        signal_sl=98.0,
        signal_target=105.0,
        direction="BUY",
        quantity=20,
        max_risk_rupees=30.0,
    )
    assert r.cap_breached is True
    assert r.sl == pytest.approx(101.0 - 1.5)  # 99.5
    assert r.incident_code == INCIDENT_SLIPPAGE_RISK_BREACH
    assert r.target == pytest.approx(106.0)  # target unaffected by cap


def test_reanchor_degenerate_geometry_flagged() -> None:
    r = reanchor_protection(
        fill_price=100.0,
        signal_entry=100.0,
        signal_sl=100.0,  # risk 0
        signal_target=100.0,
        direction="BUY",
    )
    assert r.degenerate is True
