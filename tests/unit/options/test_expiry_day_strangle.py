"""options_expiry_day with structure="strangle" (2026-10-13): the NIFTY +/-100 expiry-day short strangle with a
stop on each leg (put 50% / call 60%), run by the short_premium engine under the expiry-day strategy's own name.
Replaces the butterfly; the butterfly path stays behind structure="butterfly"."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from src.market import sessions
from src.options import seller_settings as ss
from src.options import short_premium as st
from src.options.seller_desk import EXPIRY_DAY_TYPE, OpenPosition
from src.options.short_premium import SHORT_PREMIUM_TYPE

from tests.unit.options.test_expiry_day import _chain
from tests.unit.options.test_options_intraday_short_premium import _PxExecutor, _SRepo
from tests.unit.options.test_seller_desk import _APPROVE_ALL_CRM, _desk

_IST = ZoneInfo("Asia/Kolkata")
_NAME = "options_expiry_day"
_EXP = date(2026, 10, 13)  # a Tuesday: NIFTY expiry
_AT = datetime(2026, 10, 13, 10, 5, tzinfo=_IST)
_CFG: dict[str, Any] = {
    "enabled": True,
    "registry_name": _NAME,
    "underlying": "NIFTY",
    "structure": "strangle",
    "entry_time": "10:00",
    "entry_grace_minutes": 20,
    "exit_time": "15:10",
    "ce_offset_points": 100,
    "pe_offset_points": -100,
    "sl_pct_ce": 60,
    "sl_pct_pe": 50,
    "target_pct": 0,
    "lots": 1,
    "max_lots": 1,
    "strangle_wing_points": 0,
    "allow_naked": True,
    # the legacy butterfly's keys sit in the same block and must be ignored:
    "entry_window": ["10:00", "10:20"],
    "wing_points": 150,
    "exit_by": "15:00",
}


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss, "expiry_day", lambda settings=None: dict(_CFG))
    monkeypatch.setattr(ss, "short_premium", lambda settings=None: {"enabled": False})


def _ctx(mode: str = "paper") -> SimpleNamespace:
    return SimpleNamespace(
        llm_gate_required={_NAME: False},
        strategy_execution_mode={_NAME: mode},
        mode_engine_open_positions={},
    )


def _new_desk(repo: _SRepo | None = None) -> tuple[Any, _PxExecutor]:
    """A desk whose fetcher behaves like the real one on expiry day: fetch_tier1() hands back NEXT week's
    chain, only fetch_for_expiry(today) returns today's."""
    ex = _PxExecutor()
    desk, _ = _desk(_chain(), executor=ex, repo=repo or _SRepo())
    fetcher = desk._chain_fetcher
    fetcher._tier1_chain = _chain(expiry=date(2026, 10, 20))
    fetcher.expiry_chains[_EXP] = _chain()
    return desk, ex


async def _enter(desk, *, today=_EXP, now=_AT, cfg=None):  # type: ignore[no-untyped-def]
    return await st.run_entry(
        desk,
        cfg or st.load_expiry_cfg(),
        "NSE:NIFTY",
        today=today,
        now=now,
        available_margin=1e6,
        regime="UNKNOWN",
        vix=13.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=_ctx(),
    )


# ---------------------------------------------------------------- config
def test_config_is_the_tested_strangle_not_the_butterfly() -> None:
    cfg = st.load_expiry_cfg()
    assert cfg is not None
    assert (cfg.strategy_type, cfg.expiry_only) == (EXPIRY_DAY_TYPE, True)
    assert (cfg.ce_offset, cfg.pe_offset) == (100.0, -100.0)
    assert (cfg.sl_pct_ce, cfg.sl_pct_pe) == (60.0, 50.0)
    assert (
        cfg.wing_points == 0.0
    )  # the block's wing_points=150 is the butterfly's -- never an iron condor
    assert cfg.skip_weekdays == frozenset()  # expiry-only, not weekday-gated


