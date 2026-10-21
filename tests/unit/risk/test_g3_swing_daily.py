"""G3 for swing (daily-candle) signals: no time-of-day confidence bar (2026-10-06,
CPPLUS refused at 14:33 'window 14:30-15:15 requires LLM confidence >= 85%, got 65%')."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from src.risk.crm import CentralRiskManager, CRMSignal

from tests.unit.risk.test_crm_options_seller import _ctx

_IST = ZoneInfo("Asia/Kolkata")


def _sig(engine: str) -> CRMSignal:
    s = CRMSignal(
        signal_id="S",
        symbol="NSE:CPPLUS",
        direction="LONG",
        strategy_name="llm_swing_agent",
        intended_quantity=1,
        stop_loss_pct=0.05,
        target_pct=0.10,
        risk_rupees=1000.0,
        entry_price=3989.5,
        regime="STRONG_BEAR",
        strategy_health="HEALTHY",
        vix=13.0,
        engine=engine,
    )
    s.meta["llm_confidence"] = 0.65
    return s


def _g3(engine: str, hhmm: str):  # type: ignore[no-untyped-def]
    h, m = map(int, hhmm.split(":"))
    with patch("src.risk.crm.datetime") as dt:
        dt.now.return_value = datetime(2026, 10, 7, h, m, tzinfo=_IST)
        return CentralRiskManager()._g3_time_of_day(_sig(engine), _ctx(), 1)


@pytest.mark.parametrize("hhmm", ["10:00", "14:33", "15:20", "17:00"])
def test_swing_passes_whenever_orders_are_accepted(hhmm: str) -> None:
    assert _g3("swing", hhmm).decision.value == "PASS"


def test_swing_blocked_only_when_no_order_window_exists() -> None:
    r = _g3("swing", "15:45")  # after the close, before the AMO window opens (16:00)
    assert r.decision.value == "BLOCK" and "outside market hours" in r.reason


def test_intraday_keeps_its_time_windows() -> None:
    assert _g3("intraday", "14:33").decision.value == "BLOCK"  # still the 85% bar
    assert _g3("intraday", "15:20").decision.value == "BLOCK"  # exit-only
    assert _g3("intraday", "10:00").decision.value == "PASS"


def _opt_g3(name: str, hhmm: str):  # type: ignore[no-untyped-def]
    h, m = map(int, hhmm.split(":"))
    sig = _sig("swing")
    sig.asset_class = "options"
    sig.strategy_name = name
    with patch("src.risk.crm.datetime") as dt:
        dt.now.return_value = datetime(2026, 10, 14, h, m, tzinfo=_IST)
        return CentralRiskManager()._g3_time_of_day(sig, _ctx(), 1)


def test_multi_day_options_skip_late_window_confidence_bar() -> None:
    assert _opt_g3("options_seller_desk_v3", "14:35").decision.value == "PASS"


@pytest.mark.parametrize(
    "name",
    ["options_intraday_short_premium", "options_intraday_short_premium2", "options_expiry_day"],
)
def test_same_day_options_keep_the_confidence_bar(name: str) -> None:
    assert _opt_g3(name, "14:35").decision.value == "BLOCK"


def test_multi_day_options_still_blocked_in_pre_close() -> None:
    assert _opt_g3("options_seller_desk_v3", "15:20").decision.value == "BLOCK"
