"""Broker access for the dashboard API process (spec 3.2: server.py stops owning Kite).

The API runs as its own process (launcher.py), so it cannot reach the orchestrator's
in-memory router or broker stacks. It builds its own READ-ONLY equivalents from the same
config and credentials, the same way the orchestrator does:

* market data: a MarketDataRouter from config/global.json `market_data` + .env credentials
  (src.brokers.factory.build_market_data_router), so a machine with only Angel One or only
  Flattrade still gets real prices. The configured order is kept EXCEPT that rate-capped
  providers (Flattrade: one GetQuotes call per symbol, 200 calls/min per API key, shared with
  the orchestrator's trading) are dropped from the dashboard's router whenever another
  provider is usable -- dashboard polling must never spend the trading budget. When a
  rate-capped provider is the only one, it serves with a longer price reuse window and the
  option-chain page is refused (see server.get_option_chain);
* funds: one account adapter per configured broker, read through get_margins();
* tokens: re-read from .env (written by the launcher/orchestrator logins and by the manual
  Flattrade login) and pushed in. This process never logs in to Angel One or Flattrade.

Nothing here places, modifies or cancels orders. No data is ever invented: a broker that is
not configured, or a call that fails, raises and the endpoint returns its empty shape.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from src.brokers.routing import broker_is_configured
from src.market_data.router import NoMarketDataProviderError

_log = logging.getLogger(__name__)

# Live brokers the dashboard knows how to show, in card order.
BROKERS: tuple[str, ...] = ("kite", "angel_one", "flattrade")
LABELS: dict[str, str] = {
    "kite": "Kite Connect",
    "angel_one": "Angel One",
    "flattrade": "Flattrade",
}
TOKEN_ENV: dict[str, str] = {
    "kite": "KITE_ACCESS_TOKEN",
    "angel_one": "ANGEL_ONE_ACCESS_TOKEN",
    "flattrade": "FLATTRADE_ACCESS_TOKEN",
}


# Below the dashboard's 5s poll (same reasoning as server.py's 3s positions TTL).
LTP_TTL_SECONDS = 3.0
# Providers whose REST budget is too small to poll per symbol (one call per symbol, a
# per-API-key budget shared with the orchestrator). Used by the dashboard only when sole.
RATE_CAPPED_PROVIDERS: frozenset[str] = frozenset({"flattrade"})
# Price reuse when a rate-capped provider serves the dashboard: at least two 5s polls.
RATE_CAPPED_LTP_TTL_SECONDS = 10.0
# Secondary broker checks (health cards, extra funds blocks) must answer within this, and a
# failure/timeout is reused for BROKER_FAILURE_TTL_SECONDS, so a slow broker can never hold
# up the poll that also carries the primary broker's data.
SECONDARY_TIMEOUT_SECONDS = 3.0
BROKER_FAILURE_TTL_SECONDS = 30.0


class BrokerNotConfiguredError(RuntimeError):
    """The broker has no credentials in this process's environment."""


def dashboard_market_data_cfg(global_cfg: dict[str, Any]) -> dict[str, Any]:
    """global_cfg with RATE_CAPPED_PROVIDERS removed from `market_data` providers/per_use.

    A per_use list left empty is dropped (that use then follows `providers`). Returned
    unchanged when there is no market_data block (legacy Kite-only), when it is malformed
    (the router build reports that), or when removing them would leave no provider."""
    block = global_cfg.get("market_data")
    if not isinstance(block, dict) or not isinstance(block.get("providers"), list):
        return global_cfg
    providers = [n for n in block["providers"] if n not in RATE_CAPPED_PROVIDERS]
    if not providers:
        return global_cfg
    new_block = {**block, "providers": providers}
    per_use = block.get("per_use")
    if isinstance(per_use, dict):
        kept = {
            use: [n for n in names if n not in RATE_CAPPED_PROVIDERS]
            for use, names in per_use.items()
            if isinstance(names, list)
        }
        new_block["per_use"] = {use: names for use, names in kept.items() if names}
    return {**global_cfg, "market_data": new_block}


def _default_router_factory() -> Any:
    from src.brokers.factory import _load_global_config, build_market_data_router

    glob = _load_global_config()
    try:
        return build_market_data_router(dashboard_market_data_cfg(glob))
    except NoMarketDataProviderError:
        # Nothing but a rate-capped provider is usable: it serves (sole provider).
        return build_market_data_router(glob)


def _default_broker_factory(name: str) -> Any:
    from src.brokers.factory import create_broker

    return create_broker(name)


def _default_env_reader(path: Path) -> Mapping[str, str | None]:
    from dotenv import dotenv_values

    return dotenv_values(path)


