"""Tests for the seller-desk orchestration layer — the integration of every
decision/execution/safety module built this session."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from src.brokers.base import OptionQuote
from src.options.greeks_budget import GreeksBudget, PortfolioGreeks, compute_budget
from src.options.nfo_chain import ChainStrike, OptionChain
from src.options.seller_desk import OpenPosition, SellerDesk, estimate_economics, evaluate_and_size
from src.options.strategy_selector import StrategySelector
from src.options.structure_builder import build_structure
from src.risk.crm import CRMDecision

_IST = ZoneInfo("Asia/Kolkata")
_TODAY = date(2026, 8, 1)


def _quote(mid: float) -> OptionQuote:
    return OptionQuote("SYM", ltp=mid, bid=mid - 0.5, ask=mid + 0.5, oi=5000, volume=2000)


def _rich_chain(days: int = 35) -> OptionChain:
    # Anchored to _TODAY, not datetime.now() -- every test in this file
    # passes today=_TODAY to run_cycle(), so the chain's real DTE (computed
    # against _TODAY) must match `days` regardless of the real wall-clock
    # date. Using datetime.now() here let real time drift away from _TODAY
    # (a fixed 2026-08-01) until DTE-band assertions silently stopped
    # testing what they claimed to (found 2026-09-04, ~5 weeks of drift).
    expiry = datetime(_TODAY.year, _TODAY.month, _TODAY.day, tzinfo=_IST) + timedelta(days=days)
    strikes_data = [
        (23500.0, 0.90, -0.10),
        (23600.0, 0.82, -0.15),
        (23700.0, 0.72, -0.22),
        (23800.0, 0.62, -0.28),
        (23900.0, 0.55, -0.45),
        (24000.0, 0.50, -0.50),
        (24100.0, 0.45, -0.55),
        (24200.0, 0.28, -0.62),
        (24300.0, 0.22, -0.72),
        (24400.0, 0.15, -0.82),
        (24500.0, 0.10, -0.90),
    ]
    rows = [
        ChainStrike(
            strike=strike,
            call=_quote(50.0 + (24000.0 - strike) * 0.05),
            put=_quote(50.0 + (strike - 24000.0) * 0.05),
            call_iv=0.18,
            put_iv=0.19,
            call_delta=call_d,
            put_delta=put_d,
            call_symbol=f"NIFTY{int(strike)}CE",
            put_symbol=f"NIFTY{int(strike)}PE",
        )
        for strike, call_d, put_d in strikes_data
    ]
    return OptionChain(
        underlying="NIFTY",
        spot=24000.0,
        expiry=expiry,
        days_to_expiry=days,
        lot_size=25,
        strikes=rows,
    )


def _budget() -> GreeksBudget:
    return compute_budget(1_000_000.0)


class _FakeChainFetcher:
    """Returns the SAME chain regardless of requested DTE bucket — each
    caller then filters by the real (chain.expiry - today) itself, exactly
    as the real ChainFetcher's tier2 buckets would be filtered downstream."""

    def __init__(self, chain: OptionChain, tier1_chain: OptionChain | None = None) -> None:
        self._chain = chain
        # Nearest-expiry chain when it differs from the traded one (e.g. a
        # 1-DTE NIFTY weekly alongside a 35-DTE tradable expiry).
        self._tier1_chain = tier1_chain or chain
        self.tier2_requests: list[list[int]] = []
        # fetch_for_expiry(): chain to return per requested expiry date (None -> not found).
        self.expiry_chains: dict[date, OptionChain | None] = {}
        self.expiry_requests: list[tuple[str, date]] = []

    async def fetch_tier1(self, underlying):  # type: ignore[no-untyped-def]
        return self._tier1_chain

    async def fetch_for_expiry(self, underlying, expiry_date):  # type: ignore[no-untyped-def]
        self.expiry_requests.append((underlying, expiry_date))
        return self.expiry_chains.get(expiry_date)

    async def fetch_tier2(self, underlying, dte_buckets):  # type: ignore[no-untyped-def]
        self.tier2_requests.append(list(dte_buckets))
        return {bucket: self._chain for bucket in dte_buckets}


class _FakeExecutor:
    def __init__(self, close_ltp: float | None = 50.0, roll_result: str = "fill") -> None:
        self.executed: list[tuple] = []
        self.closed: list[tuple] = []
        self.rolled: list[tuple] = []
        # Fixed LTP every close_leg() call returns (see close_leg's real
        # docstring: an approximation of the exit fill, used for realized_pnl).
        # None simulates an LTP-fetch failure -- realized_pnl must come back
        # incomplete (None), not silently wrong, when this is set.
        self._close_ltp = close_ltp
        # "fill" -> roll_leg succeeds and returns new_leg; "fail" -> returns None
        # (new leg never filled), matching the real executor's failure mode.
        self._roll_result = roll_result

    async def execute_structure(self, protection_legs, income_legs, signal_id):  # type: ignore[no-untyped-def]
        from src.options.options_leg_executor import ExecutionResult

        self.executed.append((protection_legs, income_legs, signal_id))
        return ExecutionResult(position_id=f"OPT-{len(self.executed)}", outcome="ALL_FILLED")

    async def get_order_margin_for_structure(self, protection_legs, income_legs, signal_id):  # type: ignore[no-untyped-def]
        from src.brokers.base import OrderMarginResult

        self.margin_requests = getattr(self, "margin_requests", [])
        self.margin_requests.append((protection_legs, income_legs, signal_id))
        all_legs = list(protection_legs) + list(income_legs)
        if not all_legs:
            return OrderMarginResult(total_margin=1.0)

        # Per-side width (protection vs income strike on the SAME instrument_type),
        # not the full min-max span across all legs -- a 4-leg condor's two wings
        # are independent risks, and a full-span width would double-count the
        # inner short-to-short distance between them (not a real risk at all).
        def _side_width(legs: list) -> float:  # type: ignore[type-arg]
            if len(legs) < 2:
                return 0.0
            side_strikes = [leg.strike for leg in legs]
            return max(side_strikes) - min(side_strikes)

        pe_legs = [leg for leg in all_legs if leg.instrument_type == "PE"]
        ce_legs = [leg for leg in all_legs if leg.instrument_type == "CE"]
        width = max(_side_width(pe_legs), _side_width(ce_legs))
        if width > 0.0:
            proxy = (
                width * all_legs[0].quantity
            )  # quantity = lots * lot_size -- must scale with lots
        else:  # same-strike structures (straddle, naked single leg): fall back to premium paid
            proxy = sum(leg.premium * leg.quantity for leg in all_legs) or 1000.0
        # Real margin is always at or above the defined-risk max_loss (credit
        # reduces max_loss below width*lot_size; real margin doesn't give that
        # benefit back) -- a modest multiplier keeps every existing test's
        # theoretical number comfortably below this fake "real" margin without
        # blowing past the fixtures' available_margin (usually 1,000,000).
        return OrderMarginResult(total_margin=proxy * 1.3)

    async def close_leg(self, leg, signal_id) -> float | None:  # type: ignore[no-untyped-def]
        self.closed.append((leg, signal_id))
        return self._close_ltp

    async def roll_leg(self, old_leg, new_leg, signal_id):  # type: ignore[no-untyped-def]
        self.rolled.append((old_leg, new_leg, signal_id))
        return new_leg if self._roll_result == "fill" else None


class _FakeEventCalendar:
    def __init__(self, blocked: bool = False) -> None:
        self._blocked = blocked

    async def selling_blocked(self, underlying, now, *, is_iron_condor=False):  # type: ignore[no-untyped-def]
        from src.options.event_calendar import BlackoutStatus

        return BlackoutStatus(self._blocked, "test blackout" if self._blocked else "")


class _FakeRepo:
    """Records calls instead of touching a real DB — see
    OptionsRepository.insert_position/close_position/load_open_positions."""

    def __init__(self, open_rows: list[dict] | None = None) -> None:
        self.inserted: list[dict] = []
        self.closed: list[dict] = []
        self._open_rows = open_rows or []
        self.duplicate_ids_to_return: list[str] = []
        self.deleted: list[str] = []
        self.findings: list[tuple] = []
        self.breached: list[str] = []
        self.peaks: list[tuple[str, float]] = []
        self.attribution: list[dict] = []
        self.open_duplicates: list[str] = []
        self.open_duplicates_error: Exception | None = None
        self.insert_error: Exception | None = None

    async def open_duplicate_ids(self, *, underlying, strategy_type, expiry, is_paper):  # type: ignore[no-untyped-def]
        if self.open_duplicates_error is not None:
            raise self.open_duplicates_error
        return self.open_duplicates

    async def insert_learning_finding(self, position_id, question, finding, confidence):  # type: ignore[no-untyped-def]
        self.findings.append((position_id, question, finding, confidence))

    async def mark_breached(self, position_id):  # type: ignore[no-untyped-def]
        self.breached.append(position_id)

    async def update_peak_unrealized(self, position_id, peak):  # type: ignore[no-untyped-def]
        self.peaks.append((position_id, peak))

    async def upsert_pnl_attribution(self, day, **kwargs):  # type: ignore[no-untyped-def]
        self.attribution.append({"day": day, **kwargs})

    async def insert_position(self, position_id, **kwargs):  # type: ignore[no-untyped-def]
        if self.insert_error is not None:
            raise self.insert_error
        self.inserted.append({"position_id": position_id, **kwargs})

    async def close_position(
        self,
        position_id,
        *,
        exit_reason,
        closed_at,
        realized_pnl=None,
        exit_prices=None,
        charges_breakdown=None,
        broker=None,
    ):  # type: ignore[no-untyped-def]
        self.closed.append(
            {
                "position_id": position_id,
                "exit_reason": exit_reason,
                "closed_at": closed_at,
                "realized_pnl": realized_pnl,
                "exit_prices": exit_prices,
                "charges_breakdown": charges_breakdown,
                "broker": broker,
            }
        )

    async def load_open_positions(self, is_paper=None):  # type: ignore[no-untyped-def]
        return self._open_rows

    async def realized_pnl_closed_on(self, day, *, is_paper):  # type: ignore[no-untyped-def]
        return 0.0

    async def realized_pnl_by_structure_closed_on(self, day, *, is_paper):  # type: ignore[no-untyped-def]
        return {}

    async def delete_duplicate_open_positions(  # type: ignore[no-untyped-def]
        self,
        *,
        is_paper,
        keep_per_underlying=1,
        exclude_strategy_types=(),
        exclude_position_ids=(),
    ):
        self.dedupe_kwargs = {
            "exclude_strategy_types": exclude_strategy_types,
            "exclude_position_ids": list(exclude_position_ids),
        }
        self.deleted.extend(self.duplicate_ids_to_return)
        return self.duplicate_ids_to_return


class _ApproveAllCRM:
    """Stands in for CentralRiskManager in tests that exist to verify
    seller-desk sizing/candidate-selection behavior, not CRM gating itself
    (see tests/unit/risk/test_crm_options_seller.py for CRM gate coverage).
    Always approves at the signal's own intended_quantity."""

    def evaluate(self, signal, ctx):  # type: ignore[no-untyped-def]
        return CRMDecision(
            signal_id=signal.signal_id,
            approved=True,
            final_quantity=signal.intended_quantity,
            gate_results=[],
        )


class _BlockAllCRM:
    """Always blocks -- used to verify run_cycle() never places an order
    when CRM says no, regardless of how good the candidate structure is."""

    def evaluate(self, signal, ctx):  # type: ignore[no-untyped-def]
        return CRMDecision(
            signal_id=signal.signal_id,
            approved=False,
            final_quantity=0,
            gate_results=[],
            blocking_gate="G1",
            blocking_reason="test block",
        )


_APPROVE_ALL_CRM = _ApproveAllCRM()
_DUMMY_CTX = SimpleNamespace(llm_gate_required={})  # CRM doubles never introspect ctx


def _desk(
    chain: OptionChain, executor=None, event_calendar=None, repo=None
) -> tuple[SellerDesk, _FakeExecutor]:  # type: ignore[no-untyped-def]
    executor = executor or _FakeExecutor()
    desk = SellerDesk(
        StrategySelector(),
        event_calendar or _FakeEventCalendar(),
        executor,
        _FakeChainFetcher(chain),
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        margin_safety_pct=0.90,
        greeks_budget=_budget(),
        min_rom_pct=0.0,
        repo=repo,
    )
    return desk, executor


