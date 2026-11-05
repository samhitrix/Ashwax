"""options_intraday_short_premium2 (docs/hypotheses/P_sensex_intraday_straddle_paper.md): the second instance of the
short-premium engine -- a SENSEX ATM straddle whose SL % and times are plain config variables. Covers the shipped
config, that every rule is a variable, independence from the first instance, and the per-leg SL on its own type.
"""

from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from src.market import sessions
from src.options import seller_desk as sd
from src.options import seller_settings as ss
from src.options import short_premium as st
from src.options.seller_desk import OpenPosition
from src.options.short_premium import SHORT_PREMIUM2_TYPE, SHORT_PREMIUM_TYPE
from src.options.structure_selector import OptionLeg

from tests.unit.options.hosted_helpers import _RecordingCRM
from tests.unit.options.test_options_intraday_short_premium import _new_desk
from tests.unit.options.test_seller_desk import _APPROVE_ALL_CRM

_IST = ZoneInfo("Asia/Kolkata")
_NAME = "options_intraday_short_premium2"
_MON = date(2026, 10, 12)
_WED = date(2026, 10, 14)
_AT = datetime(2026, 10, 12, 10, 31, tzinfo=_IST)
_CFG: dict[str, Any] = {
    "enabled": True,
    "registry_name": _NAME,
    "underlying": "SENSEX",
    "entry_time": "10:30",
    "entry_grace_minutes": 5,
    "exit_time": "15:10",
    "skip_weekdays": [],
    "ce_offset_points": 0,
    "pe_offset_points": 0,
    "sl_pct": 40,
    "target_pct": 0,
    "lots": 1,
    "max_lots": 1,
    "wing_points": 0,
    "allow_naked": True,
    "llm_shadow": True,
}


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        llm_gate_required={_NAME: False},
        strategy_execution_mode={_NAME: "paper"},
        mode_engine_open_positions={},
    )


async def _enter(desk, *, cfg=None, now=_AT, today=_MON, crm=None):  # type: ignore[no-untyped-def]
    return await st.run_entry(
        desk,
        st.parse_cfg({**_CFG, **(cfg or {})}, strategy_type=SHORT_PREMIUM2_TYPE),
        "BSE:SENSEX",
        today=today,
        now=now,
        available_margin=1e6,
        regime="UNKNOWN",
        vix=13.0,
        crm=crm or _APPROVE_ALL_CRM,
        crm_ctx=_ctx(),
    )


# ---------------------------------------------------------------- the shipped config
def test_shipped_block_is_the_agreed_sensex_straddle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the real config/options_settings.json, not a stub
    ss.reload()
    cfg = st.load_cfg2()
    assert cfg is not None, "options_intraday_short_premium2 must be enabled and valid"
    assert cfg.strategy_type == SHORT_PREMIUM2_TYPE and cfg.registry_name == _NAME
    assert cfg.underlying == "SENSEX"
    assert (cfg.ce_offset, cfg.pe_offset) == (0.0, 0.0)  # straddle
    # SL %, entry/exit time and lots are the user's tuning knobs (edited in the block, no code change), so only
    # their validity is pinned here -- not their current values.
    assert 0 < cfg.sl_pct_ce <= 500 and 0 < cfg.sl_pct_pe <= 500
    assert cfg.entry_time < cfg.exit_time < sessions.regular_close("nse_fo")
    assert cfg.skip_weekdays == frozenset()  # test N traded every day
    assert cfg.wing_points == 0 and cfg.allow_naked


# ---------------------------------------------------------------- SL and times are variables
def test_sl_and_times_are_config_variables_applied_on_the_next_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = dict(_CFG)
    monkeypatch.setattr(ss, "short_premium2", lambda settings=None: dict(block))
    first = st.load_cfg2()
    assert first is not None and first.sl_pct_ce == 40.0 and first.entry_time == time(10, 30)

    block.update({"sl_pct": 25, "entry_time": "11:30", "exit_time": "14:00"})
    second = st.load_cfg2()
    assert second is not None
    assert second.sl_pct_ce == second.sl_pct_pe == 25.0
    assert second.entry_time == time(11, 30) and second.exit_time == time(14, 0)

    block.update({"sl_pct_ce": 60, "skip_weekdays": ["Fri"]})  # per-side override + weekday
    third = st.load_cfg2()
    assert third is not None and (third.sl_pct_ce, third.sl_pct_pe) == (60.0, 25.0)
    assert third.skip_weekdays == frozenset({4})


