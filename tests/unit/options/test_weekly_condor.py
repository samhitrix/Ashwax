"""Hypothesis B -- weekly NIFTY condor inside the seller desk (run_weekly_condor_cycle)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from src.brokers.base import OptionQuote
from src.options import seller_desk as sd
from src.options.greeks_budget import GreeksBudget
from src.options.nfo_chain import ChainStrike, OptionChain
from src.options.seller_desk import (
    WEEKLY_CONDOR_TYPE,
    OpenPosition,
    build_weekly_condor,
    first_trading_day_of_cycle,
)
from src.options.structure_selector import OptionLeg

from tests.unit.options.test_seller_desk import (
    _APPROVE_ALL_CRM,
    _BlockAllCRM,
    _desk,
    _FakeEventCalendar,
    _FakeRepo,
)

_IST = ZoneInfo("Asia/Kolkata")
_WED = date(2026, 10, 7)  # first trading day after Tue 2026-10-06 expiry
_EXPIRY = date(2026, 10, 13)  # next Tuesday weekly
_IN_WINDOW = datetime(2026, 10, 7, 10, 10, tzinfo=_IST)
_NAME = "nifty_weekly_condor_simple"
_CFG = {
    "enabled": True,
    "registry_name": _NAME,
    "underlying": "NIFTY",
    "short_delta": 0.15,
    "wing_points": 200,
    "entry_window": ["10:00", "10:30"],
    "max_lots": 1,
    "profit_target_pct": 50,
    "stop_loss_credit_multiple": 2.0,
}


def _q(mid: float) -> OptionQuote:
    return OptionQuote("SYM", ltp=mid, bid=mid - 0.05, ask=mid + 0.05, oi=5000, volume=2000)


def _weekly_chain(*, drop_strike: float | None = None) -> OptionChain:
    """Strikes 23000..25000 step 100 around spot 24000; |delta| 0.15 at 23600 PE / 24400 CE."""
    rows = []
    for k in range(23000, 25001, 100):
        strike = float(k)
        if strike == drop_strike:
            continue
        dist = (strike - 24000.0) / 100.0  # -10..+10
        call_d = max(0.01, min(0.99, 0.5 - dist * 0.0875))  # 24400 -> 0.15
        put_d = -max(0.01, min(0.99, 0.5 + dist * 0.0875))  # 23600 -> -0.15
        rows.append(
            ChainStrike(
                strike=strike,
                call=_q(max(1.0, 120.0 - dist * 12.0)),
                put=_q(max(1.0, 120.0 + dist * 12.0)),
                call_iv=0.14,
                put_iv=0.15,
                call_delta=call_d,
                put_delta=put_d,
                call_symbol=f"NIFTY{k}CE",
                put_symbol=f"NIFTY{k}PE",
            )
        )
    exp = datetime(_EXPIRY.year, _EXPIRY.month, _EXPIRY.day, 15, 30, tzinfo=_IST)
    return OptionChain(
        underlying="NIFTY", spot=24000.0, expiry=exp, days_to_expiry=6, lot_size=75, strikes=rows
    )


class _Repo(_FakeRepo):
    def __init__(self, traded: bool = False) -> None:
        super().__init__()
        self.traded = traded

    async def has_position_for_expiry(self, **kwargs):  # type: ignore[no-untyped-def]
        return self.traded


class _RecordingCRM:
    def __init__(self) -> None:
        self.signals: list = []

    def evaluate(self, signal, ctx):  # type: ignore[no-untyped-def]
        self.signals.append(signal)
        return _APPROVE_ALL_CRM.evaluate(signal, ctx)


def _ctx(mode: str = "paper") -> SimpleNamespace:
    return SimpleNamespace(llm_gate_required={}, strategy_execution_mode={_NAME: mode})


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sd._seller_settings, "weekly_condor", lambda settings=None: dict(_CFG))
    monkeypatch.setattr(
        "src.market.calendar.is_trading_day", lambda d: d.weekday() < 5, raising=True
    )


async def _run(desk, *, today=_WED, now=_IN_WINDOW, crm=None, ctx=None):  # type: ignore[no-untyped-def]
    return await desk.run_weekly_condor_cycle(
        "NSE:NIFTY",
        today=today,
        now=now,
        available_margin=1_000_000.0,
        regime="UNKNOWN",
        vix=13.0,
        crm=crm or _APPROVE_ALL_CRM,
        crm_ctx=ctx or _ctx(),
    )


def test_build_uses_target_delta_shorts_and_fixed_wings() -> None:
    protection, income = build_weekly_condor(_weekly_chain(), 1, short_delta=0.15, wing_points=200)
    shorts = {(leg.instrument_type, leg.strike) for leg in income}
    longs = {(leg.instrument_type, leg.strike) for leg in protection}
    assert shorts == {("PE", 23600.0), ("CE", 24400.0)}
    assert longs == {("PE", 23400.0), ("CE", 24600.0)}
    assert all(leg.direction == "SELL" for leg in income)
    assert all(leg.direction == "BUY" for leg in protection)


def test_build_refuses_to_substitute_a_missing_wing() -> None:
    chain = _weekly_chain(drop_strike=24600.0)
    assert build_weekly_condor(chain, 1, short_delta=0.15, wing_points=200) is None


def test_first_trading_day_of_cycle_skips_weekend_and_holiday(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert first_trading_day_of_cycle(_EXPIRY) == _WED
    holiday = date(2026, 10, 7)
    monkeypatch.setattr(
        "src.market.calendar.is_trading_day", lambda d: d.weekday() < 5 and d != holiday
    )
    assert first_trading_day_of_cycle(_EXPIRY) == date(2026, 10, 8)


async def test_desk_gamma_budget_blocks_a_weekly_condor() -> None:
    # Real finding 2026-10-05: one lot of a 6-DTE 0.15-delta condor carries ~Rs 2 lakh of
    # rupee-gamma vs the desk's 8%-of-capital (Rs 80k) budget -- the shared control wins.
    desk, executor = _desk(_weekly_chain(), repo=_Repo())
    assert await _run(desk) is None
    assert executor.executed == []


async def test_opens_condor_on_first_day_in_window_with_true_max_loss() -> None:
    desk, executor = _desk(_weekly_chain(), repo=_Repo())
    desk._greeks_budget = GreeksBudget(vega=1e9, delta=1e9, theta=1e9, gamma=1e9)
    crm = _RecordingCRM()
    pos = await _run(desk, crm=crm)
    assert pos is not None and pos.strategy_type == WEEKLY_CONDOR_TYPE
    assert len(executor.executed) == 1
    sig = crm.signals[0]
    assert sig.strategy_name == _NAME  # registry name -> registry G9/mode apply
    credit = pos.entry_premium
    assert credit > 0
    assert sig.risk_rupees == pytest.approx(200 * 75 - credit)  # one wing, not the whole span
    assert desk._repo.inserted[0]["strategy_type"] == WEEKLY_CONDOR_TYPE


@pytest.mark.parametrize(
    "kwargs",
    [
        {"now": datetime(2026, 10, 7, 9, 55, tzinfo=_IST)},  # before window
        {"now": datetime(2026, 10, 7, 10, 35, tzinfo=_IST)},  # after window
        {
            "today": date(2026, 10, 6),
            "now": datetime(2026, 10, 6, 10, 10, tzinfo=_IST),
        },  # cycle hasn't started yet (previous expiry day itself)
    ],
)
async def test_no_entry_outside_window_or_before_cycle_starts(kwargs) -> None:  # type: ignore[no-untyped-def]
    desk, executor = _desk(_weekly_chain(), repo=_Repo())
    assert await _run(desk, **kwargs) is None
    assert executor.executed == []


async def test_entry_still_allowed_a_day_after_the_window_was_missed() -> None:
    # 2026-10-07: a restart/outage on first_day used to lose the whole week's trade --
    # catching up a day later (same expiry, not yet traded, still inside min_entry_dte)
    # must still open the condor, not wait for next week.
    desk, executor = _desk(_weekly_chain(), repo=_Repo())
    desk._greeks_budget = GreeksBudget(vega=1e9, delta=1e9, theta=1e9, gamma=1e9)
    pos = await _run(desk, today=date(2026, 10, 8), now=datetime(2026, 10, 8, 10, 10, tzinfo=_IST))
    assert pos is not None and pos.strategy_type == WEEKLY_CONDOR_TYPE
    assert len(executor.executed) == 1


async def test_once_per_expiry_even_after_restart_or_early_exit() -> None:
    desk, executor = _desk(_weekly_chain(), repo=_Repo(traded=True))
    assert await _run(desk) is None
    assert executor.executed == []


async def test_event_blackout_blocks_entry() -> None:
    desk, executor = _desk(
        _weekly_chain(), repo=_Repo(), event_calendar=_FakeEventCalendar(blocked=True)
    )
    assert await _run(desk) is None
    assert executor.executed == []


async def test_paper_hard_lock_refuses_when_desk_is_live() -> None:
    desk, executor = _desk(_weekly_chain(), repo=_Repo())
    desk._is_paper = False
    assert await _run(desk, ctx=_ctx("paper")) is None
    assert executor.executed == []


async def test_crm_block_and_missing_crm_both_fail_closed() -> None:
    desk, executor = _desk(_weekly_chain(), repo=_Repo())
    assert await _run(desk, crm=_BlockAllCRM()) is None
    assert (
        await desk.run_weekly_condor_cycle(
            "NSE:NIFTY",
            today=_WED,
            now=_IN_WINDOW,
            available_margin=1e6,
            regime="UNKNOWN",
            vix=13.0,
            crm=None,
            crm_ctx=None,
        )
        is None
    )
    assert executor.executed == []


async def test_disabled_in_settings_does_nothing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        sd._seller_settings, "weekly_condor", lambda settings=None: {"enabled": False}
    )
    desk, executor = _desk(_weekly_chain(), repo=_Repo())
    assert await _run(desk) is None
    assert executor.executed == []


def _open_condor(desk, credit_per_unit: float = 30.0) -> OpenPosition:  # type: ignore[no-untyped-def]
    def leg(sym: str, kind: str, strike: float, d: str, prem: float) -> OptionLeg:
        return OptionLeg(
            tradingsymbol=sym,
            instrument_type=kind,
            strike=strike,
            direction=d,
            lots=1,
            lot_size=75,
            premium=prem,
            delta=0.15,
        )

    # shorts 20 each, wings 5 each -> net credit 30 points x 75
    prot = [leg("LP", "PE", 23400.0, "BUY", 5.0), leg("LC", "CE", 24600.0, "BUY", 5.0)]
    inc = [leg("SP", "PE", 23600.0, "SELL", 20.0), leg("SC", "CE", 24400.0, "SELL", 20.0)]
    pos = OpenPosition(
        position_id="OPT-B",
        underlying="NSE:NIFTY",
        strategy_type=WEEKLY_CONDOR_TYPE,
        direction="credit",
        protection_legs=prot,
        income_legs=inc,
        entry_premium=credit_per_unit * 75,
        entry_margin=(200 - credit_per_unit) * 75,
        expiry=_EXPIRY,
        entered_at=_IN_WINDOW,
    )
    desk.positions[pos.position_id] = pos
    return pos


def _open_desk_spread(desk, position_id: str, strategy_type: str = "bull_put_spread") -> None:  # type: ignore[no-untyped-def]
    """A desk-own NIFTY structure (not the condor) -- for proving the condor's own slot
    is independent of how many of the desk's OWN structures already occupy the index."""

    def leg(sym: str, kind: str, strike: float, d: str, prem: float) -> OptionLeg:
        return OptionLeg(
            tradingsymbol=sym,
            instrument_type=kind,
            strike=strike,
            direction=d,
            lots=1,
            lot_size=75,
            premium=prem,
            delta=0.2,
        )

    prot = [leg(f"{position_id}B", "PE", 23400.0, "BUY", 5.0)]
    inc = [leg(f"{position_id}S", "PE", 23600.0, "SELL", 25.0)]
    desk.positions[position_id] = OpenPosition(
        position_id=position_id,
        underlying="NSE:NIFTY",
        strategy_type=strategy_type,
        direction="credit",
        protection_legs=prot,
        income_legs=inc,
        entry_premium=20.0 * 75,
        entry_margin=200.0 * 75,
        expiry=_EXPIRY,
        entered_at=_IN_WINDOW,
    )


