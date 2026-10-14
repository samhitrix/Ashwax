"""Signal-driven pass trigger for llm_intraday_agent (pipeline.evaluate_trigger)."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from src.strategies.llm_intraday.pipeline import TriggerState, evaluate_trigger

_IST = ZoneInfo("Asia/Kolkata")
_CFG = {
    "min_setup_score": 30,
    "min_gap_minutes": 30,
    "max_passes_per_day": 3,
    "re_offer_minutes": 60,
    "features_max_age_seconds": 900,
    "min_tokens_to_fire": 8000,
}


def _at(h: int, m: int) -> datetime:
    return datetime(2026, 9, 21, h, m, tzinfo=_IST)


def _run(state: TriggerState, now: datetime, scores: dict[str, Any], **kw: Any) -> Any:
    args: dict[str, Any] = {
        "first_pass": time(9, 45),
        "last_pass": time(14, 15),
        "scores": scores,
        "traded_today": set(),
        "trades_today": 0,
        "max_trades": 2,
        "tokens_left": 50_000,
    }
    args.update(kw)
    return evaluate_trigger(state, _CFG, now, **args)


def _score(now: datetime, sym: str = "AAA", v: float = 45.0, age: float = 60.0) -> dict[str, Any]:
    return {sym: (v, now.timestamp() - age)}


def _after_first_pass() -> TriggerState:
    return TriggerState(passes_today=1, last_pass_ts=_at(10, 0).timestamp())


def test_no_pass_before_first_pass_time() -> None:
    now = _at(9, 30)
    assert _run(TriggerState(), now, _score(now))[:2] == (False, "before_first_pass")


def test_baseline_pass_fires_at_first_pass_even_without_setup() -> None:
    fire, reason, _ = _run(TriggerState(), _at(9, 46), {})
    assert (fire, reason) == (True, "baseline")


def test_signal_pass_after_gap_on_fresh_high_score() -> None:
    now = _at(11, 0)
    assert _run(_after_first_pass(), now, _score(now)) == (True, "signal", ["AAA"])


def test_min_gap_blocks_back_to_back_passes() -> None:
    now = _at(10, 20)
    assert _run(_after_first_pass(), now, _score(now))[:2] == (False, "min_gap")


def test_low_score_or_recently_offered_or_stale_does_not_fire() -> None:
    now = _at(11, 0)
    st = _after_first_pass()
    assert _run(st, now, _score(now, v=10))[1] == "no_new_setup"
    st.offered["AAA"] = _at(10, 30).timestamp()
    assert _run(st, now, _score(now))[1] == "no_new_setup"
    st.offered.clear()
    assert _run(st, now, _score(now, age=3600))[1] == "no_new_setup"


def test_guards_pass_cap_trade_cap_token_budget_and_last_pass() -> None:
    now = _at(11, 0)
    capped = TriggerState(passes_today=3, last_pass_ts=_at(10, 0).timestamp())
    assert _run(capped, now, _score(now))[1] == "pass_cap_reached"
    assert _run(_after_first_pass(), now, _score(now), trades_today=2)[1] == "trade_cap_reached"
    assert _run(_after_first_pass(), now, _score(now), tokens_left=1000)[1] == "token_budget_low"
    late = _at(14, 20)
    assert _run(_after_first_pass(), late, _score(late))[1] == "after_last_pass"


def test_traded_symbol_is_never_offered_again() -> None:
    now = _at(11, 0)
    assert _run(_after_first_pass(), now, _score(now), traded_today={"AAA"})[1] == "no_new_setup"


# ── no calls while CRM gate G3 would refuse every entry (2026-09-24) ─────────────────────
# 2026-09-24 12:58: the trigger asked the LLM (and G9) for ideas while G3 was blocking all
# entries; both ideas passed every check and were then refused. Wasted by construction:
# the trigger must not fire while entries cannot happen.


def test_no_signal_pass_while_the_entry_window_is_closed_even_with_a_hot_setup() -> None:
    now = _at(12, 58)
    fire, reason, fresh = _run(
        _after_first_pass(), now, _score(now, v=99.0), entry_window_open=False
    )
    assert (fire, reason, fresh) == (False, "entry_window_closed", [])


def test_signal_pass_fires_when_the_entry_window_is_open() -> None:
    now = _at(12, 58)  # midday: an ordinary trading minute, no lunch restriction
    fire, reason, fresh = _run(
        _after_first_pass(), now, _score(now, v=99.0), entry_window_open=True
    )
    assert (fire, reason, fresh) == (True, "signal", ["AAA"])


def test_entry_window_is_open_by_default_keeps_the_old_behaviour() -> None:
    now = _at(12, 58)
    assert _run(_after_first_pass(), now, _score(now))[:2] == (True, "signal")
