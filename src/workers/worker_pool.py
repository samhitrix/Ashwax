"""
WorkerPool — manages StrategyWorker lifecycle and hot-reload.

Per docs/09-multiprocess-engine.md:
  recycle(): new strategies pushed to workers without process restart.
  Workers drain current tick, swap compiled set, resume.
Phase 1: asyncio tasks. Phase 2: separate OS processes with CPU affinity.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.shared.ohlcv_shm import CandleBlock, ORBBlock, SHMRegistry, TickBlock
from src.shared.signal_ring import SignalRing
from src.workers.strategy_worker import StrategyWorker

_LOG = logging.getLogger(__name__)  # "src.workers.worker_pool" -> workers.log


class WorkerPool:
    """N StrategyWorkers sharing a common trigger queue. Hot-reload via recycle()."""

    def __init__(
        self,
        n_workers: int,
        tick_block: TickBlock,
        candle_block: CandleBlock,
        orb_block: ORBBlock,
        registry: SHMRegistry,
        signal_ring: SignalRing,
        trigger_queue_capacity: int = 1024,
    ) -> None:
        self._trigger_queue: asyncio.Queue[int] = asyncio.Queue(maxsize=trigger_queue_capacity)
        self._workers = [
            StrategyWorker(
                worker_id=i,
                tick_block=tick_block,
                candle_block=candle_block,
                orb_block=orb_block,
                registry=registry,
                trigger_queue=self._trigger_queue,
                signal_ring=signal_ring,
            )
            for i in range(n_workers)
        ]
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self, initial_strategies: list[Any] | None = None) -> None:
        if initial_strategies:
            for w in self._workers:
                w.load_strategies(initial_strategies)
        for w in self._workers:
            self._tasks.append(asyncio.create_task(w.run(), name=f"worker-{w._worker_id}"))  # noqa: SLF001
        _LOG.info("worker_pool_started n=%d", len(self._workers))

    async def stop(self) -> None:
        for w in self._workers:
            w.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def recycle(self, new_strategies: list[Any]) -> None:
        """Hot-reload compiled strategies into all workers without restart."""
        for w in self._workers:
            w.load_strategies(new_strategies)
        _LOG.info("worker_pool_recycled count=%d", len(new_strategies))

    @property
    def trigger_queue(self) -> asyncio.Queue[int]:
        return self._trigger_queue
