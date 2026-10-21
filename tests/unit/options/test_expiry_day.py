"""options_expiry_day (hypothesis F2): NIFTY expiry-day ATM iron butterfly inside SellerDesk,
plus per-strategy paper/live routing for desk-hosted strategies."""

from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from src.options import seller_desk as sd
from src.options.nfo_chain import ChainStrike, OptionChain
from src.options.seller_desk import EXPIRY_DAY_TYPE, OpenPosition, build_expiry_butterfly
from src.options.short_premium import SHORT_PREMIUM2_TYPE, SHORT_PREMIUM_TYPE
from src.options.structure_selector import OptionLeg

from tests.unit.options.hosted_helpers import _q, _RecordingCRM, _Repo
from tests.unit.options.test_seller_desk import _APPROVE_ALL_CRM, _FakeExecutor
from tests.unit.options.test_seller_desk import _desk as _seller_desk

_IST = ZoneInfo("Asia/Kolkata")
_EXP = date(2026, 10, 13)
_AT = datetime(2026, 10, 13, 10, 5, tzinfo=_IST)
_NAME = "options_expiry_day"
_CFG = {
    "enabled": True,
    "registry_name": _NAME,
    "underlying": "NIFTY",
    "entry_window": ["10:00", "10:20"],
    "wing_points": 150,
    "exit_by": "14:45",
    "profit_target_pct": 50,
    "stop_loss_credit_multiple": 2.0,
    "max_lots": 1,
    "skip_greeks_budget": True,
}


def _chain(expiry: date = _EXP, spot: float = 24010.0) -> OptionChain:
    rows = []
    for k in range(23500, 24501, 50):
        dist = (k - spot) / 50.0
        rows.append(
            ChainStrike(
                strike=float(k),
                call=_q(max(1.0, 60.0 - dist * 9.0)),
                put=_q(max(1.0, 60.0 + dist * 9.0)),
                call_iv=0.13,
                put_iv=0.13,
                call_delta=max(0.01, min(0.99, 0.5 - dist * 0.08)),
                put_delta=-max(0.01, min(0.99, 0.5 + dist * 0.08)),
                call_symbol=f"NIFTY{k}CE",
                put_symbol=f"NIFTY{k}PE",
            )
        )
    exp = datetime(expiry.year, expiry.month, expiry.day, 15, 30, tzinfo=_IST)
    return OptionChain(
        underlying="NIFTY", spot=spot, expiry=exp, days_to_expiry=0, lot_size=65, strikes=rows
    )


def _desk(chain, **kwargs):  # type: ignore[no-untyped-def]
    """Desk whose fetcher behaves like the real one on expiry day: fetch_tier1() hands back
    NEXT week's chain (Kite stamps expiry 00:00, so today's is skipped -- found live 2026-10-13)
    and only fetch_for_expiry(today) returns today's."""
    desk, executor = _seller_desk(chain, **kwargs)
    fetcher = desk._chain_fetcher
    fetcher._tier1_chain = _chain(expiry=date(2026, 10, 19))
    fetcher.expiry_chains[chain.expiry.date()] = chain
    return desk, executor


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sd._seller_settings, "expiry_day", lambda settings=None: dict(_CFG))


def _ctx(mode: str = "paper") -> SimpleNamespace:
    return SimpleNamespace(
        llm_gate_required={}, strategy_execution_mode={_NAME: mode}, mode_engine_open_positions={}
    )


async def _run(desk, *, today=_EXP, now=_AT, crm=None, ctx=None):  # type: ignore[no-untyped-def]
    return await desk.run_expiry_day_cycle(
        "NSE:NIFTY",
        today=today,
        now=now,
        available_margin=1e6,
        regime="UNKNOWN",
        vix=13.0,
        crm=crm or _APPROVE_ALL_CRM,
        crm_ctx=ctx or _ctx(),
    )


def test_butterfly_is_atm_with_fixed_wings() -> None:
    prot, inc = build_expiry_butterfly(_chain(), 1, wing_points=150)
    assert {(leg.instrument_type, leg.strike, leg.direction) for leg in inc} == {
        ("CE", 24000.0, "SELL"),
        ("PE", 24000.0, "SELL"),
    }
    assert {(leg.instrument_type, leg.strike, leg.direction) for leg in prot} == {
        ("CE", 24150.0, "BUY"),
        ("PE", 23850.0, "BUY"),
    }


