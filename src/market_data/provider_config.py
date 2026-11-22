"""Market-data provider configuration and eligibility.

`config/global.json` -> `market_data` decides which brokers may serve market data and in
what order. A provider is eligible only if it is registered AND its credentials are
present, so a user without a Kite account simply never loads Kite.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# Env vars that must all be non-empty for a provider to be usable. A registered broker
# with no rule here is ineligible (fail safe) — each new adapter adds its own rule.
CREDENTIAL_ENV: dict[str, tuple[str, ...]] = {
    "kite": ("KITE_API_KEY",),
    "angel_one": ("ANGEL_ONE_API_KEY", "ANGEL_ONE_CLIENT_CODE"),
    "flattrade": ("FLATTRADE_API_KEY", "FLATTRADE_USER_ID"),
}

USES: frozenset[str] = frozenset({"ohlcv", "ltp", "options_chain", "depth"})


@dataclass(frozen=True)
class ProviderConfig:
    providers: tuple[str, ...]
    max_consecutive_failures: int
    cooldown_seconds: float
    per_use: dict[str, tuple[str, ...]]


def _names(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise ValueError(f"market_data.{where} must be a non-empty list of broker names")
    return tuple(value)


def parse_provider_config(global_cfg: dict[str, Any]) -> ProviderConfig:
    """Parse `global_cfg["market_data"]`; a missing block means legacy Kite-only.

    Raises:
        ValueError: on a malformed block (never silently falls back — a typo here
            would otherwise change which broker serves prices).
    """
    block = global_cfg.get("market_data")
    if block is None:
        return ProviderConfig(("kite",), 3, 300.0, {})
    providers = _names(block.get("providers"), "providers")
    failover = block.get("failover", {})
    max_fail = int(failover.get("max_consecutive_failures", 3))
    cooldown = float(failover.get("cooldown_seconds", 300))
    if max_fail < 1 or cooldown < 0:
        raise ValueError("market_data.failover: max_consecutive_failures>=1, cooldown_seconds>=0")
    per_use: dict[str, tuple[str, ...]] = {}
    for use, names in block.get("per_use", {}).items():
        if use not in USES:
            raise ValueError(f"market_data.per_use: unknown use {use!r}; allowed {sorted(USES)}")
        per_use[use] = _names(names, f"per_use.{use}")
    return ProviderConfig(providers, max_fail, cooldown, per_use)


def resolve_eligible(
    cfg: ProviderConfig, registered: Iterable[str], env: Mapping[str, str]
) -> tuple[list[str], dict[str, str]]:
    """Return (eligible provider names in config order, {skipped name: reason})."""
    reg = set(registered)
    eligible: list[str] = []
    skipped: dict[str, str] = {}
    wanted = list(dict.fromkeys([*cfg.providers, *(n for v in cfg.per_use.values() for n in v)]))
    for name in wanted:
        if name not in reg:
            skipped[name] = "not registered (no adapter yet)"
        elif name not in CREDENTIAL_ENV:
            skipped[name] = "no credential rule defined"
        elif not all(env.get(k, "").strip() for k in CREDENTIAL_ENV[name]):
            skipped[name] = f"credentials missing ({', '.join(CREDENTIAL_ENV[name])})"
        else:
            eligible.append(name)
    return eligible, skipped


def _default_refreshers() -> dict[str, Any]:
    """Refresh hooks that import their broker SDK only when called, so a machine without
    (say) the Angel One `SmartApi` package still boots when Angel is not configured."""

    def _kite(env_path: Any) -> Any:
        from src.brokers import kite_auth

        return kite_auth.auto_refresh(env_path)

    def _angel(env_path: Any) -> Any:
        from src.brokers import angel_one_auth

        return angel_one_auth.auto_refresh(env_path)

    return {"kite": _kite, "angel_one": _angel}


def refresh_provider_credentials(
    names: Iterable[str],
    env_path: Any,
    refreshers: Mapping[str, Any] | None = None,
) -> dict[str, bool]:
    """Run each named provider's token refresh if (and only if) its credentials exist.

    Never raises: a failed login is logged and reported as False, matching the old
    `kite_auth_skipped` behaviour so a bad token cannot stop the system booting.
    """
    import logging
    import os

    log = logging.getLogger(__name__)
    fns = refreshers if refreshers is not None else _default_refreshers()
    result: dict[str, bool] = {}
    for name in dict.fromkeys(names):
        rule = CREDENTIAL_ENV.get(name)
        if rule is None or name not in fns:
            continue
        if not all(os.environ.get(k, "").strip() for k in rule):
            continue
        try:
            fns[name](env_path)
            result[name] = True
        except Exception as exc:  # noqa: BLE001 — see docstring
            log.warning("%s_auth_skipped reason=%s", name, exc)
            result[name] = False
    return result
