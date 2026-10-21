"""Phase 2 task 7: the options seller desk trades, closes and rolls each position through the
executor of the broker that position belongs to, and positions remember their broker.

Fail closed everywhere: a live strategy or position whose broker has no executor places
nothing and is never rerouted to another broker's (or the paper) executor."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from src.brokers.routing import BrokerRoute
from src.options import seller_desk as sd
from src.options.greeks_budget import GreeksBudget
from src.options.seller_desk import EXPIRY_DAY_TYPE, SellerDesk
from src.options.strategy_selector import StrategySelector

from tests.unit.options.hosted_helpers import _Repo
from tests.unit.options.test_expiry_day import _NAME, _ctx, _run
from tests.unit.options.test_expiry_day import _chain as _hosted_chain
from tests.unit.options.test_expiry_day import _settings as _hosted_settings  # noqa: F401
from tests.unit.options.test_seller_desk import (
    _IST,
    _TODAY,
    _adjustment_chain,
    _budget,
    _FakeChainFetcher,
    _FakeEventCalendar,
    _FakeExecutor,
    _FakeRepo,
    _iron_condor_position,
    _rich_chain,
)


def _mdesk(chain: Any = None, *, is_paper: bool = True, repo: Any = None, angel: bool = True):  # type: ignore[no-untyped-def]
    """Desk wired like the orchestrator: one executor per stack, default live broker kite."""
    paper, kite, ang = _FakeExecutor(), _FakeExecutor(), _FakeExecutor()
    fetcher = _FakeChainFetcher(chain or _hosted_chain())
    if chain is None:  # the hosted expiry-day entry reads today's chain via fetch_for_expiry
        fetcher.expiry_chains[fetcher._chain.expiry.date()] = fetcher._chain
    executors: dict[str, Any] = {"paper": paper, "live:kite": kite}
    if angel:
        executors["live:angel_one"] = ang
    desk = SellerDesk(
        StrategySelector(),
        _FakeEventCalendar(),
        paper if is_paper else kite,
        fetcher,
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        margin_safety_pct=0.90,
        greeks_budget=_budget(),
        min_rom_pct=0.0,
        repo=repo,
        is_paper=is_paper,
        alt_executor=kite if is_paper else paper,
        executors=executors,
    )
    desk.live_broker_name = "kite"
    desk._greeks_budget = GreeksBudget(vega=1e9, delta=1e9, theta=1e9, gamma=1e9)
    return desk, paper, kite, ang


def _routed(route: BrokerRoute) -> SimpleNamespace:
    ctx = _ctx("live")
    ctx.strategy_routes = {_NAME: route}
    return ctx


# ── executor lookup ──────────────────────────────────────────────────────────


def test_executor_lookup_by_stack_key() -> None:
    desk, paper, kite, ang = _mdesk()
    assert desk._executor_for(True) is paper
    assert desk._executor_for(True, "angel_one") is paper  # paper never consults a broker
    assert desk._executor_for(False) is kite  # no broker = the primary (legacy) live broker
    assert desk._executor_for(False, "kite") is kite
    assert desk._executor_for(False, "angel_one") is ang
    assert desk._executor_for(False, "flattrade") is None  # fail closed, never another broker


def test_primary_follows_the_legacy_broker_when_it_is_not_kite() -> None:
    desk, _paper, _kite, ang = _mdesk()
    desk.live_broker_name = "angel_one"
    assert desk._executor_for(False) is ang


def test_missing_paper_executor_is_none() -> None:
    desk, *_ = _mdesk()
    desk._executors.pop("paper")
    assert desk._executor_for(True) is None


def test_legacy_desk_without_a_map_behaves_as_before() -> None:
    main, alt = _FakeExecutor(), _FakeExecutor()
    desk = SellerDesk(
        StrategySelector(),
        _FakeEventCalendar(),
        main,
        _FakeChainFetcher(_rich_chain()),
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        margin_safety_pct=0.90,
        greeks_budget=_budget(),
        min_rom_pct=0.0,
        is_paper=True,
        alt_executor=alt,
    )
    assert desk._executor_for(True) is main
    assert desk._executor_for(False) is alt
    assert desk._executor_for(False, "kite") is alt  # unknown live broker = legacy kite
    assert desk._executor_for(False, "angel_one") is None
    desk.live_broker_name = "angel_one"
    assert desk._executor_for(False, "angel_one") is alt
    assert desk._executor_for(False, "kite") is None


# ── entry, close, single-leg close and roll follow the position's broker ─────


async def test_live_hosted_routed_to_angel_opens_on_angel_and_remembers_it() -> None:
    repo = _Repo()
    desk, paper, kite, ang = _mdesk(repo=repo)  # desk default: paper, live default: kite
    pos = await _run(desk, ctx=_routed(BrokerRoute("angel_one", None)))
    assert pos is not None and pos.strategy_type == EXPIRY_DAY_TYPE
    assert len(ang.executed) == 1 and kite.executed == [] and paper.executed == []
    assert pos.exec_broker == "angel_one" and pos.is_paper is False
    assert repo.inserted[0]["exec_broker"] == "angel_one"
    assert repo.inserted[0]["is_paper"] is False

    await desk._close_position(pos.position_id, reason="test_close")
    assert ang.closed and kite.closed == [] and paper.closed == []
    assert repo.closed[0]["broker"] == "angel_one"


async def test_live_hosted_without_route_uses_the_primary_and_stamps_it() -> None:
    repo = _Repo()
    desk, paper, kite, ang = _mdesk(repo=repo)
    pos = await _run(desk, ctx=_ctx("live"))
    assert pos is not None and len(kite.executed) == 1 and ang.executed == []
    assert pos.exec_broker == "kite" and repo.inserted[0]["exec_broker"] == "kite"


async def test_paper_hosted_stamps_no_broker() -> None:
    repo = _Repo()
    desk, paper, kite, ang = _mdesk(repo=repo)
    pos = await _run(desk, ctx=_ctx("paper"))
    assert pos is not None and len(paper.executed) == 1
    assert pos.exec_broker is None and repo.inserted[0]["exec_broker"] is None


async def test_live_hosted_routed_to_a_broker_without_executor_places_nothing(caplog) -> None:  # type: ignore[no-untyped-def]
    desk, paper, kite, ang = _mdesk(repo=_Repo(), angel=False)
    with caplog.at_level(logging.WARNING, logger="options.seller_desk"):
        assert await _run(desk, ctx=_routed(BrokerRoute("angel_one", None))) is None
    assert paper.executed == [] and kite.executed == [] and ang.executed == []
    assert (
        f"broker_route_rejected strategy={_NAME} segment=options "
        "reason=no live executor for broker angel_one" in caplog.text
    )


async def test_angel_position_is_rolled_and_leg_closed_via_angel(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        sd._seller_settings, "adjustment_settings", lambda *a, **kw: {"execute_rolls": True}
    )
    chain = _adjustment_chain()
    desk, paper, kite, ang = _mdesk(chain, is_paper=False)  # live desk, default broker kite
    pos = _iron_condor_position("OPT-A")
    pos.exec_broker = "angel_one"
    desk.positions["OPT-A"] = pos
    await desk.sweep_position_manager(
        today=_TODAY,
        blackout_active=False,
        current_prices={},
        current_ivs={},
        chains={"NSE:NIFTY": chain},
    )
    assert len(ang.rolled) == 1 and kite.rolled == [] and paper.rolled == []

    leg = pos.income_legs[0]
    ok = await desk.close_single_leg(pos, leg, ltp=10.0, now=datetime.now(_IST), reason="sl")
    assert ok and ang.closed and kite.closed == [] and paper.closed == []


async def test_paper_position_rolls_on_the_paper_executor_even_on_a_live_desk(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The latent bug: roll_leg used the desk's default executor regardless of the position.
    monkeypatch.setattr(
        sd._seller_settings, "adjustment_settings", lambda *a, **kw: {"execute_rolls": True}
    )
    chain = _adjustment_chain()
    desk, paper, kite, ang = _mdesk(chain, is_paper=False)
    pos = _iron_condor_position("OPT-P")
    pos.is_paper = True
    desk.positions["OPT-P"] = pos
    await desk.sweep_position_manager(
        today=_TODAY,
        blackout_active=False,
        current_prices={},
        current_ivs={},
        chains={"NSE:NIFTY": chain},
    )
    assert len(paper.rolled) == 1 and kite.rolled == [] and ang.rolled == []


async def test_position_whose_broker_has_no_executor_is_never_closed_elsewhere(caplog) -> None:  # type: ignore[no-untyped-def]
    desk, paper, kite, ang = _mdesk(is_paper=False, repo=_FakeRepo())
    pos = _iron_condor_position("OPT-F")
    pos.exec_broker = "flattrade"
    desk.positions["OPT-F"] = pos
    with caplog.at_level(logging.CRITICAL, logger="options.seller_desk"):
        await desk._close_position("OPT-F", reason="test_close")
        ok = await desk.close_single_leg(
            pos, pos.income_legs[0], ltp=1.0, now=datetime.now(_IST), reason="sl"
        )
    assert not ok
    assert paper.closed == [] and kite.closed == [] and ang.closed == []
    assert "OPT-F" in desk.positions  # kept, so a later attempt can still close it
    assert "seller_desk_no_executor_for_position" in caplog.text


# ── restore after restart ────────────────────────────────────────────────────


def _row(
    pid: str, stype: str, is_paper: bool, exec_broker: str | None, expiry: date | None = None
) -> dict[str, Any]:
    from src.db.repositories.options import _serialize_leg

    pos = _iron_condor_position(pid)
    return {
        "position_id": pid,
        "underlying": "NSE:NIFTY",
        "strategy_type": stype,
        "legs": {
            "protection": [_serialize_leg(leg) for leg in pos.protection_legs],
            "income": [_serialize_leg(leg) for leg in pos.income_legs],
        },
        # restore compares expiry with the real date: default = not expired
        "expiry": expiry or (datetime.now(_IST).date() + timedelta(days=20)),
        "net_credit": 100.0,
        "entry_margin": 1000.0,
        "opened_at": datetime.now(_IST),
        "is_paper": is_paper,
        "exec_broker": exec_broker,
    }


def _capture_alerts(monkeypatch) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    alerts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.infrastructure.operator_alerts.raise_alert",
        lambda msg, **kw: alerts.append({"msg": msg, **kw}) or True,
    )
    return alerts


async def test_restore_reattaches_every_brokers_positions_and_keeps_paper_paper(
    monkeypatch, caplog
) -> None:  # type: ignore[no-untyped-def]
    alerts = _capture_alerts(monkeypatch)
    repo = _FakeRepo(
        open_rows=[
            _row("P", "iron_condor", True, None),  # paper desk-own
            _row("K", "iron_condor", False, None),  # live desk-own on a paper desk: unaudited
            _row("A", EXPIRY_DAY_TYPE, False, "angel_one"),  # hosted: managed as before
            _row("F", "iron_condor", False, "flattrade"),  # unaudited AND no executor
        ]
    )
    desk, paper, kite, ang = _mdesk(_adjustment_chain(), is_paper=True, repo=repo)
    with caplog.at_level(logging.CRITICAL, logger="options.seller_desk"):
        assert await desk.restore_positions() == 4
    p, h = desk.positions, desk.held_positions
    assert set(p) == {"P", "A"} and set(h) == {"K", "F"}
    assert desk._pos_is_paper(p["P"]) and p["P"].exec_broker is None and not p["P"].hold
    assert not desk._pos_is_paper(p["A"]) and p["A"].exec_broker == "angel_one"
    assert not p["A"].hold
    assert h["K"].exec_broker == "kite" and h["K"].hold_reason == "restored_unaudited_live_row"
    assert h["F"].exec_broker == "flattrade" and h["F"].broker_unwired and h["F"].hold
    assert "seller_desk_position_restored_on_hold" in caplog.text
    assert len(alerts) == 1 and alerts[0]["tier"] == "CRITICAL"
    assert "MONITORING ONLY" in alerts[0]["msg"]
    assert "K" in alerts[0]["msg"] and "flattrade" in alerts[0]["msg"]

    await desk._close_position("A", reason="t")
    await desk._close_position("P", reason="t")
    await desk._close_position("K", reason="t")  # not an operator close: held, untouched
    assert {s for _leg, s in ang.closed} == {"A"}
    assert {s for _leg, s in paper.closed} == {"P"}
    assert kite.closed == [] and "K" in desk.held_positions


def _op_close(monkeypatch, ids: list[str]) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(sd._seller_settings, "operator_close_position_ids", lambda *a, **k: ids)


async def _full_sweep(desk: SellerDesk, today: date) -> None:
    now = datetime.combine(today, datetime.min.time(), tzinfo=_IST).replace(hour=15, minute=25)
    chains = {"NSE:NIFTY": _adjustment_chain()}
    await desk.sweep_expiry_guardian(today, now)
    await desk.sweep_position_manager(
        today=today, blackout_active=False, current_prices={}, current_ivs={}, chains=chains
    )
    await desk.sweep_margin_monitor({pid: 1e12 for pid in desk.held_positions})
    await desk.sweep_portfolio_risk(today=today, current_prices={}, chains=chains, now=now)
    await desk.sweep_strategy_daily_loss(today, {})


def _calls(*exes: _FakeExecutor) -> int:
    return sum(len(e.executed) + len(e.closed) + len(e.rolled) for e in exes)


async def test_unaudited_live_row_on_a_paper_desk_is_held_through_a_full_sweep(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    alerts = _capture_alerts(monkeypatch)
    _op_close(monkeypatch, [])
    monkeypatch.setattr(
        sd._seller_settings, "adjustment_settings", lambda *a, **kw: {"execute_rolls": True}
    )
    today = datetime.now(_IST).date()
    # expiring tomorrow: the expiry guardian WOULD force-close a managed position
    rows = [_row("K", "iron_condor", False, "kite", expiry=today + timedelta(days=1))]
    desk, paper, kite, ang = _mdesk(_adjustment_chain(), is_paper=True, repo=_FakeRepo(rows))
    await desk.restore_positions()
    for _ in range(2):
        await _full_sweep(desk, today)
    assert _calls(paper, kite, ang) == 0
    assert "K" in desk.held_positions and "K" not in desk.positions
    assert desk.available_margin() == desk._options_capital  # no paper-sleeve consumption
    assert desk.paper_sleeve_margin() == desk._options_capital
    assert desk.portfolio_greeks().net_delta == 0.0
    assert len(alerts) == 1


async def test_expired_live_row_is_held_even_when_the_old_filter_kept_it(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _capture_alerts(monkeypatch)
    today = datetime.now(_IST).date()
    rows = [
        _row("X", "iron_condor", False, "kite", expiry=today - timedelta(days=1)),
        _row("L", "iron_condor", False, "kite"),  # old filter kept it: unchanged, managed
    ]
    desk, *_ = _mdesk(_adjustment_chain(), is_paper=False, repo=_FakeRepo(rows))
    await desk.restore_positions()
    assert desk.held_positions["X"].hold_reason == "expired_live_row"
    assert "L" in desk.positions and not desk.positions["L"].hold


async def test_operator_close_of_a_held_row_uses_its_own_brokers_executor(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _capture_alerts(monkeypatch)
    repo = _FakeRepo([_row("H", "iron_condor", False, "angel_one")])
    desk, paper, kite, ang = _mdesk(_adjustment_chain(), is_paper=True, repo=repo)
    await desk.restore_positions()
    assert "H" in desk.held_positions
    _op_close(monkeypatch, ["H"])
    await desk.sweep_position_manager(
        today=_TODAY, blackout_active=False, current_prices={}, current_ivs={}, chains={}
    )
    assert {s for _leg, s in ang.closed} == {"H"} and _calls(paper, kite) == 0
    assert "H" not in desk.held_positions and "H" not in desk.positions
    assert repo.closed[0]["broker"] == "angel_one"
    assert desk._realized_today == 0.0  # a held live row never feeds the desk's aggregates


async def test_held_row_without_executor_stays_flagged_without_reroute_or_spam(
    monkeypatch, caplog
) -> None:  # type: ignore[no-untyped-def]
    alerts = _capture_alerts(monkeypatch)
    repo = _FakeRepo([_row("F", "iron_condor", False, "flattrade")])
    desk, paper, kite, ang = _mdesk(_adjustment_chain(), is_paper=False, repo=repo)
    await desk.restore_positions()
    assert desk.held_positions["F"].hold_reason == "broker_unwired"
    _op_close(monkeypatch, ["F"])
    with caplog.at_level(logging.CRITICAL, logger="options.seller_desk"):
        for _ in range(3):
            await _full_sweep(desk, datetime.now(_IST).date())
    assert _calls(paper, kite, ang) == 0
    assert "F" in desk.held_positions and desk.held_positions["F"].broker_unwired
    assert caplog.text.count("seller_desk_no_executor_for_position") == 1
    assert len(alerts) == 1


class _RaisingExecutor(_FakeExecutor):
    async def close_leg(self, leg, signal_id):  # type: ignore[no-untyped-def]
        raise RuntimeError("broker down")


async def test_one_failing_position_does_not_stop_the_expiry_guardian(caplog) -> None:  # type: ignore[no-untyped-def]
    desk, paper, kite, ang = _mdesk(_adjustment_chain(), is_paper=False)
    desk._executors["live:angel_one"] = _RaisingExecutor()
    for pid, broker in (("A", "angel_one"), ("K", "kite")):
        pos = _iron_condor_position(pid, expiry_days=1)
        pos.exec_broker = broker
        desk.positions[pid] = pos
    now = datetime.combine(_TODAY, datetime.min.time(), tzinfo=_IST).replace(hour=15, minute=25)
    with caplog.at_level(logging.CRITICAL, logger="options.seller_desk"):
        await desk.sweep_expiry_guardian(_TODAY, now)
    assert {s for _leg, s in kite.closed} == {"K"} and "K" not in desk.positions
    assert "A" in desk.positions
    assert "seller_desk_guardian_position_failed id=A" in caplog.text


async def test_live_desk_still_does_not_restore_paper_desk_own_rows() -> None:
    repo = _FakeRepo(
        open_rows=[
            _row("P", "iron_condor", True, None),
            _row("K", "iron_condor", False, "kite"),
            _row("W", EXPIRY_DAY_TYPE, True, None),  # hosted: own mode, restored
        ]
    )
    desk, *_ = _mdesk(_adjustment_chain(), is_paper=False, repo=repo)
    await desk.restore_positions()
    assert set(desk.positions) == {"K", "W"} and not desk.held_positions
    assert not desk.positions["K"].hold  # the old filter kept it: managed exactly as before
    assert desk._pos_is_paper(desk.positions["W"]) and desk.positions["W"].exec_broker is None


async def test_single_broker_restore_without_exec_broker_column_is_kite() -> None:
    row = _row("K", "iron_condor", False, None)
    del row["exec_broker"]  # pre-089 row shape
    desk, _paper, kite, _ang = _mdesk(_adjustment_chain(), is_paper=False, repo=_FakeRepo([row]))
    await desk.restore_positions()
    assert desk.positions["K"].exec_broker == "kite"
    await desk._close_position("K", reason="t")
    assert kite.closed


def test_load_open_positions_selects_exec_broker() -> None:
    import inspect

    from src.db.repositories.options import OptionsRepository

    assert "exec_broker" in inspect.getsource(OptionsRepository.load_open_positions)


# ── orchestrator wiring ──────────────────────────────────────────────────────


def test_boot_builds_one_options_executor_per_stack() -> None:
    import inspect

    from src import orchestrator

    src = inspect.getsource(orchestrator)
    assert 'executors=_seller_options["executors"]' in src
    assert 'OptionsLegExecutor(_st["broker"])' in src


# ── R1 on the buyer-first path (_try_options_structure) ─────────────────────


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def get_chain(self, *a: Any, **k: Any) -> None:
        self.calls += 1
        return  # stop right after the guard


def _opt_shared(is_paper: bool, exec_broker: str | None) -> tuple[dict[str, Any], _Provider, Any]:
    provider = _Provider()
    executor = SimpleNamespace(_is_paper=is_paper, placed=[])
    shared = {
        "options": {
            "enabled": True,
            "provider": provider,
            "selector": None,
            "gate": None,
            "executor": executor,
            "repo": None,
            "cfg": {},
            "exec_broker": exec_broker,
        }
    }
    return shared, provider, executor


async def _try(shared: dict[str, Any], routes: dict[str, BrokerRoute]) -> bool:
    from src import orchestrator

    ctx = SimpleNamespace(segment_master_switch={}, can_trade_options=True, strategy_routes=routes)
    strategy = SimpleNamespace(spec=SimpleNamespace(meta=SimpleNamespace(product="CNC")))
    return await orchestrator._try_options_structure(
        shared=shared,
        crm_ctx=ctx,
        strategy=strategy,
        symbol="NSE:INFY",
        direction="BUY",
        entry=100.0,
        sl_pct=2.0,
        tgt_pct=4.0,
        conviction=0.6,
        earnings_days=999,
        signal_id="S1",
        paper_mode=False,
        paper_engine=None,
    )


_DESK = "options_seller_desk_v3"


@pytest.mark.parametrize(
    ("route", "why"),
    [
        (BrokerRoute(None, "live strategy has no 'broker' field"), "no 'broker' field"),
        (BrokerRoute("angel_one", None), "boot options executor trades through kite"),
    ],
)
async def test_buyer_first_live_path_rejects_unusable_or_foreign_route(route, why, caplog) -> None:  # type: ignore[no-untyped-def]
    shared, provider, _ex = _opt_shared(False, "kite")
    with caplog.at_level(logging.WARNING):
        assert await _try(shared, {_DESK: route}) is False
    assert provider.calls == 0  # nothing fetched, nothing placed
    assert f"broker_route_rejected strategy={_DESK} segment=options_buyer_first" in caplog.text
    assert why in caplog.text


async def test_buyer_first_live_path_with_matching_route_or_no_route_proceeds() -> None:
    shared, provider, _ex = _opt_shared(False, "kite")
    assert await _try(shared, {_DESK: BrokerRoute("kite", None)}) is False
    assert await _try(shared, {}) is False
    assert provider.calls == 2


async def test_buyer_first_paper_path_never_consults_routes() -> None:
    shared, provider, _ex = _opt_shared(True, None)
    assert await _try(shared, {_DESK: BrokerRoute(None, "x")}) is False
    assert provider.calls == 1


# ── boot route report (lock-aware) ───────────────────────────────────────────


def test_route_table_lists_ok_routes_without_a_live_stack() -> None:
    from src.brokers.routing import route_table

    routes = {"a": BrokerRoute("kite", None), "b": BrokerRoute("angel_one", None)}
    table, rejected = route_table(routes, built_brokers={"kite"})
    assert "a -> kite" in table and "b -> angel_one (NO LIVE STACK)" in table
    assert rejected == ["b (route ok but no live stack/executor built for broker angel_one)"]
    assert route_table(routes) == ("a -> kite | b -> angel_one", [])  # unchanged without it


def test_rejected_routes_message_is_lock_aware() -> None:
    from src.brokers.routing import rejected_routes_message

    unlocked = rejected_routes_message(["x (no broker)"], locked=False)
    assert "rejected" in unlocked and "x (no broker)" in unlocked and "PAPER" not in unlocked
    locked = rejected_routes_message(["x (no broker)"], locked=True)
    assert "PAPER" in locked and "paper_mode_locked" in locked and "x (no broker)" in locked


def test_boot_raises_a_critical_alert_for_rejected_live_routes() -> None:
    import inspect

    from src import orchestrator

    src = inspect.getsource(orchestrator)
    assert 'source="strategy_routes_rejected"' in src
    assert "rejected_routes_message(" in src


# ── real Postgres round trip (scratch DB only) ───────────────────────────────

_DB = os.environ.get("DB_URL")


@pytest.mark.skipif(not _DB or "scratch" not in (_DB or ""), reason="scratch DB_URL not set")
async def test_options_position_exec_broker_round_trip_on_postgres() -> None:
    import asyncpg
    from src.db.repositories.options import OptionsRepository

    conn = await asyncpg.connect(_DB)
    tx = conn.transaction()
    await tx.start()
    try:
        if not await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='options_positions' AND column_name='exec_broker'"
        ):
            pytest.skip("migration 089 not applied on this DB")
        await conn.execute("UPDATE options_positions SET status='CLOSED' WHERE status='OPEN'")

        class _Pool:
            def acquire(self) -> Any:
                class _Ctx:
                    async def __aenter__(self) -> Any:
                        return conn

                    async def __aexit__(self, *a: Any) -> bool:
                        return False

                return _Ctx()

        repo = OptionsRepository(_Pool())  # type: ignore[arg-type]
        for pid, paper, eb in (
            ("T7-ANG", False, "angel_one"),
            ("T7-NUL", False, None),
            ("T7-PAP", True, None),
        ):
            pos = _iron_condor_position(pid)
            await repo.insert_position(
                pid,
                underlying="NSE:NIFTY",
                strategy_type="iron_condor",
                protection_legs=pos.protection_legs,
                income_legs=pos.income_legs,
                expiry=date(2099, 1, 1),
                dte_at_entry=20,
                lot_size_at_entry=25,
                net_credit=100.0,
                max_loss=1000.0,
                max_profit=100.0,
                entry_margin=1000.0,
                entry_greeks=None,
                opened_at=datetime.now(_IST),
                is_paper=paper,
                exec_broker=eb,
            )
        rows = {r["position_id"]: r for r in await repo.load_open_positions()}
        assert rows["T7-ANG"]["exec_broker"] == "angel_one"
        assert rows["T7-NUL"]["exec_broker"] is None

        # realized-P&L seeds read other tables/columns; stub them, restore is what's tested
        async def _zero(*a: Any, **k: Any) -> float:
            return 0.0

        async def _empty(*a: Any, **k: Any) -> dict[str, float]:
            return {}

        repo.realized_pnl_closed_on = _zero  # type: ignore[method-assign]
        repo.realized_pnl_by_structure_closed_on = _empty  # type: ignore[method-assign]
        desk, paper, kite, ang = _mdesk(_adjustment_chain(), is_paper=True, repo=repo)
        await desk.restore_positions()
        # live desk-own rows on a paper desk come back ON HOLD (unaudited), paper ones managed
        assert desk.held_positions["T7-ANG"].exec_broker == "angel_one"
        assert desk.held_positions["T7-NUL"].exec_broker == "kite"
        assert desk._pos_is_paper(desk.positions["T7-PAP"])
    finally:
        await tx.rollback()
        await conn.close()


# ── fix round 2: held rows vs slots/dedupe (N1); isolated operator closes (N2) ──


def _slot_desk(is_paper: bool, monkeypatch) -> tuple[SellerDesk, _FakeExecutor, Any]:  # type: ignore[no-untyped-def]
    from src.risk import budget as budget_module

    monkeypatch.setattr(budget_module, "_current_equity", 100_000_000.0)
    monkeypatch.setattr(sd._seller_settings, "max_strategies_per_index", lambda settings=None: 1)
    chain = _rich_chain()
    ex = _FakeExecutor()
    desk = SellerDesk(
        StrategySelector(),
        _FakeEventCalendar(),
        ex,
        _FakeChainFetcher(chain),
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        margin_safety_pct=0.90,
        greeks_budget=_budget(),
        min_rom_pct=0.0,
        is_paper=is_paper,
        enabled_strategy_types={"bull_put_spread"},
    )
    return desk, ex, chain


def _held(pid: str, *, live: bool, reason: str = "expired_live_row") -> Any:
    pos = _iron_condor_position(pid)
    pos.is_paper = not live
    pos.exec_broker = "kite" if live else None
    pos.hold_reason = reason
    return pos


async def _slot_cycle(desk: SellerDesk, chain: Any) -> Any:
    from tests.unit.options.test_seller_desk import _APPROVE_ALL_CRM, _DUMMY_CTX

    calendar = {"NIFTY": [chain.expiry.date() + timedelta(days=i) for i in range(-2, 3)]}
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


async def test_held_row_of_the_desks_own_mode_blocks_the_index_slot(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    desk, ex, chain = _slot_desk(False, monkeypatch)  # live desk
    desk.held_positions["OLD"] = _held("OLD", live=True)
    with caplog.at_level(logging.INFO, logger="options.seller_desk"):
        assert await _slot_cycle(desk, chain) is None
    assert ex.executed == []  # a held live NIFTY row still occupies NIFTY's only live slot
    assert "seller_desk_already_open" in caplog.text


async def test_held_row_of_the_other_mode_does_not_use_the_desks_slot(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    desk, ex, chain = _slot_desk(True, monkeypatch)  # paper desk, held unaudited LIVE row
    desk.held_positions["OLD"] = _held("OLD", live=True, reason="restored_unaudited_live_row")
    assert await _slot_cycle(desk, chain) is not None
    assert len(ex.executed) == 1


async def test_reconcile_never_deletes_held_rows_and_passes_them_to_the_repo() -> None:
    repo = _FakeRepo()
    repo.duplicate_ids_to_return = []
    desk, *_ = _mdesk(_adjustment_chain(), is_paper=False, repo=repo)
    desk.held_positions["OLD"] = _held("OLD", live=True)
    desk.positions["NEW"] = _iron_condor_position("NEW")
    assert await desk.reconcile_duplicate_positions() == 0
    assert repo.dedupe_kwargs["exclude_position_ids"] == ["OLD"]
    assert "OLD" in desk.held_positions and "NEW" in desk.positions


async def test_operator_close_failure_is_isolated_logged_once_and_throttled(
    monkeypatch, caplog
) -> None:  # type: ignore[no-untyped-def]
    _capture_alerts(monkeypatch)
    desk, paper, kite, ang = _mdesk(_adjustment_chain(), is_paper=False)
    attempts: list[str] = []

    class _CountingRaiser(_FakeExecutor):
        async def close_leg(self, leg, signal_id):  # type: ignore[no-untyped-def]
            attempts.append(signal_id)
            raise RuntimeError("broker down")

    desk._executors["live:angel_one"] = _CountingRaiser()
    for pid, broker in (("A", "angel_one"), ("K", "kite")):
        pos = _iron_condor_position(pid)
        pos.exec_broker = broker
        desk.positions[pid] = pos
    _op_close(monkeypatch, ["A", "K"])
    clock = [1000.0]
    monkeypatch.setattr(sd, "_monotonic", lambda: clock[0])

    async def _sweep() -> None:
        await desk.sweep_position_manager(
            today=_TODAY, blackout_active=False, current_prices={}, current_ivs={}, chains={}
        )

    with caplog.at_level(logging.WARNING, logger="options.seller_desk"):
        await _sweep()  # A raises; K is still closed; the sweep does not raise
        assert {s for _leg, s in kite.closed} == {"K"} and "A" in desk.positions
        await _sweep()
        await _sweep()  # within the retry interval: A is not retried
        assert attempts == ["A"]
        clock[0] += sd._OPERATOR_CLOSE_RETRY_SECONDS + 1
        await _sweep()  # interval passed: exactly one more attempt
        assert attempts == ["A", "A"]
    crit = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert sum("seller_desk_operator_close_failed id=A" in r.getMessage() for r in crit) == 1
    assert "A" in desk.positions


async def test_operator_close_does_not_swallow_cancellation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    desk, *_ = _mdesk(_adjustment_chain(), is_paper=False)

    class _Cancel(_FakeExecutor):
        async def close_leg(self, leg, signal_id):  # type: ignore[no-untyped-def]
            raise asyncio.CancelledError

    desk._executors["live:kite"] = _Cancel()
    desk.positions["K"] = _iron_condor_position("K")
    desk.positions["K"].exec_broker = "kite"
    _op_close(monkeypatch, ["K"])
    with pytest.raises(asyncio.CancelledError):
        await desk.sweep_position_manager(
            today=_TODAY, blackout_active=False, current_prices={}, current_ivs={}, chains={}
        )


async def test_on_hold_alert_names_mode_and_broker_per_row(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    alerts = _capture_alerts(monkeypatch)
    repo = _FakeRepo(
        open_rows=[
            _row("K", "iron_condor", False, "kite"),  # live, unaudited on a paper desk
            _row("Q", "iron_condor", True, None),  # paper row without a paper executor
        ]
    )
    desk, *_ = _mdesk(_adjustment_chain(), is_paper=True, repo=repo)
    desk._executors.pop("paper")
    await desk.restore_positions()
    msg = alerts[0]["msg"]
    assert msg.startswith("2 options positions restored for MONITORING ONLY")
    assert "K (iron_condor, mode=live broker=kite," in msg
    assert "Q (iron_condor, mode=paper broker=-," in msg


@pytest.mark.skipif(not _DB or "scratch" not in (_DB or ""), reason="scratch DB_URL not set")
async def test_dedupe_on_postgres_skips_excluded_ids_entirely() -> None:
    import asyncpg
    from src.db.repositories.options import OptionsRepository

    conn = await asyncpg.connect(_DB)
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute("UPDATE options_positions SET status='CLOSED' WHERE status='OPEN'")

        class _Pool:
            def acquire(self) -> Any:
                class _Ctx:
                    async def __aenter__(self) -> Any:
                        return conn

                    async def __aexit__(self, *a: Any) -> bool:
                        return False

                return _Ctx()

        repo = OptionsRepository(_Pool())  # type: ignore[arg-type]
        t0 = datetime.now(_IST)
        for i, pid in enumerate(("N1-HELD", "N1-NEW", "N1-DUP")):
            pos = _iron_condor_position(pid)
            await repo.insert_position(
                pid,
                underlying="NSE:NIFTY",
                strategy_type="iron_condor",
                protection_legs=pos.protection_legs,
                income_legs=pos.income_legs,
                expiry=date(2099, 1, 1),
                dte_at_entry=20,
                lot_size_at_entry=25,
                net_credit=100.0,
                max_loss=1000.0,
                max_profit=100.0,
                entry_margin=1000.0,
                entry_greeks=None,
                opened_at=t0 + timedelta(seconds=i),
                is_paper=False,
                exec_broker="kite",
            )
        deleted = await repo.delete_duplicate_open_positions(
            is_paper=False, keep_per_underlying=1, exclude_position_ids=["N1-HELD"]
        )
        # the held row neither counts nor is deleted: NEW is the kept one, only DUP goes
        assert deleted == ["N1-DUP"]
        left = {
            r["position_id"]
            for r in await conn.fetch(
                "SELECT position_id FROM options_positions WHERE status='OPEN'"
            )
        }
        assert left == {"N1-HELD", "N1-NEW"}
        # legacy call (no exclusions) is unchanged: the earliest is kept
        assert await repo.delete_duplicate_open_positions(is_paper=False) == ["N1-NEW"]
    finally:
        await tx.rollback()
        await conn.close()


# ── C1: a close that fails part-way never re-sends a leg that is already closed ──


class _MetaRepo(_FakeRepo):
    def __init__(self, open_rows: list[dict] | None = None) -> None:
        super().__init__(open_rows)
        self.meta: dict[str, dict[str, Any]] = {}

    async def set_legs_meta(self, position_id, meta):  # type: ignore[no-untyped-def]
        self.meta[position_id] = {**self.meta.get(position_id, {}), **meta}


class _FailOnNth(_FakeExecutor):
    """close_leg raises on the n-th call while ``fail`` is True (the broker is down)."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self.n, self.calls, self.fail = n, 0, True

    async def close_leg(self, leg, signal_id):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.fail and self.calls >= self.n:
            raise RuntimeError("broker down")
        return await super().close_leg(leg, signal_id)