def test_butterfly_refuses_a_missing_wing() -> None:
    chain = _chain()
    chain.strikes = [r for r in chain.strikes if r.strike != 24150.0]
    assert build_expiry_butterfly(chain, 1, wing_points=150) is None


async def test_opens_on_expiry_day_with_true_max_loss_and_own_name() -> None:
    desk, executor = _desk(_chain(), repo=_Repo())
    crm = _RecordingCRM()
    pos = await _run(desk, crm=crm)
    assert pos is not None and pos.strategy_type == EXPIRY_DAY_TYPE
    sig = crm.signals[0]
    assert sig.strategy_name == _NAME and sig.meta["registry_name"] == _NAME
    assert sig.risk_rupees == pytest.approx(150 * 65 - pos.entry_premium)
    assert desk._repo.inserted[0]["is_paper"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "today": date(2026, 10, 12),
            "now": datetime(2026, 10, 12, 10, 5, tzinfo=_IST),
        },  # not expiry
        {"now": datetime(2026, 10, 13, 9, 55, tzinfo=_IST)},  # before window
        {"now": datetime(2026, 10, 13, 10, 25, tzinfo=_IST)},  # after window
    ],
)
async def test_no_entry_off_expiry_or_outside_window(kwargs) -> None:  # type: ignore[no-untyped-def]
    desk, executor = _desk(_chain(), repo=_Repo())
    assert await _run(desk, **kwargs) is None
    assert executor.executed == []


async def test_greeks_budget_would_block_without_the_documented_exception(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        sd._seller_settings,
        "expiry_day",
        lambda settings=None: {**_CFG, "skip_greeks_budget": False},
    )
    # A days_to_expiry=0 chain yields ZERO Greeks in _per_lot_greeks (t<=0), so the budget
    # can only bite when the chain carries time; use 1 day to prove the gate is real.
    chain = _chain()
    chain.days_to_expiry = 1
    desk, executor = _desk(chain, repo=_Repo())
    assert await _run(desk) is None
    assert executor.executed == []


async def test_live_registry_mode_routes_to_the_live_executor() -> None:
    desk, paper_exec = _desk(_chain(), repo=_Repo())
    live_exec = _FakeExecutor()
    desk._alt_executor = live_exec  # desk is paper; alt = live
    pos = await _run(desk, ctx=_ctx("live"))
    assert pos is not None and pos.is_paper is False
    assert live_exec.executed and not paper_exec.executed
    assert desk._repo.inserted[0]["is_paper"] is False
    ctx = _ctx("live")
    desk._stamp_open_strategies(ctx)
    # The fly has its own slot (2026-10-09): never counted against the desk's G7 cap.
    assert ctx.mode_engine_open_positions == {"paper:options": 0, "live:options": 0}
    await desk._close_position(pos.position_id, reason="t")
    assert live_exec.closed and not paper_exec.closed


async def test_live_mode_without_a_live_executor_refuses() -> None:
    desk, executor = _desk(_chain(), repo=_Repo())
    assert desk._alt_executor is None
    assert await _run(desk, ctx=_ctx("live")) is None
    assert executor.executed == []


def _open(desk) -> OpenPosition:  # type: ignore[no-untyped-def]
    def leg(sym, kind, k, d, prem):  # type: ignore[no-untyped-def]
        return OptionLeg(
            tradingsymbol=sym,
            instrument_type=kind,
            strike=k,
            direction=d,
            lots=1,
            lot_size=65,
            premium=prem,
            delta=0.5,
        )

    prot = [leg("LP", "PE", 23850.0, "BUY", 10.0), leg("LC", "CE", 24150.0, "BUY", 10.0)]
    inc = [leg("SP", "PE", 24000.0, "SELL", 60.0), leg("SC", "CE", 24000.0, "SELL", 60.0)]
    pos = OpenPosition(
        position_id="OPT-F",
        underlying="NSE:NIFTY",
        strategy_type=EXPIRY_DAY_TYPE,
        direction="credit",
        protection_legs=prot,
        income_legs=inc,
        entry_premium=100.0 * 65,
        entry_margin=50.0 * 65,
        expiry=_EXP,
        entered_at=_AT,
    )
    desk.positions[pos.position_id] = pos
    return pos