def test_butterfly_structure_or_disabled_means_no_strangle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss, "expiry_day", lambda settings=None: {**_CFG, "structure": "butterfly"})
    assert st.load_expiry_cfg() is None
    monkeypatch.setattr(ss, "expiry_day", lambda settings=None: {**_CFG, "enabled": False})
    assert st.load_expiry_cfg() is None


def test_naked_strangle_requires_the_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss, "expiry_day", lambda settings=None: {**_CFG, "allow_naked": False})
    assert (
        st.load_expiry_cfg() is None
    )  # invalid config disables entries loudly, never trades half-understood


def test_the_real_config_file_loads_as_the_frozen_strangle() -> None:
    """Reads config/options_settings.json itself: a hand edit that breaks the frozen rules must fail here."""
    import json
    from pathlib import Path

    raw = json.loads(
        (Path(__file__).resolve().parents[3] / "config" / "options_settings.json").read_text(
            encoding="utf-8"
        )
    )["options_trading"]["options_expiry_day"]
    cfg = st.parse_cfg(
        {**raw, "wing_points": raw.get("strangle_wing_points", 0)},
        strategy_type=EXPIRY_DAY_TYPE,
        expiry_only=True,
    )
    assert raw["structure"] == "strangle"
    assert (cfg.ce_offset, cfg.pe_offset, cfg.sl_pct_ce, cfg.sl_pct_pe) == (100, -100, 60, 50)
    assert (cfg.wing_points, cfg.lots) == (0, 1)
    assert cfg.entry_time.strftime("%H:%M") == "10:00"
    # exit_time is the operator's own setting (2026-10-27): only its validity is pinned.
    assert cfg.entry_time < cfg.exit_time < sessions.regular_close("nse_fo")


def test_strategy_type_constants_agree() -> None:
    assert st.EXPIRY_STRANGLE_TYPE == EXPIRY_DAY_TYPE


# ---------------------------------------------------------------- entry
async def test_opens_atm_plus_minus_100_naked_with_each_legs_own_stop() -> None:
    desk, ex = _new_desk()
    pos = await _enter(desk)
    assert pos is not None and pos.strategy_type == EXPIRY_DAY_TYPE and pos.expiry == _EXP
    assert pos.protection_legs == []  # naked: no wings
    strikes = {leg.instrument_type: leg.strike for leg in pos.income_legs}
    assert strikes == {"CE": 24100.0, "PE": 23900.0}  # spot 24010 -> ATM 24000
    assert all(leg.direction == "SELL" and leg.lots == 1 for leg in pos.income_legs)
    assert pos.meta["sl_pct"] == {"CE": 60.0, "PE": 50.0} and pos.meta["exit_time"] == "15:10"
    assert desk._repo.metas and desk._repo.metas[0]["sl_pct"] == {"CE": 60.0, "PE": 50.0}
    assert len(ex.executed) == 1 and desk._repo.inserted[0]["is_paper"] is True


async def test_trades_todays_expiry_never_next_weeks_chain() -> None:
    desk, _ = _new_desk()
    pos = await _enter(desk)
    assert pos is not None and pos.expiry == _EXP
    assert desk._chain_fetcher.expiry_requests == [("NSE:NIFTY", _EXP)]


async def test_a_day_that_is_not_an_expiry_does_nothing_and_never_falls_back() -> None:
    desk, ex = _new_desk()
    monday = date(2026, 10, 12)
    pos = await _enter(desk, today=monday, now=datetime(2026, 10, 12, 10, 5, tzinfo=_IST))
    assert (
        pos is None and ex.executed == []
    )  # tier1 holds a Tuesday chain, but this is not expiry day


@pytest.mark.parametrize("hour,minute", [(9, 59), (10, 21)])
async def test_only_inside_the_entry_window(hour: int, minute: int) -> None:
    desk, ex = _new_desk()
    assert await _enter(desk, now=datetime(2026, 10, 13, hour, minute, tzinfo=_IST)) is None
    assert ex.executed == []


async def test_one_entry_per_expiry_even_after_it_closed_and_across_a_restart() -> None:
    desk, ex = _new_desk()
    assert await _enter(desk) is not None
    assert await _enter(desk) is None and len(ex.executed) == 1  # still open
    desk2, ex2 = _new_desk(_SRepo(traded=True))  # restart: no memory, the DB knows
    assert await _enter(desk2) is None and ex2.executed == []


