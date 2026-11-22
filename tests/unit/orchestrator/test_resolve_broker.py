"""Per-strategy live broker routing at the two equity executors (spec rule R1).

A registry strategy resolved LIVE trades only through the broker its route names. A live
strategy whose route is not usable (no broker / unregistered / not configured) or whose
broker's stack does not exist is REJECTED: no order, no default broker, no failover, no
demotion to paper. Strategies with no route entry (not live in the registry -> the segment
decides, the "inherit" path) keep the legacy global.json broker exactly as before.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from src import orchestrator as orch
from src.brokers.routing import BrokerRoute
from src.execution.order_lifecycle import OrderState
from tests.unit.execution.test_swing_signal import _NOW, _SIG, _shared, _with_cash

_NO_BROKER = "live strategy has no 'broker' field"
_UNREGISTERED = "broker 'kitee' is not registered"
_UNCONFIGURED = "broker 'angel_one' is not configured (credentials)"


@pytest.fixture(autouse=True)
def _quiet_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fake rejections out of the real logs/ directory (see test_swing_signal)."""
    log = logging.getLogger("test_resolve_broker")
    monkeypatch.setattr(orch, "_log", log)
    monkeypatch.setattr(orch, "_swing_log", log)


# ── _resolve_broker (pure) ────────────────────────────────────────────────────


def _routing(**routes: BrokerRoute) -> dict:
    return {"strategy_routes": dict(routes), "legacy_live_broker": "kite"}


def test_paper_never_has_a_broker_even_with_a_bad_route() -> None:
    shared = _routing(s=BrokerRoute(None, _NO_BROKER))
    assert orch._resolve_broker(shared, "s", True) == (None, None)


def test_usable_route_gives_its_broker() -> None:
    shared = _routing(s=BrokerRoute("angel_one", None))
    assert orch._resolve_broker(shared, "s", False) == ("angel_one", None)


@pytest.mark.parametrize("reason", [_NO_BROKER, _UNREGISTERED, _UNCONFIGURED])
def test_unusable_route_gives_its_reason_and_no_broker(reason: str) -> None:
    shared = _routing(s=BrokerRoute(None, reason))
    assert orch._resolve_broker(shared, "s", False) == (None, reason)


def test_unusable_route_without_a_reason_is_still_rejected() -> None:
    shared = _routing(s=BrokerRoute(None, None))
    broker, reason = orch._resolve_broker(shared, "s", False)
    assert broker is None and reason


def test_inherit_path_uses_the_legacy_broker() -> None:
    shared = _routing(other=BrokerRoute("angel_one", None))
    assert orch._resolve_broker(shared, "not_in_routes", False) == ("kite", None)
    shared["legacy_live_broker"] = "angel_one"
    assert orch._resolve_broker(shared, "not_in_routes", False) == ("angel_one", None)


def test_no_routing_state_at_all_is_the_legacy_path() -> None:
    assert orch._resolve_broker({}, "s", False) == (None, None)


def test_locked_account_resolves_paper_so_routes_are_never_consulted() -> None:
    shared = _routing(s=BrokerRoute(None, _NO_BROKER))
    shared.update(paper_mode_locked=True, strategy_execution_mode={"s": "live"})
    ctx = SimpleNamespace(segment_paper_mode={"swing": False})
    is_paper = orch._resolve_is_paper(shared, ctx, "swing", "s", False)
    assert is_paper is True
    assert orch._resolve_broker(shared, "s", is_paper) == (None, None)


# ── fakes: one stack per broker ───────────────────────────────────────────────


def _stack(name: str) -> dict:
    lc = AsyncMock()
    lc.place_entry_protection_and_poll.return_value = SimpleNamespace(
        agent_trade_id=f"TRD-{name}",
        state=OrderState.FILLED,
        fill_price=100.5,
        filled_quantity=10,
        sl_price=95.0,
        target_price=110.0,
        sl_order_id="SL1",
        target_order_id="TG1",
        gtt_sl_id="GTT-1",
    )
    _with_cash(lc)
    om = MagicMock()
    om.get_monitored.return_value = []
    return {"lifecycle": lc, "order_monitor": om, "name": name}


def _stacks(*live: str) -> dict:
    out = {"paper": _stack("paper")}
    for b in live:
        out[f"live:{b}"] = _stack(b)
    return out


def _placed(stacks: dict) -> list[str]:
    return [
        s["name"]
        for s in stacks.values()
        if s["lifecycle"].place_entry_protection_and_poll.await_count
    ]


