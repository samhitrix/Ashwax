"""Periodic position update + EOD summary tests, via fake repos/brokers/monitors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from src.notifications.summary import (
    build_eod_stats,
    build_options_snapshot,
    build_position_snapshot,
    realized_pnl_today_by_mode,
)


@dataclass
class _FakePos:
    symbol: str
    direction: str
    quantity: int
    fill_price: float
    current_sl: float
    current_target: float = 0.0
    product: str = "MIS"


class _FakeOrderMonitor:
    def __init__(self, positions: list[_FakePos]) -> None:
        self._positions = positions

    def get_monitored(self) -> list[_FakePos]:
        return self._positions


class _FakeBroker:
    def __init__(self, ltp_map: dict[str, float]) -> None:
        self._ltp_map = ltp_map

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return {s: self._ltp_map.get(s, 0.0) for s in symbols}


async def test_build_position_snapshot_computes_unrealized_pnl() -> None:
    stacks = {
        "paper": {
            "order_monitor": _FakeOrderMonitor([_FakePos("RELIANCE", "BUY", 10, 2500.0, 2450.0)]),
            "broker": _FakeBroker({"RELIANCE": 2550.0}),
        },
        "live": {
            "order_monitor": _FakeOrderMonitor([]),
            "broker": _FakeBroker({}),
        },
    }
    rows = await build_position_snapshot(stacks)
    assert len(rows) == 1
    row = rows[0]
    assert row["is_paper"] is True
    assert row["symbol"] == "RELIANCE"
    assert row["unrealized_pnl"] == (2550.0 - 2500.0) * 10


async def test_build_position_snapshot_handles_short_direction() -> None:
    stacks = {
        "live": {
            "order_monitor": _FakeOrderMonitor([_FakePos("TCS", "SELL", 5, 3500.0, 3550.0)]),
            "broker": _FakeBroker({"TCS": 3400.0}),
        },
    }
    rows = await build_position_snapshot(stacks)
    assert rows[0]["unrealized_pnl"] == (3500.0 - 3400.0) * 5


async def test_build_position_snapshot_empty_when_flat() -> None:
    stacks = {"paper": {"order_monitor": _FakeOrderMonitor([]), "broker": _FakeBroker({})}}
    rows = await build_position_snapshot(stacks)
    assert rows == []


async def test_build_position_snapshot_ltp_failure_falls_back_to_fill_price() -> None:
    class _BrokenBroker:
        async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
            raise ConnectionError("broker down")

    stacks = {
        "paper": {
            "order_monitor": _FakeOrderMonitor([_FakePos("X", "BUY", 1, 100.0, 90.0)]),
            "broker": _BrokenBroker(),
        },
    }
    rows = await build_position_snapshot(stacks)  # must not raise
    assert rows[0]["ltp"] == 100.0  # fell back to fill_price
    assert rows[0]["unrealized_pnl"] == 0.0


class _Row(dict):
    """asyncpg.Record-like: supports both .get() and mapping-style access."""


class _FakeTradeRepo:
    def __init__(self, trades_rows: list[dict], paper_rows: list[dict]) -> None:
        self._trades_rows = trades_rows
        self._paper_rows = paper_rows

    async def closed_trades(self, limit: int = 500, is_paper: bool = False) -> list[dict]:
        return [_Row(r) for r in (self._paper_rows if is_paper else self._trades_rows)]

    async def daily_realized_pnl(self, date_str: str, is_paper: bool = False):
        from decimal import Decimal

        rows = self._paper_rows if is_paper else self._trades_rows
        total = sum(
            float(r["pnl_realized"])
            for r in rows
            if str(r.get("exit_timestamp", ""))[:10] == date_str
        )
        return Decimal(str(total))


def _mk_row(symbol: str, pnl: float, risk: float, exit_date: str):
    import datetime as _dt

    return {
        "symbol": symbol,
        "pnl_realized": pnl,
        "actual_risk_rupees": risk,
        "exit_timestamp": _dt.datetime.fromisoformat(exit_date + "T15:30:00"),
    }


async def test_build_eod_stats_splits_paper_and_live() -> None:
    live_rows = [_mk_row("RELIANCE", 500.0, 200.0, "2026-09-15")]
    paper_rows = [
        _mk_row("TCS", -100.0, 100.0, "2026-09-15"),
        _mk_row("WIPRO", 300.0, 150.0, "2026-09-15"),
        _mk_row("OLDONE", 999.0, 10.0, "2026-09-01"),  # different date, excluded
    ]
    repo = _FakeTradeRepo(live_rows, paper_rows)
    stats = await build_eod_stats(repo, "2026-09-15")
    assert stats["trade_count"] == 3
    assert stats["wins"] == 2
    assert stats["losses"] == 1
    assert stats["net_pnl"] == 500.0 - 100.0 + 300.0
    assert stats["best_symbol"] == "RELIANCE"
    assert stats["worst_symbol"] == "TCS"


async def test_build_eod_stats_empty_day() -> None:
    repo = _FakeTradeRepo([], [])
    stats = await build_eod_stats(repo, "2026-09-15")
    assert stats["trade_count"] == 0
    assert stats["net_pnl"] == 0.0
    assert stats["win_rate"] == 0.0


async def test_build_eod_stats_repo_failure_does_not_raise() -> None:
    class _BrokenRepo:
        async def closed_trades(self, limit: int = 500, is_paper: bool = False):
            raise ConnectionError("db down")

        async def daily_realized_pnl(self, date_str: str, is_paper: bool = False):
            raise ConnectionError("db down")

    stats = await build_eod_stats(_BrokenRepo(), "2026-09-15")  # must not raise
    assert stats["trade_count"] == 0


async def test_build_eod_stats_ignores_daily_realized_pnl_entirely() -> None:
    """Regression for the real bug: net_pnl must come from the same
    IST-filtered rows as trade_count/wins/losses, never from
    daily_realized_pnl()'s separate exit_timestamp::date SQL comparison,
    which is timezone-fragile (DB session timezone is never set to IST
    anywhere in this codebase) and can silently disagree with the correctly
    IST-filtered `closed` list — producing a real loss counted as "L 1" but
    displayed as a wrongly-green "Net P&L: ₹0.00"."""

    class _DisagreeingRepo:
        """closed_trades() correctly finds one lossy trade for today;
        daily_realized_pnl() (simulating the timezone bug) always returns 0,
        as if its date comparison missed every row."""

        async def closed_trades(self, limit: int = 500, is_paper: bool = False):
            if is_paper:
                return []
            return [_Row(_mk_row("CGCL", -250.0, 100.0, "2026-09-15"))]

        async def daily_realized_pnl(self, date_str: str, is_paper: bool = False):
            from decimal import Decimal

            return Decimal("0")  # simulates the timezone-mismatched query

    stats = await build_eod_stats(_DisagreeingRepo(), "2026-09-15")

    assert stats["trade_count"] == 1
    assert stats["losses"] == 1
    assert stats["net_pnl"] == -250.0  # NOT 0.00 — the real bug's symptom


# ── build_options_snapshot ──────────────────────────────────────────────────


def _fake_desk(positions: list, is_paper: bool = True):  # type: ignore[no-untyped-def]
    """Minimal stand-in for SellerDesk — build_options_snapshot only reads
    .positions (dict keyed by position_id) and ._is_paper via getattr, so it
    never needs the real class's CRM/broker/repo dependencies."""

    class _Desk:
        pass

    desk = _Desk()
    desk.positions = {p.position_id: p for p in positions}  # type: ignore[attr-defined]
    desk._is_paper = is_paper  # type: ignore[attr-defined]
    return desk


