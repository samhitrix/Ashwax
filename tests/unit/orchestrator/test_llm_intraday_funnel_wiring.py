"""Every outcome of an llm_intraday idea's entry check / execution is logged + alerted."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest
from src import orchestrator as orch

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 24, 11, 30, tzinfo=IST)


def _compiled():
    return SimpleNamespace(spec=SimpleNamespace(meta=SimpleNamespace(name="llm_intraday_agent")))


def _df(close: float) -> pd.DataFrame:
    return pd.DataFrame({"close": [close]})


def _idea(age_min: float, entry: float = 1389.30) -> dict:
    return {
        "symbol": "CIPLA",
        "direction": "LONG",
        "entry_price": entry,
        "stop_loss": 1380.5,
        "target": 1405.5,
        "confidence": 0.55,
        "ts": NOW.timestamp() - age_min * 60,
    }


@pytest.fixture
def funnel():
    with (
        patch("src.strategies.llm_intraday.funnel.funnel_log") as flog,
        patch("src.strategies.llm_intraday.funnel.funnel_alert") as falert,
    ):
        yield SimpleNamespace(log=flog, alert=falert)


def _calls(mock) -> list[dict]:
    return [c.kwargs for c in mock.call_args_list]


def test_expired_idea_is_logged_alerted_and_carries_the_last_check(funnel) -> None:
    idea = _idea(age_min=30)
    idea["_last_check"] = "entry_drift 0.90% > 0.40%"
    shared = {"llm_intraday_ideas": {"CIPLA": idea}}
    assert (
        orch._evaluate_llm_intraday_signal(_compiled(), shared, "NSE:CIPLA", _df(1400), NOW) is None
    )
    assert "CIPLA" not in shared["llm_intraday_ideas"]  # evicted
    a = _calls(funnel.alert)[-1]
    assert (a["stage"], a["outcome"], a["reason"], a["symbol"]) == (
        "entry_check",
        "expired",
        "ttl_expired",
        "CIPLA",
    )
    assert "entry_drift 0.90%" in a["detail"]  # tells you WHY it never entered


def test_entry_drift_is_logged_as_waiting_and_remembered(funnel) -> None:
    shared = {"llm_intraday_ideas": {"CIPLA": _idea(age_min=2)}}
    out = orch._evaluate_llm_intraday_signal(_compiled(), shared, "NSE:CIPLA", _df(1420), NOW)
    assert out is None
    kw = _calls(funnel.log)[-1]
    assert (kw["stage"], kw["outcome"], kw["reason"]) == ("entry_check", "waiting", "entry_drift")
    assert "entry_drift" in shared["llm_intraday_ideas"]["CIPLA"]["_last_check"]


def test_price_in_tolerance_emits_signal_step(funnel) -> None:
    shared = {"llm_intraday_ideas": {"CIPLA": _idea(age_min=2)}}
    with patch(
        "src.strategies.llm_intraday.pipeline.still_valid_at", return_value=(1380.5, 1405.5)
    ):
        sig = orch._evaluate_llm_intraday_signal(_compiled(), shared, "NSE:CIPLA", _df(1389.5), NOW)
    assert sig is not None
    kw = _calls(funnel.log)[-1]
    assert (kw["stage"], kw["outcome"]) == ("entry_check", "signal")


def test_max_trades_is_alerted_as_blocked(funnel) -> None:
    shared = {
        "llm_intraday_ideas": {"CIPLA": _idea(age_min=2)},
        "llm_intraday_traded": {"2026-09-24": 999},  # >= any configured cap
    }
    assert (
        orch._evaluate_llm_intraday_signal(_compiled(), shared, "NSE:CIPLA", _df(1389.5), NOW)
        is None
    )
    a = _calls(funnel.alert)[-1]
    assert (a["outcome"], a["reason"]) == ("blocked", "max_trades_reached")


@pytest.mark.asyncio
async def test_already_traded_refusal_is_labelled_not_silent() -> None:
    sig = SimpleNamespace(
        entry_price=1389.3, sl=1380.5, target=1405.5, direction=SimpleNamespace(value="LONG")
    )
    shared = {"intraday_traded": {"2026-09-24": {"NSE:CIPLA"}}}
    out = await orch._execute_intraday_signal(shared, "llm_intraday_agent", "NSE:CIPLA", sig, NOW)
    assert out is None
    assert shared["_intraday_refusals"]["CIPLA"][0] == "already_traded_today"


@pytest.mark.asyncio
async def test_invalid_levels_refusal_is_labelled() -> None:
    sig = SimpleNamespace(
        entry_price=1389.3, sl=1389.3, target=1405.5, direction=SimpleNamespace(value="LONG")
    )
    shared: dict = {"held_symbols": set(), "held_symbols_loaded": True, "order_monitor": None}
    out = await orch._execute_intraday_signal(shared, "llm_intraday_agent", "NSE:CIPLA", sig, NOW)
    assert out is None
    assert shared["_intraday_refusals"]["CIPLA"][0] == "invalid_levels"


@pytest.mark.asyncio
async def test_unreconciled_refusal_is_labelled() -> None:
    sig = SimpleNamespace(
        entry_price=1389.3, sl=1380.5, target=1405.5, direction=SimpleNamespace(value="LONG")
    )
    shared: dict = {}  # held_symbols_loaded not set -> fail closed
    out = await orch._execute_intraday_signal(shared, "llm_intraday_agent", "NSE:CIPLA", sig, NOW)
    assert out is None
    assert shared["_intraday_refusals"]["CIPLA"][0] == "held_positions_not_loaded"


def test_every_return_none_in_the_executor_records_a_reason() -> None:
    """Guard against a NEW silent refusal path being added later."""
    import inspect

    lines = inspect.getsource(orch._execute_intraday_signal).splitlines()
    silent = [
        i
        for i, line in enumerate(lines)
        if line.strip() == "return None"
        and "_intraday_refusal(" not in chr(10).join(lines[max(0, i - 14) : i])
    ]
    assert not silent, f"return None without a recorded refusal reason at function lines {silent}"


def test_waiting_trail_says_what_an_unchecked_idea_is_waiting_for(funnel) -> None:
    shared = {
        "llm_intraday_ideas": {
            "AXISBANK": _idea(age_min=2),
            "PINELABS": {**_idea(2), "_checked_at": 1},
        },
        "intraday_scan_timing": {"done_ts": NOW.timestamp() - 40, "cycle_s": 194.0},
    }
    orch._log_llm_ideas_waiting(shared, NOW)
    logged = _calls(funnel.log)
    assert len(logged) == 1  # PINELABS was already checked: not "waiting"
    kw = logged[0]
    assert (kw["stage"], kw["outcome"], kw["reason"], kw["symbol"]) == (
        "entry_check",
        "waiting",
        "awaiting_scan",
        "AXISBANK",
    )
    assert "NOT yet price-checked" in kw["detail"] and "194s" in kw["detail"]
    assert funnel.alert.call_args.kwargs["outcome"] == "waiting"  # logged; never a phone alert


def test_first_price_check_marks_the_idea_so_it_stops_being_listed_as_waiting(funnel) -> None:
    idea = _idea(age_min=2)
    shared = {"llm_intraday_ideas": {"CIPLA": idea}}
    orch._evaluate_llm_intraday_signal(_compiled(), shared, "NSE:CIPLA", _df(1420), NOW)
    assert "_checked_at" in idea


def test_dma_trend_rule_uses_the_live_price_not_yesterdays_daily_close() -> None:
    """2026-09-24: PINELABS fell 189.10 -> 179.65 (below its 200-DMA 183.08) yet was refused as a
    short 'above its 50/200-DMA' because the rule used the last DAILY close."""
    import inspect

    from src.risk.trend_filter import daily_trend_size_multiplier

    src = inspect.getsource(orch._execute_intraday_signal)
    assert "_c = float(entry)" in src and '_c = float(_ddf["close"]' not in src
    # yesterday's close (189.10) says "uptrend, cautious"; the live price says "below 200-DMA, fine"
    assert daily_trend_size_multiplier(189.10, 163.49, 183.08, "SELL") < 1.0
    assert daily_trend_size_multiplier(179.65, 163.49, 183.08, "SELL") == 1.0


def test_the_intraday_result_lines_carry_the_models_reasoning() -> None:
    """2026-09-28 14:14: the call returned PASS but the INFO lines said only `decision=no_ideas`; the
    reason lived in a DEBUG line 100s of lines away. Both the funnel `response` line and the
    scan-result line must carry the thesis (and the scan-result line the stocks shown)."""
    import inspect

    src = inspect.getsource(orch._run_llm_intraday_pass)
    response_call = src[src.index('stage="response"') :]
    assert "thesis=" in response_call[:900]
    scan_result = src[src.index("llm_intraday_scan_result slot=") :]
    assert "shown=%s thesis=%r" in scan_result[:400]