def _live_shared(stacks: dict, strategy: str, route: BrokerRoute | None, **extra) -> dict:
    """Shared state where `strategy` resolves LIVE (registry override) with `route`."""
    shared = _shared(stacks=stacks, paper_mode=True)
    shared["strategy_execution_mode"] = {strategy: "live"} if route is not None else {}
    shared["strategy_routes"] = {strategy: route} if route is not None else {}
    shared["legacy_live_broker"] = "kite"
    shared["crm_ctx"].segment_paper_mode = {"swing": True, "intraday": True}  # type: ignore[attr-defined]
    shared.update(extra)
    return shared


_BAD_ROUTES = [
    BrokerRoute(None, _NO_BROKER),
    BrokerRoute(None, _UNREGISTERED),
    BrokerRoute(None, _UNCONFIGURED),
]


# ── swing executor ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _BAD_ROUTES)
async def test_swing_live_strategy_without_a_usable_route_places_nothing(
    route: BrokerRoute, caplog: pytest.LogCaptureFixture
) -> None:
    stacks = _stacks("kite", "angel_one")
    shared = _live_shared(stacks, "minervini_vcp", route)
    with caplog.at_level(logging.WARNING, logger="test_resolve_broker"):
        tid, *_rest, reason, gates = await orch._execute_swing_signal(
            shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW
        )
    assert tid is None and reason == "broker_route_rejected" and gates == []
    assert _placed(stacks) == []  # not live, not the legacy broker, not paper
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "broker_route_rejected strategy=minervini_vcp segment=swing" in m and route.reason in m
        for m in msgs
    )


@pytest.mark.asyncio
async def test_swing_routed_broker_without_a_stack_places_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stacks = _stacks("kite")  # angel_one configured but its stack failed to build
    shared = _live_shared(stacks, "minervini_vcp", BrokerRoute("angel_one", None))
    with caplog.at_level(logging.WARNING, logger="test_resolve_broker"):
        tid, *_rest, reason, _g = await orch._execute_swing_signal(
            shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW
        )
    assert tid is None and reason == "broker_route_rejected"
    assert _placed(stacks) == []
    assert any("no live stack for broker angel_one" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["kite", "angel_one"])
async def test_swing_usable_route_places_through_that_brokers_stack(broker: str) -> None:
    stacks = _stacks("kite", "angel_one")
    shared = _live_shared(stacks, "minervini_vcp", BrokerRoute(broker, None))
    tid, *_rest, reason, _g = await orch._execute_swing_signal(
        shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW
    )
    assert reason == "executed" and tid == f"TRD-{broker}"
    assert _placed(stacks) == [broker]


@pytest.mark.asyncio
async def test_swing_inherit_path_places_through_the_legacy_broker() -> None:
    stacks = _stacks("kite", "angel_one")
    shared = _live_shared(stacks, "minervini_vcp", None)
    shared["crm_ctx"].segment_paper_mode = {"swing": False}  # segment decides: live
    shared["strategy_routes"] = {"some_other": BrokerRoute("angel_one", None)}
    tid, *_rest, reason, _g = await orch._execute_swing_signal(
        shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW
    )
    assert reason == "executed" and _placed(stacks) == ["kite"]


@pytest.mark.asyncio
async def test_swing_inherit_path_rejects_when_the_legacy_stack_is_missing() -> None:
    # Before: broker=None fell back to whichever live stack existed -- the wrong account.
    stacks = _stacks("angel_one")
    shared = _live_shared(stacks, "minervini_vcp", None)
    shared["crm_ctx"].segment_paper_mode = {"swing": False}
    tid, *_rest, reason, _g = await orch._execute_swing_signal(
        shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW
    )
    assert tid is None and reason == "broker_route_rejected" and _placed(stacks) == []


@pytest.mark.asyncio
async def test_swing_paper_strategy_never_consults_routes() -> None:
    stacks = _stacks("kite")
    shared = _live_shared(stacks, "minervini_vcp", BrokerRoute(None, _NO_BROKER))
    shared["strategy_execution_mode"] = {"minervini_vcp": "paper"}
    tid, *_rest, reason, _g = await orch._execute_swing_signal(
        shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW
    )
    assert reason == "executed" and _placed(stacks) == ["paper"]