async def test_the_daily_short_premium_trade_does_not_block_the_expiry_strangle() -> None:
    desk, ex = _new_desk()
    daily = OpenPosition(
        position_id="OPT-DAILY",
        underlying="NSE:NIFTY",
        strategy_type=SHORT_PREMIUM_TYPE,
        direction="credit",
        protection_legs=[],
        income_legs=[],
        entry_premium=100.0,
        entry_margin=5000.0,
        expiry=_EXP,
        entered_at=_AT,
        meta={"sl_pct": {"CE": 23, "PE": 23}},
    )
    desk.positions["OPT-DAILY"] = daily
    assert await _enter(desk) is not None and len(ex.executed) == 1


async def test_one_leg_filled_flattens_instead_of_holding_a_lone_short() -> None:
    from src.options.options_leg_executor import ExecutionResult, LegFillState

    desk, ex = _new_desk()

    async def _half(prot, inc, signal_id):  # type: ignore[no-untyped-def]
        res = ExecutionResult(position_id="OPT-X", outcome="PARTIAL_DEGRADED")
        res.income_legs = [
            LegFillState(leg=inc[0], order_id="1", filled=True),
            LegFillState(leg=inc[1], order_id="2", rejected=True),
        ]
        return res

    ex.execute_structure = _half  # type: ignore[method-assign]
    assert await _enter(desk) is None
    assert len(ex.closed) == 1 and desk.positions == {}


async def test_shared_risk_ceiling_does_not_block_it() -> None:
    from src.risk import budget as budget_module

    budget_module._current_equity = 2_000_000.0  # ceiling Rs 60,000
    try:
        desk, ex = _new_desk()
        desk.positions["OPT-D1"] = OpenPosition(
            position_id="OPT-D1",
            underlying="NSE:NIFTY",
            strategy_type="bull_put_spread",
            direction="credit",
            protection_legs=[],
            income_legs=[],
            entry_premium=100.0,
            entry_margin=59_500.0,
            expiry=date(2026, 10, 20),
            entered_at=_AT,
        )
        assert await _enter(desk) is not None and len(ex.executed) == 1
    finally:
        budget_module._current_equity = None


async def test_butterfly_cycle_stays_off_while_the_strategy_is_the_strangle() -> None:
    desk, ex = _new_desk()
    pos = await desk.run_expiry_day_cycle(
        "NSE:NIFTY",
        today=_EXP,
        now=_AT,
        available_margin=1e6,
        regime="UNKNOWN",
        vix=13.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=_ctx(),
    )
    assert pos is None and ex.executed == []


# ---------------------------------------------------------------- per-leg stops and exits
async def _open(desk):  # type: ignore[no-untyped-def]
    pos = await _enter(desk)
    assert pos is not None
    return pos


def _prices(pos, *, ce: float, pe: float) -> dict[str, float]:
    return {
        leg.tradingsymbol: (ce if leg.instrument_type == "CE" else pe) for leg in pos.income_legs
    }


async def _manage(desk, prices, now):  # type: ignore[no-untyped-def]
    await st.manage(desk, _EXP, now, prices, st.load_expiry_cfg(), EXPIRY_DAY_TYPE)


_T1 = datetime(2026, 10, 13, 11, 0, tzinfo=_IST)


