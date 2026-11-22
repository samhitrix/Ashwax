"""
EquityIngester — Core 0: KiteWebSocket → SHM blocks → trigger_queue.

Per docs/09-multiprocess-engine.md IPC data flow:
  KiteWebSocket → _on_tick()
    → seqlock write → TickBlock (price, bid, ask, vol, ts, seq)
    → FiveMinCandleBuilder → seqlock write → CandleBlock
    → ORBBlock update
    → trigger_queue.put_nowait(ticker_idx)  ← one int, zero pickle

Latency: Tick → SHM write < 50µs | SHM → Worker trigger < 100µs
Phase 1: single-process asyncio. Phase 2: dedicated process on Core 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.brokers.base import BrokerInterface

from src.shared.ohlcv_shm import CandleBlock, ORBBlock, SHMRegistry, TickBlock

_LOG = logging.getLogger(__name__)  # "src.ingestion.tick_ingester" -> ingestion.log
_ORB_MINUTES = 30  # first 30 min = opening range


class FiveMinCandleBuilder:
    """Accumulates ticks into 5-min candles, returns completed candle on bar close."""

    _BUCKET_NS = 300 * 1_000_000_000  # 5 min in nanoseconds

    def __init__(self) -> None:
        self._state: dict[str, dict[str, Any]] = {}

    def update(self, symbol: str, price: float, volume: int, ts_ns: int) -> dict[str, Any] | None:
        bucket = ts_ns // self._BUCKET_NS
        s = self._state.get(symbol)

        if s is None or s["bucket"] < bucket:
            completed = dict(s) if (s is not None and s["bucket"] < bucket) else None
            self._state[symbol] = {
                "bucket": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
            }
            return completed

        s["high"] = max(s["high"], price)
        s["low"] = min(s["low"], price)
        s["close"] = price
        s["volume"] += volume
        return None


class TickIngester:
    """
    Receives broker ticks → writes TickBlock/CandleBlock/ORBBlock → queues worker triggers.

    Phase 1: single-process. Phase 2: Core 0 dedicated process with CPU affinity.
    """

    def __init__(
        self,
        broker: BrokerInterface,
        tick_block: TickBlock,
        candle_block: CandleBlock,
        orb_block: ORBBlock,
        registry: SHMRegistry,
        trigger_queue: asyncio.Queue[int],
    ) -> None:
        self._broker = broker
        self._ticks = tick_block
        self._candles = candle_block
        self._orb = orb_block
        self._registry = registry
        self._trigger_q = trigger_queue
        self._candle_builder = FiveMinCandleBuilder()
        self._orb_state: dict[str, dict[str, Any]] = {}
        self._running = False
        # WebSocket health tracking (read via health_snapshot() → DB → /api/ws_health)
        self._subscribed: list[str] = []
        self._last_tick: dict[str, float] = {}
        self._tick_count = 0
        self._window_start = time.monotonic()

    async def start(self, symbols: list[str]) -> None:
        self._running = True
        self._subscribed = list(symbols)
        self._window_start = time.monotonic()
        for sym in symbols:
            self._registry.register(sym)
        await self._broker.subscribe_ticks(symbols, self._on_tick)  # type: ignore[call-arg]
        _LOG.info("tick_ingester_started symbols=%d", len(symbols))

    async def stop(self) -> None:
        self._running = False

    def health_snapshot(self) -> dict[str, Any]:
        """Current WebSocket health — real counters, persisted to DB by the
        orchestrator heartbeat loop so the separate API process can read it.
        Resets the tick-rate window on each read."""
        now = time.monotonic()
        stale = [s for s in self._subscribed if (now - self._last_tick.get(s, 0.0)) > 10.0]
        elapsed = max(now - self._window_start, 1e-9)
        tps = round(self._tick_count / elapsed, 1)
        self._tick_count = 0
        self._window_start = now
        return {
            "status": "CONNECTED" if self._running else "DISCONNECTED",
            "subscribed": len(self._subscribed),
            "active": len(self._subscribed) - len(stale),
            "stale": stale[:50],
            "ticks_per_second": tps,
        }

    def _on_tick(self, tick: dict[str, Any]) -> None:
        if not self._running:
            return
        symbol: str = tick.get("tradingsymbol") or tick.get("symbol", "")
        if not symbol:
            return

        self._last_tick[symbol] = time.monotonic()
        self._tick_count += 1

        price = float(tick.get("last_price", 0))
        bid = float(tick.get("best_bid_price", 0))
        ask = float(tick.get("best_ask_price", 0))
        volume = int(tick.get("last_traded_quantity", 0))
        ts_ns = int(tick.get("exchange_timestamp") or time.time() * 1e9)

        self._ticks.write(symbol, price, bid, ask, volume, ts_ns)

        candle = self._candle_builder.update(symbol, price, volume, ts_ns)
        if candle:
            self._candles.write(
                symbol,
                open=candle["open"],
                high=candle["high"],
                low=candle["low"],
                close=candle["close"],
                volume=candle["volume"],
                timestamp_ns=ts_ns,
            )

        self._update_orb(symbol, price, ts_ns)

        idx = self._registry.index_of(symbol)
        if idx is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._trigger_q.put_nowait(idx)

    def _update_orb(self, symbol: str, price: float, ts_ns: int) -> None:
        s = self._orb_state.get(symbol)
        if s is None:
            self._orb_state[symbol] = {"high": price, "low": price, "start_ts": ts_ns}
            return
        if (ts_ns - s["start_ts"]) / 1e9 <= _ORB_MINUTES * 60:
            s["high"] = max(s["high"], price)
            s["low"] = min(s["low"], price)
            self._orb.set_orb(symbol, s["high"], s["low"])