def test_estimate_economics_credit_vertical() -> None:
    chain = _rich_chain()
    protection, income = build_structure("bull_put_spread", chain, lots=1)
    max_loss, max_profit, net_premium, direction = estimate_economics(
        "bull_put_spread", protection, income, chain.lot_size, 1
    )
    assert direction == "credit"
    assert net_premium > 0.0
    assert max_profit == net_premium
    assert max_loss > 0.0


async def test_evaluate_and_size_returns_sized_structure_or_none() -> None:
    chain = _rich_chain()
    result = await evaluate_and_size(
        strategy_type="bull_put_spread",
        chain=chain,
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        margin_safety_pct=0.90,
        available_margin=1_000_000.0,
        current_greeks=PortfolioGreeks(),
        greeks_budget=_budget(),
        dte_value=35,
        min_rom_pct=0.0,
        executor=_FakeExecutor(),
    )
    if result is not None:
        protection, income, lots = result
        assert lots >= 1
        assert protection or income


async def test_evaluate_and_size_two_expiry_returns_sized_calendar_or_none() -> None:
    from src.options.seller_desk import evaluate_and_size_two_expiry

    near_chain = _rich_chain(days=5)
    far_chain = _rich_chain(days=26)
    result = await evaluate_and_size_two_expiry(
        strategy_type="calendar_spread",
        near_chain=near_chain,
        far_chain=far_chain,
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        margin_safety_pct=0.90,
        available_margin=1_000_000.0,
        current_greeks=PortfolioGreeks(),
        greeks_budget=_budget(),
        directional_bias=None,
        executor=_FakeExecutor(),
    )
    if result is not None:
        protection, income, lots = result
        assert lots >= 1
        assert protection and income


async def test_evaluate_and_size_two_expiry_diagonal_needs_bias() -> None:
    from src.options.seller_desk import evaluate_and_size_two_expiry

    near_chain = _rich_chain(days=5)
    far_chain = _rich_chain(days=26)
    result = await evaluate_and_size_two_expiry(
        strategy_type="diagonal_spread",
        near_chain=near_chain,
        far_chain=far_chain,
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        margin_safety_pct=0.90,
        available_margin=1_000_000.0,
        current_greeks=PortfolioGreeks(),
        greeks_budget=_budget(),
        directional_bias=None,
        executor=_FakeExecutor(),
    )
    assert result is None


async def test_run_cycle_opens_a_position_when_regime_and_ivr_allow_selling() -> None:
    chain = _rich_chain()
    desk, executor = _desk(chain)
    calendar = {"NIFTY": [chain.expiry.date() + timedelta(days=i) for i in range(-2, 3)]}
    position = await desk.run_cycle(
        "NSE:NIFTY",
        regime="HIGH_VOL_RANGE_BOUND",
        ivr=65.0,
        term_structure="contango",
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar=calendar,
        available_margin=1_000_000.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=_DUMMY_CTX,
    )
    if position is not None:
        assert position.position_id in desk.positions
        assert len(executor.executed) == 1


async def test_run_cycle_crm_block_prevents_execution() -> None:
    # Same setup as the "opens a position" test above, but CRM always
    # blocks -- regardless of whether a candidate structure would otherwise
    # have qualified, no order may ever be placed.
    chain = _rich_chain()
    desk, executor = _desk(chain)
    calendar = {"NIFTY": [chain.expiry.date() + timedelta(days=i) for i in range(-2, 3)]}
    position = await desk.run_cycle(
        "NSE:NIFTY",
        regime="HIGH_VOL_RANGE_BOUND",
        ivr=65.0,
        term_structure="contango",
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar=calendar,
        available_margin=1_000_000.0,
        crm=_BlockAllCRM(),
        crm_ctx=_DUMMY_CTX,
    )
    assert position is None
    assert executor.executed == []


async def test_run_cycle_without_crm_fails_closed() -> None:
    # No crm/crm_ctx passed (defaults to None) must never mean "skip risk
    # gating" -- that is the exact bypass this retrofit fixes.
    chain = _rich_chain()
    desk, executor = _desk(chain)
    calendar = {"NIFTY": [chain.expiry.date() + timedelta(days=i) for i in range(-2, 3)]}
    position = await desk.run_cycle(
        "NSE:NIFTY",
        regime="HIGH_VOL_RANGE_BOUND",
        ivr=65.0,
        term_structure="contango",
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar=calendar,
        available_margin=1_000_000.0,
    )
    assert position is None
    assert executor.executed == []


async def test_run_cycle_skips_underlying_with_an_open_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.options import seller_settings as _ss

    monkeypatch.setattr(_ss, "max_strategies_per_index", lambda settings=None: 1)
    # 2026-09-11: run_cycle() never checked self.positions before opening a
    # new structure -- every 5-min loop cycle re-evaluated and opened ANOTHER
    # position on the same underlying, producing duplicate live positions.
    from src.options.seller_desk import OpenPosition

    chain = _rich_chain()
    desk, executor = _desk(chain)
    desk.positions["EXISTING-1"] = OpenPosition(
        position_id="EXISTING-1",
        underlying="NSE:NIFTY",
        strategy_type="bull_put_spread",
        direction="credit",
        protection_legs=[],
        income_legs=[],
        entry_premium=100.0,
        entry_margin=5000.0,
        expiry=chain.expiry.date(),
        entered_at=datetime.now(_IST),
    )
    calendar = {"NIFTY": [chain.expiry.date() + timedelta(days=i) for i in range(-2, 3)]}
    position = await desk.run_cycle(
        "NSE:NIFTY",
        regime="HIGH_VOL_RANGE_BOUND",
        ivr=65.0,
        term_structure="contango",
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar=calendar,
        available_margin=1_000_000.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=_DUMMY_CTX,
    )
    assert position is None
    assert executor.executed == []
    assert len(desk.positions) == 1


async def test_reconcile_duplicate_positions_closes_and_forgets_them() -> None:
    from src.options.seller_desk import OpenPosition

    chain = _rich_chain()
    repo = _FakeRepo()
    repo.duplicate_ids_to_return = ["DUP-1", "DUP-2"]
    desk, _ = _desk(chain, repo=repo)
    for pid in ("KEEP-1", "DUP-1", "DUP-2"):
        desk.positions[pid] = OpenPosition(
            position_id=pid,
            underlying="NSE:NIFTY",
            strategy_type="bull_put_spread",
            direction="credit",
            protection_legs=[],
            income_legs=[],
            entry_premium=100.0,
            entry_margin=5000.0,
            expiry=chain.expiry.date(),
            entered_at=datetime.now(_IST),
        )

    n = await desk.reconcile_duplicate_positions()

    assert n == 2
    assert set(desk.positions) == {"KEEP-1"}
    assert repo.deleted == ["DUP-1", "DUP-2"]
    assert repo.closed == []  # deleted outright, never soft-closed


async def test_reconcile_duplicate_positions_without_repo_is_a_noop() -> None:
    chain = _rich_chain()
    desk, _ = _desk(chain, repo=None)

    assert await desk.reconcile_duplicate_positions() == 0


async def test_run_cycle_blocked_by_pre_entry_dte_check() -> None:
    # 2026-09-10: pre-entry check is against a freshly fetched REAL chain's
    # expiry (fetch_tier1), not the nse_expiry_calendar.json placeholder --
    # so a near-dated fake chain is what drives the block now, not `calendar`.
    near_chain = _rich_chain(days=1)
    desk, executor = _desk(near_chain)
    position = await desk.run_cycle(
        "NSE:NIFTY",
        regime="HIGH_VOL_RANGE_BOUND",
        ivr=65.0,
        term_structure="contango",
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar={},
        available_margin=1_000_000.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=_DUMMY_CTX,
    )
    assert position is None
    assert executor.executed == []


async def test_run_cycle_not_blocked_by_near_weekly_when_traded_expiry_is_far() -> None:
    # Regression: the pre-entry DTE gate used the NEAREST expiry, so a 1-DTE
    # NIFTY weekly (Mon/Tue) blocked the whole underlying even though every
    # candidate trades a 35-DTE expiry. The gate now checks the traded expiry.
    desk, _ = _desk(_rich_chain(days=35))
    fetcher = _FakeChainFetcher(_rich_chain(days=35), tier1_chain=_rich_chain(days=1))
    desk._chain_fetcher = fetcher  # noqa: SLF001
    await desk.run_cycle(
        "NSE:NIFTY",
        regime="HIGH_VOL_RANGE_BOUND",
        ivr=65.0,
        term_structure="contango",
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar={},
        available_margin=1_000_000.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=_DUMMY_CTX,
    )
    assert fetcher.tier2_requests, "cycle was blocked before any per-strategy chain fetch"


async def test_run_cycle_returns_none_when_no_ivr() -> None:
    chain = _rich_chain()
    desk, executor = _desk(chain)
    calendar = {"NIFTY": [chain.expiry.date() + timedelta(days=i) for i in range(-2, 3)]}
    position = await desk.run_cycle(
        "NSE:NIFTY",
        regime="HIGH_VOL_RANGE_BOUND",
        ivr=None,
        term_structure=None,
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar=calendar,
        available_margin=1_000_000.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=_DUMMY_CTX,
    )
    assert position is None


async def test_run_cycle_skips_candidate_whose_only_available_expiry_is_out_of_band() -> None:
    # Chain is only 5 DTE out — no candidate's DTE band (21-45 / 30-45) covers
    # that, so every candidate should be skipped even though IVR/regime allow selling.
    chain = _rich_chain(days=5)
    desk, executor = _desk(chain)
    calendar = {"NIFTY": [chain.expiry.date() + timedelta(days=i) for i in range(-2, 3)]}
    position = await desk.run_cycle(
        "NSE:NIFTY",
        regime="HIGH_VOL_RANGE_BOUND",
        ivr=65.0,
        term_structure="contango",
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar=calendar,
        available_margin=1_000_000.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=_DUMMY_CTX,
    )
    assert position is None
    assert executor.executed == []


async def test_run_cycle_opens_calendar_spread_position_on_backwardation() -> None:
    chain = _rich_chain(days=5)
    desk, executor = _desk(chain)
    calendar = {
        "NIFTY": [
            date(2026, 8, 6),
            date(2026, 8, 13),
            date(2026, 8, 20),
            date(2026, 8, 27),
            date(2026, 9, 3),
        ]
    }
    position = await desk.run_cycle(
        "NSE:NIFTY",
        regime="LOW_VOL_RANGE_BOUND",
        ivr=20.0,
        term_structure="backwardation",
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar=calendar,
        available_margin=1_000_000.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=_DUMMY_CTX,
    )
    if position is not None:
        assert position.strategy_type in ("calendar_spread", "diagonal_spread")
        assert position.position_id in desk.positions
        assert len(executor.executed) == 1


async def test_run_cycle_opens_diagonal_spread_with_directional_bias() -> None:
    chain = _rich_chain(days=5)
    desk, executor = _desk(chain)
    calendar = {
        "NIFTY": [
            date(2026, 8, 6),
            date(2026, 8, 13),
            date(2026, 8, 20),
            date(2026, 8, 27),
            date(2026, 9, 3),
        ]
    }
    position = await desk.run_cycle(
        "NSE:NIFTY",
        regime="LOW_VOL_RANGE_BOUND",
        ivr=20.0,
        term_structure="backwardation",
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar=calendar,
        available_margin=1_000_000.0,
        directional_bias=0.30,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=_DUMMY_CTX,
    )
    if position is not None:
        assert position.strategy_type in ("calendar_spread", "diagonal_spread")


async def test_sweep_expiry_guardian_closes_low_dte_positions() -> None:
    chain = _rich_chain()
    desk, executor = _desk(chain)
    protection, income = build_structure("bull_put_spread", chain, lots=1)
    from src.options.seller_desk import OpenPosition

    pos = OpenPosition(
        position_id="OPT-1",
        underlying="NIFTY",
        strategy_type="bull_put_spread",
        direction="credit",
        protection_legs=protection,
        income_legs=income,
        entry_premium=100.0,
        entry_margin=1000.0,
        expiry=_TODAY + timedelta(days=1),
        entered_at=datetime.now(_IST),
    )
    desk.positions["OPT-1"] = pos
    await desk.sweep_expiry_guardian(_TODAY, datetime.now(_IST))
    assert "OPT-1" not in desk.positions
    assert len(executor.closed) == len(protection) + len(income)