def _leg(
    symbol: str,
    direction: str,
    premium: float,
    lots: int = 1,
    lot_size: int = 25,
    strike: float = 24000.0,
    instrument_type: str = "CE",
):  # type: ignore[no-untyped-def]
    from src.options.structure_selector import OptionLeg

    return OptionLeg(
        tradingsymbol=symbol,
        instrument_type=instrument_type,
        strike=strike,
        direction=direction,
        lots=lots,
        lot_size=lot_size,
        premium=premium,
    )


def _open_position(position_id: str, underlying: str, income_legs: list, protection_legs=None):  # type: ignore[no-untyped-def]
    import datetime as _dt

    from src.options.seller_desk import OpenPosition

    return OpenPosition(
        position_id=position_id,
        underlying=underlying,
        strategy_type="iron_condor",
        direction="credit",
        protection_legs=protection_legs or [],
        income_legs=income_legs,
        entry_premium=100.0,
        entry_margin=5000.0,
        expiry=_dt.date(2026, 9, 25),
        entered_at=_dt.datetime(2026, 9, 15, 10, 0, tzinfo=_dt.UTC),
    )


async def test_build_options_snapshot_empty_when_no_positions() -> None:
    desk = _fake_desk([])
    rows = await build_options_snapshot(desk, _FakeBroker({}))
    assert rows == []


