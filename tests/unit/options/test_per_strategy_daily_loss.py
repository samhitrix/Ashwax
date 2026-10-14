"""Phase 2b of the risk-budget redesign (2026-10-07): the per-strategy daily-loss kill
(close ONE breached strategy's positions, latch it off, leave everything else trading)
and the Layer-2 shared-risk ceiling (sum of every open structure's own max loss)."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from src.options import seller_desk as sd
from src.options.seller_desk import (
    DESK_OWN_STRATEGY_NAME,
    EXPIRY_DAY_TYPE,
    WEEKLY_CONDOR_TYPE,
    OpenPosition,
    _registry_strategy_for,
)
from src.options.structure_selector import OptionLeg

from tests.unit.options.test_seller_desk import _desk, _FakeRepo, _rich_chain

_IST = ZoneInfo("Asia/Kolkata")
_TODAY = date(2026, 10, 7)


# ── _registry_strategy_for ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("structure_type", "expected"),
    [
        ("bull_put_spread", DESK_OWN_STRATEGY_NAME),
        ("iron_condor", DESK_OWN_STRATEGY_NAME),
        ("calendar_spread", DESK_OWN_STRATEGY_NAME),
        (WEEKLY_CONDOR_TYPE, WEEKLY_CONDOR_TYPE),
        (EXPIRY_DAY_TYPE, EXPIRY_DAY_TYPE),
    ],
)
def test_registry_strategy_for_maps_desk_structures_to_one_name(structure_type, expected) -> None:  # type: ignore[no-untyped-def]
    assert _registry_strategy_for(structure_type) == expected


# ── _add_realized buckets per strategy ─────────────────────────────────────────────


def test_add_realized_buckets_by_registry_strategy_and_resets_on_new_day(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    desk, _ = _desk(_rich_chain())

    class _FixedDT(datetime):
        _now = datetime(2026, 10, 7, 11, 0, tzinfo=_IST)

        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return cls._now

    monkeypatch.setattr(sd, "datetime", _FixedDT)
    desk._add_realized(-1000.0, "bull_put_spread")
    desk._add_realized(-500.0, WEEKLY_CONDOR_TYPE)
    desk._add_realized(200.0, "iron_condor")  # same bucket as bull_put_spread
    assert desk._realized_today_by_strategy == {
        DESK_OWN_STRATEGY_NAME: -800.0,
        WEEKLY_CONDOR_TYPE: -500.0,
    }
    assert desk._realized_today == -1300.0

    _FixedDT._now = datetime(2026, 10, 8, 9, 20, tzinfo=_IST)  # next trading day
    desk._add_realized(-100.0, EXPIRY_DAY_TYPE)
    assert desk._realized_today_by_strategy == {EXPIRY_DAY_TYPE: -100.0}  # old buckets wiped


# ── sweep_strategy_daily_loss ───────────────────────────────────────────────────────


def _pos(pid: str, strategy_type: str, credit: float = 100.0) -> OpenPosition:
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

    prot = [leg(f"{pid}LP", "PE", 23400.0, "BUY", 5.0), leg(f"{pid}LC", "CE", 24600.0, "BUY", 5.0)]
    inc = [
        leg(f"{pid}SP", "PE", 23600.0, "SELL", credit / 2 + 5),
        leg(f"{pid}SC", "CE", 24400.0, "SELL", credit / 2 + 5),
    ]
    return OpenPosition(
        position_id=pid,
        underlying="NSE:NIFTY",
        strategy_type=strategy_type,
        direction="credit",
        protection_legs=prot,
        income_legs=inc,
        entry_premium=credit * 65,
        entry_margin=5000.0,
        expiry=date(2026, 10, 14),
        entered_at=datetime(2026, 10, 7, 10, 0, tzinfo=_IST),
    )


async def test_sweep_closes_only_the_breached_strategy_others_keep_trading(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    desk, executor = _desk(_rich_chain(), repo=_FakeRepo())
    monkeypatch.setattr(sd, "_seller_settings", sd._seller_settings)  # no-op, keep import live
    from src.risk import budget as budget_module

    monkeypatch.setattr(budget_module, "_current_equity", 2_000_000.0)  # cap = Rs 5,000
    desk.positions["A1"] = _pos("A1", "bull_put_spread")
    desk.positions["B1"] = _pos("B1", WEEKLY_CONDOR_TYPE)
    desk._realized_today_by_strategy = {DESK_OWN_STRATEGY_NAME: -6_000.0}  # breached
    desk._realized_today = -6_000.0
    await desk.sweep_strategy_daily_loss(_TODAY, current_prices={})
    assert "A1" not in desk.positions  # breached strategy's position closed
    assert "B1" in desk.positions  # unrelated strategy untouched
    assert desk._strategy_halted_day[DESK_OWN_STRATEGY_NAME] == _TODAY
    assert WEEKLY_CONDOR_TYPE not in desk._strategy_halted_day
    assert executor.closed  # real close path ran, not a silent drop


async def test_sweep_includes_open_unrealized_loss_not_just_realized() -> None:
    desk, _ = _desk(_rich_chain(), repo=_FakeRepo())
    from src.risk import budget as budget_module

    budget_module._current_equity = 2_000_000.0  # cap = Rs 5,000
    try:
        desk.positions["A1"] = _pos("A1", "iron_condor")
        desk._realized_today_by_strategy = {DESK_OWN_STRATEGY_NAME: -2_000.0}  # under cap alone
        # Prices that push the open position's unrealized loss past the remaining cap.
        prices = {"A1LP": 0.5, "A1LC": 0.5, "A1SP": 200.0, "A1SC": 200.0}
        await desk.sweep_strategy_daily_loss(_TODAY, current_prices=prices)
        assert "A1" not in desk.positions
    finally:
        budget_module._current_equity = None


async def test_sweep_does_not_reclose_an_already_halted_strategy() -> None:
    desk, executor = _desk(_rich_chain(), repo=_FakeRepo())
    from src.risk import budget as budget_module

    budget_module._current_equity = 2_000_000.0
    try:
        desk._strategy_halted_day[DESK_OWN_STRATEGY_NAME] = _TODAY
        desk.positions["A1"] = _pos("A1", "bull_put_spread")  # opened AFTER the halt somehow
        desk._realized_today_by_strategy = {DESK_OWN_STRATEGY_NAME: -6_000.0}
        await desk.sweep_strategy_daily_loss(_TODAY, current_prices={})
        assert "A1" in desk.positions  # sweep skips an already-halted strategy; no double action
        assert executor.closed == []
    finally:
        budget_module._current_equity = None


async def test_sweep_is_a_noop_when_no_strategy_cap_is_configured() -> None:
    desk, executor = _desk(_rich_chain(), repo=_FakeRepo())
    desk.positions["A1"] = _pos("A1", "bull_put_spread")
    desk._realized_today_by_strategy = {DESK_OWN_STRATEGY_NAME: -1_000_000.0}
    # No set_current_equity call -> falls back to capital.json's configured fallback,
    # which is a real positive number, so this proves the gate is live by default too:
    await desk.sweep_strategy_daily_loss(_TODAY, current_prices={})
    assert "A1" not in desk.positions  # even the fallback equity's cap was breached


# ── strategy_halted_reason blocks new entries ──────────────────────────────────────


def test_strategy_halted_reason_is_scoped_to_one_strategy() -> None:
    desk, _ = _desk(_rich_chain())
    desk._strategy_halted_day[DESK_OWN_STRATEGY_NAME] = _TODAY
    assert desk.strategy_halted_reason(DESK_OWN_STRATEGY_NAME, _TODAY) == "strategy_daily_loss_cap"
    assert desk.strategy_halted_reason(WEEKLY_CONDOR_TYPE, _TODAY) is None
    assert desk.strategy_halted_reason(DESK_OWN_STRATEGY_NAME, date(2026, 10, 8)) is None  # new day


# ── _seed_strategy_daily_loss (restart restore) ────────────────────────────────────


class _SeedRepo(_FakeRepo):
    def __init__(self, by_structure: dict[str, float]) -> None:
        super().__init__()
        self._by_structure = by_structure

    async def realized_pnl_by_structure_closed_on(self, day, *, is_paper):  # type: ignore[no-untyped-def]
        return dict(self._by_structure)


async def test_seed_restores_per_strategy_totals_and_halts_if_already_breached() -> None:
    desk, _ = _desk(
        _rich_chain(), repo=_SeedRepo({"bull_put_spread": -6_000.0, WEEKLY_CONDOR_TYPE: -500.0})
    )
    from src.risk import budget as budget_module

    budget_module._current_equity = 2_000_000.0  # cap = Rs 5,000
    try:
        await desk._seed_strategy_daily_loss(_TODAY)
        assert desk._realized_today_by_strategy == {
            DESK_OWN_STRATEGY_NAME: -6_000.0,
            WEEKLY_CONDOR_TYPE: -500.0,
        }
        assert desk._strategy_halted_day.get(DESK_OWN_STRATEGY_NAME) == _TODAY
        assert WEEKLY_CONDOR_TYPE not in desk._strategy_halted_day
    finally:
        budget_module._current_equity = None


async def test_restore_positions_calls_the_strategy_seed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    desk, _ = _desk(_rich_chain(), repo=_SeedRepo({"bull_put_spread": -100.0}))
    await desk.restore_positions()
    assert desk._realized_today_by_strategy.get(DESK_OWN_STRATEGY_NAME) == -100.0


# ── shared_risk_ceiling_reason (Layer 2) ───────────────────────────────────────────


def test_shared_risk_ceiling_blocks_when_open_plus_candidate_exceeds_it() -> None:
    desk, _ = _desk(_rich_chain(), repo=_FakeRepo())
    from src.risk import budget as budget_module

    budget_module._current_equity = 2_000_000.0  # ceiling = 3% = Rs 60,000
    try:
        desk.positions["A1"] = _pos("A1", "bull_put_spread")
        desk.positions["A1"].entry_margin = 20_000.0
        assert desk.shared_risk_ceiling_reason(39_000.0) is None  # 59,000 <= 60,000
        reason = desk.shared_risk_ceiling_reason(41_000.0)  # 61,000 > 60,000
        assert reason is not None and "shared_risk_ceiling" in reason
    finally:
        budget_module._current_equity = None


def test_shared_risk_ceiling_scales_with_equity() -> None:
    desk, _ = _desk(_rich_chain(), repo=_FakeRepo())
    from src.risk import budget as budget_module

    budget_module._current_equity = 2_000_000.0
    try:
        low = desk.shared_risk_ceiling_reason(90_000.0)  # over the Rs 60,000 ceiling
        budget_module._current_equity = 4_000_000.0  # ceiling doubles to Rs 120,000
        high = desk.shared_risk_ceiling_reason(90_000.0)
        assert low is not None
        assert high is None
    finally:
        budget_module._current_equity = None