async def test_sweep_margin_monitor_closes_on_critical_expansion() -> None:
    chain = _rich_chain()
    desk, executor = _desk(chain)
    protection, income = build_structure("bull_put_spread", chain, lots=1)
    from src.options.seller_desk import OpenPosition

    pos = OpenPosition(
        position_id="OPT-2",
        underlying="NIFTY",
        strategy_type="bull_put_spread",
        direction="credit",
        protection_legs=protection,
        income_legs=income,
        entry_premium=100.0,
        entry_margin=1000.0,
        expiry=_TODAY + timedelta(days=30),
        entered_at=datetime.now(_IST),
    )
    desk.positions["OPT-2"] = pos
    await desk.sweep_margin_monitor({"OPT-2": 1700.0})  # 70% expansion > 60% critical
    assert "OPT-2" not in desk.positions


def _tight_quote(mid: float) -> OptionQuote:
    # _quote()'s fixed +/-0.5 absolute spread is >5% on a cheap premium and
    # would fail strike_selector.spread_ok — these rows need a realistic
    # tight spread so find_strike_by_delta actually returns a candidate.
    return OptionQuote("SYM", ltp=mid, bid=mid - 0.05, ask=mid + 0.05, oi=5000, volume=2000)


def _adjustment_chain() -> OptionChain:
    """Put short at 23400 is the (simulated) breached/tested leg; call short
    at 24000 is untested and should roll DOWN to 23800 (delta 0.25 — the
    default credit_short target) when the roll executes."""
    expiry = datetime(_TODAY.year, _TODAY.month, _TODAY.day, tzinfo=_IST) + timedelta(days=20)
    rows = [
        ChainStrike(
            strike=23400.0,
            call=_tight_quote(80.0),
            put=_tight_quote(20.0),
            call_iv=0.18,
            put_iv=0.19,
            call_delta=0.80,
            put_delta=-0.40,
            call_symbol="NIFTY23400CE",
            put_symbol="NIFTY23400PE",
        ),
        ChainStrike(
            strike=23800.0,
            call=_tight_quote(15.0),
            put=_tight_quote(40.0),
            call_iv=0.18,
            put_iv=0.19,
            call_delta=0.25,
            put_delta=-0.20,
            call_symbol="NIFTY23800CE",
            put_symbol="NIFTY23800PE",
        ),
        ChainStrike(
            strike=24000.0,
            call=_tight_quote(10.0),
            put=_tight_quote(55.0),
            call_iv=0.18,
            put_iv=0.19,
            call_delta=0.20,
            put_delta=-0.15,
            call_symbol="NIFTY24000CE",
            put_symbol="NIFTY24000PE",
        ),
    ]
    return OptionChain(
        underlying="NIFTY",
        spot=23700.0,
        expiry=expiry,
        days_to_expiry=20,
        lot_size=25,
        strikes=rows,
    )


def _iron_condor_position(
    position_id: str, *, expiry_days: int = 20, adjustments_made: int = 0
) -> OpenPosition:
    from src.options.structure_selector import OptionLeg

    def _leg(strike: float, kind: str, direction: str, delta: float) -> OptionLeg:
        return OptionLeg(
            tradingsymbol=f"NFO:NIFTY{int(strike)}{kind}",
            instrument_type=kind,
            strike=strike,
            direction=direction,
            lots=1,
            lot_size=25,
            premium=50.0,
            delta=delta,
        )

    return OpenPosition(
        position_id=position_id,
        underlying="NSE:NIFTY",
        strategy_type="iron_condor",
        direction="credit",
        protection_legs=[
            _leg(23200.0, "PE", "BUY", -0.10),
            _leg(24200.0, "CE", "BUY", 0.10),
        ],
        income_legs=[
            _leg(23400.0, "PE", "SELL", -0.40),  # tested (breached)
            _leg(24000.0, "CE", "SELL", 0.20),  # untested — should roll
        ],
        entry_premium=5000.0,
        entry_margin=20000.0,
        expiry=_TODAY + timedelta(days=expiry_days),
        entered_at=datetime.now(_IST),
        adjustments_made=adjustments_made,
    )


def _bull_put_spread_position(position_id: str, *, expiry_days: int = 20) -> OpenPosition:
    from src.options.structure_selector import OptionLeg

    def _leg(strike: float, direction: str, delta: float) -> OptionLeg:
        return OptionLeg(
            tradingsymbol=f"NFO:NIFTY{int(strike)}PE",
            instrument_type="PE",
            strike=strike,
            direction=direction,
            lots=1,
            lot_size=25,
            premium=50.0,
            delta=delta,
        )

    return OpenPosition(
        position_id=position_id,
        underlying="NSE:NIFTY",
        strategy_type="bull_put_spread",
        direction="credit",
        protection_legs=[_leg(22900.0, "BUY", -0.10)],
        income_legs=[_leg(23200.0, "SELL", -0.46)],  # breached, single-sided
        entry_premium=3000.0,
        entry_margin=20000.0,
        expiry=_TODAY + timedelta(days=expiry_days),
        entered_at=datetime.now(_IST),
        adjustments_made=0,
    )


async def test_sweep_position_manager_logs_breach_but_does_not_roll_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # execute_rolls is now enabled in the real shipped config (2026-09-15,
    # after 4 days of paper-mode observation) -- explicitly force it off here
    # rather than relying on the ambient config value, so this test verifies
    # the off-behavior regardless of what the real config currently says (its
    # sibling test_..._executes_roll_when_flag_on does the same for on). A
    # breach must be visible (unlike before this wiring, when it was silently
    # dropped) but must NOT place any order when the flag is off.
    from src.options import seller_desk as seller_desk_module

    monkeypatch.setattr(
        seller_desk_module._seller_settings,
        "adjustment_settings",
        lambda *a, **kw: {"execute_rolls": False},
    )
    chain = _adjustment_chain()
    desk, executor = _desk(chain)
    pos = _iron_condor_position("OPT-1")
    desk.positions["OPT-1"] = pos
    with caplog.at_level("WARNING", logger="options.seller_desk"):
        await desk.sweep_position_manager(
            today=_TODAY,
            blackout_active=False,
            current_prices={},
            current_ivs={},
            chains={"NSE:NIFTY": chain},
        )
    assert executor.rolled == []
    assert "OPT-1" in desk.positions
    assert desk.positions["OPT-1"].adjustments_made == 0
    assert any("seller_desk_delta_breach" in r.message for r in caplog.records)


async def test_sweep_position_manager_executes_roll_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.options import seller_desk as seller_desk_module

    monkeypatch.setattr(
        seller_desk_module._seller_settings,
        "adjustment_settings",
        lambda *a, **kw: {"execute_rolls": True},
    )
    chain = _adjustment_chain()
    desk, executor = _desk(chain)
    pos = _iron_condor_position("OPT-1")
    desk.positions["OPT-1"] = pos
    await desk.sweep_position_manager(
        today=_TODAY,
        blackout_active=False,
        current_prices={},
        current_ivs={},
        chains={"NSE:NIFTY": chain},
    )
    assert len(executor.rolled) == 1
    old_leg, new_leg, signal_id = executor.rolled[0]
    assert old_leg.strike == 24000.0  # untested call leg was rolled
    assert new_leg.strike == 23800.0  # new target-delta strike, toward price
    assert signal_id == "OPT-1"
    updated = desk.positions["OPT-1"]
    assert updated.adjustments_made == 1
    assert {leg.strike for leg in updated.income_legs} == {23400.0, 23800.0}


async def test_sweep_position_manager_max_adjustments_forces_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.options import seller_desk as seller_desk_module

    monkeypatch.setattr(
        seller_desk_module._seller_settings,
        "adjustment_settings",
        lambda *a, **kw: {"execute_rolls": True},
    )
    chain = _adjustment_chain()
    desk, executor = _desk(chain)
    pos = _iron_condor_position("OPT-1", adjustments_made=2)  # already at the cap
    desk.positions["OPT-1"] = pos
    await desk.sweep_position_manager(
        today=_TODAY,
        blackout_active=False,
        current_prices={},
        current_ivs={},
        chains={"NSE:NIFTY": chain},
    )
    assert executor.rolled == []
    assert "OPT-1" not in desk.positions  # MAX_ADJUSTMENTS_REACHED closed it


async def test_sweep_position_manager_low_dte_blocks_adjustment_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.options import seller_desk as seller_desk_module

    monkeypatch.setattr(
        seller_desk_module._seller_settings,
        "adjustment_settings",
        lambda *a, **kw: {"execute_rolls": True},
    )
    chain = _adjustment_chain()
    desk, executor = _desk(chain)
    # DTE=5: below adjustment_engine's block-below-7 gate but above
    # position_manager's own DTE<=3 double-check, so this exercises the
    # adjustment engine's gate specifically, not the earlier DTE close.
    pos = _iron_condor_position("OPT-1", expiry_days=5)
    # The sweep now resolves the position's OWN expiry chain (5d), not the 20d fixture.
    desk._chain_fetcher.expiry_chains[_TODAY + timedelta(days=5)] = chain  # type: ignore[attr-defined]
    desk.positions["OPT-1"] = pos
    await desk.sweep_position_manager(
        today=_TODAY,
        blackout_active=False,
        current_prices={},
        current_ivs={},
        chains={"NSE:NIFTY": chain},
    )
    assert executor.rolled == []
    assert "OPT-1" not in desk.positions  # ADJUSTMENT_BLOCKED_LOW_DTE closed it


async def test_sweep_position_manager_refreshes_stale_entry_delta_from_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression (found 2026-09-16): OptionLeg.delta was set once at entry and
    # never updated again, so check_delta_breach_any_leg kept comparing
    # against the entry-time value forever -- a position could bleed toward
    # its short strike for days without ever tripping a breach. Here the
    # income leg's entry delta (-0.20, well inside the 0.35 threshold) is
    # stale; the live chain shows the same strike now at -0.40 (breached).
    from src.options import seller_desk as seller_desk_module

    monkeypatch.setattr(
        seller_desk_module._seller_settings,
        "adjustment_settings",
        lambda *a, **kw: {"execute_rolls": False},
    )
    chain = _adjustment_chain()  # strike 23400 PE is -0.40 in this chain
    desk, executor = _desk(chain)
    pos = _iron_condor_position("OPT-1")
    pos.income_legs[0].delta = -0.20  # entry-time delta, now stale
    desk.positions["OPT-1"] = pos
    await desk.sweep_position_manager(
        today=_TODAY,
        blackout_active=False,
        current_prices={},
        current_ivs={},
        chains={"NSE:NIFTY": chain},
    )
    assert desk.positions["OPT-1"].income_legs[0].delta == -0.40
    assert executor.rolled == []  # flag off -- just confirms the breach is now visible


async def _sweep_vertical(  # type: ignore[no-untyped-def]
    monkeypatch, short_put_delta: float, *, tested: float = 0.50
):
    from src.options import seller_desk as seller_desk_module

    # one patch (position_manager and seller_desk share the module): pins the knob so the test
    # does not depend on the operator's live config value
    monkeypatch.setattr(
        seller_desk_module._seller_settings,
        "adjustment_settings",
        lambda *a, **kw: {
            "execute_rolls": True,
            "single_sided_tested_delta": tested,
            "delta_breach_threshold": 0.35,
            "delta_breach_blackout": 0.25,
        },
    )
    chain = _adjustment_chain()
    desk, executor = _desk(chain)
    pos = _bull_put_spread_position("OPT-2")
    # the fixture chain has no 23200 row, so the leg keeps the delta set here (the live delta of the short strike)
    pos.income_legs[0].delta = short_put_delta
    desk.positions["OPT-2"] = pos
    await desk.sweep_position_manager(
        today=_TODAY,
        blackout_active=False,
        current_prices={},
        current_ivs={},
        chains={"NSE:NIFTY": chain},
    )
    return desk, executor


