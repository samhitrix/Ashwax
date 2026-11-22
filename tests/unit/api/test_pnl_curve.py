"""Server-side Intraday P&L curve: the day-P&L maths, persistence, and skip rules."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.api import pnl_curve as pc

DAY = date(2026, 1, 5)  # a Monday
OPEN_TIME = datetime(2026, 1, 5, 11, 0, tzinfo=pc.IST)


def test_day_pnl_combines_all_segments() -> None:
    net = [
        {"tradingsymbol": "ABC", "exchange": "NSE", "quantity": 10, "pnl": 100.0},
        {"tradingsymbol": "ZERO", "exchange": "NSE", "quantity": 0, "pnl": 999.0},
        # a live option leg is also a broker position: counted via options rows only
        {"tradingsymbol": "IDX26JAN100CE", "exchange": "NFO", "quantity": -20, "pnl": 50.0},
    ]
    closed = [{"pnl": 200.0}]
    options = [
        {"status": "OPEN", "pnl": -30.0},
        {"status": "CLOSED", "pnl": 40.0, "closedAt": "2026-01-05 05:00:00+00:00"},
        {"status": "CLOSED", "pnl": 777.0, "closedAt": "2026-01-02 05:00:00+00:00"},  # older day
    ]
    swing = [
        {"pnl": 500.0, "partialPnlBooked": 120.0},
        {"pnl": 9999.0, "partialPnlBooked": 0.0, "exitTime": "2026-01-05 04:00"},  # closed
    ]
    out = pc.day_pnl(net, closed, options, swing, DAY)
    assert out["booked"] == 200 + 40 + 120
    assert out["unrealised"] == 100 + (-30) + (500 - 120)
    assert out["pnl"] == out["booked"] + out["unrealised"]


def test_day_pnl_excludes_paper_rows() -> None:
    net = [
        {"tradingsymbol": "ABC", "exchange": "NSE", "quantity": 10, "pnl": 100.0},
        {"tradingsymbol": "PAP", "exchange": "NSE", "quantity": 10, "pnl": 5000.0, "paper": True},
    ]
    closed = [{"pnl": 200.0}, {"pnl": 6000.0, "paper": True}]
    options = [
        {"status": "OPEN", "pnl": -30.0, "isPaper": False},
        {"status": "OPEN", "pnl": 7000.0, "isPaper": True},
        {
            "status": "CLOSED",
            "pnl": 8000.0,
            "isPaper": True,
            "closedAt": "2026-01-05 05:00:00+00:00",
        },
    ]
    swing = [
        {"pnl": 500.0, "partialPnlBooked": 120.0, "paper": False},
        {"pnl": 9000.0, "partialPnlBooked": 1000.0, "paper": True},
    ]
    out = pc.day_pnl(net, closed, options, swing, DAY)
    assert out["booked"] == 200 + 120
    assert out["unrealised"] == 100 + (-30) + (500 - 120)


def test_day_pnl_empty_is_zero() -> None:
    assert pc.day_pnl([], [], [], [], DAY) == {"booked": 0.0, "unrealised": 0.0, "pnl": 0.0}


def test_record_and_read_one_point_per_minute_last_wins(tmp_path: Path) -> None:
    s1 = {"pnl": 1.0, "booked": 0.0, "unrealised": 1.0}
    s2 = {"pnl": 2.0, "booked": 0.0, "unrealised": 2.0}
    pc.record(OPEN_TIME, s1, tmp_path)
    pc.record(OPEN_TIME, s2, tmp_path)
    pc.record(OPEN_TIME.replace(minute=1), s1, tmp_path)
    pts = pc.read_curve(DAY, tmp_path)
    assert [p["time"] for p in pts] == ["11:00", "11:01"]
    assert pts[0]["pnl"] == 2.0


def test_read_curve_survives_torn_line_and_missing_file(tmp_path: Path) -> None:
    assert pc.read_curve(DAY, tmp_path) == []
    pc.record(OPEN_TIME, {"pnl": 1.0, "booked": 0.0, "unrealised": 1.0}, tmp_path)
    with (tmp_path / "2026-01-05.jsonl").open("a") as fh:
        fh.write('{"time": "11:0')  # crash mid-write
    assert len(pc.read_curve(DAY, tmp_path)) == 1


def test_sample_skipped_outside_session_and_on_failed_fetch(tmp_path: Path) -> None:
    called = 0

    async def fetch() -> Any:
        nonlocal called
        called += 1
        return ([], [], [], [])

    night = datetime(2026, 1, 5, 22, 0, tzinfo=pc.IST)
    assert asyncio.run(pc.sample_once(fetch, night, tmp_path)) is False
    assert called == 0  # no broker calls outside the session

    saturday = datetime(2026, 1, 3, 11, 0, tzinfo=pc.IST)
    assert asyncio.run(pc.sample_once(fetch, saturday, tmp_path)) is False

    async def failed() -> Any:
        return None

    assert asyncio.run(pc.sample_once(failed, OPEN_TIME, tmp_path)) is False
    assert pc.read_curve(DAY, tmp_path) == []  # a failed fetch is never recorded as 0

    assert asyncio.run(pc.sample_once(fetch, OPEN_TIME, tmp_path)) is True
    assert len(pc.read_curve(DAY, tmp_path)) == 1