def test_disabled_or_invalid_block_means_no_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss, "short_premium2", lambda settings=None: {**_CFG, "enabled": False})
    assert st.load_cfg2() is None
    monkeypatch.setattr(ss, "short_premium2", lambda settings=None: {**_CFG, "sl_pct": 0})
    assert (
        st.load_cfg2() is None
    )  # a bad edit disables entries, never trades a half-understood rule


# ---------------------------------------------------------------- entry
async def test_enters_a_straddle_on_its_own_type_with_frozen_rules() -> None:
    desk, ex = _new_desk()
    crm = _RecordingCRM()
    pos = await _enter(desk, crm=crm)
    assert pos is not None and pos.strategy_type == SHORT_PREMIUM2_TYPE
    ce, pe = (
        next(leg for leg in pos.income_legs if leg.instrument_type == k) for k in ("CE", "PE")
    )
    assert ce.strike == pe.strike  # ATM straddle: both shorts on the same strike
    assert pos.meta["sl_pct"] == {"CE": 40.0, "PE": 40.0} and pos.meta["exit_time"] == "15:10"
    sig = crm.signals[0]
    assert sig.strategy_name == _NAME
    assert sig.risk_rupees == pytest.approx(
        sum(0.40 * leg.premium * leg.quantity for leg in pos.income_legs)
    )
    assert ex.executed[0][0] == []  # naked by opt-in, like the first instance


async def test_no_weekday_is_skipped_by_default() -> None:
    desk, ex = _new_desk()
    wed_at = datetime(2026, 10, 14, 10, 31, tzinfo=_IST)
    assert await _enter(desk, today=_WED, now=wed_at) is not None  # the NIFTY instance skips Wed


async def test_once_per_day_per_instance_and_independent_of_the_first() -> None:
    desk, _ = _new_desk()
    # an open position of the OTHER instance must not block this one
    _open_premium2(desk, position_id="OPT-NIFTY", strategy_type=SHORT_PREMIUM_TYPE)
    assert await _enter(desk) is not None
    assert await _enter(desk) is None  # its own once-a-day guard
    assert desk._short_premium2_entered_day == _MON
    assert desk._short_premium_entered_day != _MON  # the first instance's state is untouched


# ---------------------------------------------------------------- stops
def _open_premium2(  # type: ignore[no-untyped-def]
    desk,
    sl: dict[str, float] | None = None,
    *,
    position_id: str = "OPT-S2",
    strategy_type: str = SHORT_PREMIUM2_TYPE,
) -> OpenPosition:
    def leg(sym, kind, prem):  # type: ignore[no-untyped-def]
        return OptionLeg(
            tradingsymbol=sym,
            instrument_type=kind,
            strike=74700.0,
            direction="SELL",
            lots=1,
            lot_size=20,
            premium=prem,
            delta=0.5,
        )

    pos = OpenPosition(
        position_id=position_id,
        underlying="BSE:SENSEX",
        strategy_type=strategy_type,
        direction="credit",
        protection_legs=[],
        income_legs=[leg("BFO:SC", "CE", 450.0), leg("BFO:SP", "PE", 510.0)],
        entry_premium=960.0 * 20,
        entry_margin=200000.0,
        expiry=date(2026, 10, 15),
        entered_at=_AT,
        meta={"sl_pct": sl or {"CE": 40, "PE": 40}, "exit_time": "15:10"},
    )
    desk.positions[pos.position_id] = pos
    return pos


async def _manage(desk, prices, now):  # type: ignore[no-untyped-def]
    await st.manage(
        desk,
        _MON,
        now,
        prices,
        st.parse_cfg(_CFG, strategy_type=SHORT_PREMIUM2_TYPE),
        SHORT_PREMIUM2_TYPE,
    )


