"""options_intraday_short_premium (docs/hypotheses/I_options_intraday_short_premium.md): daily NIFTY short strangle with a stop on
EACH leg, hosted in SellerDesk. Entry at entry_time, per-leg SL, time exit, restart recovery, and the
exemptions (no slot/cap dependence, guardian holds off until exit_time)."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from src.options import seller_desk as sd
from src.options import seller_settings as ss
from src.options import short_premium as st
from src.options.options_leg_executor import ExecutionResult, LegFillState
from src.options.seller_desk import SHORT_PREMIUM_TYPE, OpenPosition
from src.options.structure_selector import OptionLeg

from tests.unit.options.test_expiry_day import _chain
from tests.unit.options.test_seller_desk import _APPROVE_ALL_CRM, _desk, _FakeExecutor
from tests.unit.options.test_weekly_condor import _RecordingCRM, _Repo

_IST = ZoneInfo("Asia/Kolkata")
_NAME = "options_intraday_short_premium"
_MON = date(2026, 10, 12)  # a Monday; _chain() expires Tue 13 Oct
_AT = datetime(2026, 10, 12, 10, 31, tzinfo=_IST)
_CFG: dict[str, Any] = {
    "enabled": True,
    "registry_name": _NAME,
    "underlying": "NIFTY",
    "entry_time": "10:30",
    "entry_grace_minutes": 5,
    "exit_time": "15:01",
    "skip_weekdays": ["Wed"],
    "ce_offset_points": 150,
    "pe_offset_points": -150,
    "sl_pct": 23,
    "lots": 1,
    "max_lots": 1,
    "wing_points": 0,
    "allow_naked": True,
    "min_leg_premium": 0,
    "poll_seconds": 10,
    "respect_event_blackout": False,
    "llm_shadow": True,
}


class _SRepo(_Repo):
    def __init__(self, traded: bool = False) -> None:
        super().__init__()
        self.opened_today = traded
        self.metas: list[dict[str, Any]] = []
        self.leg_exits: list[dict[str, Any]] = []

    async def has_position_opened_on(self, **kwargs: Any) -> bool:
        return self.opened_today

    async def set_legs_meta(self, position_id: str, meta: dict[str, Any]) -> None:
        self.metas.append(dict(meta))

    async def record_leg_exit(self, position_id: str, record: dict[str, Any]) -> None:
        self.leg_exits.append(record)


class _PxExecutor(_FakeExecutor):
    """close_leg returns a per-symbol price (the fill proxy) and can be told to fail once."""

    def __init__(self) -> None:
        super().__init__()
        self.prices: dict[str, float] = {}
        self.fail_next = False

    async def close_leg(self, leg, signal_id):  # type: ignore[no-untyped-def]
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("broker down")
        self.closed.append((leg, signal_id))
        return self.prices.get(leg.tradingsymbol, leg.premium)


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss, "short_premium", lambda settings=None: dict(_CFG))


def _ctx(mode: str = "paper", gate: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        llm_gate_required={_NAME: gate},
        strategy_execution_mode={_NAME: mode},
        mode_engine_open_positions={},
    )


def _new_desk(repo: _SRepo | None = None) -> tuple[Any, _PxExecutor]:
    ex = _PxExecutor()
    desk, _ = _desk(_chain(), executor=ex, repo=repo or _SRepo())
    return desk, ex


async def _enter(desk, *, cfg=None, now=_AT, today=_MON, crm=None, ctx=None):  # type: ignore[no-untyped-def]
    return await st.run_entry(
        desk,
        st.parse_cfg({**_CFG, **(cfg or {})}),
        "NSE:NIFTY",
        today=today,
        now=now,
        available_margin=1e6,
        regime="UNKNOWN",
        vix=13.0,
        crm=crm or _APPROVE_ALL_CRM,
        crm_ctx=ctx or _ctx(),
    )


# ---------------------------------------------------------------- config
def test_defaults_are_the_users_tested_strangle() -> None:
    cfg = st.parse_cfg({"enabled": True, "allow_naked": True})
    assert (cfg.ce_offset, cfg.pe_offset) == (150.0, -150.0)
    assert (cfg.entry_time, cfg.exit_time) == (time(10, 30), time(15, 1))
    assert cfg.sl_pct_ce == cfg.sl_pct_pe == 23.0
    assert cfg.skip_weekdays == frozenset({2})


@pytest.mark.parametrize(
    "bad",
    [
        {"allow_naked": False},  # wing_points 0 + not opted in
        {"exit_time": "15:11"},
        {"entry_time": "15:00", "exit_time": "14:00"},
        {"sl_pct": 0},
        {"sl_pct": 600},
        {"lots": -1},
        {"ce_offset_points": -50, "pe_offset_points": 50},
        {"entry_time": "ten-thirty"},
        {"poll_seconds": 0},
    ],
)
def test_invalid_config_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        st.parse_cfg({**_CFG, **bad})


def test_short_premium_is_just_zero_offsets() -> None:
    prot, inc = st.build_short_premium_legs(_chain(), 1, ce_offset=0, pe_offset=0, wing_points=0)
    assert prot == [] and {leg.strike for leg in inc} == {24000.0}


def test_invalid_block_disables_entries_not_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss, "short_premium", lambda settings=None: {**_CFG, "sl_pct": 0})
    assert st.load_cfg() is None
    monkeypatch.setattr(ss, "short_premium", lambda settings=None: {**_CFG, "enabled": False})
    assert st.load_cfg() is None


# ---------------------------------------------------------------- legs
def test_signed_offsets_around_atm_and_optional_wings() -> None:
    prot, inc = st.build_short_premium_legs(
        _chain(), 1, ce_offset=150, pe_offset=-150, wing_points=0
    )
    assert prot == []
    assert {(leg.instrument_type, leg.strike, leg.direction) for leg in inc} == {
        ("CE", 24150.0, "SELL"),
        ("PE", 23850.0, "SELL"),
    }
    prot, inc = st.build_short_premium_legs(
        _chain(), 1, ce_offset=150, pe_offset=-150, wing_points=150
    )
    assert {(leg.instrument_type, leg.strike, leg.direction) for leg in prot} == {
        ("CE", 24300.0, "BUY"),
        ("PE", 23700.0, "BUY"),
    }


def test_missing_strike_never_substituted() -> None:
    chain = _chain()
    chain.strikes = [r for r in chain.strikes if r.strike != 24150.0]
    assert (
        st.build_short_premium_legs(chain, 1, ce_offset=150, pe_offset=-150, wing_points=0) is None
    )


def test_trigger_is_entry_times_one_plus_sl() -> None:
    assert st.stop_trigger(40.0, 23) == pytest.approx(49.2)


# ---------------------------------------------------------------- entry
async def test_enters_inside_window_with_own_name_and_sl_defined_risk() -> None:
    desk, ex = _new_desk()
    crm = _RecordingCRM()
    pos = await _enter(desk, crm=crm)
    assert pos is not None and pos.strategy_type == SHORT_PREMIUM_TYPE
    sig = crm.signals[0]
    assert sig.strategy_name == _NAME and sig.meta["registry_name"] == _NAME
    expected = sum(0.23 * leg.premium * leg.quantity for leg in pos.income_legs)
    assert sig.risk_rupees == pytest.approx(expected)
    assert ex.executed and ex.executed[0][0] == []  # no protection legs: naked by opt-in
    assert pos.meta["sl_pct"] == {"CE": 23.0, "PE": 23.0} and pos.meta["exit_time"] == "15:01"
    assert desk._repo.metas  # rules frozen at entry were persisted


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 12, 10, 29, 59, tzinfo=_IST),  # before entry_time
        datetime(2026, 10, 12, 10, 35, tzinfo=_IST),  # grace (5 min) is exclusive
        datetime(2026, 10, 12, 14, 0, tzinfo=_IST),
    ],
)
async def test_no_entry_outside_the_window(now: datetime) -> None:
    desk, ex = _new_desk()
    assert await _enter(desk, now=now) is None and ex.executed == []


async def test_wednesday_is_skipped_and_other_days_trade() -> None:
    desk, ex = _new_desk()
    wed = date(2026, 10, 14)
    assert await _enter(desk, today=wed, now=datetime(2026, 10, 14, 10, 31, tzinfo=_IST)) is None
    assert ex.executed == []
    assert await _enter(desk) is not None  # Monday


async def test_once_per_day_even_after_the_position_is_gone() -> None:
    desk, ex = _new_desk()
    assert await _enter(desk) is not None
    assert await _enter(desk) is None  # in-memory guard
    desk2, ex2 = _new_desk(_SRepo(traded=True))  # restart + early full stop-out: DB guard
    assert await _enter(desk2) is None and ex2.executed == []


async def test_history_check_failure_fails_closed() -> None:
    class _Broken(_SRepo):
        async def has_position_opened_on(self, **kwargs: Any) -> bool:
            raise RuntimeError("db down")

    desk, ex = _new_desk(_Broken())
    assert await _enter(desk) is None and ex.executed == []


async def test_not_blocked_by_other_open_positions_or_per_index_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ss, "max_strategies_per_index", lambda settings=None: 1)
    desk, ex = _new_desk()
    for i in range(4):
        desk.positions[f"OPT-D{i}"] = OpenPosition(
            position_id=f"OPT-D{i}",
            underlying="NSE:NIFTY",
            strategy_type="bull_put_spread",
            direction="credit",
            protection_legs=[],
            income_legs=[],
            entry_premium=100.0,
            entry_margin=5000.0,
            expiry=date(2026, 10, 19),
            entered_at=_AT,
        )
    assert await _enter(desk) is not None and len(ex.executed) == 1


async def test_strategy_halt_and_entry_breaker_still_apply() -> None:
    desk, ex = _new_desk()
    desk._strategy_halted_day[_NAME] = _MON
    assert await _enter(desk) is None
    desk2, ex2 = _new_desk()
    desk2._entries_halted_day = _MON
    assert await _enter(desk2) is None and ex2.executed == [] and ex.executed == []


async def test_live_registry_mode_routes_to_the_live_executor() -> None:
    desk, paper_exec = _new_desk()
    live_exec = _PxExecutor()
    desk._alt_executor = live_exec
    pos = await _enter(desk, ctx=_ctx("live"))
    assert pos is not None and pos.is_paper is False
    assert live_exec.executed and not paper_exec.executed
    assert desk._repo.inserted[0]["is_paper"] is False


async def test_one_leg_filled_flattens_instead_of_holding_a_lone_short() -> None:
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
    assert len(ex.closed) == 1 and desk.positions == {}  # only the filled leg was flattened


async def test_llm_shadow_verdict_recorded_when_gate_off_and_not_when_required() -> None:
    desk, _ = _new_desk()
    calls: list[int] = []

    async def _shadow(*a, **k):  # type: ignore[no-untyped-def]
        calls.append(1)
        return {"flag": "REJECT", "confidence": 0.4, "reason": "x", "provider": "p"}

    desk._g9_shadow = _shadow  # type: ignore[method-assign]
    pos = await _enter(desk)
    await asyncio.gather(
        *desk._short_premium_bg
    )  # shadow runs in the background, never in the tick
    assert pos is not None and pos.meta["llm_shadow"]["flag"] == "REJECT" and calls == [1]
    assert any("llm_shadow" in m for m in desk._repo.metas)

    desk2, _ = _new_desk()
    desk2._g9_shadow = _shadow  # type: ignore[method-assign]

    async def _review(*a, **k):  # type: ignore[no-untyped-def]
        return True

    desk2._g9_review = _review  # type: ignore[method-assign]
    assert await _enter(desk2, ctx=_ctx(gate=True)) is not None
    assert calls == [1]  # gate required -> the real G9 path, no shadow call


async def test_required_llm_gate_with_no_verdict_blocks_entry() -> None:
    desk, ex = _new_desk()

    async def _no(*a, **k):  # type: ignore[no-untyped-def]
        return False

    desk._g9_review = _no  # type: ignore[method-assign]
    assert await _enter(desk, ctx=_ctx(gate=True)) is None and ex.executed == []


# ---------------------------------------------------------------- stops / exit
def _open(desk, *, wings: bool = False) -> OpenPosition:  # type: ignore[no-untyped-def]
    def leg(sym, kind, k, d, prem):  # type: ignore[no-untyped-def]
        return OptionLeg(
            tradingsymbol=sym,
            instrument_type=kind,
            strike=k,
            direction=d,
            lots=1,
            lot_size=65,
            premium=prem,
            delta=0.2,
        )

    prot = (
        [leg("WP", "PE", 23550.0, "BUY", 5.0), leg("WC", "CE", 24450.0, "BUY", 5.0)]
        if wings
        else []
    )
    inc = [leg("SP", "PE", 23850.0, "SELL", 40.0), leg("SC", "CE", 24150.0, "SELL", 40.0)]
    pos = OpenPosition(
        position_id="OPT-S",
        underlying="NSE:NIFTY",
        strategy_type=SHORT_PREMIUM_TYPE,
        direction="credit",
        protection_legs=prot,
        income_legs=inc,
        entry_premium=80.0 * 65,
        entry_margin=150000.0,
        expiry=date(2026, 10, 13),
        entered_at=_AT,
        meta={"sl_pct": {"CE": 23, "PE": 23}, "exit_time": "15:01"},
    )
    desk.positions[pos.position_id] = pos
    return pos


async def _manage(desk, prices, now):  # type: ignore[no-untyped-def]
    await st.manage(desk, _MON, now, prices, st.parse_cfg(_CFG))


_T1 = datetime(2026, 10, 12, 11, 0, tzinfo=_IST)


async def test_only_the_breached_leg_is_closed_and_pnl_is_kept() -> None:
    desk, ex = _new_desk()
    pos = _open(desk)
    ex.prices["SC"] = 49.5
    await _manage(desk, {"SC": 49.5, "SP": 35.0}, _T1)  # 49.5 >= 40 x 1.23 = 49.2
    assert [leg.tradingsymbol for leg in pos.income_legs] == ["SP"]
    assert len(pos.closed_legs) == 1 and pos.closed_legs[0]["reason"].startswith("SHORT_PREMIUM_SL")
    assert pos.closed_pnl == pytest.approx(-(49.5 - 40.0) * 65)
    assert desk._repo.leg_exits[0]["tradingsymbol"] == "SC" and "OPT-S" in desk.positions
    assert desk._unrealized_pnl(pos, {"SP": 35.0}) == pytest.approx(pos.closed_pnl + 5.0 * 65)


async def test_below_trigger_nothing_happens() -> None:
    desk, ex = _new_desk()
    pos = _open(desk)
    await _manage(desk, {"SC": 49.0, "SP": 31.0}, _T1)
    assert len(pos.income_legs) == 2 and ex.closed == []


async def test_both_legs_stopped_closes_position_with_total_realized() -> None:
    desk, ex = _new_desk()
    _open(desk)
    ex.prices.update({"SC": 49.5, "SP": 50.0})
    await _manage(desk, {"SC": 49.5, "SP": 30.0}, _T1)
    await _manage(desk, {"SP": 50.0}, _T1.replace(minute=5))
    assert "OPT-S" not in desk.positions
    row = desk._repo.closed[0]
    assert row["exit_reason"] == "SHORT_PREMIUM_ALL_LEGS_STOPPED"
    assert row["realized_pnl"] == pytest.approx(-(9.5 + 10.0) * 65)


async def test_time_exit_closes_remaining_legs_at_exit_time_not_before() -> None:
    desk, ex = _new_desk()
    _open(desk)
    await _manage(desk, {"SC": 30.0, "SP": 30.0}, datetime(2026, 10, 12, 15, 0, 59, tzinfo=_IST))
    assert "OPT-S" in desk.positions
    ex.prices.update({"SC": 20.0, "SP": 25.0})
    await _manage(desk, {"SC": 20.0, "SP": 25.0}, datetime(2026, 10, 12, 15, 1, tzinfo=_IST))
    assert "OPT-S" not in desk.positions
    row = desk._repo.closed[0]
    assert row["exit_reason"].startswith("SHORT_PREMIUM_TIME_EXIT")
    assert row["realized_pnl"] == pytest.approx(((40 - 20) + (40 - 25)) * 65)


async def test_wings_are_closed_once_both_shorts_are_out() -> None:
    desk, ex = _new_desk()
    pos = _open(desk, wings=True)
    ex.prices.update({"SC": 49.5, "SP": 49.5})
    await _manage(desk, {"SC": 49.5, "SP": 49.5, "WP": 3.0, "WC": 3.0}, _T1)
    assert "OPT-S" not in desk.positions and pos.protection_legs == []
    assert {c[0].tradingsymbol for c in ex.closed} == {"SC", "SP", "WP", "WC"}


async def test_failed_close_leaves_the_leg_open_and_retries_next_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alerts: list[str] = []
    monkeypatch.setattr(st, "_alert", lambda desk, *, is_paper, reason: alerts.append(reason))
    desk, ex = _new_desk()
    pos = _open(desk)
    ex.fail_next = True
    await _manage(desk, {"SC": 49.5, "SP": 35.0}, _T1)
    assert len(pos.income_legs) == 2 and alerts
    await _manage(desk, {"SC": 49.6, "SP": 35.0}, _T1.replace(minute=1))
    assert [leg.tradingsymbol for leg in pos.income_legs] == ["SP"]


async def test_blind_stops_alert_after_six_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[str] = []
    monkeypatch.setattr(st, "_alert", lambda desk, *, is_paper, reason: alerts.append(reason))
    desk, _ = _new_desk()
    _open(desk)
    for _i in range(6):
        await _manage(desk, {}, _T1)
    assert len(alerts) == 1 and "no LTP" in alerts[0]


async def test_position_left_from_a_prior_day_is_flattened() -> None:
    desk, _ = _new_desk()
    pos = _open(desk)
    pos.entered_at = datetime(2026, 10, 9, 10, 31, tzinfo=_IST)
    await _manage(desk, {"SC": 30.0, "SP": 30.0}, _T1)
    assert "OPT-S" not in desk.positions
    assert desk._repo.closed[0]["exit_reason"].startswith("SHORT_PREMIUM_TIME_EXIT")


async def test_open_position_is_still_managed_when_the_strategy_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk, ex = _new_desk()
    _open(desk)
    ex.prices.update({"SC": 49.5, "SP": 20.0})
    await st.manage(desk, _MON, _T1, {"SC": 49.5, "SP": 20.0}, None)  # cfg None = disabled/invalid
    assert [leg.tradingsymbol for leg in desk.positions["OPT-S"].income_legs] == ["SP"]


# ---------------------------------------------------------------- desk integration
async def test_generic_position_sweep_never_touches_it() -> None:
    desk, ex = _new_desk()
    _open(desk)
    await desk.sweep_position_manager(
        today=_MON,
        blackout_active=False,
        current_prices={"SC": 400.0, "SP": 0.5},
        current_ivs={},
        chains={},
    )
    assert "OPT-S" in desk.positions and ex.closed == [] and ex.rolled == []


async def test_guardian_waits_for_exit_time_then_is_the_backstop() -> None:
    desk, _ = _new_desk()
    _open(desk)
    await desk.sweep_expiry_guardian(_MON, datetime(2026, 10, 12, 14, 0, tzinfo=_IST))
    assert "OPT-S" in desk.positions
    await desk.sweep_expiry_guardian(_MON, datetime(2026, 10, 12, 15, 16, tzinfo=_IST))
    assert "OPT-S" not in desk.positions


async def test_restart_restores_closed_leg_and_frozen_rules() -> None:
    row = {
        "position_id": "OPT-R",
        "underlying": "NSE:NIFTY",
        "strategy_type": SHORT_PREMIUM_TYPE,
        "legs": json.dumps(
            {
                "protection": [],
                "income": [
                    {
                        "tradingsymbol": "SP",
                        "instrument_type": "PE",
                        "strike": 23850.0,
                        "direction": "SELL",
                        "lots": 1,
                        "lot_size": 65,
                        "premium": 40.0,
                        "delta": 0.2,
                    }
                ],
                "closed": [
                    {
                        "tradingsymbol": "SC",
                        "instrument_type": "CE",
                        "strike": 24150.0,
                        "direction": "SELL",
                        "lots": 1,
                        "lot_size": 65,
                        "premium": 40.0,
                        "exit_premium": 49.5,
                        "reason": "SHORT_PREMIUM_SL 23%",
                    }
                ],
                "meta": {"sl_pct": {"CE": 23, "PE": 23}, "exit_time": "15:01"},
            }
        ),
        "expiry": date(2026, 10, 13),
        "net_credit": 5200.0,
        "entry_margin": 150000.0,
        "opened_at": _AT,
        "is_paper": True,
    }
    repo = _SRepo()
    repo._open_rows = [row]
    desk, _ = _new_desk(repo)
    await desk.restore_positions()
    pos = desk.positions["OPT-R"]
    assert [leg.tradingsymbol for leg in pos.income_legs] == [
        "SP"
    ]  # the stopped leg is NOT re-opened
    assert pos.closed_pnl == pytest.approx(-(9.5 * 65)) and pos.meta["exit_time"] == "15:01"


async def test_open_short_premium_is_not_counted_against_the_desks_g7_cap() -> None:
    desk, _ = _new_desk()
    _open(desk)
    ctx = _ctx()
    desk._stamp_open_strategies(ctx)
    assert ctx.mode_engine_open_positions["paper:options"] == 0


async def test_reconciler_never_deletes_short_premium_rows() -> None:
    desk, _ = _new_desk()
    await desk.reconcile_duplicate_positions()
    assert SHORT_PREMIUM_TYPE in set(desk._repo.dedupe_kwargs["exclude_strategy_types"])


async def test_tick_manages_an_open_position_without_any_entry_conditions() -> None:
    desk, ex = _new_desk()
    _open(desk)
    ex.prices.update({"SC": 49.5, "SP": 20.0})

    class _Broker:
        async def get_ltp(self, syms):  # type: ignore[no-untyped-def]
            return {"SC": 49.5, "SP": 20.0}

    shared = {"seller_options": {"desk": desk}, "broker": _Broker()}
    await st.tick(shared, datetime(2026, 10, 12, 12, 0, tzinfo=_IST))
    assert [leg.tradingsymbol for leg in desk.positions["OPT-S"].income_legs] == ["SP"]


# ---------------------------------------------------------------- profit target
def test_target_defaults_off_and_rejects_negative() -> None:
    cfg = st.parse_cfg(_CFG)
    assert cfg.target_pct == 0
    with pytest.raises(ValueError):
        st.parse_cfg({**_CFG, "target_pct": -1})


async def test_target_pct_books_everything_when_total_profit_reaches_it() -> None:
    desk, ex = _new_desk()
    pos = _open(desk)
    pos.meta["target_pct"] = 50  # credit 80 pts x 65 = 5200 -> target 2600
    ex.prices.update({"SC": 20.0, "SP": 20.0})
    await _manage(desk, {"SC": 20.0, "SP": 20.0}, _T1)  # profit (20+20) x 65 = 2600
    assert "OPT-S" not in desk.positions
    row = desk._repo.closed[0]
    assert row["exit_reason"] == "SHORT_PREMIUM_TARGET"
    assert row["realized_pnl"] == pytest.approx(2600.0)


async def test_target_not_hit_leaves_the_position_alone() -> None:
    desk, ex = _new_desk()
    pos = _open(desk)
    pos.meta["target_pct"] = 50
    await _manage(desk, {"SC": 25.0, "SP": 25.0}, _T1)  # profit 1950 < 2600
    assert "OPT-S" in desk.positions and ex.closed == []


async def test_target_counts_a_leg_already_stopped_out() -> None:
    desk, ex = _new_desk()
    pos = _open(desk)
    pos.meta["target_pct"] = 20  # 20% of 5200 = 1040
    ex.prices.update({"SC": 49.5, "SP": 5.0})
    await _manage(
        desk, {"SC": 49.5, "SP": 5.0}, _T1
    )  # CE stopped -617.5; PE +35 x 65 = 2275 -> total 1657 >= 1040
    assert "OPT-S" not in desk.positions
    assert desk._repo.closed[0]["exit_reason"] == "SHORT_PREMIUM_TARGET"


async def test_blind_mark_never_triggers_the_target() -> None:
    desk, ex = _new_desk()
    pos = _open(desk)
    pos.meta["target_pct"] = 0.01
    await _manage(desk, {"SC": 1.0}, _T1)  # no price for SP: must not book a "profit"
    assert "OPT-S" in desk.positions and ex.closed == []


async def test_target_is_frozen_into_the_position_at_entry() -> None:
    desk, _ = _new_desk()
    pos = await _enter(desk, cfg={"target_pct": 40})
    assert pos.meta["target_pct"] == 40


# ---------------------------------------------------- no stupid blockers (found by the paper run)
def _cheap_expiry_day_chain():  # type: ignore[no-untyped-def]
    """Expiry day, 10:30: the +/-150 legs are worth only ~Rs3 with a Rs0.20 bid-ask spread (6.7%)."""
    from src.options.nfo_chain import OptionQuote

    chain = _chain()
    for row in chain.strikes:
        if row.strike in (23850.0, 24150.0):
            q = OptionQuote("S", ltp=3.0, bid=2.9, ask=3.1, oi=900_000, volume=400_000)
            row.call, row.put = q, q
    return chain


async def test_cheap_expiry_day_legs_with_a_wide_percent_spread_still_trade() -> None:
    desk, ex = _new_desk()
    desk._chain_fetcher._chain = desk._chain_fetcher._tier1_chain = _cheap_expiry_day_chain()
    pos = await _enter(desk)
    assert pos is not None and {leg.premium for leg in pos.income_legs} == {3.0}


def test_no_premium_floor_by_default_and_spread_rule_is_configurable() -> None:
    assert st.parse_cfg(_CFG).min_leg_premium == 0
    chain = _cheap_expiry_day_chain()
    assert (
        st.build_short_premium_legs(chain, 1, ce_offset=150, pe_offset=-150, wing_points=0)
        is not None
    )
    strict = st.build_short_premium_legs(
        chain,
        1,
        ce_offset=150,
        pe_offset=-150,
        wing_points=0,
        max_spread_pct=1.0,
        max_spread_points=0.05,
    )
    assert strict is None  # still refuses a genuinely wide market when configured to


async def test_entry_does_not_wait_for_the_llm_shadow_call() -> None:
    desk, _ = _new_desk()
    release = asyncio.Event()

    async def _slow(*a, **k):  # type: ignore[no-untyped-def]
        await release.wait()
        return {"flag": "OK", "confidence": 0.9, "reason": "", "provider": "p"}

    desk._g9_shadow = _slow  # type: ignore[method-assign]
    pos = await asyncio.wait_for(
        _enter(desk), timeout=2
    )  # returns although the LLM has not answered
    assert pos is not None and "llm_shadow" not in pos.meta
    release.set()
    await asyncio.gather(*desk._short_premium_bg)
    assert pos.meta["llm_shadow"]["flag"] == "OK"


async def test_on_expiry_day_trades_todays_expiry_not_the_next_weeks_chain() -> None:
    """Kite stamps expiry 00:00, so fetch_tier1() skips today's expiry at 10:30 and returns NEXT week's
    chain; the strategy must ask for today's expiry explicitly (found by the paper-day run)."""
    desk, ex = _new_desk()
    today_chain = _chain()  # expires today (2026-10-13 Tue)
    next_week = _chain(expiry=date(2026, 10, 20))
    desk._chain_fetcher._tier1_chain = next_week  # what fetch_tier1 wrongly returns on expiry day
    desk._chain_fetcher.expiry_chains[date(2026, 10, 13)] = today_chain
    tue = date(2026, 10, 13)
    pos = await _enter(desk, today=tue, now=datetime(2026, 10, 13, 10, 31, tzinfo=_IST))
    assert pos is not None and pos.expiry == tue
    assert desk._chain_fetcher.expiry_requests == [("NSE:NIFTY", tue)]