async def test_close_failing_on_third_leg_holds_and_retry_never_resends_closed_legs(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    alerts = _capture_alerts(monkeypatch)
    repo = _MetaRepo()
    desk, *_ = _mdesk(_adjustment_chain(), is_paper=False, repo=repo)
    ex = _FailOnNth(3)
    desk._executors["live:kite"] = ex
    pos = _iron_condor_position("K")
    pos.exec_broker = "kite"
    desk.positions["K"] = pos

    with pytest.raises(RuntimeError):
        await desk._close_position("K", reason="STOP_LOSS")
    # Shorts first: both SELL legs were closed while their long wings were still held.
    assert [leg.direction for leg, _ in ex.closed] == ["SELL", "SELL"]
    # On HOLD (no automatic retry), persisted, one CRITICAL operator alert.
    assert "K" in desk.held_positions and "K" not in desk.positions
    assert desk.held_positions["K"].hold_reason == "partial_close"
    assert repo.meta["K"]["hold_reason"] == "partial_close"
    assert set(repo.meta["K"]["close_sent"]) == {
        "NFO:NIFTY23400PE",
        "NFO:NIFTY24000CE",
    }
    assert any(a["source"] == "options_partial_close:K" for a in alerts)

    # Automatic sweeps never touch it (held = monitoring only).
    await _full_sweep(desk, _TODAY)
    assert len(ex.closed) == 2

    # Operator close once the broker is back: ONLY the legs still open are sent.
    ex.fail = False
    _op_close(monkeypatch, ["K"])
    await desk.sweep_position_manager(
        today=_TODAY, blackout_active=False, current_prices={}, current_ivs={}, chains={}
    )
    resent = [leg.tradingsymbol for leg, _ in ex.closed[2:]]
    assert sorted(resent) == ["NFO:NIFTY23200PE", "NFO:NIFTY24200CE"]
    assert "K" not in desk.held_positions and repo.closed[0]["position_id"] == "K"
    assert repo.closed[0]["realized_pnl"] is not None  # sent legs keep their exit prices


async def test_restart_mid_close_restores_on_hold_and_skips_sent_legs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _capture_alerts(monkeypatch)
    row = _row("K", "iron_condor", False, "kite")
    row["legs"]["meta"] = {"close_sent": {"NFO:NIFTY23400PE": 50.0}}  # died after leg 1
    repo = _MetaRepo(open_rows=[row])
    desk, _paper, kite, _ang = _mdesk(_adjustment_chain(), is_paper=False, repo=repo)
    await desk.restore_positions()
    assert desk.held_positions["K"].hold_reason == "partial_close"
    _op_close(monkeypatch, ["K"])
    await desk.sweep_position_manager(
        today=_TODAY, blackout_active=False, current_prices={}, current_ivs={}, chains={}
    )
    sent = [leg.tradingsymbol for leg, _ in kite.closed]
    assert "NFO:NIFTY23400PE" not in sent and len(sent) == 3
    assert sent[0] == "NFO:NIFTY24000CE"  # the remaining short goes first


async def test_first_leg_failure_with_nothing_sent_is_not_held(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _capture_alerts(monkeypatch)
    desk, *_ = _mdesk(_adjustment_chain(), is_paper=False, repo=_MetaRepo())
    desk._executors["live:kite"] = _FailOnNth(1)
    desk.positions["K"] = _iron_condor_position("K")
    desk.positions["K"].exec_broker = "kite"
    with pytest.raises(RuntimeError):
        await desk._close_position("K", reason="STOP_LOSS")
    assert "K" in desk.positions and desk.positions["K"].hold_reason is None