async def test_each_leg_stops_at_its_own_percentage_and_only_that_leg() -> None:
    desk, ex = _new_desk()
    pos = await _open(desk)
    ce0 = next(leg.premium for leg in pos.income_legs if leg.instrument_type == "CE")
    pe0 = next(leg.premium for leg in pos.income_legs if leg.instrument_type == "PE")
    # just below both triggers (call 60%, put 50%): nothing happens
    await _manage(desk, _prices(pos, ce=ce0 * 1.59, pe=pe0 * 1.49), _T1)
    assert len(pos.income_legs) == 2 and ex.closed == []
    # the put is at +51% (stopped at 50%) while the call at +59% is NOT yet (stops at 60%)
    await _manage(desk, _prices(pos, ce=ce0 * 1.59, pe=pe0 * 1.51), _T1)
    assert [leg.instrument_type for leg in pos.income_legs] == ["CE"]
    assert pos.closed_legs[0]["reason"].startswith("SHORT_PREMIUM_SL 50%")
    # now the call crosses its own 60%
    await _manage(desk, _prices(pos, ce=ce0 * 1.61, pe=0.0), _T1.replace(minute=5))
    assert desk.positions == {}  # both shorts out -> the position itself is closed
    assert desk._repo.closed[0]["exit_reason"] == "SHORT_PREMIUM_ALL_LEGS_STOPPED"


async def test_time_exit_at_15_10_not_before() -> None:
    desk, ex = _new_desk()
    pos = await _open(desk)
    flat = _prices(pos, ce=1.0, pe=1.0)
    await _manage(desk, flat, datetime(2026, 10, 13, 15, 9, 59, tzinfo=_IST))
    assert pos.position_id in desk.positions
    await _manage(desk, flat, datetime(2026, 10, 13, 15, 10, tzinfo=_IST))
    assert pos.position_id not in desk.positions
    assert desk._repo.closed[0]["exit_reason"].startswith("SHORT_PREMIUM_TIME_EXIT")


async def test_guardian_leaves_it_alone_until_its_own_exit_time_then_is_the_backstop() -> None:
    desk, _ = _new_desk()
    pos = await _open(desk)
    # the legacy butterfly's exit_by (15:00) must NOT close a strangle whose own exit is 15:10
    await desk.sweep_expiry_guardian(_EXP, datetime(2026, 10, 13, 15, 5, tzinfo=_IST))
    assert pos.position_id in desk.positions
    await desk.sweep_expiry_guardian(_EXP, datetime(2026, 10, 13, 15, 16, tzinfo=_IST))
    assert pos.position_id not in desk.positions


async def test_the_5_minute_position_manager_does_not_manage_a_strangle() -> None:
    """Its stops live in the fast loop; the butterfly's credit-multiple stop/TP must never touch it."""
    desk, ex = _new_desk()
    pos = await _open(desk)
    prices = _prices(pos, ce=0.05, pe=0.05)  # deep in profit: the butterfly's 50% TP would fire
    await desk.sweep_position_manager(
        today=_EXP, blackout_active=False, current_prices=prices, current_ivs={}, chains={}
    )
    assert pos.position_id in desk.positions and ex.closed == []


async def test_a_legacy_butterfly_row_is_not_picked_up_by_the_strangle_engine() -> None:
    desk, ex = _new_desk()
    fly = OpenPosition(
        position_id="OPT-FLY",
        underlying="NSE:NIFTY",
        strategy_type=EXPIRY_DAY_TYPE,
        direction="credit",
        protection_legs=[],
        income_legs=[],
        entry_premium=100.0,
        entry_margin=5000.0,
        expiry=_EXP,
        entered_at=_AT,
    )  # no meta["sl_pct"]: the butterfly never had per-leg stops
    desk.positions["OPT-FLY"] = fly
    assert not st.is_strangle_position(fly, EXPIRY_DAY_TYPE)
    await _manage(desk, {}, datetime(2026, 10, 13, 15, 30, tzinfo=_IST))
    assert "OPT-FLY" in desk.positions and ex.closed == []


async def test_tick_manages_the_open_strangle_with_no_entry_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk, ex = _new_desk()
    pos = await _open(desk)
    ce0 = next(leg.premium for leg in pos.income_legs if leg.instrument_type == "CE")
    px = _prices(pos, ce=ce0 * 1.7, pe=0.1)  # call far through its stop
    ex.prices.update(px)

    class _Broker:
        async def get_ltp(self, syms):  # type: ignore[no-untyped-def]
            return dict(px)

    shared = {"seller_options": {"desk": desk}, "broker": _Broker()}
    await st.tick(shared, datetime(2026, 10, 13, 12, 0, tzinfo=_IST))
    assert [leg.instrument_type for leg in pos.income_legs] == ["PE"]  # CE stopped by the fast loop