async def test_on_a_normal_day_it_falls_back_to_the_nearest_expiry_chain() -> None:
    desk, _ = _new_desk()
    pos = await _enter(
        desk
    )  # Monday: no chain for today's date as an expiry -> tier1 (Tuesday expiry)
    assert pos is not None and pos.expiry == date(2026, 10, 13)


async def test_no_false_missed_entry_alert_when_the_position_already_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Found by the paper-day run: a quick target/stop-out inside the 30 minutes after the entry window
    left no open position, and the 'missed entry' alert fired although the day was traded."""
    alerts: list[str] = []
    monkeypatch.setattr(st, "_alert", lambda desk, *, is_paper, reason: alerts.append(reason))
    monkeypatch.setattr("src.market.calendar.is_trading_day", lambda d: True)
    cfg = st.parse_cfg(_CFG)
    after = datetime(2026, 10, 12, 10, 50, tzinfo=_IST)
    desk, _ = _new_desk()
    desk._short_premium_entered_day = _MON  # traded earlier today, already closed
    await st._maybe_report_missed(desk, cfg, _MON, after)
    assert alerts == []
    desk2, _ = _new_desk(_SRepo(traded=True))  # restart: no memory, the DB knows
    await st._maybe_report_missed(desk2, cfg, _MON, after)
    assert alerts == []
    desk3, _ = _new_desk()  # genuinely nothing happened -> exactly one alert, once
    await st._maybe_report_missed(desk3, cfg, _MON, after)
    await st._maybe_report_missed(desk3, cfg, _MON, after)
    assert len(alerts) == 1


# ---------------------------------------------------- lots scale from the PERCENTAGE budget, no rupee constants
def _budget(monkeypatch: pytest.MonkeyPatch, equity: float, pct: float = 0.25) -> None:
    import src.risk.budget as rb

    class _Opt:
        def max_loss_per_day_per_strategy_rupees(self, eq: float) -> float:
            return eq * pct / 100.0

    monkeypatch.setattr(rb, "get_budget", lambda: SimpleNamespace(options=_Opt()))
    monkeypatch.setattr(rb, "get_current_equity", lambda: equity)


def _one_lot_income():  # type: ignore[no-untyped-def]
    return st.build_short_premium_legs(_chain(), 1, ce_offset=150, pe_offset=-150, wing_points=0)[1]


def test_auto_lots_grow_with_equity_and_never_exceed_the_cap_fraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = st.parse_cfg({**_CFG, "lots": 0, "max_lots": 50})
    income = _one_lot_income()
    per_lot = st.sl_defined_max_loss(income, cfg)
    lots = {}
    for eq in (1_000_000, 2_000_000, 4_000_000, 8_000_000):
        _budget(monkeypatch, eq)
        lots[eq], _ = st.size_lots(cfg, income)
        cap = eq * 0.25 / 100
        assert (
            lots[eq] * per_lot <= 0.8 * cap + 1e-6
        )  # the all-stopped loss fits the fraction of the % cap
    assert (
        lots[1_000_000] <= lots[2_000_000] < lots[4_000_000] < lots[8_000_000]
    )  # scales with equity


def test_auto_lots_respect_the_max_lots_ceiling_and_a_fixed_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    income = _one_lot_income()
    _budget(monkeypatch, 80_000_000)
    assert st.size_lots(st.parse_cfg({**_CFG, "lots": 0, "max_lots": 3}), income)[0] == 3
    assert (
        st.size_lots(st.parse_cfg({**_CFG, "lots": 2, "max_lots": 5}), income)[0] == 2
    )  # explicit override
    assert st.size_lots(st.parse_cfg({**_CFG, "lots": 9, "max_lots": 5}), income)[0] == 5


def test_auto_lots_refuse_when_not_even_one_lot_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    _budget(monkeypatch, 100_000)  # cap = 0.25% = Rs250: far below one lot's all-stopped loss
    lots, why = st.size_lots(st.parse_cfg({**_CFG, "lots": 0}), _one_lot_income())
    assert lots == 0 and "capital too small" in why


async def test_entry_uses_the_auto_sized_lots_and_logs_why(monkeypatch: pytest.MonkeyPatch) -> None:
    _budget(monkeypatch, 4_000_000)
    desk, ex = _new_desk()
    pos = await _enter(desk, cfg={"lots": 0, "max_lots": 50})
    assert pos is not None and pos.lots >= 2
    assert all(leg.lots == pos.lots for leg in pos.income_legs)


def test_invalid_sizing_config_is_refused() -> None:
    for bad in (
        {"lots": -1},
        {"risk_fraction_of_daily_cap": 0},
        {"risk_fraction_of_daily_cap": 1.5},
    ):
        with pytest.raises(ValueError):
            st.parse_cfg({**_CFG, **bad})


# ---------------------------------------------------- speed: no Kite round trips on the time-critical path
class _HintExecutor(_PxExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.hints: list[dict[str, float]] = []

    def hint_prices(self, prices):  # type: ignore[no-untyped-def]
        self.hints.append(dict(prices))


async def test_entry_hands_the_decision_prices_to_the_paper_fill() -> None:
    ex = _HintExecutor()
    desk, _ = _desk(_chain(), executor=ex, repo=_SRepo())
    pos = await _enter(desk)
    assert pos is not None
    assert ex.hints and set(ex.hints[0]) == {leg.tradingsymbol for leg in pos.income_legs}


async def test_a_stop_closes_at_the_price_that_triggered_it() -> None:
    ex = _HintExecutor()
    desk, _ = _desk(_chain(), executor=ex, repo=_SRepo())
    pos = _open(desk)
    ex.prices["SC"] = 49.5
    await _manage(desk, {"SC": 49.5, "SP": 35.0}, _T1)
    assert {"SC": 49.5} in ex.hints and [leg.tradingsymbol for leg in pos.income_legs] == ["SP"]


async def test_price_polling_uses_the_priority_lane_when_the_broker_has_one() -> None:
    desk, ex = _new_desk()
    _open(desk)

    class _Broker:
        lane = ""

        async def get_ltp(self, syms):  # type: ignore[no-untyped-def]
            self.lane = "shared"
            return {"SC": 40.0, "SP": 40.0}

        async def priority_ltp(self, syms):  # type: ignore[no-untyped-def]
            self.lane = "priority"
            return {"SC": 40.0, "SP": 40.0}

    broker = _Broker()
    await st.tick(
        {"seller_options": {"desk": desk}, "broker": broker},
        datetime(2026, 10, 12, 12, 0, tzinfo=_IST),
    )
    assert broker.lane == "priority"


# ------------------------------------- target/stop adjust to the legs that are still open
async def test_target_adjusts_to_the_remaining_leg_after_the_other_is_stopped() -> None:
    """After one leg is stopped the ORIGINAL target (50% of both legs' credit on total P&L) is unreachable:
    the closed leg's loss alone can exceed what the last leg can still earn. The target now applies to the
    legs still open -- 50% of THEIR premium on THEIR live P&L."""
    desk, ex = _new_desk()
    pos = _open(desk)
    pos.meta["target_pct"] = 50
    ex.prices["SP"] = 49.5
    await _manage(desk, {"SC": 40.0, "SP": 49.5}, _T1)  # PE stopped: -(49.5-40)*65 = -617.5 booked
    assert [leg.tradingsymbol for leg in pos.income_legs] == ["SC"]
    # old rule: total 5200*50% = 2600 -> CE at 25 gives total +975-617 = 357: nowhere near. New rule: 50% of
    # the CE's own 40*65 = 2600 premium = 1300 on its own P&L.
    await _manage(desk, {"SC": 25.0}, _T1.replace(minute=2))  # CE live +975 < 1300
    assert "OPT-S" in desk.positions
    ex.prices["SC"] = 20.0
    await _manage(desk, {"SC": 20.0}, _T1.replace(minute=3))  # CE live +1300 >= 1300
    assert "OPT-S" not in desk.positions
    assert desk._repo.closed[0]["exit_reason"] == "SHORT_PREMIUM_TARGET"


def test_open_legs_credit_is_the_net_premium_of_what_is_still_open() -> None:
    desk_pos = _open(_new_desk()[0], wings=True)
    assert st.open_legs_credit(desk_pos.income_legs) == (40.0 + 40.0) * 65
    assert (
        st.open_legs_credit(desk_pos.income_legs + desk_pos.protection_legs) == (80.0 - 10.0) * 65
    )


# ------------------------------------------- realised-P&L alerts come from the DESK, for every strategy
async def test_closing_a_leg_sends_the_realised_pnl_alert_and_the_exit_lists_each_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(sd, "notify", lambda kind, **kw: sent.append((kind, kw)))
    desk, ex = _new_desk()
    pos = _open(desk)
    ex.prices.update({"SP": 49.5, "SC": 25.0})
    await _manage(desk, {"SC": 40.0, "SP": 49.5}, _T1)  # PE stopped at 49.5: -(49.5-40)*65 = -617.5

    from src.notifications.events import EventKind

    leg_alerts = [kw for kind, kw in sent if kind == EventKind.PARTIAL_BOOK]
    assert len(leg_alerts) == 1
    a = leg_alerts[0]
    assert a["realized_pnl"] == pytest.approx(-617.5)
    assert a["leg_label"] == "NIFTY 23850 PE" and a["segment"] == sd.Segment.OPTIONS
    assert a["entry"] == 40.0 and a["exit"] == 49.5

    await _manage(
        desk, {"SC": 25.0}, datetime(2026, 10, 12, 15, 1, tzinfo=_IST)
    )  # time exit closes the CE
    exits = [kw for kind, kw in sent if kind == EventKind.EXIT]
    assert exits and exits[0]["net_pnl"] == pytest.approx(
        -617.5 + 15.0 * 65
    )  # total = booked + final leg
    legs = {x["leg"]: x["pnl"] for x in exits[0]["leg_pnls"]}
    assert legs == {"PE 23850": pytest.approx(-617.5), "CE 24150": pytest.approx(15.0 * 65)}
    assert pos.position_id not in desk.positions


async def test_a_failed_leg_close_alerts_and_does_not_send_a_booked_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[Any] = []
    monkeypatch.setattr(sd, "notify", lambda kind, **kw: sent.append(kind))
    desk, ex = _new_desk()
    _open(desk)
    ex.fail_next = True
    await _manage(desk, {"SC": 49.5, "SP": 35.0}, _T1)
    from src.notifications.events import EventKind

    assert EventKind.PARTIAL_BOOK not in sent  # nothing was booked, the leg is still open


def _px(total_premium: float) -> dict[str, float]:
    return {"SC": total_premium / 2, "SP": total_premium / 2}


async def test_trail_lock_shadow_records_once_and_never_closes() -> None:
    """Hypothesis J: credit 80 x 65 = 5200 -> arms at +1560, floor = peak - 780, only rises."""
    desk, ex = _new_desk()
    pos = _open(desk)
    await _manage(desk, _px(60.0), _T1)  # +1300: not armed
    await _manage(desk, _px(50.0), _T1)  # +1950: armed, floor 1170
    await _manage(desk, _px(60.0), _T1)  # +1300 > 1170: no fire
    assert "trail_lock_shadow" not in pos.meta
    await _manage(desk, _px(40.0), _T1)  # +2600 peak, floor 1820
    await _manage(desk, _px(50.0), _T1)  # +1950 > 1820 (old floor 1170 never comes back)
    assert "trail_lock_shadow" not in pos.meta
    await _manage(desk, _px(53.0), _T1)  # +1755 <= 1820: would exit
    rec = pos.meta["trail_lock_shadow"]
    assert (rec["total"], rec["peak"], rec["floor"]) == (1755.0, 2600.0, 1820.0)
    await _manage(desk, _px(70.0), _T1)  # recorded once, never overwritten
    assert pos.meta["trail_lock_shadow"] == rec
    assert ex.closed == [] and "OPT-S" in desk.positions  # shadow never touches a leg


async def test_trail_lock_shadow_never_fires_unarmed() -> None:
    desk, _ = _new_desk()
    pos = _open(desk)
    await _manage(desk, _px(58.0), _T1)  # +1430 < 1560
    await _manage(desk, _px(79.0), _T1)  # big drop but never armed
    assert "trail_lock_shadow" not in pos.meta
