"""Which brokers get a session at boot (spec 3.2 "Kite becomes optional").

A broker is logged in at boot only if its credentials are in the environment AND this run
can use it: it is a market-data provider in config/global.json `market_data` (incl. per_use),
the legacy global `broker.name`, or the `broker` of a live registry strategy. That is the
same set the orchestrator refreshes at boot, so a user with no Kite account is never asked
to log in to Kite, and each broker stands on its own (no broker depends on another).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.brokers.routing import broker_is_configured
from src.market_data.provider_config import CREDENTIAL_ENV, parse_provider_config

# Fixed login order so boot logs are stable; unknown names are ignored.
BOOT_LOGIN_ORDER: tuple[str, ...] = ("kite", "angel_one", "flattrade")


def boot_login_candidates(
    global_cfg: dict[str, Any], registry_entries: Iterable[dict[str, Any]]
) -> tuple[set[str], str | None]:
    """Broker names this run may need a session for, and a config problem (or None).

    A malformed `market_data` block cannot be resolved; then every known broker is a
    candidate (logging in is harmless, missing a needed login is not) and the problem is
    returned so the caller can log it. Any error counts, not just ValueError: hand edits such
    as `"failover": null` or `"market_data": []` raise AttributeError/TypeError, and this runs
    in the launcher before any service starts, so it must never raise.
    """
    problem: str | None = None
    names: set[str] = set()
    try:
        cfg = parse_provider_config(global_cfg)
        names.update(cfg.providers)
        names.update(n for v in cfg.per_use.values() for n in v)
    except Exception as exc:  # noqa: BLE001 — see docstring: boot must survive any bad config
        problem = f"{type(exc).__name__}: {exc}"
        names.update(BOOT_LOGIN_ORDER)
    try:
        broker_cfg = global_cfg.get("broker")
        legacy = broker_cfg.get("name") if isinstance(broker_cfg, dict) else broker_cfg
        if isinstance(legacy, str) and legacy.strip() and legacy != "paper":
            names.add(legacy.strip())
        for entry in registry_entries:
            broker = entry.get("broker")
            if entry.get("execution_mode") == "live" and isinstance(broker, str) and broker.strip():
                names.add(broker.strip())
    except Exception as exc:  # noqa: BLE001 — same reason; every known broker stays a candidate
        problem = problem or f"{type(exc).__name__}: {exc}"
        names.update(BOOT_LOGIN_ORDER)
    return names, problem


def select_boot_logins(
    global_cfg: dict[str, Any],
    registry_entries: Iterable[dict[str, Any]],
    env: Mapping[str, str],
) -> tuple[list[str], dict[str, str], str | None]:
    """Return (brokers to log in, {skipped broker: reason}, config problem or None)."""
    candidates, problem = boot_login_candidates(global_cfg, registry_entries)
    login: list[str] = []
    skipped: dict[str, str] = {}
    for name in BOOT_LOGIN_ORDER:
        if name not in candidates:
            skipped[name] = "not a market-data provider and no live strategy uses it"
        elif not broker_is_configured(name, env):
            skipped[name] = f"no credentials ({', '.join(CREDENTIAL_ENV[name])})"
        else:
            login.append(name)
    return login, skipped, problem