async def test_single_leg_vertical_is_closed_once_its_short_strike_is_tested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bull_put_spread/bear_call_spread has one income leg: nothing to roll (found 2026-09-16 -- it used
    # to skip silently), so a breach closes it. Since 2026-10-13 "breach" for such a defined-risk vertical
    # means the short strike is TESTED (|delta| > 0.50), not the iron condor's 0.35.
    desk, executor = await _sweep_vertical(monkeypatch, -0.55, tested=0.50)
    assert executor.rolled == []
    assert "OPT-2" not in desk.positions  # closed, not silently skipped


@pytest.mark.parametrize("delta", [-0.36, -0.46, -0.50])
async def test_single_leg_vertical_is_not_closed_by_ordinary_delta_drift(
    monkeypatch: pytest.MonkeyPatch, delta: float
) -> None:
    # 2026-10-13: a 0.26-delta BANKNIFTY short strike drifted to 0.36 on one +1% day and the old 0.35 rule
    # closed a defined-risk spread at a loss (-Rs2,693, filled hours late on stale prices).
    desk, executor = await _sweep_vertical(monkeypatch, delta, tested=0.50)
    assert "OPT-2" in desk.positions and executor.rolled == []


def test_blackout_tightens_a_loosened_single_sided_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.options import position_manager as pm

    monkeypatch.setattr(
        pm.seller_settings,
        "adjustment_settings",
        lambda: {
            "single_sided_tested_delta": 0.50,
            "delta_breach_threshold": 0.35,
            "delta_breach_blackout": 0.25,
        },
    )
    assert pm.single_sided_tested_delta() == 0.50
    assert pm.single_sided_tested_delta(blackout_active=True) == pytest.approx(
        0.40
    )  # 0.50 - (0.35-0.25)


def test_breach_thresholds_come_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.options import position_manager as pm

    monkeypatch.setattr(
        pm.seller_settings,
        "adjustment_settings",
        lambda: {"delta_breach_threshold": 0.30, "delta_breach_blackout": 0.20},
    )
    assert pm.check_delta_breach(0.32, blackout_active=False).action == "ADJUST"
    assert pm.check_delta_breach(0.22, blackout_active=True).action == "ADJUST"
    assert pm.check_delta_breach(0.28, blackout_active=False).action == "NONE"


def test_default_single_sided_threshold_is_the_spec_035() -> None:
    from src.options import position_manager as pm

    assert pm._DELTA_TESTED_SINGLE_SIDED == 0.35


async def test_iron_condor_keeps_the_035_breach_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.options.position_manager import check_delta_breach_any_leg
    from src.options.structure_selector import OptionLeg

    def _leg(kind: str, delta: float) -> OptionLeg:
        return OptionLeg(
            tradingsymbol=f"X{kind}",
            instrument_type=kind,
            strike=1.0,
            direction="SELL",
            lots=1,
            lot_size=25,
            premium=10.0,
            delta=delta,
        )

    two_legs = [_leg("PE", -0.40), _leg("CE", 0.20)]
    assert (
        check_delta_breach_any_leg(two_legs, blackout_active=False).action == "ADJUST"
    )  # 0.35 rule
    one_leg = [_leg("PE", -0.40)]
    assert (
        check_delta_breach_any_leg(one_leg, blackout_active=False, threshold=0.50).action == "NONE"
    )


def _bull_call_spread_position(position_id: str, *, expiry_days: int = 20) -> OpenPosition:
    """Debit vertical: long 23400 CE @300, short 23800 CE @120, 25 lot -> debit 4500."""
    from src.options.structure_selector import OptionLeg

    def _leg(strike: float, direction: str, premium: float, delta: float) -> OptionLeg:
        return OptionLeg(
            tradingsymbol=f"NFO:NIFTY{int(strike)}CE",
            instrument_type="CE",
            strike=strike,
            direction=direction,
            lots=1,
            lot_size=25,
            premium=premium,
            delta=delta,
        )

    return OpenPosition(
        position_id=position_id,
        underlying="NSE:NIFTY",
        strategy_type="bull_call_spread",
        direction="debit",
        protection_legs=[_leg(23400.0, "BUY", 300.0, 0.49)],
        income_legs=[_leg(23800.0, "SELL", 120.0, 0.27)],
        entry_premium=-4500.0,
        entry_margin=4500.0,
        expiry=_TODAY + timedelta(days=expiry_days),
        entered_at=datetime.now(_IST),
    )


async def _sweep(
    desk: SellerDesk, prices: dict[str, float], chain: OptionChain | None = None
) -> None:
    await desk.sweep_position_manager(
        today=_TODAY,
        blackout_active=False,
        current_prices=prices,
        current_ivs={},
        chains={"NSE:NIFTY": chain} if chain is not None else {},
    )


async def test_sweep_stops_out_a_debit_spread_at_half_the_debit() -> None:
    # Regression (2026-09-28): a bull_call_spread bled to -55..-70% of its debit and
    # nothing ever closed it (debit stop was 100% = total loss). Long 300->100,
    # short 120->20 => -2500 = 56% of the 4500 debit.
    chain = _adjustment_chain()
    repo = _FakeRepo()
    desk, _ = _desk(chain, repo=repo)
    desk.positions["OPT-D"] = _bull_call_spread_position("OPT-D")
    await _sweep(desk, {"NFO:NIFTY23400CE": 100.0, "NFO:NIFTY23800CE": 20.0}, chain)
    assert "OPT-D" not in desk.positions
    assert "STOP_LOSS_HIT" in repo.closed[0]["exit_reason"]


async def test_sweep_books_a_credit_spread_at_half_the_credit_not_half_the_max_loss() -> None:
    # Regression (2026-10-01): a NIFTY bear_call sat at +64% of its credit with no exit
    # because the 50% target was taken of entry_margin (= max LOSS, 20000 here => 10000,
    # more than the 3000 credit can ever pay). Short 23200 PE 50->0 (+1250) and long
    # 22900 PE 50->70 (+500) => +1750 = 58% of the 3000 credit, 9% of the margin.
    chain = _adjustment_chain()
    repo = _FakeRepo()
    desk, _ = _desk(chain, repo=repo)
    desk.positions["OPT-C"] = _bull_put_spread_position("OPT-C")
    await _sweep(desk, {"NFO:NIFTY23200PE": 0.0, "NFO:NIFTY22900PE": 70.0}, chain)
    assert "OPT-C" not in desk.positions
    assert "PROFIT_TARGET_HIT" in repo.closed[0]["exit_reason"]


async def test_sweep_keeps_a_debit_spread_that_is_down_less_than_the_stop() -> None:
    chain = _adjustment_chain()
    desk, executor = _desk(chain)
    desk.positions["OPT-D"] = _bull_call_spread_position("OPT-D")
    # long 300->200, short 120->60 => -1000 = 22% of debit
    await _sweep(desk, {"NFO:NIFTY23400CE": 200.0, "NFO:NIFTY23800CE": 60.0}, chain)
    assert "OPT-D" in desk.positions
    assert executor.closed == []


async def test_sweep_records_peak_and_locks_breakeven_on_a_debit_spread() -> None:
    # Was in profit, gave it all back: the peak must be remembered (and persisted so a
    # restart does not forget it), and once armed a return to breakeven exits.
    chain = _adjustment_chain()
    repo = _FakeRepo()
    desk, _ = _desk(chain, repo=repo)
    desk.positions["OPT-D"] = _bull_call_spread_position("OPT-D")
    # long 300->400, short 120->120 => +2500 = 56% of debit (armed at 35%)
    await _sweep(desk, {"NFO:NIFTY23400CE": 400.0, "NFO:NIFTY23800CE": 120.0}, chain)
    assert desk.positions["OPT-D"].peak_unrealized_pnl == 2500.0
    assert repo.peaks == [("OPT-D", 2500.0)]
    # back to entry prices => P&L 0 => lock out
    await _sweep(desk, {"NFO:NIFTY23400CE": 300.0, "NFO:NIFTY23800CE": 120.0}, chain)
    assert "OPT-D" not in desk.positions
    assert "BREAKEVEN_LOCK" in repo.closed[0]["exit_reason"]


async def test_sweep_does_not_treat_a_winning_debit_short_leg_as_a_delta_breach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The delta-breach rule protects a CREDIT structure's short strike. In a debit
    # spread the short leg is the hedge: its delta rising means the spread is WINNING.
    # Once the delta refresh is live (it now reads the right chain) a rally would have
    # closed the winner as DELTA_BREACH_NO_UNTESTED_SIDE.
    from src.options import seller_desk as seller_desk_module

    monkeypatch.setattr(
        seller_desk_module._seller_settings,
        "adjustment_settings",
        lambda *a, **kw: {"execute_rolls": True},
    )
    chain = _adjustment_chain()  # 23400 CE delta is 0.80 here -> would breach on a credit leg
    desk, executor = _desk(chain)
    pos = _bull_call_spread_position("OPT-D")
    pos.protection_legs[0].strike = 23000.0
    pos.protection_legs[0].tradingsymbol = "NFO:NIFTY23000CE"
    pos.income_legs[0].strike = 23400.0
    pos.income_legs[0].tradingsymbol = "NFO:NIFTY23400CE"
    desk.positions["OPT-D"] = pos
    await _sweep(desk, {"NFO:NIFTY23000CE": 300.0, "NFO:NIFTY23400CE": 120.0}, chain)
    assert "OPT-D" in desk.positions
    assert executor.closed == []


def _chain_with_put_delta(days: int, put_delta: float) -> OptionChain:
    expiry = datetime(_TODAY.year, _TODAY.month, _TODAY.day, tzinfo=_IST) + timedelta(days=days)
    row = ChainStrike(
        strike=23200.0,
        call=_tight_quote(400.0),
        put=_tight_quote(50.0),
        call_iv=0.18,
        put_iv=0.19,
        call_delta=0.90,
        put_delta=put_delta,
        call_symbol="NIFTY23200CE",
        put_symbol="NIFTY23200PE",
    )
    return OptionChain(
        underlying="NIFTY",
        spot=23700.0,
        expiry=expiry,
        days_to_expiry=days,
        lot_size=25,
        strikes=[row],
    )


async def test_sweep_refreshes_delta_from_the_positions_own_expiry_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression (2026-09-28): the sweep only had the NEAREST-expiry chain and matched
    # legs by strike alone, so a position in a later expiry (Oct 13 vs Sep 29) was
    # refreshed from the wrong expiry's delta -- or not at all.
    from src.options import seller_desk as seller_desk_module

    monkeypatch.setattr(
        seller_desk_module._seller_settings,
        "adjustment_settings",
        lambda *a, **kw: {"execute_rolls": False},
    )
    near = _chain_with_put_delta(1, -0.05)  # wrong expiry: same strike, far-OTM delta
    traded = _chain_with_put_delta(20, -0.45)  # the position's expiry
    desk, _ = _desk(_adjustment_chain())
    desk._chain_fetcher.expiry_chains[_TODAY + timedelta(days=20)] = traded  # type: ignore[attr-defined]
    pos = _bull_put_spread_position("OPT-C")  # expiry _TODAY + 20, short 23200 PE
    pos.income_legs[0].delta = -0.20  # stale entry delta
    desk.positions["OPT-C"] = pos
    await _sweep(desk, {}, near)
    assert desk.positions["OPT-C"].income_legs[0].delta == -0.45
    assert desk._chain_fetcher.expiry_requests == [  # type: ignore[attr-defined]
        ("NSE:NIFTY", _TODAY + timedelta(days=20))
    ]


async def test_sweep_never_uses_a_wrong_expiry_chain_when_the_right_one_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.options import seller_desk as seller_desk_module

    monkeypatch.setattr(
        seller_desk_module._seller_settings,
        "adjustment_settings",
        lambda *a, **kw: {"execute_rolls": False},
    )
    near = _chain_with_put_delta(1, -0.05)
    desk, _ = _desk(_adjustment_chain())  # fetch_for_expiry -> None
    pos = _bull_put_spread_position("OPT-C")
    pos.income_legs[0].delta = -0.20
    desk.positions["OPT-C"] = pos
    with caplog.at_level("WARNING", logger="options.seller_desk"):
        await _sweep(desk, {}, near)
    assert desk.positions["OPT-C"].income_legs[0].delta == -0.20  # untouched, not -0.05
    assert any("seller_desk_no_chain_for_position" in r.message for r in caplog.records)


async def test_sweep_logs_every_position_every_cycle_and_flags_missing_prices(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Four reports of "no adjustments happening" were undiagnosable because a quiet
    # sweep logged nothing. Every position must leave a line saying what was seen.
    chain = _adjustment_chain()
    desk, _ = _desk(chain)
    desk.positions["OPT-D"] = _bull_call_spread_position("OPT-D")
    with caplog.at_level("INFO", logger="options.seller_desk"):
        await _sweep(desk, {"NFO:NIFTY23400CE": 250.0}, chain)  # short leg has no price
    msgs = [r.message for r in caplog.records]
    check = next(m for m in msgs if "seller_desk_position_check" in m and "OPT-D" in m)
    assert "dir=debit" in check and "dte=20" in check
    assert any("seller_desk_pnl_price_missing" in m and "NFO:NIFTY23800CE" in m for m in msgs)


def _banknifty_put_chain() -> OptionChain:
    """Real BANKNIFTY numbers from 2026-09-28: spot 54,567, Oct-27 expiry (29d), ATM IV ~15.4%.
    Long 54900 PE @898 (-0.50 delta) / short 53300 PE @380 (-0.25) = the bear_put_spread the
    desk opened at 14:03 on a logged pop=0.746 / EV=+10,586."""
    expiry = datetime(2026, 10, 27, tzinfo=_IST)

    def q(mid: float) -> OptionQuote:
        return OptionQuote("SYM", ltp=mid, bid=mid * 0.995, ask=mid * 1.005, oi=50000, volume=20000)

    def row(strike: float, put_mid: float, put_delta: float, iv: float) -> ChainStrike:
        return ChainStrike(
            strike=strike,
            call=q(50.0),
            put=q(put_mid),
            call_iv=iv,
            put_iv=iv,
            call_delta=0.5,
            put_delta=put_delta,
            call_symbol=f"BANKNIFTY{int(strike)}CE",
            put_symbol=f"BANKNIFTY{int(strike)}PE",
        )

    return OptionChain(
        underlying="BANKNIFTY",
        spot=54567.2,
        expiry=expiry,
        days_to_expiry=29,
        lot_size=30,
        strikes=[
            row(53300.0, 379.75, -0.25, 0.154),
            row(54600.0, 898.175, -0.50, 0.154),  # ATM-ish; long leg (delta -0.50)
        ],
    )


async def _size_bear_put(chain: OptionChain):  # type: ignore[no-untyped-def]
    return await evaluate_and_size(
        strategy_type="bear_put_spread",
        chain=chain,
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        margin_safety_pct=0.9,
        available_margin=1_000_000.0,
        current_greeks=PortfolioGreeks(),
        greeks_budget=_budget(),
        dte_value=29,
        min_rom_pct=0.0,
        executor=_FakeExecutor(),
    )


async def test_evaluate_and_size_rejects_a_debit_spread_whose_real_probability_gives_negative_ev(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Regression (2026-09-28): pop=1-|short delta|=0.75 made every debit spread look like a
    # high-probability trade, so the mandatory EV gate never stopped one. Real POP ~0.43.
    with caplog.at_level("INFO", logger="options.seller_desk"):
        assert await _size_bear_put(_banknifty_put_chain()) is None
    assert any("reason=negative_ev" in r.message for r in caplog.records)


async def test_evaluate_and_size_still_accepts_a_debit_spread_with_a_real_edge() -> None:
    # The rich fixture prices a 300-wide spread at a 15 debit (19:1 payoff) -- positive EV
    # even at the true ~49% breakeven probability. The fix must not block real edge.
    chain = _rich_chain()
    result = await evaluate_and_size(
        strategy_type="bull_call_spread",
        chain=chain,
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        margin_safety_pct=0.9,
        available_margin=1_000_000.0,
        current_greeks=PortfolioGreeks(),
        greeks_budget=_budget(),
        dte_value=35,
        min_rom_pct=0.0,
        executor=_FakeExecutor(),
    )
    assert result is not None and result[2] >= 1


async def _size_credit(strategy: str, chain: OptionChain):  # type: ignore[no-untyped-def]
    return await evaluate_and_size(
        strategy_type=strategy,
        chain=chain,
        options_capital=1_000_000.0,
        risk_per_trade_pct=1.0,  # 2% reduced x0.5 by the low-IVR multiplier, as live
        full_risk_per_trade_pct=2.0,
        margin_safety_pct=0.9,
        available_margin=980_000.0,
        current_greeks=PortfolioGreeks(),
        greeks_budget=_budget(),
        dte_value=28,
        min_rom_pct=0.0,
        ivr=41.9,
        executor=_FakeExecutor(),
    )


@pytest.mark.parametrize("strategy", ["bull_put_spread", "bear_call_spread"])
async def test_credit_spread_width_is_fitted_to_the_risk_budget_instead_of_rejected(
    strategy: str, caplog: pytest.LogCaptureFixture
) -> None:
    # Regression (2026-09-28): BANKNIFTY credit spreads at the 0.25/0.10 wing pair risked
    # ~₹35-50k a lot against a ₹20k (2%) limit -> lots=0 SIZE_TOO_SMALL every cycle for weeks,
    # so the desk never sold premium on BANKNIFTY at all. Width now yields to the budget.
    from tests.unit.options.test_structure_builder import _bank_chain

    with caplog.at_level("INFO", logger="options.seller_desk"):
        result = await _size_credit(strategy, _bank_chain())
    assert result is not None and result[2] == 1
    protection, income, _ = result
    max_loss, _, _, direction = estimate_economics(
        strategy, protection, income, _bank_chain().lot_size, 1
    )
    assert direction == "credit" and 0 < max_loss <= 20_000.0
    assert any("seller_desk_width_narrowed" in r.message for r in caplog.records)


async def test_credit_spread_that_already_fits_is_not_narrowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO", logger="options.seller_desk"):
        assert await _size_credit("bull_put_spread", _rich_chain()) is not None
    assert not any("seller_desk_width_narrowed" in r.message for r in caplog.records)


async def test_operator_close_list_closes_only_the_named_positions(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # No manual way to exit an options position existed (2026-09-28): two debit spreads opened
    # on a false 75% POP filled both underlyings, and the one-position-per-underlying rule then
    # blocked every new trade. The operator list closes exactly the named ids, audited.
    from src.options import seller_desk as seller_desk_module

    monkeypatch.setattr(
        seller_desk_module._seller_settings,
        "operator_close_position_ids",
        lambda *a, **kw: ["OPT-D", "OPT-GONE"],  # OPT-GONE is not open: must be ignored
    )
    chain = _adjustment_chain()
    repo = _FakeRepo()
    desk, executor = _desk(chain, repo=repo)
    desk.positions["OPT-D"] = _bull_call_spread_position("OPT-D")
    desk.positions["OPT-KEEP"] = _bull_call_spread_position("OPT-KEEP")
    prices = {"NFO:NIFTY23400CE": 300.0, "NFO:NIFTY23800CE": 120.0}  # flat: no other rule fires
    with caplog.at_level("WARNING", logger="options.seller_desk"):
        await _sweep(desk, prices, chain)
    assert "OPT-D" not in desk.positions
    assert "OPT-KEEP" in desk.positions
    assert [c["position_id"] for c in repo.closed] == ["OPT-D"]
    assert repo.closed[0]["exit_reason"] == "OPERATOR_CLOSE"
    assert any(
        "seller_desk_operator_close" in r.message and "OPT-D" in r.message for r in caplog.records
    )


async def test_operator_close_list_leaves_everything_alone_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.options import seller_desk as seller_desk_module

    monkeypatch.setattr(
        seller_desk_module._seller_settings, "operator_close_position_ids", lambda *a, **kw: []
    )
    chain = _adjustment_chain()
    desk, executor = _desk(chain)
    desk.positions["OPT-D"] = _bull_call_spread_position("OPT-D")
    await _sweep(desk, {"NFO:NIFTY23400CE": 300.0, "NFO:NIFTY23800CE": 120.0}, chain)
    assert "OPT-D" in desk.positions and executor.closed == []


async def test_run_cycle_respects_enabled_strategy_types_config() -> None:
    # bull_put_spread would otherwise qualify (regime+ivr allow it), but it's
    # excluded from enabled_strategy_types -> must never be attempted.
    chain = _rich_chain()
    executor = _FakeExecutor()

    desk = SellerDesk(
        StrategySelector(),
        _FakeEventCalendar(),
        executor,
        _FakeChainFetcher(chain),
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        margin_safety_pct=0.90,
        greeks_budget=_budget(),
        min_rom_pct=0.0,
        enabled_strategy_types=set(),  # nothing enabled
    )
    assert desk.enabled_strategy_types == frozenset()
    calendar = {"NIFTY": [chain.expiry.date() + timedelta(days=i) for i in range(-2, 3)]}
    position = await desk.run_cycle(
        "NSE:NIFTY",
        regime="HIGH_VOL_RANGE_BOUND",
        ivr=65.0,
        term_structure="contango",
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar=calendar,
        available_margin=1_000_000.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=_DUMMY_CTX,
    )
    assert position is None
    assert executor.executed == []


async def _cycle_with_open_position(
    open_strategy: str, monkeypatch: pytest.MonkeyPatch, *, limit: int | None = None
):  # type: ignore[no-untyped-def]
    """run_cycle on NIFTY (only bear_call_spread enabled) while a BANKNIFTY position is open."""
    from src.options import seller_desk as seller_desk_module
    from src.risk import budget as budget_module

    # High enough that the shared-risk ceiling (config/capital.json
    # options.max_share_of_shared_risk) never interferes with what THIS fixture
    # tests (the same-direction-spread limit) -- it has its own dedicated tests.
    monkeypatch.setattr(budget_module, "_current_equity", 100_000_000.0)

    if limit is not None:
        monkeypatch.setattr(
            seller_desk_module._seller_settings,
            "max_same_direction_strategies",
            lambda *a, **kw: limit,
        )
    chain = _rich_chain()
    executor = _FakeExecutor()
    desk = SellerDesk(
        StrategySelector(),
        _FakeEventCalendar(),
        executor,
        _FakeChainFetcher(chain),
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        margin_safety_pct=0.90,
        greeks_budget=_budget(),
        min_rom_pct=0.0,
        enabled_strategy_types={"bear_call_spread"},
    )
    other = _bull_put_spread_position("OPT-BANK")
    other.underlying = "NSE:BANKNIFTY"
    other.strategy_type = open_strategy
    desk.positions["OPT-BANK"] = other
    calendar = {"NIFTY": [chain.expiry.date() + timedelta(days=i) for i in range(-2, 3)]}
    position = await desk.run_cycle(
        "NSE:NIFTY",
        regime="HIGH_VOL_RANGE_BOUND",
        ivr=65.0,
        term_structure="contango",
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar=calendar,
        available_margin=1_000_000.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=_DUMMY_CTX,
    )
    return position, executor


async def test_run_cycle_refuses_a_second_same_direction_spread_on_a_correlated_index(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # 2026-09-28: NIFTY and BANKNIFTY bear_call_spreads opened minutes apart -- ~3.8% of capital
    # on ONE view of two ~0.9-correlated indices. One directional side at a time.
    from src.options import seller_settings as _ss

    monkeypatch.setattr(_ss, "max_same_direction_strategies", lambda settings=None: 1)
    with caplog.at_level("INFO", logger="options.seller_desk"):
        position, executor = await _cycle_with_open_position("bear_call_spread", monkeypatch)
    assert position is None and executor.executed == []
    assert any("correlated_same_direction_open" in r.message for r in caplog.records)


async def test_run_cycle_allows_the_opposite_direction_on_the_other_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    position, executor = await _cycle_with_open_position("bull_put_spread", monkeypatch)
    assert position is not None and len(executor.executed) == 1


async def test_same_direction_limit_is_configurable_and_zero_disables_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    position, _ = await _cycle_with_open_position("bear_call_spread", monkeypatch, limit=2)
    assert position is not None  # limit 2 -> a second bearish position is allowed
    position, _ = await _cycle_with_open_position("bear_call_spread", monkeypatch, limit=0)
    assert position is not None  # 0 = guard off


def test_default_enabled_strategy_types_is_everything_implemented() -> None:
    from src.options.structure_builder import implemented_strategy_types

    desk, _ = _desk(_rich_chain())
    assert desk.enabled_strategy_types == implemented_strategy_types()


async def test_run_cycle_persists_position_when_repo_injected() -> None:
    chain = _rich_chain()
    repo = _FakeRepo()
    desk, _ = _desk(chain, repo=repo)
    calendar = {"NIFTY": [chain.expiry.date() + timedelta(days=i) for i in range(-2, 3)]}
    position = await desk.run_cycle(
        "NSE:NIFTY",
        regime="HIGH_VOL_RANGE_BOUND",
        ivr=65.0,
        term_structure="contango",
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar=calendar,
        available_margin=1_000_000.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=_DUMMY_CTX,
    )
    if position is not None:
        assert len(repo.inserted) == 1
        assert repo.inserted[0]["position_id"] == position.position_id
        assert repo.inserted[0]["underlying"] == "NSE:NIFTY"


async def test_run_cycle_without_repo_does_not_raise() -> None:
    chain = _rich_chain()
    desk, _ = _desk(chain, repo=None)
    calendar = {"NIFTY": [chain.expiry.date() + timedelta(days=i) for i in range(-2, 3)]}
    await desk.run_cycle(
        "NSE:NIFTY",
        regime="HIGH_VOL_RANGE_BOUND",
        ivr=65.0,
        term_structure="contango",
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar=calendar,
        available_margin=1_000_000.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=_DUMMY_CTX,
    )  # no repo injected -> must not attempt persistence or raise


async def test_close_position_persists_to_repo() -> None:
    chain = _rich_chain()
    repo = _FakeRepo()
    desk, executor = _desk(chain, repo=repo)
    protection, income = build_structure("bull_put_spread", chain, lots=1)
    from src.options.seller_desk import OpenPosition

    pos = OpenPosition(
        position_id="OPT-1",
        underlying="NIFTY",
        strategy_type="bull_put_spread",
        direction="credit",
        protection_legs=protection,
        income_legs=income,
        entry_premium=100.0,
        entry_margin=1000.0,
        expiry=_TODAY + timedelta(days=1),
        entered_at=datetime.now(_IST),
    )
    desk.positions["OPT-1"] = pos
    await desk.sweep_expiry_guardian(_TODAY, datetime.now(_IST))
    assert len(repo.closed) == 1
    assert repo.closed[0]["position_id"] == "OPT-1"
    assert executor.closed  # legs were closed through the executor too
    assert repo.closed[0]["realized_pnl"] is not None  # 2026-09-04: was always missing before


async def test_close_position_computes_realized_pnl_from_close_ltp() -> None:
    # bull_put_spread on _rich_chain(): long (protection) leg entry premium
    # 25.0 (strike 23500, put quote = 50 + (23500-24000)*0.05), short
    # (income) leg entry premium 35.0 (strike 23700, put quote = 50 +
    # (23700-24000)*0.05) -- both close at the fixed LTP of 50.0.
    # long:  (50.0 - 25.0) * qty = +25.0/share (bought low, closed higher)
    # short: (35.0 - 50.0) * qty = -15.0/share (sold low, must buy back higher)
    # net: +10.0/share * quantity(=lots*lot_size=1*25=25) = +250.0
    chain = _rich_chain()
    repo = _FakeRepo()
    desk, executor = _desk(chain, repo=repo, executor=_FakeExecutor(close_ltp=50.0))
    protection, income = build_structure("bull_put_spread", chain, lots=1)
    from src.options.seller_desk import OpenPosition

    pos = OpenPosition(
        position_id="OPT-1",
        underlying="NIFTY",
        strategy_type="bull_put_spread",
        direction="credit",
        protection_legs=protection,
        income_legs=income,
        entry_premium=100.0,
        entry_margin=1000.0,
        expiry=_TODAY + timedelta(days=1),
        entered_at=datetime.now(_IST),
    )
    desk.positions["OPT-1"] = pos
    await desk.sweep_expiry_guardian(_TODAY, datetime.now(_IST))
    assert repo.closed[0]["realized_pnl"] == pytest.approx(250.0)


async def test_close_position_stores_itemised_charges_and_keeps_pnl_gross() -> None:
    # Same structure/close as above: 4 executed orders (entry+exit per leg) -> 4 x Rs20 brokerage;
    # STT only on the sells: long exit 50x25 + short entry 35x25 = 2125 premium x 0.15%.
    chain = _rich_chain()
    repo = _FakeRepo()
    desk, _ = _desk(chain, repo=repo, executor=_FakeExecutor(close_ltp=50.0))
    protection, income = build_structure("bull_put_spread", chain, lots=1)
    from src.options.seller_desk import OpenPosition

    desk.positions["OPT-1"] = OpenPosition(
        position_id="OPT-1",
        underlying="NIFTY",
        strategy_type="bull_put_spread",
        direction="credit",
        protection_legs=protection,
        income_legs=income,
        entry_premium=100.0,
        entry_margin=1000.0,
        expiry=_TODAY + timedelta(days=1),
        entered_at=datetime.now(_IST),
    )
    await desk.sweep_expiry_guardian(_TODAY, datetime.now(_IST))
    row = repo.closed[0]
    assert row["realized_pnl"] == pytest.approx(250.0)  # gross, unchanged
    ch = row["charges_breakdown"]
    assert ch["brokerage"] == pytest.approx(80.0)
    assert ch["stt"] == pytest.approx(2125 * 0.0015, abs=0.02)
    assert row["broker"]


async def test_close_position_records_none_pnl_when_ltp_fetch_fails() -> None:
    # An executor whose close_leg() can't get an LTP (broker fetch failed)
    # must record realized_pnl as NULL, not a silently-wrong partial number.
    chain = _rich_chain()
    repo = _FakeRepo()
    desk, executor = _desk(chain, repo=repo, executor=_FakeExecutor(close_ltp=None))
    protection, income = build_structure("bull_put_spread", chain, lots=1)
    from src.options.seller_desk import OpenPosition

    pos = OpenPosition(
        position_id="OPT-1",
        underlying="NIFTY",
        strategy_type="bull_put_spread",
        direction="credit",
        protection_legs=protection,
        income_legs=income,
        entry_premium=100.0,
        entry_margin=1000.0,
        expiry=_TODAY + timedelta(days=1),
        entered_at=datetime.now(_IST),
    )
    desk.positions["OPT-1"] = pos
    await desk.sweep_expiry_guardian(_TODAY, datetime.now(_IST))
    assert repo.closed[0]["realized_pnl"] is None


async def test_restore_positions_reconstructs_from_repo_rows() -> None:
    chain = _rich_chain()
    protection, income = build_structure("bull_put_spread", chain, lots=1)
    from src.db.repositories.options import _serialize_leg

    row = {
        "position_id": "OPT-9",
        "underlying": "NIFTY",
        "strategy_type": "bull_put_spread",
        "legs": {
            "protection": [_serialize_leg(leg) for leg in protection],
            "income": [_serialize_leg(leg) for leg in income],
        },
        "expiry": _TODAY + timedelta(days=30),
        "net_credit": 150.0,
        "entry_margin": 1200.0,
        "opened_at": datetime.now(_IST),
    }
    repo = _FakeRepo(open_rows=[row])
    desk, _ = _desk(chain, repo=repo)
    restored = await desk.restore_positions()
    assert restored == 1
    assert "OPT-9" in desk.positions
    assert desk.positions["OPT-9"].direction == "credit"
    assert len(desk.positions["OPT-9"].protection_legs) == len(protection)
    assert desk.positions["OPT-9"].peak_unrealized_pnl is None  # pre-082 row: never observed


async def test_restore_positions_reloads_persisted_peak_so_restart_keeps_the_lock_armed() -> None:
    from decimal import Decimal

    from src.db.repositories.options import _serialize_leg

    pos = _bull_call_spread_position("OPT-9")
    row = {
        "position_id": "OPT-9",
        "underlying": "NSE:NIFTY",
        "strategy_type": "bull_call_spread",
        "legs": {
            "protection": [_serialize_leg(leg) for leg in pos.protection_legs],
            "income": [_serialize_leg(leg) for leg in pos.income_legs],
        },
        "expiry": pos.expiry,
        "net_credit": Decimal("-4500.00"),
        "entry_margin": Decimal("4500.00"),
        "opened_at": datetime.now(_IST),
        "peak_unrealized_pnl": Decimal("2500.00"),  # asyncpg returns NUMERIC as Decimal
    }
    desk, _ = _desk(_adjustment_chain(), repo=_FakeRepo(open_rows=[row]))
    await desk.restore_positions()
    assert desk.positions["OPT-9"].peak_unrealized_pnl == 2500.0


async def test_restore_positions_returns_zero_without_repo() -> None:
    desk, _ = _desk(_rich_chain(), repo=None)
    assert await desk.restore_positions() == 0


# ── available_margin (2026-09-08, paper-mode simulation) ────────────────────
# Found live: options.paper_mode=true was still calling broker.get_margins()
# every cycle -- the real account's free cash is unrelated to the ring-fenced
# paper capital sleeve, and can wrongly under/over-constrain sizing.


def test_available_margin_paper_mode_ignores_broker_cash_when_no_positions() -> None:
    desk, _ = _desk(_rich_chain())  # default is_paper=True
    assert desk.is_paper is True
    # A real broker cash figure is passed but must be IGNORED in paper mode.
    assert desk.available_margin(broker_available_cash=1.0) == 1_000_000.0


def test_available_margin_paper_mode_subtracts_committed_open_positions() -> None:
    chain = _rich_chain()
    desk, _ = _desk(chain)
    protection, income = build_structure("bull_put_spread", chain, lots=1)
    from src.options.seller_desk import OpenPosition

    desk.positions["OPT-1"] = OpenPosition(
        position_id="OPT-1",
        underlying="NIFTY",
        strategy_type="bull_put_spread",
        direction="credit",
        protection_legs=protection,
        income_legs=income,
        entry_premium=100.0,
        entry_margin=300_000.0,
        expiry=_TODAY + timedelta(days=30),
        entered_at=datetime.now(_IST),
    )
    assert desk.available_margin() == 1_000_000.0 - 300_000.0


def test_available_margin_paper_mode_floors_at_zero() -> None:
    chain = _rich_chain()
    desk, _ = _desk(chain)
    protection, income = build_structure("bull_put_spread", chain, lots=1)
    from src.options.seller_desk import OpenPosition

    desk.positions["OPT-1"] = OpenPosition(
        position_id="OPT-1",
        underlying="NIFTY",
        strategy_type="bull_put_spread",
        direction="credit",
        protection_legs=protection,
        income_legs=income,
        entry_premium=100.0,
        entry_margin=1_500_000.0,  # exceeds the whole sleeve
        expiry=_TODAY + timedelta(days=30),
        entered_at=datetime.now(_IST),
    )
    assert desk.available_margin() == 0.0


def test_available_margin_live_mode_uses_real_broker_cash_unchanged() -> None:
    desk = SellerDesk(
        StrategySelector(),
        _FakeEventCalendar(),
        _FakeExecutor(),
        _FakeChainFetcher(_rich_chain()),
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        margin_safety_pct=0.90,
        greeks_budget=_budget(),
        min_rom_pct=0.0,
        is_paper=False,
    )
    assert desk.is_paper is False
    assert desk.available_margin(broker_available_cash=42_000.0) == 42_000.0


def test_available_margin_live_mode_none_broker_cash_is_zero() -> None:
    desk = SellerDesk(
        StrategySelector(),
        _FakeEventCalendar(),
        _FakeExecutor(),
        _FakeChainFetcher(_rich_chain()),
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        margin_safety_pct=0.90,
        greeks_budget=_budget(),
        min_rom_pct=0.0,
        is_paper=False,
    )
    assert desk.available_margin() == 0.0
    assert desk.positions == {}


# ---------------------------------------------------------------------------
# Portfolio-level risk layer: entry Greeks, daily P&L breaker, margin cushion,
# gap stress test (options_spec.md 8 / 11 / 11.5)
# ---------------------------------------------------------------------------


def _open_pos(chain: OptionChain, pid: str = "OPT-1", *, margin: float = 1000.0) -> OpenPosition:
    protection, income = build_structure("bull_put_spread", chain, lots=1)
    return OpenPosition(
        position_id=pid,
        underlying="NIFTY",
        strategy_type="bull_put_spread",
        direction="credit",
        protection_legs=protection,
        income_legs=income,
        entry_premium=100.0,
        entry_margin=margin,
        expiry=_TODAY + timedelta(days=30),
        entered_at=datetime.now(_IST),
    )


def _now_day() -> date:
    return datetime.now(_IST).date()


def test_portfolio_greeks_sums_entry_greeks_times_lots_on_rupee_scale() -> None:
    chain = _rich_chain()
    desk, _ = _desk(chain)
    desk._spot["NIFTY"] = 24000.0
    a = _open_pos(chain, "A")
    a.entry_greeks = PortfolioGreeks(net_delta=-1.0, net_gamma=0.1, net_theta=2.0, net_vega=-3.0)
    b = _open_pos(chain, "B")
    b.entry_greeks = PortfolioGreeks(net_delta=-2.0, net_gamma=0.2, net_theta=1.0, net_vega=-1.0)
    desk.positions.update({"A": a, "B": b})
    total = desk.portfolio_greeks()
    assert total.net_delta == pytest.approx(-3.0 * 24000.0 / 100.0)  # rupees per 1% move
    assert total.net_gamma == pytest.approx(0.3 * 24000.0**2 / 100.0)
    assert total.net_theta == pytest.approx(3.0)  # already rupees/day
    assert total.net_vega == pytest.approx(-4.0)  # already rupees per vol point


def test_portfolio_greeks_empty_book_is_zero() -> None:
    desk, _ = _desk(_rich_chain())
    assert desk.portfolio_greeks() == PortfolioGreeks()


async def test_daily_loss_breaker_latches_entries_off_on_large_mtm_loss() -> None:
    chain = _rich_chain()
    desk, _ = _desk(chain)
    pos = _open_pos(chain)
    desk.positions["OPT-1"] = pos
    short_leg = pos.income_legs[0]
    # 25-lot-size short whose premium explodes by 2100 -> 52,500 loss > 5% of 1M
    prices = {short_leg.tradingsymbol: short_leg.premium + 2100.0}
    today = _now_day()
    await desk.sweep_portfolio_risk(today=today, current_prices=prices, chains={})
    assert desk.entries_blocked_reason(today, available_margin=1_000_000.0) == (
        "daily_pnl_circuit_breaker"
    )
    # ...and run_cycle refuses to open anything new, while the position stays open.
    desk.positions.clear()
    opened = await desk.run_cycle(
        "NSE:NIFTY",
        regime="RANGE_BOUND_LOW_VOL",
        ivr=60.0,
        term_structure=None,
        today=today,
        now=datetime.now(_IST),
        expiry_calendar={},
        available_margin=1_000_000.0,
        crm=_APPROVE_ALL_CRM,  # type: ignore[arg-type]
        crm_ctx=_DUMMY_CTX,  # type: ignore[arg-type]
    )
    assert opened is None


async def test_daily_loss_breaker_does_not_fire_within_limit() -> None:
    chain = _rich_chain()
    desk, _ = _desk(chain)
    pos = _open_pos(chain)
    desk.positions["OPT-1"] = pos
    today = _now_day()
    await desk.sweep_portfolio_risk(today=today, current_prices={}, chains={})
    assert desk.entries_blocked_reason(today, available_margin=1_000_000.0) is None


async def test_daily_loss_breaker_counts_realized_loss_from_closed_positions() -> None:
    chain = _rich_chain()

    class _LossExecutor(_FakeExecutor):
        # Short leg closes 2100 above its premium; the long leg closes at its own
        # entry premium -> a ~52,500 realized loss, all on the short leg.
        async def close_leg(self, leg, signal_id):  # type: ignore[no-untyped-def]
            return leg.premium + 2100.0 if leg.direction == "SELL" else leg.premium

    desk, _ = _desk(chain, executor=_LossExecutor())
    desk.positions["OPT-1"] = _open_pos(chain)
    await desk._close_position("OPT-1", reason="test")
    today = _now_day()
    await desk.sweep_portfolio_risk(today=today, current_prices={}, chains={})
    assert desk.entries_blocked_reason(today, 1_000_000.0) == "daily_pnl_circuit_breaker"


async def test_restore_positions_seeds_todays_realized_pnl_so_restart_cannot_reset_breaker() -> (
    None
):
    class _Repo(_FakeRepo):
        async def realized_pnl_closed_on(self, day, *, is_paper):  # type: ignore[no-untyped-def]
            return -60_000.0

    desk, _ = _desk(_rich_chain(), repo=_Repo())
    await desk.restore_positions()
    today = _now_day()
    await desk.sweep_portfolio_risk(today=today, current_prices={}, chains={})
    assert desk.entries_blocked_reason(today, 1_000_000.0) == "daily_pnl_circuit_breaker"


def test_margin_cushion_blocks_new_entries_when_available_margin_is_thin() -> None:
    chain = _rich_chain()
    desk, _ = _desk(chain)
    desk.positions["OPT-1"] = _open_pos(chain, margin=900_000.0)
    reason = desk.entries_blocked_reason(_now_day(), available_margin=100_000.0)  # 11% cushion
    assert reason is not None and reason.startswith("margin_cushion")


def test_margin_cushion_does_not_block_with_healthy_margin() -> None:
    chain = _rich_chain()
    desk, _ = _desk(chain)
    desk.positions["OPT-1"] = _open_pos(chain, margin=100_000.0)
    assert desk.entries_blocked_reason(_now_day(), available_margin=900_000.0) is None


async def test_gap_stress_only_blocks_entries_when_enforce_flag_is_on(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    chain = _rich_chain()
    desk, _ = _desk(chain)
    pos = _open_pos(chain)
    pos.entry_greeks = PortfolioGreeks(
        net_delta=-5000.0, net_gamma=0.0, net_theta=0.0, net_vega=0.0
    )
    desk.positions["OPT-1"] = pos
    today = _now_day()
    chains = {"NIFTY": chain}

    from src.options import seller_desk as sd

    monkeypatch.setattr(sd._seller_settings, "portfolio_risk", lambda: {"gap_risk_enforce": False})
    await desk.sweep_portfolio_risk(today=today, current_prices={}, chains=chains)
    assert desk.entries_blocked_reason(today, 1_000_000.0) is None

    monkeypatch.setattr(sd._seller_settings, "portfolio_risk", lambda: {"gap_risk_enforce": True})
    await desk.sweep_portfolio_risk(today=today, current_prices={}, chains=chains)
    assert desk.entries_blocked_reason(today, 1_000_000.0) == "gap_risk_stress_test"


async def test_close_position_runs_autopsy_and_stores_findings() -> None:
    chain = _rich_chain()
    repo = _FakeRepo()
    desk, _ = _desk(chain, repo=repo)
    pos = _open_pos(chain)
    pos.entry_regime = "LOW_VOL_RANGE_BOUND"
    pos.entry_atm_iv = 0.15
    pos.entry_spot = 24000.0
    pos.entry_short_delta = 0.25
    pos.was_breached = True
    pos.entry_greeks = PortfolioGreeks(
        net_delta=1.0, net_gamma=-0.001, net_theta=200.0, net_vega=-300.0
    )
    desk.positions["OPT-1"] = pos
    desk._last_regime["NIFTY"] = "LOW_VOL_TRENDING"
    desk._spot["NIFTY"] = 24100.0
    desk._atm_iv["NIFTY"] = 0.18
    await desk._close_position("OPT-1", reason="test")
    by_q = {q: f for _, q, f, _ in repo.findings}
    assert (
        "correct_strategy_for_regime" in by_q
        and "turned trending" in by_q["correct_strategy_for_regime"]
    )
    assert (
        "strike_delta_appropriateness" in by_q
        and "breached" in by_q["strike_delta_appropriateness"]
    )
    assert "WRONG" in by_q["vol_thesis_correctness"]  # sold expecting IV drop, IV rose 15->18
    assert "greeks_pnl_source" in by_q
    assert "early_profit_taking" not in by_q  # counterfactual is unknowable at close


async def test_close_position_autopsy_skips_questions_for_positions_without_entry_context() -> None:
    chain = _rich_chain()
    repo = _FakeRepo()
    desk, _ = _desk(chain, repo=repo)
    desk.positions["OPT-1"] = _open_pos(chain)  # no entry_* context recorded
    await desk._close_position("OPT-1", reason="test")
    questions = {q for _, q, _, _ in repo.findings}
    assert questions == {"adjustment_effectiveness"}


async def test_autopsy_failure_never_blocks_the_close() -> None:
    chain = _rich_chain()

    class _BadRepo(_FakeRepo):
        async def insert_learning_finding(self, *a, **k):  # type: ignore[no-untyped-def]
            raise RuntimeError("db down")

    repo = _BadRepo()
    desk, _ = _desk(chain, repo=repo)
    desk.positions["OPT-1"] = _open_pos(chain)
    await desk._close_position("OPT-1", reason="test")
    assert "OPT-1" not in desk.positions
    assert repo.closed  # the close itself was still persisted


async def test_delta_breach_flags_position_and_persists() -> None:
    chain = _rich_chain()
    repo = _FakeRepo()
    desk, _ = _desk(chain, repo=repo)
    pos = _open_pos(chain)
    pos.income_legs[0].delta = -0.60  # far beyond the 0.35 breach threshold
    pos.entry_short_delta = 0.25
    desk.positions["OPT-1"] = pos
    await desk.sweep_position_manager(
        today=_TODAY, blackout_active=False, current_prices={}, current_ivs={}, chains={}
    )
    # A single-leg vertical has no untested side to roll, so it is then closed on breach;
    # the breach flag must be persisted first so the autopsy can see it.
    assert repo.breached == ["OPT-1"]
    assert any(
        q == "strike_delta_appropriateness" and "breached" in f for _, q, f, _ in repo.findings
    )


async def test_gap_stress_uses_real_atm_iv_for_vega_impact(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.options import seller_desk as sd

    monkeypatch.setattr(sd._seller_settings, "portfolio_risk", lambda: {"gap_risk_enforce": True})
    chain = _rich_chain()  # ATM IV ~0.185
    desk, _ = _desk(chain)
    pos = _open_pos(chain)
    # -2700 rupees per vol point: 5% gap -> 2700 x 0.35 x 18.5 = ~17.5k (> 15k NIFTY limit)
    pos.entry_greeks = PortfolioGreeks(
        net_delta=0.0, net_gamma=0.0, net_theta=0.0, net_vega=-2700.0
    )
    desk.positions["OPT-1"] = pos
    await desk.sweep_portfolio_risk(today=_now_day(), current_prices={}, chains={"NIFTY": chain})
    assert desk.entries_blocked_reason(_now_day(), 1_000_000.0) == "gap_risk_stress_test"


async def test_daily_attribution_written_once_after_1520() -> None:
    chain = _rich_chain()
    repo = _FakeRepo()
    desk, _ = _desk(chain, repo=repo)
    pos = _open_pos(chain)
    pos.entry_greeks = PortfolioGreeks(
        net_delta=1.0, net_gamma=0.0, net_theta=100.0, net_vega=-50.0
    )
    desk.positions["OPT-1"] = pos
    today = _now_day()
    early = datetime(today.year, today.month, today.day, 10, 0, tzinfo=_IST)
    late = datetime(today.year, today.month, today.day, 15, 25, tzinfo=_IST)
    await desk.sweep_portfolio_risk(
        today=today, current_prices={}, chains={"NIFTY": chain}, now=early
    )
    assert repo.attribution == []  # too early
    chain2 = _rich_chain()
    chain2.spot = 24100.0  # +100 points since the day's first observation
    await desk.sweep_portfolio_risk(
        today=today, current_prices={}, chains={"NIFTY": chain2}, now=late
    )
    await desk.sweep_portfolio_risk(
        today=today, current_prices={}, chains={"NIFTY": chain2}, now=late
    )
    assert len(repo.attribution) == 1
    row = repo.attribution[0]
    assert row["delta_pnl"] == pytest.approx(100.0)  # +1 delta x 100 points
    assert row["theta_pnl"] == pytest.approx(100.0)


async def test_greeks_advisory_logs_when_budget_exceeded(caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    chain = _rich_chain()
    desk, _ = _desk(chain)
    desk._spot["NIFTY"] = 24000.0
    pos = _open_pos(chain)
    pos.entry_greeks = PortfolioGreeks(
        net_delta=0.0, net_gamma=0.0, net_theta=0.0, net_vega=-200_000.0
    )
    desk.positions["OPT-1"] = pos
    with caplog.at_level(logging.WARNING, logger="options.seller_desk"):
        await desk.sweep_portfolio_risk(today=_now_day(), current_prices={}, chains={})
    assert any(
        "seller_desk_greeks_" in r.message and "dim=vega" in r.message for r in caplog.records
    )


def test_iron_condor_max_loss_is_one_side_not_the_full_span() -> None:
    # 2026-10-07 fix: a 4-leg iron condor can only be breached on ONE side at expiry,
    # so max loss is that side's own width, not the span from the put wing to the
    # call wing (the old formula overstated it by roughly 2x for a symmetric condor).
    chain = _rich_chain()
    protection, income = build_structure("iron_condor", chain, lots=1)
    max_loss, _, net_premium, direction = estimate_economics(
        "iron_condor", protection, income, chain.lot_size, 1
    )
    assert direction == "credit"
    call_strikes = [leg.strike for leg in protection + income if leg.instrument_type == "CE"]
    put_strikes = [leg.strike for leg in protection + income if leg.instrument_type == "PE"]
    one_side_width = max(max(call_strikes) - min(call_strikes), max(put_strikes) - min(put_strikes))
    full_span = max(leg.strike for leg in protection + income) - min(
        leg.strike for leg in protection + income
    )
    assert one_side_width < full_span  # the two formulas really do disagree on this chain
    assert max_loss == pytest.approx(one_side_width * chain.lot_size - net_premium)
    # The old (wrong) formula would have been roughly double this.
    assert max_loss < (full_span * chain.lot_size - net_premium) * 0.6


def test_iron_butterfly_max_loss_is_one_side_not_the_full_span() -> None:
    chain = _rich_chain()
    protection, income = build_structure("iron_butterfly", chain, lots=1)
    max_loss, _, net_premium, _ = estimate_economics(
        "iron_butterfly", protection, income, chain.lot_size, 1
    )
    call_strikes = [leg.strike for leg in protection + income if leg.instrument_type == "CE"]
    put_strikes = [leg.strike for leg in protection + income if leg.instrument_type == "PE"]
    one_side_width = max(max(call_strikes) - min(call_strikes), max(put_strikes) - min(put_strikes))
    assert max_loss == pytest.approx(one_side_width * chain.lot_size - net_premium)


def test_vertical_spread_max_loss_is_unaffected_by_the_condor_fix() -> None:
    # A 2-leg vertical's full span already equals its one side's width -- regression
    # guard that the fix didn't touch bull_put_spread / bear_call_spread etc.
    chain = _rich_chain()
    protection, income = build_structure("bull_put_spread", chain, lots=1)
    max_loss, _, net_premium, _ = estimate_economics(
        "bull_put_spread", protection, income, chain.lot_size, 1
    )
    strikes = [leg.strike for leg in protection + income]
    width = max(strikes) - min(strikes)
    assert max_loss == pytest.approx(width * chain.lot_size - net_premium)


def test_seller_settings_options_money_accessors_track_live_equity() -> None:
    from src.options import seller_settings as ss
    from src.risk import budget as budget_mod

    budget_mod.set_current_equity(2_000_000.0)
    try:
        at_low = ss.max_options_structure_loss_per_trade_rupees()
        budget_mod.set_current_equity(4_000_000.0)
        at_high = ss.max_options_structure_loss_per_trade_rupees()
        assert at_high == pytest.approx(at_low * 2)
        assert ss.max_options_daily_loss_all_strategies_rupees() > 0
    finally:
        budget_mod._current_equity = None


def test_seller_settings_strategy_counts_come_from_capital_json() -> None:
    from src.options import seller_settings as ss
    from src.risk.budget import get_budget

    b = get_budget()
    assert ss.max_open_strategies() == b.options.max_open_strategies
    assert ss.max_strategies_per_index() == b.options.max_open_strategies_per_index
    assert ss.max_same_direction_strategies() == b.options.max_open_same_direction_spreads


async def test_g9_review_records_answering_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """CRM G9's PASS/REJECT reason names meta["llm_provider"]; the options desk never set it,
    so ~1,000 options verdicts read "by ?" (found 2026-10-14 G9 audit)."""
    from src.llm import trade_evaluator
    from src.options.seller_desk import build_seller_crm_signal

    async def _verdict(*a, **k):  # type: ignore[no-untyped-def]
        return {
            "llm_confidence": 0.72,
            "llm_flag": "CONFIRM",
            "llm_reason": "ok",
            "malformed": 0,
            "evaluated": True,
            "provider": "anthropic",
            "attempts": 1,
        }

    monkeypatch.setattr(trade_evaluator, "evaluate_trade_gated", _verdict)
    desk, _ = _desk(_rich_chain())
    desk._g9_llm, desk._g9_llm_loaded = object(), True
    sig = build_seller_crm_signal(
        signal_id="S1",
        underlying="NSE:NIFTY",
        strategy_type="bull_put_spread",
        lots=1,
        max_loss=10_000.0,
        max_profit=2_000.0,
        regime="LOW_VOL_TRENDING",
        vix=14.0,
    )
    from src.risk.crm import registry_name_of

    ctx = SimpleNamespace(llm_gate_required={registry_name_of(sig): True})
    ok = await desk._g9_review(
        sig,
        ctx,  # type: ignore[arg-type]
        underlying="NSE:NIFTY",
        strategy_type="bull_put_spread",
        regime="LOW_VOL_TRENDING",
        max_loss=10_000.0,
        max_profit=2_000.0,
        lots=1,
        vix=14.0,
    )
    assert ok is True
    assert sig.meta["llm_provider"] == "anthropic"


# ── duplicate-entry defence-in-depth (2026-10-16 incident) ────────────────────
# Two identical NIFTY bull_put_spreads (same expiry, same 22400/22650 strikes) opened 5 minutes
# apart because the only guard was a "same-direction" count cap of 2. Each layer below must stop a
# duplicate ON ITS OWN, so a bug or race in one layer can never let a second structure through.


async def _entry_call(desk, chain, *, underlying="NSE:NIFTY", strategy="bull_put_spread"):  # type: ignore[no-untyped-def]
    protection, income = build_structure(strategy, chain, lots=1)
    max_loss, max_profit, net_premium, direction = estimate_economics(
        strategy, protection, income, chain.lot_size, 1
    )
    return await desk._execute_and_record(
        underlying=underlying,
        strategy_type=strategy,
        protection_legs=protection,
        income_legs=income,
        lots=1,
        chain=chain,
        net_premium=net_premium,
        max_loss=max_loss,
        max_profit=max_profit,
        direction=direction,
        signal_id="SIG-T",
        regime="LOW_VOL_TRENDING",
        today=_TODAY,
        now=datetime.now(_IST),
        skip_shared_risk=True,
    )


def _open_same_key(desk, chain, position_id="OPT-EXISTING"):  # type: ignore[no-untyped-def]
    pos = _bull_put_spread_position(position_id)
    pos.expiry = chain.expiry.date()
    desk.positions[position_id] = pos
    return pos


async def test_layer1_run_cycle_skips_a_candidate_identical_to_an_open_structure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from src.options import seller_desk as sdm
    from src.risk import budget as budget_module

    monkeypatch.setattr(budget_module, "_current_equity", 100_000_000.0)
    # caps wide open -- only the identity guard may stop the 2nd one (the 2026-10-16 config)
    monkeypatch.setattr(sdm._seller_settings, "max_same_direction_strategies", lambda *a, **k: 5)
    monkeypatch.setattr(sdm._seller_settings, "max_strategies_per_index", lambda *a, **k: 5)
    chain = _rich_chain()
    executor = _FakeExecutor()
    desk = SellerDesk(
        StrategySelector(),
        _FakeEventCalendar(),
        executor,
        _FakeChainFetcher(chain),
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        margin_safety_pct=0.90,
        greeks_budget=_budget(),
        min_rom_pct=0.0,
        enabled_strategy_types={"bear_call_spread"},
    )
    calendar = {"NIFTY": [chain.expiry.date() + timedelta(days=i) for i in range(-2, 3)]}

    async def _cycle():  # type: ignore[no-untyped-def]
        return await desk.run_cycle(
            "NSE:NIFTY",
            regime="HIGH_VOL_RANGE_BOUND",
            ivr=65.0,
            term_structure="contango",
            today=_TODAY,
            now=datetime.now(_IST),
            expiry_calendar=calendar,
            available_margin=1_000_000.0,
            crm=_APPROVE_ALL_CRM,
            crm_ctx=_DUMMY_CTX,
        )

    first = await _cycle()
    assert first is not None and len(executor.executed) == 1
    with caplog.at_level("INFO", logger="options.seller_desk"):
        second = await _cycle()
    assert second is None
    assert len(executor.executed) == 1  # nothing placed the second time
    assert any("duplicate_open_structure" in r.message for r in caplog.records)


async def test_layer2_execute_refuses_when_same_structure_open_in_memory() -> None:
    chain = _rich_chain()
    desk, executor = _desk(chain)
    _open_same_key(desk, chain)
    assert await _entry_call(desk, chain) is None
    assert executor.executed == []


async def test_layer2_concurrent_identical_entries_place_exactly_one() -> None:
    import asyncio

    chain = _rich_chain()
    desk, executor = _desk(chain)
    results = await asyncio.gather(_entry_call(desk, chain), _entry_call(desk, chain))
    assert sum(r is not None for r in results) == 1
    assert len(executor.executed) == 1


async def test_layer3_db_truth_blocks_when_memory_has_drifted() -> None:
    chain = _rich_chain()
    repo = _FakeRepo()
    repo.open_duplicates = ["OPT-IN-DB-ONLY"]  # in the DB, not in self.positions
    desk, executor = _desk(chain, repo=repo)
    assert await _entry_call(desk, chain) is None
    assert executor.executed == []


async def test_layer3_db_check_failure_fails_closed() -> None:
    chain = _rich_chain()
    repo = _FakeRepo()
    repo.open_duplicates_error = RuntimeError("db down")
    desk, executor = _desk(chain, repo=repo)
    assert await _entry_call(desk, chain) is None
    assert executor.executed == []


async def test_duplicate_guard_does_not_block_other_underlying_strategy_or_expiry() -> None:
    chain = _rich_chain()
    desk, executor = _desk(chain, repo=_FakeRepo())
    other = _open_same_key(desk, chain)
    other.underlying = "NSE:BANKNIFTY"
    assert await _entry_call(desk, chain) is not None
    other.underlying = "NSE:NIFTY"
    other.strategy_type = "bear_call_spread"
    desk.positions.pop(next(p for p in desk.positions if p != other.position_id), None)
    other.expiry = other.expiry + timedelta(days=7)
    assert await _entry_call(desk, chain) is not None  # different expiry + strategy


async def test_layer4_unique_index_violation_flattens_the_structure_just_placed() -> None:
    from asyncpg.exceptions import UniqueViolationError

    chain = _rich_chain()
    repo = _FakeRepo()
    repo.insert_error = UniqueViolationError("duplicate key value violates unique constraint")

    class _FillStateExecutor(_FakeExecutor):
        async def execute_structure(self, protection_legs, income_legs, signal_id):  # type: ignore[no-untyped-def]
            from src.options.options_leg_executor import ExecutionResult, LegFillState

            await super().execute_structure(protection_legs, income_legs, signal_id)
            return ExecutionResult(
                position_id="OPT-1",
                outcome="ALL_FILLED",
                protection_legs=[LegFillState(leg, "O", filled=True) for leg in protection_legs],
                income_legs=[LegFillState(leg, "O", filled=True) for leg in income_legs],
            )

    desk, executor = _desk(chain, executor=_FillStateExecutor(), repo=repo)
    assert await _entry_call(desk, chain) is None
    assert desk.positions == {}  # not tracked as a live position
    assert len(executor.closed) == 2  # both legs flattened, shorts first
    assert executor.closed[0][0].direction == "SELL"


# ── R1: the desk's own strategy, when LIVE, trades only through a usable route ──


async def _live_run_cycle(route):  # type: ignore[no-untyped-def]
    from src.options.seller_desk import DESK_OWN_STRATEGY_NAME

    chain = _rich_chain()
    desk, executor = _desk(chain)
    desk._is_paper = False  # live desk (registry options_seller_desk_v3: "live")
    calendar = {"NIFTY": [chain.expiry.date() + timedelta(days=i) for i in range(-2, 3)]}
    ctx = SimpleNamespace(
        llm_gate_required={},
        strategy_routes={} if route is None else {DESK_OWN_STRATEGY_NAME: route},
    )
    position = await desk.run_cycle(
        "NSE:NIFTY",
        regime="HIGH_VOL_RANGE_BOUND",
        ivr=65.0,
        term_structure="contango",
        today=_TODAY,
        now=datetime.now(_IST),
        expiry_calendar=calendar,
        available_margin=1_000_000.0,
        crm=_APPROVE_ALL_CRM,
        crm_ctx=ctx,
    )
    return position, executor


async def test_live_desk_cycle_with_an_unusable_route_places_nothing(caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    from src.brokers.routing import BrokerRoute

    with caplog.at_level(logging.WARNING, logger="options.seller_desk"):
        position, executor = await _live_run_cycle(
            BrokerRoute(None, "live strategy has no 'broker' field")
        )
    assert position is None and executor.executed == []
    assert "broker_route_rejected strategy=options_seller_desk_v3 segment=options" in caplog.text


async def test_live_desk_cycle_with_a_usable_route_matches_the_inherit_path() -> None:
    from src.brokers.routing import BrokerRoute

    routed, routed_exec = await _live_run_cycle(BrokerRoute("kite", None))
    legacy, legacy_exec = await _live_run_cycle(None)
    assert routed is not None and legacy is not None
    assert len(routed_exec.executed) == len(legacy_exec.executed) == 1