async def test_condor_opens_even_when_desk_already_has_two_own_nifty_structures(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    # 2026-10-07: found live -- desk had 2 of its OWN NIFTY spreads open (at the real
    # max_strategies_per_index=2 cap), and the condor was silently refused for sharing
    # that same per-index counter despite its own spec promising an independent slot.
    monkeypatch.setattr(sd._seller_settings, "max_strategies_per_index", lambda settings=None: 2)
    desk, executor = _desk(_weekly_chain(), repo=_Repo())
    desk._greeks_budget = GreeksBudget(vega=1e9, delta=1e9, theta=1e9, gamma=1e9)
    _open_desk_spread(desk, "OPT-D1", "bull_put_spread")
    _open_desk_spread(desk, "OPT-D2", "bear_call_spread")
    pos = await _run(desk)
    assert pos is not None and pos.strategy_type == WEEKLY_CONDOR_TYPE
    assert len(executor.executed) == 1


async def _sweep(desk, prices):  # type: ignore[no-untyped-def]
    await desk.sweep_position_manager(
        today=_WED, blackout_active=False, current_prices=prices, current_ivs={}, chains={}
    )


@pytest.mark.parametrize(
    ("prices", "closed", "reason_prefix"),
    [
        ({"LP": 3.0, "LC": 3.0, "SP": 10.0, "SC": 10.0}, True, "WEEKLY_CONDOR_TP"),  # +16 >= 15
        ({"LP": 5.0, "LC": 30.0, "SP": 20.0, "SC": 110.0}, True, "WEEKLY_CONDOR_SL"),  # -65 <= -60
        ({"LP": 5.0, "LC": 5.0, "SP": 25.0, "SC": 25.0}, False, None),  # -10 pts: hold
    ],
)
async def test_condor_take_profit_and_stop(prices, closed, reason_prefix) -> None:  # type: ignore[no-untyped-def]
    desk, executor = _desk(_weekly_chain(), repo=_Repo())
    _open_condor(desk)
    await _sweep(desk, prices)
    if closed:
        assert "OPT-B" not in desk.positions
        assert desk._repo.closed[0]["exit_reason"].startswith(reason_prefix)
    else:
        assert "OPT-B" in desk.positions
    assert executor.rolled == []  # never rolled


async def test_condor_holds_when_a_leg_is_unpriced() -> None:
    desk, _ = _desk(_weekly_chain(), repo=_Repo())
    _open_condor(desk)
    await _sweep(desk, {"LP": 0.1, "LC": 0.1, "SP": 0.1})  # SC missing -> no decision
    assert "OPT-B" in desk.positions


async def test_guardian_still_closes_condor_near_expiry() -> None:
    desk, _ = _desk(_weekly_chain(), repo=_Repo())
    _open_condor(desk)
    monday = _EXPIRY - timedelta(days=1)
    await desk.sweep_expiry_guardian(monday, datetime.combine(monday, time(9, 20), _IST))
    assert "OPT-B" not in desk.positions


async def test_open_condor_does_not_block_the_desk_on_same_index(caplog) -> None:  # type: ignore[no-untyped-def]
    desk, _ = _desk(_weekly_chain(), repo=_Repo())
    _open_condor(desk)
    with caplog.at_level(logging.INFO, logger="options.seller_desk"):
        await desk.run_cycle(
            "NSE:NIFTY",
            regime="HIGH_VOL_RANGE_BOUND",
            ivr=65.0,
            term_structure="contango",
            today=_WED,
            now=_IN_WINDOW,
            expiry_calendar={},
            available_margin=1e6,
            crm=_APPROVE_ALL_CRM,
            crm_ctx=SimpleNamespace(llm_gate_required={}),
        )
    assert "seller_desk_already_open" not in caplog.text


async def test_desk_run_cycle_enforces_registry_g9_for_its_own_trades() -> None:
    # 2026-10-05 fix: options_seller_desk_v3 llm_gate_required=true must apply to desk
    # signals named "options_seller_desk::<type>". No LLM provider -> no trade.
    from tests.unit.options.test_seller_desk import _rich_chain

    chain = _rich_chain()
    desk, executor = _desk(chain)
    desk._g9_llm_loaded, desk._g9_llm = True, None  # provider unavailable
    calendar = {"NIFTY": [chain.expiry.date() + timedelta(days=i) for i in range(-2, 3)]}
    pos = await desk.run_cycle(
        "NSE:NIFTY",
        regime="HIGH_VOL_RANGE_BOUND",
        ivr=65.0,
        term_structure="contango",
        today=date(2026, 8, 1),
        now=datetime(2026, 8, 1, 11, 0, tzinfo=_IST),
        expiry_calendar=calendar,
        available_margin=1_000_000.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=SimpleNamespace(llm_gate_required={"options_seller_desk_v3": True}),
    )
    assert pos is None
    assert executor.executed == []


async def test_per_underlying_cap_allows_a_second_structure_when_set_to_two(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # 2026-10-06: limits count STRATEGIES; with max_strategies_per_index=2 an open
    # NIFTY condor no longer stops a second NIFTY strategy.
    monkeypatch.setattr(sd._seller_settings, "max_strategies_per_index", lambda settings=None: 2)
    desk, _ = _desk(_weekly_chain(), repo=_Repo())
    _open_condor(desk)
    pos = SimpleNamespace(underlying="NSE:NIFTY")
    assert sum(1 for p in desk.positions.values() if p.underlying == pos.underlying) == 1
    monkeypatch.setattr(sd._seller_settings, "max_strategies_per_index", lambda settings=None: 1)
    import logging as _lg

    class _H(_lg.Handler):
        msgs: list = []

        def emit(self, rec):  # type: ignore[no-untyped-def]
            self.msgs.append(rec.getMessage())

    h = _H()
    _lg.getLogger("options.seller_desk").addHandler(h)
    try:
        await desk.run_cycle(
            "NSE:NIFTY",
            regime="HIGH_VOL_RANGE_BOUND",
            ivr=65.0,
            term_structure="contango",
            today=_WED,
            now=_IN_WINDOW,
            expiry_calendar={},
            available_margin=1e6,
            crm=_APPROVE_ALL_CRM,
            crm_ctx=SimpleNamespace(llm_gate_required={}),
        )
    finally:
        _lg.getLogger("options.seller_desk").removeHandler(h)
    assert any(
        "seller_desk_already_open" in m and "max_strategies_per_index=1" in m for m in h.msgs
    )


def test_open_structure_count_reaches_crm_g7() -> None:
    # Root cause 2026-10-06: nothing filled CRM's options open count, so the total cap
    # (max_open_options_positions) always saw 0 and never blocked.
    desk, _ = _desk(_weekly_chain(), repo=_Repo())
    _open_condor(desk)
    ctx = SimpleNamespace(mode_engine_open_positions={})
    desk._stamp_open_strategies(ctx)
    assert ctx.mode_engine_open_positions == {"paper:options": 1, "live:options": 0}


def test_g7_blocks_options_at_max_open_strategies() -> None:
    from src.options.seller_crm_adapter import build_seller_crm_signal
    from src.risk.crm import CentralRiskManager

    from tests.unit.risk.test_crm_options_seller import _ctx

    sig = build_seller_crm_signal(
        signal_id="S1",
        underlying="NSE:NIFTY",
        strategy_type="iron_condor",
        lots=1,
        max_loss=8000.0,
        max_profit=3000.0,
        regime="X",
        vix=15.0,
    )
    ctx = _ctx(max_options_open_positions=3, segment_paper_mode={"options": True})
    ctx.mode_engine_open_positions["paper:options"] = 3
    from unittest.mock import patch

    with patch("src.risk.crm.datetime") as dt:  # inside market hours, so G3 passes
        dt.now.return_value = datetime(2026, 10, 7, 11, 0, tzinfo=_IST)
        d = CentralRiskManager().evaluate(sig, ctx)
    assert d.approved is False and d.blocking_gate == "G7"


async def test_stamp_daily_loss_fills_the_real_running_total(monkeypatch) -> None:
    # 2026-10-06: options_daily_loss_so_far was never written, so it stayed Rs 0 all day
    # and G1/G8's "daily loss" check behaved as a flat per-trade ceiling instead.
    desk, _ = _desk(_weekly_chain(), repo=_Repo())
    ctx = SimpleNamespace(mode_engine_open_positions={}, options_daily_loss_so_far=123.0)
    desk._realized_today = 0.0
    desk._stamp_daily_loss(ctx)
    assert ctx.options_daily_loss_so_far == 0.0  # flat/positive day -> no loss booked
    desk._realized_today = -7500.0
    desk._stamp_daily_loss(ctx)
    assert ctx.options_daily_loss_so_far == 7500.0  # magnitude of the loss
    desk._realized_today = 4000.0  # a net-winning day never goes negative
    desk._stamp_daily_loss(ctx)
    assert ctx.options_daily_loss_so_far == 0.0


def test_daily_loss_now_blocks_on_the_real_running_total_not_one_trade_size() -> None:
    from unittest.mock import patch

    from src.options.seller_crm_adapter import build_seller_crm_signal
    from src.risk.crm import CentralRiskManager

    from tests.unit.risk.test_crm_options_seller import _ctx

    sig = build_seller_crm_signal(
        signal_id="S",
        underlying="NSE:NIFTY",
        strategy_type="bull_put_spread",
        lots=1,
        max_loss=10800.0,
        max_profit=3000.0,
        regime="X",
        vix=15.0,
    )
    crm = CentralRiskManager()
    with patch("src.risk.crm.datetime") as dt:
        dt.now.return_value = datetime(2026, 10, 7, 11, 0, tzinfo=_IST)
        # A single Rs 10,800 trade must NOT be blocked outright by a Rs 25,000 daily ceiling
        # with zero loss booked so far (the old bug: it compared the trade's own size against
        # the ceiling as if it were a per-trade cap).
        ok = crm.evaluate(
            sig,
            _ctx(
                max_options_open_positions=10,
                max_options_daily_loss=10000.0,
                options_daily_loss_so_far=0.0,
            ),
        )
        assert ok.approved is True
        # Once today's REALIZED options loss reaches the ceiling, new trades are blocked (G1).
        blocked = crm.evaluate(
            sig,
            _ctx(
                max_options_open_positions=10,
                max_options_daily_loss=10000.0,
                options_daily_loss_so_far=10000.0,
            ),
        )
        assert blocked.approved is False and blocked.blocking_gate == "G1"


async def test_strategy_halt_blocks_a_new_weekly_condor_entry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # 2026-10-07: the per-strategy daily-loss kill must stop THIS strategy from
    # reopening on the same day it was closed for a daily loss.
    desk, executor = _desk(_weekly_chain(), repo=_Repo())
    desk._strategy_halted_day[_NAME] = _WED
    pos = await _run(desk)
    assert pos is None
    assert executor.executed == []