async def test_build_options_snapshot_marks_to_market_short_leg() -> None:
    # SELL leg: profits when the current premium is BELOW the entry premium.
    pos = _open_position(
        "OPT-1", "NSE:NIFTY", income_legs=[_leg("NIFTY24000CE", "SELL", 100.0, lots=2)]
    )
    desk = _fake_desk([pos])
    broker = _FakeBroker({"NIFTY24000CE": 80.0})  # premium dropped 20 -> profit for the seller
    rows = await build_options_snapshot(desk, broker)
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "NSE:NIFTY"
    assert row["segment"] == "OPTIONS"
    assert row["structure_type"] == "iron_condor"
    assert row["position_id"] == "OPT-1"
    assert row["strike"] == 24000.0
    assert row["instrument_type"] == "CE"
    assert row["direction"] == "SELL"
    assert row["lots"] == 2
    assert row["premium"] == 100.0
    assert row["ltp"] == 80.0
    assert row["is_paper"] is True
    assert row["unrealized_pnl"] == (100.0 - 80.0) * (2 * 25)  # sign=-1 -> (80-100)*-1*qty


async def test_build_options_snapshot_returns_one_row_per_leg() -> None:
    """Regression (found 2026-09-16): a real 2-leg bull_put_spread only ever
    showed as one netted row. Each leg must be its own row, sharing the same
    position_id so the formatter can group them back under one structure."""
    pos = _open_position(
        "OPT-SPREAD",
        "NSE:NIFTY",
        protection_legs=[
            _leg("NIFTY22900PE", "BUY", 33.175, lots=1, strike=22900.0, instrument_type="PE")
        ],
        income_legs=[
            _leg("NIFTY23200PE", "SELL", 78.225, lots=1, strike=23200.0, instrument_type="PE")
        ],
    )
    desk = _fake_desk([pos])
    broker = _FakeBroker({"NIFTY22900PE": 50.6, "NIFTY23200PE": 126.6})
    rows = await build_options_snapshot(desk, broker)

    assert len(rows) == 2
    assert {r["position_id"] for r in rows} == {"OPT-SPREAD"}
    by_strike = {r["strike"]: r for r in rows}
    assert set(by_strike) == {22900.0, 23200.0}
    long_leg = by_strike[22900.0]
    short_leg = by_strike[23200.0]
    assert long_leg["direction"] == "BUY"
    assert long_leg["unrealized_pnl"] == pytest.approx((50.6 - 33.175) * 25)
    assert short_leg["direction"] == "SELL"
    assert short_leg["unrealized_pnl"] == pytest.approx((78.225 - 126.6) * 25)


async def test_build_options_snapshot_ltp_failure_falls_back_to_entry_premium() -> None:
    pos = _open_position(
        "OPT-2", "NSE:BANKNIFTY", income_legs=[_leg("BANKNIFTY52000PE", "SELL", 50.0)]
    )
    desk = _fake_desk([pos])

    class _BrokenBroker:
        async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
            raise ConnectionError("down")

    rows = await build_options_snapshot(desk, _BrokenBroker())
    assert len(rows) == 1
    assert rows[0]["unrealized_pnl"] == 0.0  # current == premium -> flat, not a crash


async def test_build_options_snapshot_live_desk_reports_is_paper_false() -> None:
    pos = _open_position("OPT-3", "NSE:NIFTY", income_legs=[_leg("NIFTY24000CE", "SELL", 100.0)])
    desk = _fake_desk([pos], is_paper=False)
    rows = await build_options_snapshot(desk, _FakeBroker({}))
    assert rows[0]["is_paper"] is False