class BrokerAccess:
    """Lazily-built read-only router + per-broker account adapters, with an async TTL cache."""

    def __init__(
        self,
        env_path: Path,
        *,
        router_factory: Callable[[], Any] | None = None,
        broker_factory: Callable[[str], Any] | None = None,
        env: Mapping[str, str] | None = None,
        env_reader: Callable[[Path], Mapping[str, str | None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        token_sync_seconds: float = 60.0,
        router_retry_seconds: float = 60.0,
    ) -> None:
        self._env_path = env_path
        self._router_factory = router_factory or _default_router_factory
        self._broker_factory = broker_factory or _default_broker_factory
        self._env = env if env is not None else os.environ
        self._env_reader = env_reader or _default_env_reader
        self._clock = clock
        self._token_sync_seconds = token_sync_seconds
        self._router_retry_seconds = router_retry_seconds
        self._router: Any = None
        self._router_failed_at: float | None = None
        self._router_error = ""
        self._accounts: dict[str, Any] = {}
        self._tokens: dict[str, str] = {}
        self._last_sync: float | None = None
        self._cache: dict[str, tuple[float, Any]] = {}
        self._failures: dict[str, tuple[float, str]] = {}
        self._ltp_cache: dict[str, tuple[float, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── configuration ────────────────────────────────────────────────────────

    def configured(self) -> list[str]:
        """Brokers with credentials present, in card order."""
        return [n for n in BROKERS if broker_is_configured(n, self._env)]

    def token_present(self, name: str) -> bool:
        return bool(self._tokens.get(name) or self._env.get(TOKEN_ENV[name], "").strip())

    def primary_account(self, legacy: str | None) -> str | None:
        """Broker whose funds the single-account dashboard cards show: the legacy global
        broker.name when it is configured, else the first configured broker. Display only --
        orders never use this (rule R1)."""
        configured = self.configured()
        if legacy in configured:
            return legacy
        return configured[0] if configured else None

    # ── objects ──────────────────────────────────────────────────────────────

    def router(self) -> Any:
        """The read-only market-data router, or None when no provider is usable (retried
        after router_retry_seconds so credentials added later are picked up)."""
        if self._router is None:
            now = self._clock()
            if (
                self._router_failed_at is not None
                and now - self._router_failed_at < self._router_retry_seconds
            ):
                return None
            try:
                self._router = self._router_factory()
            except Exception as exc:  # noqa: BLE001 — no provider => empty data, never mock
                self._router_failed_at = now
                self._router_error = str(exc)
                _log.warning("api_market_data_router_unavailable err=%s", exc)
                return None
            self._router_failed_at = None
            for name, token in self._tokens.items():
                self._router.set_provider_token(name, token)
        return self._router

    def account(self, name: str) -> Any:
        """Read-only account adapter for a configured broker (funds/session only)."""
        if not broker_is_configured(name, self._env):
            raise BrokerNotConfiguredError(f"{LABELS.get(name, name)} is not configured")
        acct = self._accounts.get(name)
        if acct is None:
            acct = self._broker_factory(name)
            if name in self._tokens:
                acct.set_access_token(self._tokens[name])
            self._accounts[name] = acct
        return acct

    def sync_tokens(self, *, force: bool = False) -> bool:
        """Pick up access tokens rotated in .env by the login helpers (throttled).

        Returns True when at least one token changed."""
        now = self._clock()
        if (
            not force
            and self._last_sync is not None
            and now - self._last_sync < self._token_sync_seconds
        ):
            return False
        self._last_sync = now
        try:
            values = self._env_reader(self._env_path)
        except Exception:  # noqa: BLE001 — unreadable .env: keep the tokens we have
            values = {}
        changed = False
        for name, var in TOKEN_ENV.items():
            token = (values.get(var) or self._env.get(var) or "").strip()
            if not token or token == self._tokens.get(name):
                continue
            changed = True
            self._tokens[name] = token
            if self._router is not None:
                self._router.set_provider_token(name, token)
            acct = self._accounts.get(name)
            if acct is not None:
                acct.set_access_token(token)
        return changed

    async def _retry_on_new_token(self, call: Callable[[], Awaitable[Any]]) -> Any:
        """await call(); if it fails and .env now holds a rotated token (a mid-day re-login the
        60s throttle hasn't seen yet), adopt it and retry once."""
        try:
            return await call()
        except Exception:
            if not self.sync_tokens(force=True):
                raise
            return await call()

    # ── cache ────────────────────────────────────────────────────────────────

    async def cached(
        self,
        key: str,
        ttl_s: float,
        fetch: Callable[[], Awaitable[Any]],
        *,
        fail_ttl_s: float = 0.0,
    ) -> Any:
        """fetch()'s result, reused while younger than ttl_s; concurrent callers share one
        in-flight call. A failure is re-raised (as RuntimeError with the same message) for
        fail_ttl_s without calling fetch again; with the default 0 failures are not cached."""
        hit = self._cache.get(key)
        if hit is not None and self._clock() - hit[0] < ttl_s:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit is not None and self._clock() - hit[0] < ttl_s:
                return hit[1]
            fail = self._failures.get(key)
            if fail is not None and self._clock() - fail[0] < fail_ttl_s:
                raise RuntimeError(fail[1])
            try:
                value = await fetch()
            except Exception as exc:
                if fail_ttl_s > 0:
                    self._failures[key] = (self._clock(), str(exc) or type(exc).__name__)
                raise
            self._failures.pop(key, None)
            self._cache[key] = (self._clock(), value)
            return value

    def clear_cache(self) -> None:
        self._cache.clear()
        self._failures.clear()
        self._ltp_cache.clear()

    # ── reads ────────────────────────────────────────────────────────────────

    def require_router(self) -> Any:
        self.sync_tokens()
        router = self.router()
        if router is None:
            raise NoMarketDataProviderError(
                self._router_error or "no market-data provider is configured"
            )
        return router

    def rate_capped(self) -> bool:
        """The dashboard's router includes a rate-capped provider (only ever as the sole one,
        see dashboard_market_data_cfg). False when there is no router."""
        router = self.router()
        names = getattr(router, "names", None) or []
        return any(n in RATE_CAPPED_PROVIDERS for n in names)

    async def ltp(self, keys: list[str]) -> dict[str, dict[str, float]]:
        """Kite-shaped {key: {"last_price": x}} from the configured providers.

        Each symbol's price is reused for LTP_TTL_SECONDS (RATE_CAPPED_LTP_TTL_SECONDS when a
        rate-capped provider serves): several dashboard endpoints ask for the same symbols on
        one poll, and a REST-only provider must not pay for each of them."""
        router = self.require_router()
        ttl = RATE_CAPPED_LTP_TTL_SECONDS if self.rate_capped() else LTP_TTL_SECONDS
        now = self._clock()
        fresh = {
            k: hit[1]
            for k in keys
            if (hit := self._ltp_cache.get(k)) is not None and now - hit[0] < ttl
        }
        missing = [k for k in dict.fromkeys(keys) if k not in fresh]
        if missing:
            prices = await self._retry_on_new_token(lambda: router.get_ltp(missing))
            for k, v in (prices or {}).items():
                fresh[k] = float(v)
                self._ltp_cache[k] = (self._clock(), float(v))
        return {k: {"last_price": v} for k, v in fresh.items()}

    async def quote(self, keys: list[str], *, ltp_fallback: bool = True) -> dict[str, Any]:
        """Kite-shaped quotes; `ohlc.close` (previous close) only from a provider that has
        get_quote (today Kite). Without one: LTP-only rows (day-change unknown), or -- with
        ltp_fallback=False, for callers that are useless without a previous close --
        NoMarketDataProviderError, so no provider calls are spent on them."""
        router = self.require_router()
        try:
            return dict(await self._retry_on_new_token(lambda: router.get_quote(keys)))
        except NoMarketDataProviderError:
            if not ltp_fallback:
                raise
            return await self.ltp(keys)

    async def margins(
        self, name: str, *, timeout_s: float | None = None, fail_ttl_s: float = 0.0
    ) -> Any:
        """The broker's normalized Margin (src.brokers.base.Margin), cached 4s. timeout_s /
        fail_ttl_s bound a secondary broker's read (see SECONDARY_TIMEOUT_SECONDS)."""
        acct = self.account(name)
        self.sync_tokens()
        return await self.cached(
            f"margins:{name}",
            4.0,
            lambda: self._retry_on_new_token(
                lambda: _bounded(acct.get_margins(), timeout_s, LABELS.get(name, name))
            ),
            fail_ttl_s=fail_ttl_s,
        )

    async def session_status(self, name: str) -> Any:
        """The broker's SessionStatus (an authenticated REST call), cached 30s; answers
        within SECONDARY_TIMEOUT_SECONDS and a failure is reused for
        BROKER_FAILURE_TTL_SECONDS."""
        acct = self.account(name)
        self.sync_tokens()
        return await self.cached(
            f"session:{name}",
            30.0,
            lambda: self._retry_on_new_token(
                lambda: _bounded(
                    acct.get_session_status(), SECONDARY_TIMEOUT_SECONDS, LABELS.get(name, name)
                )
            ),
            fail_ttl_s=BROKER_FAILURE_TTL_SECONDS,
        )


async def _bounded(aw: Awaitable[Any], timeout_s: float | None, label: str) -> Any:
    """await aw, raising a readable TimeoutError after timeout_s (None: no bound)."""
    if timeout_s is None:
        return await aw
    try:
        return await asyncio.wait_for(aw, timeout=timeout_s)
    except TimeoutError as exc:
        raise TimeoutError(f"{label} did not answer within {timeout_s:g}s") from exc