_T1 = datetime(2026, 10, 12, 11, 0, tzinfo=_IST)


async def test_leg_is_stopped_at_forty_percent_not_before() -> None:
    desk, ex = _new_desk()
    pos = _open_premium2(desk)
    await _manage(desk, {"BFO:SC": 629.0, "BFO:SP": 510.0}, _T1)  # 450 x 1.40 = 630: not yet
    assert len(pos.income_legs) == 2 and ex.closed == []
    ex.prices["BFO:SC"] = 631.0
    await _manage(desk, {"BFO:SC": 631.0, "BFO:SP": 480.0}, _T1)
    assert [leg.tradingsymbol for leg in pos.income_legs] == ["BFO:SP"]  # one leg at a time
    assert pos.closed_legs[0]["reason"].startswith("SHORT_PREMIUM_SL 40")


async def test_the_frozen_sl_not_a_later_config_edit_governs_an_open_position() -> None:
    desk, ex = _new_desk()
    pos = _open_premium2(desk, sl={"CE": 25, "PE": 25})  # entered under 25%
    ex.prices["BFO:SC"] = 563.0
    await _manage(desk, {"BFO:SC": 563.0, "BFO:SP": 510.0}, _T1)  # 450 x 1.25 = 562.5
    assert [leg.tradingsymbol for leg in pos.income_legs] == ["BFO:SP"]


async def test_time_exit_uses_the_frozen_exit_time() -> None:
    desk, ex = _new_desk()
    pos = _open_premium2(desk)
    before = datetime(2026, 10, 12, 15, 9, tzinfo=_IST)
    await _manage(desk, {"BFO:SC": 450.0, "BFO:SP": 510.0}, before)
    assert len(pos.income_legs) == 2
    at = datetime(2026, 10, 12, 15, 10, tzinfo=_IST)
    await _manage(desk, {"BFO:SC": 450.0, "BFO:SP": 510.0}, at)
    assert "OPT-S2" not in desk.positions  # fully closed by its own time exit


# ---------------------------------------------------------------- wiring
async def test_tick_runs_the_second_instance_with_its_own_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, Any]] = []

    async def fake_tick_one(shared, now, strategy_type, cfg):  # type: ignore[no-untyped-def]
        seen.append((strategy_type, cfg))

    monkeypatch.setattr(st, "_tick_one", fake_tick_one)
    monkeypatch.setattr(ss, "short_premium2", lambda settings=None: dict(_CFG))
    await st.tick({}, _AT)
    by_type = dict(seen)
    assert SHORT_PREMIUM2_TYPE in by_type and by_type[SHORT_PREMIUM2_TYPE].underlying == "SENSEX"
    assert SHORT_PREMIUM_TYPE in by_type  # the first instance still ticks


def test_hosted_sets_cover_both_instances() -> None:
    assert SHORT_PREMIUM2_TYPE in sd.HOSTED_STRATEGY_TYPES  # the reconciler never deletes its rows
    assert SHORT_PREMIUM2_TYPE in sd.HOSTED_OWN_MODE_TYPES  # it carries its own paper/live mode


def test_each_instance_manages_only_its_own_positions() -> None:
    desk, _ = _new_desk()
    _open_premium2(desk)
    assert st.is_strangle_position(desk.positions["OPT-S2"], SHORT_PREMIUM2_TYPE)
    assert not st.is_strangle_position(desk.positions["OPT-S2"], SHORT_PREMIUM_TYPE)


async def test_live_margin_input_counts_collateral_not_just_cash() -> None:
    """2026-10-29: the live SENSEX straddle was refused 'need 258513 have 31024' because the
    margin input was the cash-only figure; the broker accepts collateral for F&O."""
    from src.brokers.base import Margin
    from src.orchestrator import _hosted_margin

    class _Broker:
        async def get_margins(self) -> Margin:
            return Margin(
                equity=531024.0,
                available_cash=31024.0,
                used_margin=0.0,
                collateral=500000.0,
                fno_available=531024.0,
            )

    assert await _hosted_margin({}, None, _Broker(), is_paper=False) == 531024.0
