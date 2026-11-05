"""Tests for the seller-desk expiry selection engine (options_spec.md §6)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.options.expiry_selector import (
    dte,
    load_expiry_calendar,
    next_expiry,
    pre_entry_dte_check,
    pre_entry_dte_check_from_expiry,
    select_calendar_expiries,
    select_expiry,
)

_TODAY = date(2026, 8, 1)


def _calendar() -> dict[str, list[date]]:
    return {
        "NIFTY": [
            date(2026, 8, 6),
            date(2026, 8, 13),
            date(2026, 8, 20),
            date(2026, 8, 27),
            date(2026, 9, 24),
            date(2026, 10, 29),
        ]
    }


def test_load_expiry_calendar_ignores_comments(tmp_path: Path) -> None:
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"_comment": "x", "NIFTY": ["2026-08-06", "2026-08-13"]}))
    cal = load_expiry_calendar(p)
    assert cal == {"NIFTY": [date(2026, 8, 6), date(2026, 8, 13)]}


def test_next_expiry_index_specific() -> None:
    assert next_expiry("NIFTY", _TODAY, _calendar()) == date(2026, 8, 6)


def test_next_expiry_none_when_no_future_dates() -> None:
    assert next_expiry("BANKNIFTY", _TODAY, _calendar()) is None


def test_next_expiry_strips_exchange_prefix() -> None:
    """Regression: orchestrator.py's seller-desk loop calls run_cycle with an
    'NSE:'-prefixed underlying (e.g. 'NSE:NIFTY'), but the calendar JSON keys
    are bare roots ('NIFTY'). Without stripping the prefix, next_expiry always
    misses the lookup and pre_entry_dte_check blocks every real cycle silently
    (found live 2026-07-29: zero trades all day, zero seller_desk_cycle_start
    logs — pre_entry_dte_check returned False on every call before that log
    line was ever reached)."""
    assert next_expiry("NSE:NIFTY", _TODAY, _calendar()) == date(2026, 8, 6)


def test_dte_computation() -> None:
    assert dte(date(2026, 8, 6), _TODAY) == 5


def test_pre_entry_dte_check_blocks_below_minimum() -> None:
    calendar = {"NIFTY": [date(2026, 8, 2)]}  # dte=1
    allowed, reason = pre_entry_dte_check("NIFTY", _TODAY, calendar)
    assert allowed is False
    assert "dte=1" in reason


def test_pre_entry_dte_check_allows_above_minimum() -> None:
    allowed, reason = pre_entry_dte_check("NIFTY", _TODAY, _calendar())
    assert allowed is True


def test_pre_entry_dte_check_allows_with_exchange_prefix() -> None:
    """Regression: this is the exact call shape used in production
    (seller_desk.py's run_cycle receives 'NSE:NIFTY' from orchestrator.py)."""
    allowed, reason = pre_entry_dte_check("NSE:NIFTY", _TODAY, _calendar())
    assert allowed is True


# ── pre_entry_dte_check_from_expiry (2026-09-10) ─────────────────────────────
# nse_expiry_calendar.json is unverified placeholder data (its own
# _VERIFY_BEFORE_ENABLE note admits this) and disagreed with the real NFO
# instrument dump for BANKNIFTY/FINNIFTY (both actually ~monthly, calendar
# still listed them weekly) -- letting pre_entry_dte_check wrongly ALLOW
# entry against a fictional near-dated expiry. This variant checks against
# an already-known real expiry (e.g. a freshly fetched OptionChain's) instead.


def test_pre_entry_dte_check_from_expiry_blocks_below_minimum() -> None:
    allowed, reason = pre_entry_dte_check_from_expiry(date(2026, 8, 2), _TODAY)  # dte=1
    assert allowed is False
    assert "dte=1" in reason


def test_pre_entry_dte_check_from_expiry_allows_above_minimum() -> None:
    allowed, reason = pre_entry_dte_check_from_expiry(date(2026, 8, 6), _TODAY)  # dte=5
    assert allowed is True
    assert "dte=5" in reason


def test_pre_entry_dte_check_from_expiry_respects_custom_min_dte() -> None:
    allowed, _ = pre_entry_dte_check_from_expiry(date(2026, 8, 6), _TODAY, min_dte=10)
    assert allowed is False  # dte=5 < min_dte=10


def test_select_expiry_iron_condor_band_30_45() -> None:
    # Aug 27 = dte 26 (below band), Sep 24 = dte 54 (above band)... need one in 30-45
    calendar = {"NIFTY": [date(2026, 8, 27), date(2026, 9, 10), date(2026, 10, 29)]}
    picked = select_expiry("iron_condor", _TODAY, calendar, "NIFTY")
    assert picked == date(2026, 9, 10)  # dte=40, within [30,45]


def test_select_expiry_none_when_nothing_in_band() -> None:
    calendar = {"NIFTY": [date(2026, 8, 3)]}  # dte=2, far below any band
    assert select_expiry("iron_condor", _TODAY, calendar, "NIFTY") is None


def test_select_expiry_straddle_must_bracket_event() -> None:
    # Both dates are within the straddle DTE band [7,21]; Aug 10 (dte=9) sits
    # BEFORE the event and must be filtered out even though it's in-band.
    calendar = {"NIFTY": [date(2026, 8, 10), date(2026, 8, 20)]}  # dte 9, 19
    event = date(2026, 8, 15)
    picked = select_expiry("long_straddle", _TODAY, calendar, "NIFTY", event_date=event)
    assert picked == date(2026, 8, 20)  # only one strictly after the event


def test_select_calendar_expiries() -> None:
    calendar = {
        "NIFTY": [
            date(2026, 8, 6),  # weekly
            date(2026, 8, 13),
            date(2026, 8, 20),
            date(2026, 8, 27),  # last Aug expiry -> "monthly" for August
            date(2026, 9, 24),  # last Sep expiry -> monthly for September
        ]
    }
    result = select_calendar_expiries(_TODAY, calendar, "NIFTY")
    assert result == (date(2026, 8, 6), date(2026, 8, 27))


def test_select_calendar_expiries_none_when_insufficient() -> None:
    calendar = {"NIFTY": [date(2026, 8, 6)]}
    assert select_calendar_expiries(_TODAY, calendar, "NIFTY") is None


def test_dte_band_known_strategy() -> None:
    from src.options.expiry_selector import dte_band

    assert dte_band("iron_condor") == (30, 45)
    assert dte_band("iron_butterfly") == (14, 21)


def test_dte_band_unknown_strategy_unrestricted() -> None:
    from src.options.expiry_selector import dte_band

    assert dte_band("unknown_type") == (0, 999)


def test_dte_band_low_ivr_mode_disabled_ivr_has_no_effect(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.options import seller_settings
    from src.options.expiry_selector import dte_band

    # Explicit disabled config, not the live config's current value (which
    # is operator-controlled -- see config/options_settings.json
    # low_ivr_mode.enabled) -- this test verifies the disabled CODE PATH.
    monkeypatch.setattr(seller_settings, "low_ivr_mode", lambda settings=None: {"enabled": False})
    assert dte_band("bull_put_spread", ivr=5.0) == (21, 45)


def test_dte_band_low_ivr_mode_enabled_and_below_threshold(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.options import seller_settings
    from src.options.expiry_selector import dte_band

    monkeypatch.setattr(
        seller_settings,
        "low_ivr_mode",
        lambda settings=None: {"enabled": True, "ivr_threshold": 25.0, "dte_range": [10, 18]},
    )
    assert dte_band("bull_put_spread", ivr=10.0) == (10, 18)
    assert dte_band("bear_call_spread", ivr=10.0) == (10, 18)


def test_dte_band_low_ivr_mode_enabled_but_above_threshold_unaffected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.options import seller_settings
    from src.options.expiry_selector import dte_band

    monkeypatch.setattr(
        seller_settings,
        "low_ivr_mode",
        lambda settings=None: {"enabled": True, "ivr_threshold": 25.0, "dte_range": [10, 18]},
    )
    assert dte_band("bull_put_spread", ivr=40.0) == (21, 45)


def test_dte_band_low_ivr_mode_scoped_to_credit_spreads_only(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.options import seller_settings
    from src.options.expiry_selector import dte_band

    monkeypatch.setattr(
        seller_settings,
        "low_ivr_mode",
        lambda settings=None: {"enabled": True, "ivr_threshold": 25.0, "dte_range": [10, 18]},
    )
    # iron_condor is not in _LOW_IVR_DTE_TYPES -- unaffected even below threshold
    assert dte_band("iron_condor", ivr=10.0) == (30, 45)
