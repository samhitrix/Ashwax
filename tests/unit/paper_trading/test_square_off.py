"""Unit tests for PaperTradingEngine.square_off_open — DB calls mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from src.paper_trading.paper_engine import PaperTradingEngine

_IST = ZoneInfo("Asia/Kolkata")


def _pool(rows: list[dict]) -> tuple[MagicMock, AsyncMock]:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    pool = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def _engine(pool: MagicMock) -> PaperTradingEngine:
    return PaperTradingEngine(MagicMock(), pool=pool)


def _ohlcv(close: float, ts: datetime) -> MagicMock:
    repo = MagicMock()
    repo.fetch_candles = AsyncMock(return_value=[{"ts": ts, "close": close}])
    return repo


async def test_squares_off_at_entry_day_close() -> None:
    entry = datetime(2026, 6, 17, 9, 30, tzinfo=_IST)
    close_ts = datetime(2026, 6, 17, 15, 25, tzinfo=_IST)
    rows = [
        {
            "agent_trade_id": "PAPER-1",
            "symbol": "INFY",
            "direction": "long",
            "quantity": 10,
            "fill_price": 100.0,
            "entry_timestamp": entry,
        }
    ]
    pool, conn = _pool(rows)
    eng = _engine(pool)
    n = await eng.square_off_open(_ohlcv(110.0, close_ts), prior_day_only=True)
    assert n == 1
    sql, *args = conn.execute.call_args[0]
    assert "UPDATE paper_trades" in sql and "CLOSED" in sql
    assert args[0] == "PAPER-1"
    assert float(args[1]) == 110.0  # exit_price = entry-day close
    assert float(args[2]) == 100.0  # pnl = (110-100)*10
    assert args[3] == close_ts  # exit_timestamp = candle ts


async def test_short_pnl_sign() -> None:
    entry = datetime(2026, 6, 17, 9, 30, tzinfo=_IST)
    close_ts = datetime(2026, 6, 17, 15, 25, tzinfo=_IST)
    rows = [
        {
            "agent_trade_id": "PAPER-2",
            "symbol": "TCS",
            "direction": "short",
            "quantity": 5,
            "fill_price": 200.0,
            "entry_timestamp": entry,
        }
    ]
    pool, conn = _pool(rows)
    n = await _engine(pool).square_off_open(_ohlcv(180.0, close_ts), prior_day_only=True)
    assert n == 1
    args = conn.execute.call_args[0][1:]
    assert float(args[2]) == 100.0  # short: (200-180)*5


async def test_scratch_when_no_candle() -> None:
    entry = datetime(2026, 6, 17, 9, 30, tzinfo=_IST)
    rows = [
        {
            "agent_trade_id": "PAPER-3",
            "symbol": "WIPRO",
            "direction": "long",
            "quantity": 7,
            "fill_price": 50.0,
            "entry_timestamp": entry,
        }
    ]
    pool, conn = _pool(rows)
    repo = MagicMock()
    repo.fetch_candles = AsyncMock(return_value=[])  # no candle
    n = await _engine(pool).square_off_open(repo, prior_day_only=True)
    assert n == 1
    args = conn.execute.call_args[0][1:]
    assert float(args[1]) == 50.0  # exit = fill (scratch)
    assert float(args[2]) == 0.0  # pnl = 0
    assert args[3] == entry  # exit_ts = entry_timestamp


async def test_no_pool_returns_zero() -> None:
    eng = PaperTradingEngine(MagicMock(), pool=None)
    assert await eng.square_off_open(None) == 0


async def test_prior_day_only_filters_sql() -> None:
    pool, conn = _pool([])
    await _engine(pool).square_off_open(None, prior_day_only=True)
    sql = conn.fetch.call_args[0][0]
    assert "Asia/Kolkata" in sql and "<" in sql


async def test_never_squares_off_cnc_swing_positions() -> None:
    """Regression: this sweep is the intraday (MIS) exit-window/EOD flat-by. It
    must never touch CNC (swing/delivery) positions — those hold overnight and
    exit only through their own SL/target/CRM path. Found 2026-07-29 after a
    launcher restart's first flat-by tick force-closed same-minute swing fills
    (PARADEEP/RADICO) as EXIT_WINDOW_FLAT."""
    pool, conn = _pool([])
    await _engine(pool).square_off_open(None)
    sql = conn.fetch.call_args[0][0]
    assert "product = 'MIS'" in sql


async def test_untracks_closed_position_from_every_order_monitor() -> None:
    """Regression (found 2026-09-16): square_off_open closed the DB row and the
    legacy in-process tracker, but never told OrderMonitor — so a position
    flattened here (EOD/exit-window/daily-loss-breaker) stayed in
    OrderMonitor._positions forever, and every consumer of get_monitored()
    (dashboard, the periodic position-update notification) kept reporting an
    already-closed trade as still open for the rest of the day. Must untrack
    from EVERY monitor passed in, not just one stack."""
    entry = datetime(2026, 9, 16, 9, 30, tzinfo=_IST)
    rows = [
        {
            "agent_trade_id": "PAPER-STALE-1",
            "symbol": "CGCL",
            "direction": "long",
            "quantity": 10,
            "fill_price": 100.0,
            "entry_timestamp": entry,
        }
    ]
    pool, conn = _pool(rows)
    eng = _engine(pool)
    om_intraday = MagicMock()
    om_swing = MagicMock()  # e.g. the swing stack's monitor — never tracked this id
    n = await eng.square_off_open(_ohlcv(110.0, entry), order_monitors=[om_intraday, om_swing])
    assert n == 1
    om_intraday.untrack.assert_called_once_with("PAPER-STALE-1")
    om_swing.untrack.assert_called_once_with("PAPER-STALE-1")


async def test_untrack_failure_on_one_monitor_does_not_block_others_or_raise() -> None:
    """A monitor that raises on untrack (e.g. a stale/torn-down stack) must
    never stop the DB close from completing or stop the OTHER monitor's
    untrack from running."""
    rows = [
        {
            "agent_trade_id": "PAPER-STALE-2",
            "symbol": "CGCL",
            "direction": "long",
            "quantity": 10,
            "fill_price": 100.0,
            "entry_timestamp": datetime(2026, 9, 16, 9, 30, tzinfo=_IST),
        }
    ]
    pool, conn = _pool(rows)
    eng = _engine(pool)
    broken_om = MagicMock()
    broken_om.untrack.side_effect = RuntimeError("torn down")
    healthy_om = MagicMock()
    n = await eng.square_off_open(None, order_monitors=[broken_om, healthy_om])
    assert n == 1  # DB close still counted
    healthy_om.untrack.assert_called_once_with("PAPER-STALE-2")


async def test_no_order_monitors_passed_is_safe() -> None:
    """order_monitors is optional — existing/future callers that don't pass it
    must keep working exactly as before (just without the untrack fix)."""
    pool, conn = _pool(
        [
            {
                "agent_trade_id": "PAPER-1",
                "symbol": "INFY",
                "direction": "long",
                "quantity": 10,
                "fill_price": 100.0,
                "entry_timestamp": datetime(2026, 9, 16, 9, 30, tzinfo=_IST),
            }
        ]
    )
    n = await _engine(pool).square_off_open(None)  # no order_monitors kwarg
    assert n == 1


async def test_square_off_adds_partial_pnl_booked_in_sql() -> None:
    """`quantity` is what remained after book_partial(); the sweep's P&L covers only that
    last leg. pnl_realized is the whole trade, so the UPDATE must add partial_pnl_booked
    (2026-09-24 LAURUSLABS lost half its P&L this way on the regular close path)."""
    entry = datetime(2026, 6, 17, 9, 30, tzinfo=_IST)
    close_ts = datetime(2026, 6, 17, 15, 25, tzinfo=_IST)
    rows = [
        {
            "agent_trade_id": "PAPER-4",
            "symbol": "INFY",
            "direction": "long",
            "quantity": 5,
            "fill_price": 100.0,
            "entry_timestamp": entry,
        }
    ]
    pool, conn = _pool(rows)
    await _engine(pool).square_off_open(_ohlcv(110.0, close_ts), prior_day_only=True)
    sql = conn.execute.call_args[0][0]
    assert "pnl_realized=$3 + COALESCE(partial_pnl_booked, 0)" in sql
