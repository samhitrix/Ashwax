"""I2: with system.per_broker_equity on, the options desk sizes and caps a LIVE entry or
position on ITS broker's own equity (never the primary's); unknown equity refuses the entry.
Flag off: the one legacy equity figure, exactly as before."""

from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from src.brokers.routing import BrokerRoute
from src.options.short_premium import size_lots
from src.risk import budget as budget_module
from src.risk.budget import get_budget

from tests.unit.options.test_desk_broker_executors import _mdesk, _routed
from tests.unit.options.test_expiry_day import _EXP, _run
from tests.unit.options.test_expiry_day import _settings as _hosted_settings  # noqa: F401
from tests.unit.options.test_seller_desk import _IST, _TODAY, _iron_condor_position

_KITE, _ANGEL = 2_000_000.0, 100_000.0


@pytest.fixture(autouse=True)
def _equities():
    budget_module.set_current_equity(_KITE)  # the legacy (primary) figure
    budget_module.set_current_equity(_KITE, key="live:kite")
    budget_module.set_current_equity(_ANGEL, key="live:angel_one")
    yield
    budget_module._current_equity = None
    budget_module._equity_by_key.clear()


def _desk(flag: bool) -> tuple[Any, Any, Any, Any]:
    desk, paper, kite, ang = _mdesk(is_paper=False)
    desk.per_broker_equity = flag
    return desk, paper, kite, ang


def _angel_pos(pid: str = "A") -> Any:
    pos = _iron_condor_position(pid)
    pos.exec_broker = "angel_one"
    return pos


def test_shared_risk_ceiling_uses_the_brokers_equity_and_its_own_book() -> None:
    desk, *_ = _desk(True)
    opt = get_budget().options
    angel_ceiling = opt.max_share_of_shared_risk_rupees(_ANGEL)
    kite_ceiling = opt.max_share_of_shared_risk_rupees(_KITE)
    candidate = (angel_ceiling + kite_ceiling) / 2  # fits kite's ceiling, not angel's
    assert desk.shared_risk_ceiling_reason(candidate, equity_key="live:angel_one") is not None
    assert desk.shared_risk_ceiling_reason(candidate, equity_key="live:kite") is None
    # An open angel structure does not eat kite's headroom.
    big = _angel_pos()
    big.entry_margin = kite_ceiling * 0.9
    desk.positions["A"] = big
    assert desk.shared_risk_ceiling_reason(candidate * 0.1, equity_key="live:kite") is None
    # Unknown broker equity refuses (never a 0 ceiling read as "no cap").
    assert "broker_equity_unknown" in (
        desk.shared_risk_ceiling_reason(1.0, equity_key="live:flattrade") or ""
    )


def test_flag_off_keys_nothing_and_uses_the_legacy_figures() -> None:
    desk, *_ = _desk(False)
    assert desk._equity_key(False, "angel_one") is None
    assert desk._risk_pct_for(None) == desk._risk_per_trade_pct
    assert desk._pos_equity_key(_angel_pos()) is None


def test_lot_budget_follows_the_routed_broker() -> None:
    desk, *_ = _desk(True)
    opt = get_budget().options
    pct_angel = desk._risk_pct_for(desk._equity_key(False, "angel_one"))
    assert pct_angel == pytest.approx(
        opt.max_structure_loss_per_strategy_rupees(_ANGEL) / desk._options_capital * 100.0
    )
    assert pct_angel < desk._risk_pct_for("live:kite")
    assert desk._risk_pct_for("live:flattrade") == 0.0  # unknown: sizes 0 lots
    assert desk._equity_key(True, "angel_one") is None  # paper: legacy


def test_short_premium_lots_use_the_brokers_daily_cap() -> None:
    from src.options.structure_selector import OptionLeg

    cfg = SimpleNamespace(
        lots=0, max_lots=100, risk_fraction_of_daily_cap=1.0, sl_pct_ce=100.0, sl_pct_pe=100.0
    )
    leg = OptionLeg(
        tradingsymbol="NFO:NIFTY24000CE",
        instrument_type="CE",
        strike=24000.0,
        direction="SELL",
        lots=1,
        lot_size=75,
        premium=10.0,
        delta=0.2,
    )
    legacy, _ = size_lots(cfg, [leg])
    kite, _ = size_lots(cfg, [leg], "live:kite")
    angel, _ = size_lots(cfg, [leg], "live:angel_one")
    assert legacy == kite and angel < kite
    assert size_lots(cfg, [leg], "live:flattrade")[0] == 0


@pytest.mark.parametrize("flag", [True, False])
async def test_strategy_daily_loss_caps_a_live_position_on_its_brokers_equity(flag: bool) -> None:
    desk, _paper, kite, ang = _desk(flag)
    opt = get_budget().options
    cap_angel = opt.max_loss_per_day_per_strategy_rupees(_ANGEL)
    cap_primary = opt.max_loss_per_day_per_strategy_rupees(_KITE)
    assert cap_angel < cap_primary
    loss = (cap_angel + cap_primary) / 2
    pos = _angel_pos()
    desk.positions["A"] = pos
    # Both shorts (qty 25, entry 50) marked up so the structure is down `loss`.
    px = 50.0 + loss / 50.0
    prices = {leg.tradingsymbol: px for leg in pos.income_legs}
    prices |= {leg.tradingsymbol: 50.0 for leg in pos.protection_legs}
    await desk.sweep_strategy_daily_loss(_TODAY, prices)
    if flag:  # past angel's own cap: closed on angel, never on kite
        assert "A" not in desk.positions and ang.closed and kite.closed == []
    else:  # legacy: measured against the primary's equity, still under the cap
        assert "A" in desk.positions and ang.closed == []


async def test_desk_breaker_halts_only_the_broker_that_breached(caplog) -> None:  # type: ignore[no-untyped-def]
    # Angel large enough to size the expiry-day fly; the breach is on angel only (the legacy
    # combined breaker, on the primary's larger figure, does not trip).
    budget_module.set_current_equity(10_000_000.0)
    budget_module.set_current_equity(2_000_000.0, key="live:angel_one")
    now = datetime.now(_IST)
    desk, _paper, kite, ang = _desk(True)
    halt_pct = get_budget().options.max_loss_per_day_all_strategies_pct
    desk._add_realized(-(2_000_000.0 * halt_pct / 100.0) * 1.5, "iron_condor", "live:angel_one")
    await desk.sweep_portfolio_risk(today=now.date(), current_prices={}, chains={}, now=now)
    assert desk._entries_halted_keys == {"live:angel_one": now.date()}
    assert desk._entries_halted_day != now.date()  # the combined (primary) breaker did not trip

    # The expiry-day fly routed to angel is refused at execution; routed to kite it trades.
    desk._entries_halted_keys["live:angel_one"] = _EXP
    with caplog.at_level(logging.INFO, logger="options.seller_desk"):
        assert await _run(desk, ctx=_routed(BrokerRoute("angel_one", None))) is None
    assert ang.executed == [] and "broker_daily_loss_halt" in caplog.text
    assert await _run(desk, ctx=_routed(BrokerRoute("kite", None))) is not None
    assert len(kite.executed) == 1
