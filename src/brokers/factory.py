"""
Broker factory — creates and configures broker adapter instances.

Entry point for the rest of the system:
  broker = create_broker("kite")        # live Kite trading
  broker = create_broker("paper", ...)  # paper simulation

Configuration comes from:
  - operator.json: trading parameters
  - .env: API keys and credentials
  - kwargs: overrides for testing
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.brokers.base import BrokerInterface
from src.brokers.registry import get_adapter_class, list_brokers
from src.market_data.provider_config import parse_provider_config, resolve_eligible
from src.market_data.router import MarketDataRouter, NoMarketDataProviderError

_log = logging.getLogger(__name__)  # "src.brokers.factory" -> brokers.log
_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "operator.json"


def _load_operator_config() -> dict[str, Any]:
    if _CONFIG_PATH.exists():
        with _CONFIG_PATH.open() as f:
            return dict(json.load(f))
    return {}


_GLOBAL_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "global.json"
_router: MarketDataRouter | None = None


def _load_global_config() -> dict[str, Any]:
    if _GLOBAL_CONFIG_PATH.exists():
        with _GLOBAL_CONFIG_PATH.open(encoding="utf-8") as f:
            return dict(json.load(f))
    return {}


def create_broker(
    broker_name: str | None = None,
    **kwargs: Any,
) -> BrokerInterface:
    """
    Create and return a configured broker adapter.

    broker_name: "kite" | "angel_one" | "paper" | None (reads BROKER env var,
    defaults to "paper")

    For "kite": constructs KiteAdapter; sets access token from KITE_ACCESS_TOKEN
    env var if present (skips full TOTP login at construction time).

    For "angel_one": constructs AngelOneAdapter; sets access/refresh token from
    ANGEL_ONE_ACCESS_TOKEN/ANGEL_ONE_REFRESH_TOKEN env vars if present (skips
    full TOTP login at construction time).

    For "flattrade": constructs FlattradeAdapter with FLATTRADE_ACCESS_TOKEN if
    present; never logs in (the daily login is manual-assisted).

    For "paper": constructs PaperAdapter with capital from operator.json,
    using the configured market-data router (global.json `market_data`) unless
    market_data_broker is overridden via kwargs.
    """
    name: str = broker_name if broker_name is not None else (os.getenv("BROKER") or "paper")
    adapter_cls = get_adapter_class(name)
    _log.debug(
        "create_broker_received requested=%s resolved=%s adapter_cls=%s",
        broker_name,
        name,
        adapter_cls.__name__,
    )

    if name == "kite":
        broker = _create_kite(adapter_cls, **kwargs)
    elif name == "angel_one":
        broker = _create_angel_one(adapter_cls, **kwargs)
    elif name == "flattrade":
        broker = _create_flattrade(adapter_cls, **kwargs)
    elif name == "paper":
        broker = _create_paper(adapter_cls, **kwargs)
    else:
        broker = adapter_cls(**kwargs)
    _log.debug(
        "create_broker_found name=%s action=constructed class=%s", name, type(broker).__name__
    )
    return broker


def _create_kite(adapter_cls: type[BrokerInterface], **kwargs: Any) -> BrokerInterface:
    from src.brokers.adapters.kite.auth import KiteAuth

    auth = KiteAuth.from_env()
    token = os.getenv("KITE_ACCESS_TOKEN", "")
    if token:
        auth.set_access_token(token)

    return adapter_cls(auth=auth)  # type: ignore[call-arg]


def _create_angel_one(adapter_cls: type[BrokerInterface], **kwargs: Any) -> BrokerInterface:
    from src.brokers.adapters.angel_one.auth import AngelOneAuth

    auth = AngelOneAuth.from_env()
    token = os.getenv("ANGEL_ONE_ACCESS_TOKEN", "")
    if token:
        auth.set_access_token(token)

    return adapter_cls(auth=auth)  # type: ignore[call-arg]


def _create_flattrade(adapter_cls: type[BrokerInterface], **kwargs: Any) -> BrokerInterface:
    from src.brokers.adapters.flattrade.auth import FlattradeAuth

    # No login here: Flattrade's daily login is manual (python -m src.brokers.flattrade_auth).
    auth = FlattradeAuth.from_env()
    token = os.getenv("FLATTRADE_ACCESS_TOKEN", "")
    if token:
        auth.set_access_token(token)

    return adapter_cls(auth=auth)  # type: ignore[call-arg]


# Providers the router can construct. A new broker's adapter task adds its builder here,
# only once that adapter's market data really works (flattrade: task F2).
_PROVIDER_BUILDERS = {
    "kite": "_create_kite",
    "angel_one": "_create_angel_one",
    "flattrade": "_create_flattrade",
}


def build_market_data_router(
    global_cfg: dict[str, Any] | None = None, env: Mapping[str, str] | None = None
) -> MarketDataRouter:
    """Build a router from config/global.json `market_data` and available credentials.

    Raises:
        NoMarketDataProviderError: nothing eligible; message lists every skip reason.
    """
    cfg = parse_provider_config(global_cfg if global_cfg is not None else _load_global_config())
    eligible, skipped = resolve_eligible(cfg, list_brokers(), os.environ if env is None else env)
    for name, reason in skipped.items():
        _log.warning("market_data_provider_skipped provider=%s reason=%s", name, reason)
    providers: dict[str, BrokerInterface] = {}
    for name in eligible:
        if name not in _PROVIDER_BUILDERS:
            skipped[name] = (
                f"no market-data builder yet ({name} market data arrives with a later task)"
            )
            _log.warning("market_data_provider_skipped provider=%s reason=%s", name, skipped[name])
            continue
        builder = globals()[_PROVIDER_BUILDERS[name]]
        try:
            providers[name] = builder(get_adapter_class(name))
        except Exception as exc:  # noqa: BLE001 — an adapter that cannot load is just ineligible
            skipped[name] = f"build failed: {exc!r}"
            _log.warning("market_data_provider_skipped provider=%s reason=%s", name, skipped[name])
    if not providers:
        raise NoMarketDataProviderError(
            "No market-data provider is usable. Configure credentials for at least one of "
            f"{list(cfg.providers)} (see .env.example). Skipped: {skipped}"
        )
    _log.info("market_data_router_built providers=%s", list(providers))
    if len(providers) > 1 and next(iter(providers)) != "kite":
        _log.warning(
            "market_data_primary_is_not_kite primary=%s also_eligible=%s - "
            "run scripts/compare_provider_data.py before relying on it",
            next(iter(providers)),
            list(providers)[1:],
        )
    return MarketDataRouter(providers, cfg)


def get_market_data_router() -> MarketDataRouter:
    """Process-wide router (one shared set of adapters and rate limiters)."""
    global _router
    if _router is None:
        _router = build_market_data_router()
    return _router


def peek_market_data_router() -> MarketDataRouter | None:
    """The cached router if one was built; never builds."""
    return _router


def reset_market_data_router() -> None:
    """Drop the cached router (tests)."""
    global _router
    _router = None


def _create_paper(adapter_cls: type[BrokerInterface], **kwargs: Any) -> BrokerInterface:
    config = _load_operator_config()
    capital_config = config.get("capital", {})
    default_capital = (
        float(capital_config["total_capital_rupees"])
        if isinstance(capital_config, dict)
        else float(capital_config)
    )
    initial_capital = float(kwargs.pop("initial_capital", default_capital))
    slippage_bps = float(kwargs.pop("slippage_bps", 5.0))
    kwargs.setdefault("enforce_order_windows", True)

    market_broker: Any = kwargs.pop("market_data_broker", None) or get_market_data_router()

    return adapter_cls(  # type: ignore[call-arg]
        market_data_broker=market_broker,
        initial_capital=initial_capital,
        slippage_bps=slippage_bps,
        **kwargs,
    )
