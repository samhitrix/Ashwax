"""
SPSC ring buffer — workers push signals, CRM pops them.

Per docs/09-multiprocess-engine.md:
  32-byte slot, zero pickle, all fields in one cache line. < 5µs target.

Phase 1: asyncio.Queue (single-process). Phase 2: mmap-backed SPSC ring.
Interface is identical across both phases.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class SignalSlot:
    """32-byte signal payload — zero-copy IPC slot."""

    ticker_idx: int  # index into SHMRegistry
    strategy_idx: int  # index into compiled strategy list
    worker_id: int
    signal_type: int  # 1=LONG  2=SHORT  0=NONE
    confidence: float  # 0.0–1.0
    price: float  # entry price hint
    timestamp_ns: int  # nanosecond epoch


class SignalRing:
    """
    SPSC ring buffer between strategy workers and CRM.

    Phase 1: asyncio.Queue. Phase 2: mmap-backed ring (same interface).
    """

    def __init__(self, capacity: int = 256) -> None:
        self._queue: asyncio.Queue[SignalSlot] = asyncio.Queue(maxsize=capacity)

    def push(self, slot: SignalSlot) -> bool:
        """Non-blocking push. Returns False if full (signal dropped, never blocks)."""
        try:
            self._queue.put_nowait(slot)
            return True
        except asyncio.QueueFull:
            return False

    async def wait_and_pop(self) -> SignalSlot:
        """Block until a signal is available."""
        return await self._queue.get()

    def try_pop(self) -> SignalSlot | None:
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    @property
    def size(self) -> int:
        return self._queue.qsize()
