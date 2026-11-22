"""Market-data router: config-ordered providers with health-based failover.

Duck-types the read-only slice of BrokerInterface that PaperAdapter and the data loops
use. Order placement never goes through here — orders follow the per-strategy broker.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.market_data.provider_config import ProviderConfig

_log = logging.getLogger(__name__)


class NoMarketDataProviderError(RuntimeError):
    """No configured market-data provider is eligible. Never replaced by mock data."""


@dataclass
class _Health:
    failures: int = 0
    skip_until: float = 0.0


class MarketDataRouter:
    """Serve market data from the first healthy provider in config order.

    Deliberately has NO set_access_token: tokens are broker-specific and a broadcast would
    push a Kite token into the Angel adapter. Refresh each provider via `provider(name)`.
    """

    def __init__(
        self,
        providers: dict[str, Any],
        config: ProviderConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not providers:
            raise ValueError("MarketDataRouter needs at least one provider")
        self._providers = dict(providers)
        self._cfg = config
        self._clock = clock
        self._health: dict[str, _Health] = {n: _Health() for n in self._providers}

    @property
    def names(self) -> list[str]:
        return list(self._providers)

    def provider(self, name: str) -> Any | None:
        return self._providers.get(name)

    def set_provider_token(self, name: str, token: str) -> None:
        """Push an access token to one named provider.

        Tokens are broker-specific, so callers name the provider; this is the explicit
        replacement for a broadcast ``set_access_token`` (deliberately not defined here).
        Unknown providers, or ones without ``set_access_token``, are silently ignored.
        """
        set_token = getattr(self._providers.get(name), "set_access_token", None)
        if callable(set_token):
            set_token(token)

    def _order(self, use: str) -> list[str]:
        wanted = self._cfg.per_use.get(use, self._cfg.providers)
        return [n for n in wanted if n in self._providers]

    def _record_failure(self, name: str, exc: Exception) -> None:
        h = self._health[name]
        h.failures += 1
        if h.failures >= self._cfg.max_consecutive_failures:
            h.skip_until = self._clock() + self._cfg.cooldown_seconds
            _log.warning(
                "market_data_provider_cooldown provider=%s failures=%d seconds=%.0f err=%s",
                name,
                h.failures,
                self._cfg.cooldown_seconds,
                exc,
            )

    async def _call(
        self, use: str, method: str, *args: Any, fallback: str | None = None
    ) -> tuple[Any, str]:
        order = self._order(use)
        now = self._clock()
        candidates = [n for n in order if self._health[n].skip_until <= now]
        cooling = [n for n in order if n not in candidates]
        if not candidates:  # everyone cooling: try anyway rather than black out
            candidates, cooling = order, []
        last_exc: Exception | None = None  # a real provider failure
        gap_exc: KeyError | None = None  # symbol the provider cannot resolve
        # Cooling providers are a last resort, used only when the healthy ones merely lacked the
        # capability (a real failure from them means the data is down, not that Kite is needed).
        for stage in (candidates, cooling):
            if stage is cooling and last_exc is not None:
                break
            for name in stage:
                adapter = self._providers[name]
                fn = getattr(adapter, method, None)
                if not fn and fallback:
                    fn = getattr(adapter, fallback, None)
                if fn is None:
                    continue
                try:
                    result = await fn(*args)
                except NotImplementedError:
                    continue  # capability gap, not ill health
                except KeyError as exc:
                    gap_exc = exc  # symbol-specific: must not cool the provider for every symbol
                    continue
                except Exception as exc:  # noqa: BLE001 — failover is the point
                    last_exc = exc
                    self._record_failure(name, exc)
                    level = (
                        logging.WARNING
                        if self._health[name].failures in (1, self._cfg.max_consecutive_failures)
                        else logging.DEBUG
                    )
                    _log.log(
                        level,
                        "market_data_failover provider=%s method=%s err=%s",
                        name,
                        method,
                        exc,
                    )
                    continue
                self._health[name] = _Health()
                return result, name
        if last_exc is not None:
            raise last_exc
        if gap_exc is not None:
            raise gap_exc
        raise NoMarketDataProviderError(f"no provider could serve {method} (tried {order})")

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return (await self._call("ltp", "get_ltp", symbols))[0]  # type: ignore[no-any-return]

    async def priority_ltp(self, symbols: list[str]) -> dict[str, float]:
        out, _ = await self._call("ltp", "priority_ltp", symbols, fallback="get_ltp")
        return out  # type: ignore[no-any-return]

    async def get_quote(self, symbols: list[str]) -> dict[str, Any]:
        """Rich quotes (last_price + ohlc incl. previous close) from the first healthy
        provider that implements get_quote; providers without it are skipped.

        Raises:
            NoMarketDataProviderError: no eligible provider has get_quote (callers fall
                back to get_ltp and treat previous close as unknown, never invent it).
        """
        return (await self._call("ltp", "get_quote", symbols))[0]  # type: ignore[no-any-return]

    async def get_ohlcv_with_source(
        self, symbol: str, interval: str, days: int
    ) -> tuple[pd.DataFrame, str]:
        df, name = await self._call("ohlcv", "get_ohlcv", symbol, interval, days)
        return df, name

    async def get_ohlcv(self, symbol: str, interval: str, days: int) -> pd.DataFrame:
        return (await self.get_ohlcv_with_source(symbol, interval, days))[0]

    async def get_depth(self, symbol: str) -> Any:
        return (await self._call("depth", "get_depth", symbol))[0]

    async def get_instruments(self, exchange: str) -> Any:
        return (await self._call("options_chain", "get_instruments", exchange))[0]

    async def get_option_quotes(self, symbols: list[str]) -> Any:
        return (await self._call("options_chain", "get_option_quotes", symbols))[0]

    async def get_order_margin(self, orders: list[Any]) -> Any:
        return (await self._call("options_chain", "get_order_margin", orders))[0]

    async def load_instrument_tokens(self, exchange: str = "NSE") -> int:
        """Pre-warm the primary provider only; the others resolve tokens lazily."""
        try:
            return int((await self._call("ohlcv", "load_instrument_tokens", exchange))[0])
        except NoMarketDataProviderError:
            return 0

    def _primary(self, use: str = "ltp") -> Any:
        return self._providers[self._order(use)[0]]

    async def subscribe_ticks(self, tokens: list[str]) -> None:
        await self._primary().subscribe_ticks(tokens)  # tokens are provider-specific

    async def unsubscribe_ticks(self, tokens: list[str]) -> None:
        await self._primary().unsubscribe_ticks(tokens)

    def normalize_tick(self, raw: dict[str, Any]) -> Any:
        return self._primary().normalize_tick(raw)

    def normalize_order(self, raw: dict[str, Any]) -> Any:
        return self._primary().normalize_order(raw)

    def normalize_position(self, raw: dict[str, Any]) -> Any:
        return self._primary().normalize_position(raw)

    def normalize_error(self, raw: dict[str, Any]) -> Any:
        return self._primary().normalize_error(raw)
