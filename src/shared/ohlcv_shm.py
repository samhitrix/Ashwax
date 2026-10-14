"""
Shared memory blocks for the multiprocess engine.

Per docs/09-multiprocess-engine.md:
  TickBlock   — price, bid, ask, vol, ts, seq (seqlock)
  CandleBlock — OHLCV, VWAP, EMA9, EMA21 (seqlock)
  ORBBlock    — ORB high/low/is_active per symbol
  SHMRegistry — maps symbol → ticker_idx for trigger queue

Seqlock: writer marks seq ODD (in-progress), writes fields, marks seq EVEN (safe).
Reader spins until even seq. ctypes fence prevents compiler reordering.
Phase 1: in-process dicts (single process). Phase 2: mmap-backed shared_memory.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field
from typing import Any


def _fence() -> None:
    ctypes.memmove(ctypes.c_char_p(0), ctypes.c_char_p(0), 0)  # noqa: S113


@dataclass
class TickData:
    price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    volume: int = 0
    timestamp_ns: int = 0
    seq: int = 0


@dataclass
class CandleData:
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    vwap: float = 0.0
    ema9: float = 0.0
    ema21: float = 0.0
    timestamp_ns: int = 0
    seq: int = 0


@dataclass
class ORBData:
    orb_high: float = 0.0
    orb_low: float = 0.0
    is_active: bool = False
    seq: int = 0


class TickBlock:
    """Seqlock tick store. Core 0 writes, workers read."""

    def __init__(self, max_symbols: int = 500) -> None:
        self._ticks: dict[str, TickData] = {}
        self._max = max_symbols

    def write(
        self, symbol: str, price: float, bid: float, ask: float, volume: int, timestamp_ns: int
    ) -> None:
        if symbol not in self._ticks and len(self._ticks) < self._max:
            self._ticks[symbol] = TickData()
        td = self._ticks.get(symbol)
        if td is None:
            return
        td.seq += 1
        _fence()
        td.price = price
        td.bid = bid
        td.ask = ask
        td.volume = volume
        td.timestamp_ns = timestamp_ns
        _fence()
        td.seq += 1

    def read(self, symbol: str) -> TickData | None:
        td = self._ticks.get(symbol)
        if td is None:
            return None
        deadline = time.monotonic() + 0.001
        while td.seq % 2 != 0:
            if time.monotonic() > deadline:
                return None
        return TickData(
            price=td.price,
            bid=td.bid,
            ask=td.ask,
            volume=td.volume,
            timestamp_ns=td.timestamp_ns,
            seq=td.seq,
        )

    def get_ltp(self, symbol: str) -> float:
        td = self._ticks.get(symbol)
        return td.price if td else 0.0


class CandleBlock:
    """Seqlock 5-min candle store."""

    def __init__(self, max_symbols: int = 500) -> None:
        self._candles: dict[str, CandleData] = {}
        self._max = max_symbols

    def write(self, symbol: str, **kwargs: Any) -> None:
        if symbol not in self._candles and len(self._candles) < self._max:
            self._candles[symbol] = CandleData()
        cd = self._candles.get(symbol)
        if cd is None:
            return
        cd.seq += 1
        _fence()
        for k, v in kwargs.items():
            if hasattr(cd, k):
                setattr(cd, k, v)
        _fence()
        cd.seq += 1

    def read(self, symbol: str) -> CandleData | None:
        return self._candles.get(symbol)


class ORBBlock:
    """Opening Range Breakout levels per symbol."""

    def __init__(self) -> None:
        self._orb: dict[str, ORBData] = {}

    def set_orb(self, symbol: str, high: float, low: float) -> None:
        if symbol not in self._orb:
            self._orb[symbol] = ORBData()
        orb = self._orb[symbol]
        orb.seq += 1
        orb.orb_high = high
        orb.orb_low = low
        orb.is_active = True
        orb.seq += 1

    def read(self, symbol: str) -> ORBData | None:
        return self._orb.get(symbol)


@dataclass
class SHMRegistry:
    """Maps symbol → ticker_idx for trigger queue. Phase 1: pure dict."""

    _symbols: dict[str, int] = field(default_factory=dict)

    def register(self, symbol: str) -> int:
        if symbol not in self._symbols:
            self._symbols[symbol] = len(self._symbols)
        return self._symbols[symbol]

    def index_of(self, symbol: str) -> int | None:
        return self._symbols.get(symbol)

    def symbol_of(self, idx: int) -> str | None:
        for sym, i in self._symbols.items():
            if i == idx:
                return sym
        return None

    def all_symbols(self) -> list[str]:
        return list(self._symbols)