# ── realized_pnl_today_by_mode ──────────────────────────────────────────────


async def test_realized_pnl_today_by_mode_splits_paper_and_live() -> None:
    live_rows = [_mk_row("RELIANCE", 500.0, 200.0, "2026-09-15")]
    paper_rows = [
        _mk_row("TCS", -100.0, 100.0, "2026-09-15"),
        _mk_row("OLDONE", 999.0, 10.0, "2026-09-01"),  # different date, excluded
    ]
    repo = _FakeTradeRepo(live_rows, paper_rows)
    out = await realized_pnl_today_by_mode(repo, "2026-09-15")
    assert out[False] == 500.0  # live
    assert out[True] == -100.0  # paper


async def test_realized_pnl_today_by_mode_repo_failure_returns_zeros() -> None:
    class _BrokenRepo:
        async def closed_trades(self, limit: int = 500, is_paper: bool = False):
            raise ConnectionError("db down")

    out = await realized_pnl_today_by_mode(_BrokenRepo(), "2026-09-15")  # must not raise
    assert out == {True: 0.0, False: 0.0}


async def test_a_leg_closed_inside_a_still_open_position_is_booked_realised_pnl() -> None:
    """The 15-min Position update showed 'Booked (realized): 0' and a total that ignored a stopped-out leg:
    the realised figure only counted positions that had fully CLOSED. Legs closed inside an open position
    are booked money too -- read from the saved legs, so every strategy feeds it with no special code."""

    class _OptionsRepo:
        async def closed_positions_on(self, day, *, is_paper):  # type: ignore[no-untyped-def]
            return [{"underlying": "NIFTY", "realized_pnl": 500.0}] if is_paper else []

        async def closed_legs_pnl_on(self, day, *, is_paper):  # type: ignore[no-untyped-def]
            return -1735.5 if is_paper else 0.0

    repo = _FakeTradeRepo([], [_mk_row("TCS", 100.0, 100.0, "2026-09-15")])
    out = await realized_pnl_today_by_mode(repo, "2026-09-15", _OptionsRepo())
    assert out[True] == 100.0 + 500.0 - 1735.5  # equity + fully-closed options + the closed leg
    assert out[False] == 0.0


async def test_open_legs_booked_failure_never_breaks_the_update() -> None:
    class _BrokenOptionsRepo:
        async def closed_positions_on(self, day, *, is_paper):  # type: ignore[no-untyped-def]
            return []

        async def closed_legs_pnl_on(self, day, *, is_paper):  # type: ignore[no-untyped-def]
            raise ConnectionError("db down")

    out = await realized_pnl_today_by_mode(
        _FakeTradeRepo([], []), "2026-09-15", _BrokenOptionsRepo()
    )
    assert out == {True: 0.0, False: 0.0}


# ── market_holiday_loop: the one message on a weekday NSE holiday ──────────────
def _run_holiday_loop(
    monkeypatch: pytest.MonkeyPatch, when: datetime, already: bool = False
) -> list[dict[str, object]]:
    import asyncio

    from src.notifications import notifier as notifier_mod
    from src.notifications import summary

    sent: list[dict[str, object]] = []

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return when

    async def _already(key: str) -> bool:
        return already

    monkeypatch.setattr(summary, "datetime", _Clock)
    monkeypatch.setattr(summary, "get_notifier", lambda: object())
    monkeypatch.setattr(summary, "already_sent", _already)
    monkeypatch.setattr(
        notifier_mod, "notify", lambda kind, **kw: sent.append({"kind": kind, **kw})
    )

    async def _go() -> None:
        stop = asyncio.Event()
        orig = asyncio.wait_for
        calls = {"n": 0}

        async def _fast(aw, timeout):  # type: ignore[no-untyped-def]  # noqa: ASYNC109
            calls["n"] += 1
            if calls["n"] == 1:  # startup quiet-window wait elapses
                aw.close()
                raise TimeoutError
            stop.set()
            return await orig(aw, 0.01)

        monkeypatch.setattr(asyncio, "wait_for", _fast)
        await summary.market_holiday_loop(stop)

    asyncio.run(_go())
    return sent