async def test_guardian_lets_it_hold_until_exit_by_then_closes() -> None:
    desk, _ = _desk(_chain(), repo=_Repo())
    _open(desk)
    await desk.sweep_expiry_guardian(_EXP, datetime.combine(_EXP, time(11, 0), _IST))
    assert "OPT-F" in desk.positions
    await desk.sweep_expiry_guardian(_EXP, datetime.combine(_EXP, time(14, 46), _IST))
    assert "OPT-F" not in desk.positions


@pytest.mark.parametrize(
    ("prices", "closed", "prefix"),
    [
        ({"LP": 5.0, "LC": 5.0, "SP": 30.0, "SC": 30.0}, True, "EXPIRY_DAY_TP"),  # +50 pts >= 50
        ({"LP": 2.0, "LC": 90.0, "SP": 0.5, "SC": 400.0}, True, "EXPIRY_DAY_SL"),  # -208.5 <= -200
        ({"LP": 10.0, "LC": 10.0, "SP": 55.0, "SC": 55.0}, False, None),
    ],
)
async def test_take_profit_and_stop(prices, closed, prefix, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    desk, ex = _desk(_chain(), repo=_Repo())
    _open(desk)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return datetime(2026, 10, 13, 11, 0, tzinfo=_IST)

    monkeypatch.setattr(sd, "datetime", _DT)
    await desk.sweep_position_manager(
        today=_EXP, blackout_active=False, current_prices=prices, current_ivs={}, chains={}
    )
    if closed:
        assert desk._repo.closed[0]["exit_reason"].startswith(prefix)
    else:
        assert "OPT-F" in desk.positions
    assert ex.rolled == []


async def test_restore_keeps_other_mode_rows_for_hosted_strategies_and_every_live_row() -> None:
    # Phase 2 task 7: a LIVE desk-own row (B) is restored too, not dropped because the desk's
    # default mode is paper -- but ON HOLD (monitoring only, never auto-exited: the old filter
    # never audited such rows); paper rows keep the old rule.
    import json as _j

    def row(pid, stype, is_paper):  # type: ignore[no-untyped-def]
        return {
            "position_id": pid,
            "underlying": "NSE:NIFTY",
            "strategy_type": stype,
            "legs": _j.dumps({"protection": [], "income": []}),
            "expiry": _EXP,
            "net_credit": 100.0,
            "entry_margin": 50.0,
            "opened_at": _AT,
            "is_paper": is_paper,
        }

    repo = _Repo()
    repo._open_rows = [
        row("A", "bull_put_spread", True),
        row("B", "bull_put_spread", False),
        row("C", EXPIRY_DAY_TYPE, False),
    ]

    async def _realized(*a, **k):  # type: ignore[no-untyped-def]
        return 0.0

    repo.realized_pnl_closed_on = _realized
    desk, _ = _desk(_chain(), repo=repo)  # paper desk
    await desk.restore_positions()
    # C (hosted, live) is restored in its own mode too; its _EXP is already past, so it comes
    # back on hold as an expired live row (and this desk has no live executor anyway).
    assert set(desk.positions) == {"A"} and set(desk.held_positions) == {"B", "C"}
    assert desk.positions["A"].is_paper is None
    held = desk.held_positions["B"]
    assert held.is_paper is False and held.exec_broker == "kite"
    assert held.hold_reason == "restored_unaudited_live_row"
    assert desk.held_positions["C"].is_paper is False
    assert desk.held_positions["C"].hold_reason == "expired_live_row"


async def test_llm_not_called_when_a_deterministic_crm_gate_would_refuse() -> None:
    # 2026-10-06: G8 daily budget refused a spread every cycle AFTER the LLM had been asked.
    from src.risk.crm import CRMDecision

    class _BudgetBlocks:
        def precheck_without_llm(self, signal, ctx):  # type: ignore[no-untyped-def]
            return CRMDecision(
                signal_id=signal.signal_id,
                approved=False,
                final_quantity=0,
                gate_results=[],
                blocking_gate="G8",
                blocking_reason="budget",
            )

        def evaluate(self, signal, ctx):  # type: ignore[no-untyped-def]
            raise AssertionError("must not reach full evaluate")

    desk, executor = _desk(_chain(), repo=_Repo())
    called = []

    async def _no_llm(*a, **k):  # type: ignore[no-untyped-def]
        called.append(1)
        return True

    desk._g9_review = _no_llm  # type: ignore[method-assign]
    assert await _run(desk, crm=_BudgetBlocks()) is None
    assert called == [] and executor.executed == []


def test_crm_precheck_ignores_g9_and_writes_no_journal_row() -> None:
    from unittest.mock import patch

    from src.options.seller_crm_adapter import build_seller_crm_signal
    from src.risk.crm import CentralRiskManager

    from tests.unit.risk.test_crm_options_seller import _ctx as _cctx

    crm = CentralRiskManager()
    rows = []
    crm.decision_sink = lambda s, c, d: rows.append(d)
    sig = build_seller_crm_signal(
        signal_id="S",
        underlying="NSE:NIFTY",
        strategy_type="iron_condor",
        lots=1,
        max_loss=8000.0,
        max_profit=3000.0,
        regime="X",
        vix=15.0,
    )
    ctx = _cctx(llm_gate_required={"options_seller_desk_v3": True})
    with patch("src.risk.crm.datetime") as dt:
        dt.now.return_value = datetime(2026, 10, 7, 11, 0, tzinfo=_IST)
        assert crm.precheck_without_llm(sig, ctx).approved is True  # G9 not consulted yet
        assert crm.evaluate(sig, ctx).blocking_gate == "G9"  # real evaluate still requires it
    assert len(rows) == 1 and "_llm_precheck" not in sig.meta


async def test_strategy_halt_blocks_a_new_expiry_day_entry() -> None:
    desk, executor = _desk(_chain(), repo=_Repo())
    desk._strategy_halted_day[_NAME] = _EXP
    pos = await _run(desk)
    assert pos is None
    assert executor.executed == []


def _desk_spread(desk, pid: str, strategy_type: str = "bull_put_spread") -> None:  # type: ignore[no-untyped-def]
    desk.positions[pid] = OpenPosition(
        position_id=pid,
        underlying="NSE:NIFTY",
        strategy_type=strategy_type,
        direction="credit",
        protection_legs=[],
        income_legs=[],
        entry_premium=100.0,
        entry_margin=5000.0,
        expiry=date(2026, 10, 19),
        entered_at=_AT,
    )


async def test_opens_even_when_desk_already_fills_the_per_index_cap(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # 2026-10-08 live: 3 NIFTY structures (desk spreads + condor) were open, cap 3, and the
    # fly logged max_strategies_per_index -- it gets one try a week, so it has its own slot.
    monkeypatch.setattr(sd._seller_settings, "max_strategies_per_index", lambda settings=None: 3)
    desk, executor = _desk(_chain(), repo=_Repo())
    _desk_spread(desk, "OPT-D1")
    _desk_spread(desk, "OPT-D2", "bear_call_spread")
    _desk_spread(desk, "OPT-C", "iron_condor")
    pos = await _run(desk)
    assert pos is not None and pos.strategy_type == EXPIRY_DAY_TYPE
    assert len(executor.executed) == 1


def test_open_fly_is_not_counted_against_the_desks_g7_cap() -> None:
    desk, _ = _desk(_chain(), repo=_Repo())
    _open(desk)
    _desk_spread(desk, "OPT-D1")
    ctx = _ctx()
    desk._stamp_open_strategies(ctx)
    assert ctx.mode_engine_open_positions["paper:options"] == 1


async def test_reconciler_never_deletes_hosted_strategy_rows() -> None:
    desk, _ = _desk(_chain(), repo=_Repo())
    await desk.reconcile_duplicate_positions()
    assert set(desk._repo.dedupe_kwargs["exclude_strategy_types"]) == {
        EXPIRY_DAY_TYPE,
        SHORT_PREMIUM_TYPE,
        SHORT_PREMIUM2_TYPE,
    }


async def test_open_fly_does_not_take_a_desk_spread_slot(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    from tests.unit.options.test_seller_desk import _DUMMY_CTX, _TODAY, _rich_chain

    monkeypatch.setattr(sd._seller_settings, "max_strategies_per_index", lambda settings=None: 1)
    chain = _rich_chain()
    desk, _ = _desk(chain)
    _open(desk)
    with caplog.at_level("INFO", logger="options.seller_desk"):
        await desk.run_cycle(
            "NSE:NIFTY",
            regime="HIGH_VOL_RANGE_BOUND",
            ivr=65.0,
            term_structure="contango",
            today=_TODAY,
            now=datetime.now(_IST),
            expiry_calendar={"NIFTY": [chain.expiry.date()]},
            available_margin=1_000_000.0,
            crm=_APPROVE_ALL_CRM,
            crm_ctx=_DUMMY_CTX,
        )
    assert "seller_desk_already_open" not in caplog.text


async def test_opens_when_desk_spreads_already_fill_the_shared_risk_ceiling() -> None:
    # 2026-10-13 live: open Rs57,144 + fly Rs5,596 = Rs62,741 > Rs60,000 -> the fly was refused
    # by the desk's shared risk ceiling although it has its own slot.
    from src.risk import budget as budget_module

    budget_module._current_equity = 2_000_000.0  # ceiling = 3% = Rs 60,000
    try:
        desk, executor = _desk(_chain(), repo=_Repo())
        _desk_spread(desk, "OPT-D1")
        desk.positions["OPT-D1"].entry_margin = 59_000.0
        assert (
            desk.shared_risk_ceiling_reason(5_596.0) is not None
        )  # a desk spread would be refused
        pos = await _run(desk)
        assert pos is not None and pos.strategy_type == EXPIRY_DAY_TYPE
    finally:
        budget_module._current_equity = None


def test_open_fly_does_not_eat_the_shared_risk_headroom_of_desk_spreads() -> None:
    from src.risk import budget as budget_module

    budget_module._current_equity = 2_000_000.0
    try:
        desk, _ = _desk(_chain(), repo=_Repo())
        _desk_spread(desk, "OPT-D1")
        desk.positions["OPT-D1"].entry_margin = 20_000.0
        _desk_spread(desk, "OPT-FLY", EXPIRY_DAY_TYPE)
        desk.positions["OPT-FLY"].entry_margin = 30_000.0
        assert desk.shared_risk_ceiling_reason(39_000.0) is None  # 20k + 39k <= 60k; fly ignored
    finally:
        budget_module._current_equity = None


# ── R1: a LIVE strategy trades only through a usable broker route ─────────────


async def test_live_expiry_fly_with_an_unusable_route_places_nothing(caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    from src.brokers.routing import BrokerRoute

    desk, paper_exec = _desk(_chain(), repo=_Repo())
    live_exec = _FakeExecutor()
    desk._alt_executor = live_exec
    ctx = _ctx("live")
    ctx.strategy_routes = {_NAME: BrokerRoute(None, "live strategy has no 'broker' field")}
    with caplog.at_level(logging.WARNING, logger="options.seller_desk"):
        assert await _run(desk, ctx=ctx) is None
    assert live_exec.executed == [] and paper_exec.executed == []  # never demoted to paper
    assert f"broker_route_rejected strategy={_NAME} segment=options" in caplog.text


async def test_live_expiry_fly_with_a_usable_route_trades_as_today() -> None:
    from src.brokers.routing import BrokerRoute

    desk, paper_exec = _desk(_chain(), repo=_Repo())
    live_exec = _FakeExecutor()
    desk._alt_executor = live_exec
    ctx = _ctx("live")
    ctx.strategy_routes = {_NAME: BrokerRoute("kite", None)}
    pos = await _run(desk, ctx=ctx)
    assert pos is not None and pos.is_paper is False
    assert live_exec.executed and not paper_exec.executed