async def test_strangle_still_managed_to_its_exit_after_the_config_is_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk, ex = _new_desk()
    pos = await _open(desk)
    monkeypatch.setattr(ss, "expiry_day", lambda settings=None: {**_CFG, "enabled": False})
    px = _prices(pos, ce=1.0, pe=1.0)
    ex.prices.update(px)

    class _Broker:
        async def get_ltp(self, syms):  # type: ignore[no-untyped-def]
            return dict(px)

    shared = {"seller_options": {"desk": desk}, "broker": _Broker()}
    await st.tick(shared, datetime(2026, 10, 13, 15, 11, tzinfo=_IST))
    assert pos.position_id not in desk.positions  # an open position is always managed to its exit


def test_dashboard_exit_levels_use_the_per_leg_stops() -> None:
    from src.options.position_analytics import compute_exit_levels

    legs = [
        {"direction": "SELL", "instrument_type": "CE", "premium": 20.0, "lots": 1, "lot_size": 65},
        {"direction": "SELL", "instrument_type": "PE", "premium": 20.0, "lots": 1, "lot_size": 65},
    ]
    out = compute_exit_levels(EXPIRY_DAY_TYPE, legs, 2600.0, 2600.0, 100000.0)
    # (20 x 60% + 20 x 50%) x 65 = 1430: the stop, not the butterfly's 2 x credit
    assert out["stopAmount"] == pytest.approx(1430.0)
    assert out["targetAmount"] is None  # target_pct 0 = no profit target


def test_dashboard_exit_levels_fall_back_to_the_butterfly_when_that_is_the_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.options.position_analytics import compute_exit_levels

    block = {
        **_CFG,
        "structure": "butterfly",
        "profit_target_pct": 50,
        "stop_loss_credit_multiple": 2.0,
    }
    monkeypatch.setattr(ss, "expiry_day", lambda settings=None: block)
    out = compute_exit_levels(EXPIRY_DAY_TYPE, [], 4000.0, 4000.0, 100000.0)
    assert out["targetAmount"] == pytest.approx(2000.0) and out["stopAmount"] == pytest.approx(
        8000.0
    )


# ---------------------------------------------------------------- operator-defined exit time


def test_exit_time_is_whatever_the_operator_sets_until_the_fo_close(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # 2026-10-27: no hard 15:10 cap -- the only bound is that the F&O market is still open.
    for t in ("15:15", "15:25"):
        monkeypatch.setattr(ss, "expiry_day", lambda settings=None, t=t: {**_CFG, "exit_time": t})
        cfg = st.load_expiry_cfg()
        assert cfg is not None and cfg.exit_time.strftime("%H:%M") == t
    close = sessions.regular_close("nse_fo").strftime("%H:%M")
    monkeypatch.setattr(ss, "expiry_day", lambda settings=None: {**_CFG, "exit_time": close})
    assert st.load_expiry_cfg() is None  # at/after the close it could never be filled


async def test_a_15_20_exit_is_honoured_and_the_guardian_waits_for_it(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(ss, "expiry_day", lambda settings=None: {**_CFG, "exit_time": "15:20"})
    desk, _ = _new_desk()
    pos = await _open(desk)
    assert pos.meta["exit_time"] == "15:20"
    # the guardian's 15:15 stage must not pre-empt the operator's own exit
    await desk.sweep_expiry_guardian(_EXP, datetime(2026, 10, 13, 15, 16, tzinfo=_IST))
    assert pos.position_id in desk.positions
    flat = _prices(pos, ce=1.0, pe=1.0)
    await _manage(desk, flat, datetime(2026, 10, 13, 15, 19, 59, tzinfo=_IST))
    assert pos.position_id in desk.positions
    await _manage(desk, flat, datetime(2026, 10, 13, 15, 20, tzinfo=_IST))
    assert pos.position_id not in desk.positions
    assert desk._repo.closed[0]["exit_reason"].startswith("SHORT_PREMIUM_TIME_EXIT")
