"""Per-strategy live broker resolution (spec rule R1: no default, no failover)."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from src.market_data.provider_config import CREDENTIAL_ENV


@dataclass(frozen=True)
class BrokerRoute:
    broker: str | None
    reason: str | None  # None when the route is usable

    @property
    def ok(self) -> bool:
        return self.broker is not None and self.reason is None


def broker_is_configured(name: str, env: Mapping[str, str]) -> bool:
    rule = CREDENTIAL_ENV.get(name)
    return bool(rule) and all(env.get(k, "").strip() for k in rule or ())


def parse_strategy_routes(
    entries: Iterable[dict[str, Any]], registered: Iterable[str], env: Mapping[str, str]
) -> dict[str, BrokerRoute]:
    """Route for every registry entry whose execution_mode is "live"."""
    reg = set(registered) - {"paper"}
    routes: dict[str, BrokerRoute] = {}
    for e in entries:
        name = e.get("name")
        if not name or e.get("execution_mode") != "live":
            continue
        broker = e.get("broker")
        if not isinstance(broker, str) or not broker.strip():
            routes[name] = BrokerRoute(None, "live strategy has no 'broker' field")
        elif broker == "paper":
            routes[name] = BrokerRoute(None, "broker 'paper' is not a live broker")
        elif broker not in reg:
            routes[name] = BrokerRoute(None, f"broker {broker!r} is not registered")
        elif not broker_is_configured(broker, env):
            routes[name] = BrokerRoute(None, f"broker {broker!r} is not configured (credentials)")
        else:
            routes[name] = BrokerRoute(broker, None)
    return routes


def route_for(routes: Mapping[str, BrokerRoute], strategy: str) -> BrokerRoute | None:
    return routes.get(strategy)


def route_rejection(routes: Mapping[str, BrokerRoute] | None, strategy: str) -> str | None:
    """R1 reject reason for a strategy already resolved LIVE, or None when it may trade.

    No route entry (a strategy that is not "live" in the registry: the segment decides) is
    the legacy path and is never rejected here. Callers must not consult this for paper.
    Importable by the options desk without importing the orchestrator."""
    route = (routes or {}).get(strategy)
    if route is None or route.ok:
        return None
    return route.reason or "live strategy has no usable broker"


def route_table(
    routes: Mapping[str, BrokerRoute], built_brokers: Collection[str] | None = None
) -> tuple[str, list[str]]:
    """Boot log of the live routes: ("a -> kite | b -> REJECTED(reason)", ["b (reason)"]).

    With `built_brokers` (the brokers that got a live stack/executor), an ok route whose
    broker has none is shown as "(NO LIVE STACK)" and listed with the rejected ones: every
    live signal it emits will be rejected for that reason."""
    if not routes:
        return "(no live registry strategies)", []
    parts: list[str] = []
    rejected: list[str] = []
    for name, route in routes.items():
        if route.ok and built_brokers is not None and route.broker not in built_brokers:
            parts.append(f"{name} -> {route.broker} (NO LIVE STACK)")
            rejected.append(
                f"{name} (route ok but no live stack/executor built for broker {route.broker})"
            )
        elif route.ok:
            parts.append(f"{name} -> {route.broker}")
        else:
            why = route_rejection(routes, name)
            parts.append(f"{name} -> REJECTED({why})")
            rejected.append(f"{name} ({why})")
    return " | ".join(parts), rejected


def rejected_routes_message(rejected: list[str], locked: bool) -> str:
    """Operator text for the live strategies route_table() listed as rejected. Under
    operator.json paper_mode_locked nothing is rejected: everything runs as PAPER."""
    names = "; ".join(rejected)
    if locked:
        return (
            f"{len(rejected)} LIVE registry strategies have no usable live broker: {names}. "
            "paper_mode_locked is on, so they run as PAPER now; fix their broker before "
            "unlocking, or every live signal they emit will be rejected."
        )
    return (
        f"{len(rejected)} LIVE registry strategies have no usable live broker: {names}. "
        "Every live signal they emit is rejected (no default broker, no paper fallback)."
    )