@pytest.mark.asyncio
async def test_swing_locked_account_stays_paper_despite_a_bad_live_route() -> None:
    stacks = _stacks("kite")
    shared = _live_shared(
        stacks, "minervini_vcp", BrokerRoute(None, _NO_BROKER), paper_mode_locked=True
    )
    tid, *_rest, reason, _g = await orch._execute_swing_signal(
        shared, "minervini_vcp", "NSE:RELIANCE", _SIG, _NOW
    )
    assert reason == "executed" and _placed(stacks) == ["paper"]


# ── intraday executor ─────────────────────────────────────────────────────────


def _intraday_sig() -> SimpleNamespace:
    return SimpleNamespace(
        entry_price=100.0, sl=99.0, target=102.0, direction=SimpleNamespace(value="LONG")
    )


async def _intraday(shared: dict, strategy: str = "orb") -> object:
    return await orch._execute_intraday_signal(
        shared, strategy, "NSE:RELIANCE", _intraday_sig(), _NOW, ref_ltp=100.0
    )


def _monitored(stacks: dict) -> list[str]:
    return [s["name"] for s in stacks.values() if s["order_monitor"].get_monitored.called]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _BAD_ROUTES)
async def test_intraday_live_strategy_without_a_usable_route_places_nothing(
    route: BrokerRoute, caplog: pytest.LogCaptureFixture
) -> None:
    stacks = _stacks("kite", "angel_one")
    shared = _live_shared(stacks, "orb", route)
    with caplog.at_level(logging.WARNING, logger="test_resolve_broker"):
        assert await _intraday(shared) is None
    assert _placed(stacks) == [] and _monitored(stacks) == []
    assert shared["_intraday_refusals"]["RELIANCE"] == ("broker_route_rejected", route.reason)
    assert any(
        "broker_route_rejected strategy=orb segment=intraday" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_intraday_routed_broker_without_a_stack_places_nothing() -> None:
    stacks = _stacks("kite")
    shared = _live_shared(stacks, "orb", BrokerRoute("angel_one", None))
    assert await _intraday(shared) is None
    assert _placed(stacks) == [] and _monitored(stacks) == []
    assert shared["_intraday_refusals"]["RELIANCE"] == (
        "broker_route_rejected",
        "no live stack for broker angel_one",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["kite", "angel_one"])
async def test_intraday_usable_route_uses_that_brokers_stack(broker: str) -> None:
    # Stop right after the held-position check (news gate) -- that check reads the
    # chosen stack's monitor, which is exactly what the order would go through.
    stacks = _stacks("kite", "angel_one")
    shared = _live_shared(
        stacks,
        "orb",
        BrokerRoute(broker, None),
        news_avoid_symbols={"NSE:RELIANCE": 4e9},
    )
    assert await _intraday(shared) is None
    assert shared["_intraday_refusals"]["RELIANCE"][0] == "news_halt_avoid"
    assert _monitored(stacks) == [broker] and _placed(stacks) == []


@pytest.mark.asyncio
async def test_intraday_inherit_path_uses_the_legacy_broker() -> None:
    stacks = _stacks("kite", "angel_one")
    shared = _live_shared(stacks, "orb", None, news_avoid_symbols={"NSE:RELIANCE": 4e9})
    shared["crm_ctx"].segment_paper_mode = {"intraday": False}
    assert await _intraday(shared) is None
    assert _monitored(stacks) == ["kite"]


@pytest.mark.asyncio
async def test_intraday_paper_strategy_never_consults_routes() -> None:
    stacks = _stacks("kite")
    shared = _live_shared(
        stacks,
        "orb",
        BrokerRoute(None, _NO_BROKER),
        news_avoid_symbols={"NSE:RELIANCE": 4e9},
    )
    shared["strategy_execution_mode"] = {"orb": "paper"}
    assert await _intraday(shared) is None
    assert shared["_intraday_refusals"]["RELIANCE"][0] == "news_halt_avoid"
    assert _monitored(stacks) == ["paper"]


# ── boot route table ──────────────────────────────────────────────────────────


def test_route_table_lines_list_every_live_strategy_and_the_rejected_ones() -> None:
    from src.brokers.routing import route_table

    table, rejected = route_table(
        {"a": BrokerRoute("kite", None), "b": BrokerRoute(None, _NO_BROKER)}
    )
    assert table == f"a -> kite | b -> REJECTED({_NO_BROKER})"
    assert rejected == [f"b ({_NO_BROKER})"]
    assert route_table({}) == ("(no live registry strategies)", [])


def test_boot_logs_the_route_table() -> None:
    import inspect

    src = inspect.getsource(orch)
    assert "strategy_routes_table" in src and "strategy_routes_rejected" in src
