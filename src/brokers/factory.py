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
from pathlib import Path
from typing import Any

from src.brokers.base import BrokerInterface
from src.brokers.registry import get_adapter_class

_log = logging.getLogger(__name__)  # "src.brokers.factory" -> brokers.log
_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "operator.json"


def _load_operator_config() -> dict[str, Any]:
    if _CONFIG_PATH.exists():
        with _CONFIG_PATH.open() as f:
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

    For "paper": constructs PaperAdapter with capital from operator.json,
    using a real KiteAdapter as market_data_broker unless overridden via kwargs.
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

    market_broker: BrokerInterface = kwargs.pop("market_data_broker", None) or _create_kite(
        get_adapter_class("kite")
    )

    return adapter_cls(  # type: ignore[call-arg]
        market_data_broker=market_broker,
        initial_capital=initial_capital,
        slippage_bps=slippage_bps,
        **kwargs,
    )
