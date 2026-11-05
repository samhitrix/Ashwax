from __future__ import annotations

from src.brokers.routing import BrokerRoute, broker_is_configured, parse_strategy_routes, route_for

REG = ["kite", "angel_one", "paper"]
ENV = {"KITE_API_KEY": "k", "ANGEL_ONE_API_KEY": "a", "ANGEL_ONE_CLIENT_CODE": "c"}


def _e(name: str, mode: str = "live", **kw: object) -> dict:
    return {"name": name, "execution_mode": mode, **kw}


def test_paper_entries_are_not_routed() -> None:
    assert parse_strategy_routes([_e("a", "paper", broker="kite")], REG, ENV) == {}


def test_valid_live_route() -> None:
    r = parse_strategy_routes([_e("s", broker="angel_one")], REG, ENV)["s"]
    assert r == BrokerRoute("angel_one", None) and r.ok


def test_missing_broker_field_is_rejected_with_reason() -> None:
    r = parse_strategy_routes([_e("s")], REG, ENV)["s"]
    assert not r.ok and r.broker is None and "no 'broker'" in (r.reason or "")


def test_unregistered_paper_and_unconfigured_brokers_rejected() -> None:
    routes = parse_strategy_routes(
        [_e("a", broker="flattrade"), _e("b", broker="paper"), _e("c", broker="angel_one")],
        REG,
        {"KITE_API_KEY": "k"},
    )
    assert "not registered" in (routes["a"].reason or "")
    assert "paper" in (routes["b"].reason or "")
    assert "not configured" in (routes["c"].reason or "")


def test_broker_is_configured_uses_credential_rules() -> None:
    assert broker_is_configured("kite", {"KITE_API_KEY": "x"})
    assert not broker_is_configured("kite", {})
    assert not broker_is_configured("angel_one", {"ANGEL_ONE_API_KEY": "a"})
    assert not broker_is_configured("flattrade", ENV)  # no credential rule yet


def test_route_for_returns_none_for_non_live_registry_strategy() -> None:
    routes = parse_strategy_routes([_e("s", broker="kite")], REG, ENV)
    assert route_for(routes, "s") == BrokerRoute("kite", None)
    assert route_for(routes, "other") is None
