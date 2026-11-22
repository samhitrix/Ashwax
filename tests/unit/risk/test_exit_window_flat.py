"""Hard exit-window flat-by: paper positions must be force-closed within the
strategy's exit_window (1510-1520), priced at the flatten moment — never spilling
to the 15:35 EOD sweep that stamps the day's last 5m candle (15:25).

Covers:
- _exit_window_start_min reads the strategy exit_window start (fallback 15:10).
- PaperTradingEngine.square_off_open(price_mode="ltp") prices at current LTP and
  stamps exit_timestamp at "now", not the 15:25 candle.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.orchestrator import _exit_window_start_min
from src.paper_trading.paper_engine import PaperTradingEngine

# ── _exit_window_start_min ──────────────────────────────────────────────────────


def _compiled(timeframe: str, exit_window: str) -> SimpleNamespace:
    return SimpleNamespace(
        spec=SimpleNamespace(
            meta=SimpleNamespace(timeframe=timeframe),
            intraday=SimpleNamespace(exit_window=exit_window),
        )
    )


def test_exit_window_start_reads_strategy_window() -> None:
    reg = SimpleNamespace(all_enabled=lambda: [_compiled("5MIN", "1510-1520")])
    assert _exit_window_start_min({"strategy_registry": reg}) == 15 * 60 + 10


def test_exit_window_start_takes_earliest_window() -> None:
    reg = SimpleNamespace(
        all_enabled=lambda: [_compiled("5MIN", "1510-1520"), _compiled("15MIN", "1505-1515")]
    )
    assert _exit_window_start_min({"strategy_registry": reg}) == 15 * 60 + 5


def test_exit_window_start_defaults_without_registry() -> None:
    assert _exit_window_start_min({}) == 15 * 60 + 10


# ── square_off_open(price_mode="ltp") ───────────────────────────────────────────


class _Conn:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.updates: list[tuple] = []

    async def fetch(self, _sql: str) -> list[dict]:
        return self.rows

    async def execute(self, _sql: str, *args: object) -> None:
        self.updates.append(args)


class _Acq:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _Pool:
    def __init__(self, rows: list[dict]) -> None:
        self.conn = _Conn(rows)

    def acquire(self) -> _Acq:
        return _Acq(self.conn)


class _Broker:
    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return {s: 100.0 for s in symbols}


def _engine(rows: list[dict]) -> tuple[PaperTradingEngine, _Pool]:
    eng = PaperTradingEngine.__new__(PaperTradingEngine)
    pool = _Pool(rows)
    eng._pool = pool  # type: ignore[attr-defined]
    eng._broker = _Broker()  # type: ignore[attr-defined]
    eng._tracker = SimpleNamespace(get=lambda _a: None)  # type: ignore[attr-defined]
    eng._gtt_sim = SimpleNamespace(disarm=lambda _a: None)  # type: ignore[attr-defined]
    return eng, pool


async def test_square_off_ltp_prices_at_ltp_and_stamps_now() -> None:
    row = {
        "agent_trade_id": "PAPER-1",
        "symbol": "X",
        "direction": "long",
        "quantity": 10,
        "fill_price": 95.0,
        # entry well before close; the OLD behaviour would stamp the 15:25 candle.
        "entry_timestamp": datetime(2026, 6, 18, 9, 32, tzinfo=ZoneInfo("Asia/Kolkata")),
    }
    eng, pool = _engine([row])

    n = await eng.square_off_open(None, reason="EXIT_WINDOW_FLAT", price_mode="ltp")

    assert n == 1
    aid, exit_price, pnl, exit_ts, reason = pool.conn.updates[0]
    assert aid == "PAPER-1"
    assert exit_price == Decimal("100.00")  # priced at LTP, not the 15:25 candle
    assert pnl == Decimal("50.00")  # (100 - 95) * 10 long
    assert reason == "EXIT_WINDOW_FLAT"  # exit reason persisted
    # stamped at the flatten moment (~now), NOT 15:25
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    assert abs((now - exit_ts).total_seconds()) < 60
    assert not (exit_ts.hour == 15 and exit_ts.minute == 25)


async def test_square_off_ltp_short_pnl_sign() -> None:
    row = {
        "agent_trade_id": "PAPER-2",
        "symbol": "Y",
        "direction": "short",
        "quantity": 5,
        "fill_price": 110.0,
        "entry_timestamp": datetime(2026, 6, 18, 9, 32, tzinfo=ZoneInfo("Asia/Kolkata")),
    }
    eng, pool = _engine([row])

    await eng.square_off_open(None, reason="EXIT_WINDOW_FLAT", price_mode="ltp")

    _aid, exit_price, pnl, _ts, reason = pool.conn.updates[0]
    assert exit_price == Decimal("100.00")
    assert pnl == Decimal("50.00")  # short 110 → 100 = +10 * 5
    assert reason == "EXIT_WINDOW_FLAT"