_IST_TZ = ZoneInfo("Asia/Kolkata")


def test_weekday_holiday_sends_one_holiday_message(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _run_holiday_loop(
        monkeypatch, datetime(2026, 10, 2, 8, 5, tzinfo=_IST_TZ)
    )  # Gandhi Jayanti
    assert len(sent) == 1
    assert sent[0]["name"] == "Mahatma Gandhi Jayanti"
    assert sent[0]["dedupe_key"] == "holiday:2026-10-02"


def test_holiday_message_waits_for_the_notice_time(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_holiday_loop(monkeypatch, datetime(2026, 10, 2, 6, 0, tzinfo=_IST_TZ)) == []


def test_holiday_message_not_repeated_after_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        _run_holiday_loop(monkeypatch, datetime(2026, 10, 2, 9, 0, tzinfo=_IST_TZ), already=True)
        == []
    )


def test_weekend_and_normal_trading_day_send_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_holiday_loop(monkeypatch, datetime(2026, 10, 3, 9, 0, tzinfo=_IST_TZ)) == []  # Sat
    assert _run_holiday_loop(monkeypatch, datetime(2026, 10, 1, 9, 0, tzinfo=_IST_TZ)) == []  # Thu


class _FakeOptionsRepo:
    def __init__(self, paper: list[dict], live: list[dict] | None = None) -> None:
        self._paper = paper
        self._live = live or []

    async def closed_positions_on(self, day, *, is_paper: bool) -> list[dict]:
        return self._paper if is_paper else self._live


async def test_options_only_day_is_counted_in_eod_and_realized() -> None:
    """2026-10-05: +1995.50 options profit, zero equity closes, was reported as 0."""
    from src.notifications.summary import realized_pnl_today_by_mode

    opts = _FakeOptionsRepo([{"underlying": "NSE:NIFTY", "realized_pnl": 1995.5}])
    stats = await build_eod_stats(_FakeTradeRepo([], []), "2026-10-05", opts)
    assert stats["trade_count"] == 1 and stats["wins"] == 1
    assert stats["net_pnl"] == 1995.5 and stats["booked_options"] == 1995.5
    assert stats["booked_equity"] == 0.0
    realized = await realized_pnl_today_by_mode(_FakeTradeRepo([], []), "2026-10-05", opts)
    assert realized == {True: 1995.5, False: 0.0}


async def test_options_incomplete_close_is_flagged_not_invented() -> None:
    opts = _FakeOptionsRepo([{"underlying": "NSE:NIFTY", "realized_pnl": None}])
    stats = await build_eod_stats(_FakeTradeRepo([], []), "2026-10-05", opts)
    assert stats["trade_count"] == 1 and stats["options_incomplete"] == 1
    assert stats["net_pnl"] == 0.0


async def test_build_position_snapshot_labels_the_broker_of_each_live_stack() -> None:
    stacks = {
        "paper": {
            "order_monitor": _FakeOrderMonitor([_FakePos("AAA", "BUY", 1, 10.0, 9.0)]),
            "broker": _FakeBroker({}),
        },
        "live:kite": {
            "order_monitor": _FakeOrderMonitor([_FakePos("BBB", "BUY", 1, 10.0, 9.0)]),
            "broker": _FakeBroker({}),
        },
        "live:angel_one": {
            "order_monitor": _FakeOrderMonitor([_FakePos("CCC", "BUY", 1, 10.0, 9.0)]),
            "broker": _FakeBroker({}),
        },
        "live": {  # legacy bare key: single live stack, broker unknown
            "order_monitor": _FakeOrderMonitor([_FakePos("DDD", "BUY", 1, 10.0, 9.0)]),
            "broker": _FakeBroker({}),
        },
    }
    rows = {r["symbol"]: r for r in await build_position_snapshot(stacks)}
    assert rows["AAA"]["broker"] is None
    assert rows["BBB"]["broker"] == "kite"
    assert rows["CCC"]["broker"] == "angel_one"
    assert rows["DDD"]["broker"] is None
