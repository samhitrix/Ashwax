"""
StrategyWorker — evaluates compiled strategies against SHM tick data.

Per docs/09-multiprocess-engine.md:
  Tier 1 (<500µs): scalar comparisons vs SHM data
  Tier 2 (<5ms):   numpy vectorized on local candle copy

  Drains trigger_queue → reads SHM → evaluates → pushes to SignalRing.
  Phase 1: single async loop. Phase 2: dedicated process, CPU-pinned.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from src.shared.ohlcv_shm import CandleBlock, ORBBlock, SHMRegistry, TickBlock
from src.shared.signal_ring import SignalRing, SignalSlot

_LOG = logging.getLogger(__name__)  # "src.workers.strategy_worker" -> workers.log


class StrategyWorker:
    """
    Reads trigger_queue → evaluates strategies → pushes SignalSlots to ring.

    Strategies are hot-reloaded via load_strategies() — no process restart.
    """

    def __init__(
        self,
        worker_id: int,
        tick_block: TickBlock,
        candle_block: CandleBlock,
        orb_block: ORBBlock,
        registry: SHMRegistry,
        trigger_queue: asyncio.Queue[int],
        signal_ring: SignalRing,
    ) -> None:
        self._worker_id = worker_id
        self._ticks = tick_block
        self._candles = candle_block
        self._orb = orb_block
        self._registry = registry
        self._trigger_q = trigger_queue
        self._ring = signal_ring
        self._strategies: list[Any] = []
        self._running = False

    def load_strategies(self, strategies: list[Any]) -> None:
        """Atomic hot-reload — swap compiled strategy list without restart."""
        self._strategies = list(strategies)
        _LOG.info("worker_%d strategies_loaded count=%d", self._worker_id, len(self._strategies))

    async def run(self) -> None:
        self._running = True
        _LOG.info("worker_%d started", self._worker_id)
        while self._running:
            try:
                ticker_idx = await asyncio.wait_for(self._trigger_q.get(), timeout=1.0)
            except TimeoutError:
                continue

            symbol = self._registry.symbol_of(ticker_idx)
            if symbol is None:
                continue

            tick = self._ticks.read(symbol)
            if tick is None or tick.price <= 0:
                continue

            candle = self._candles.read(symbol)
            ts_ns = int(time.time() * 1e9)

            for idx, strategy in enumerate(self._strategies):
                try:
                    slot = self._evaluate(strategy, idx, symbol, tick.price, candle, ts_ns)
                    if slot:
                        self._ring.push(slot)
                except Exception as exc:
                    _LOG.error("worker_%d eval_err strat=%d err=%s", self._worker_id, idx, exc)

    def stop(self) -> None:
        self._running = False

    def _evaluate(
        self,
        strategy: Any,
        strat_idx: int,
        symbol: str,
        price: float,
        candle: Any,
        ts_ns: int,
    ) -> SignalSlot | None:
        if not hasattr(strategy, "evaluate_tick"):
            return None
        signal_type: int = strategy.evaluate_tick(symbol, price, candle)
        if signal_type == 0:
            return None
        ticker_idx = self._registry.index_of(symbol)
        if ticker_idx is None:
            return None
        return SignalSlot(
            ticker_idx=ticker_idx,
            strategy_idx=strat_idx,
            worker_id=self._worker_id,
            signal_type=signal_type,
            confidence=getattr(strategy, "last_confidence", 0.7),
            price=price,
            timestamp_ns=ts_ns,
        )
