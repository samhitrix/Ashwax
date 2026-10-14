"""Global exit_defaults (config/global.json): a spec with no exit rules still gets
breakeven/partial/trail; explicit spec values win; enabled=false restores old
behaviour. LAURUSLABS (2026-09-16) sat at +1.5R with its original stop because its
spec had none."""

from __future__ import annotations

import copy

import pandas as pd
import pytest
from src.strategies.parsers.json_parser import JSONParser
from src.strategies.uss import compiler as comp
from src.strategies.uss.compiler import SmartExitTracker, USSCompiler, effective_exit_conditions
from src.strategies.uss.schema import Direction

_SWING = {
    "meta": {"name": "t", "direction": "LONG", "timeframe": "DAILY"},
    "entry_conditions": {
        "logic": "AND",
        "conditions": [{"name": "e", "type": "PRICE", "condition": "price_above", "threshold": 1}],
    },
    "stop_loss": {"method": "fixed_pct", "fixed_pct": 4.0},
    "target": {"method": "risk_reward", "ratio": 2.0},
    "exit_conditions": {"time_stop_days": 0},
}
_CFG = {
    "enabled": True,
    "swing": {
        "breakeven_at_r": 1.0,
        "partial_book_r": 1.5,
        "trail_activation_r": 1.0,
        "trailing_stop": {
            "method": "chandelier",
            "chandelier_period": 22,
            "chandelier_multiplier": 2.5,
        },
    },
    "intraday": {"breakeven_at_r": 1.0, "partial_book_r": 1.0},
}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(comp, "_exit_defaults_cfg", lambda: copy.deepcopy(_CFG))


def _spec(exit_conditions: dict | None = None):
    raw = copy.deepcopy(_SWING)
    if exit_conditions is not None:
        raw["exit_conditions"] = exit_conditions
    return USSCompiler().compile(JSONParser().parse(raw)).spec


def test_unset_fields_filled_from_swing_profile() -> None:
    ec = effective_exit_conditions(_spec())
    assert ec.breakeven_at_r == 1.0
    assert ec.partial_book_r == 1.5
    assert ec.trailing_stop is not None and ec.trailing_stop.chandelier_multiplier == 2.5


def test_spec_with_any_exit_rule_is_left_untouched() -> None:
    # Deliberate design (here: breakeven only, no partial/trail) must not be topped up.
    ec = effective_exit_conditions(_spec({"breakeven_at_r": 0.5}))
    assert ec.breakeven_at_r == 0.5
    assert ec.partial_book_r is None and ec.trailing_stop is None


def test_kill_switch_restores_spec_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(comp, "_exit_defaults_cfg", lambda: {"enabled": False})
    ec = effective_exit_conditions(_spec())
    assert ec.breakeven_at_r is None and ec.partial_book_r is None and ec.trailing_stop is None


def test_bare_spec_tracker_moves_stop_to_breakeven_at_1r() -> None:
    # LAURUSLABS shape: entry 1905.50, SL 1828.58 (risk 76.92), price ran to ~+1.5R.
    entry, sl, target = 1905.50, 1828.58, 2053.80
    idx = pd.date_range("2026-09-01", periods=30, freq="D")
    bars = [(1900.0, 1910.0, 1890.0, 1905.0)] * 29 + [(1905.0, 2025.0, 1905.0, 2019.1)]
    o, h, low, c = zip(*bars, strict=False)
    df = pd.DataFrame(
        {"open": o, "high": h, "low": low, "close": c, "volume": [1e6] * 30}, index=idx
    )
    tracker = SmartExitTracker(_spec(), entry, sl, target, Direction.LONG)
    be = tracker.step(df)
    assert be.booked_partial is True  # high 2025 >= entry + 1.5R (2020.9)
    assert be.sl_changed is True
    assert tracker.current_sl >= entry


def test_reseed_after_breakeven_keeps_original_risk() -> None:
    """LAURUSLABS: once the stop sits at entry, a restart must NOT seed risk from it
    (risk=0 silently disables partial/trail). Risk comes from initial_sl."""
    from src.orchestrator import _tracker_from_db_row

    row = {"initial_sl": 1828.58, "partial_pnl_booked": 0}
    t = _tracker_from_db_row(SmartExitTracker, _spec(), row, 1905.5, 1905.5, 2053.8, Direction.LONG)
    st = t._state
    assert round(st.initial_risk, 2) == 76.92
    assert st.sl == 1905.5 and st.be_moved is True and st.partial_booked is False
