"""Dashboard API: /api/options_positions -- the real options position/strategy
view (legs, live P&L, max profit/loss) that replaced the bare Option Chain
strike-ladder tab. DB truth: options_positions (migration 051 + is_paper from
061)."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import pytest
from src.api import server


class _FakePool:
    def __init__(
        self, rows: list[dict[str, Any]], baseline_rows: list[dict[str, Any]] | None = None
    ) -> None:
        self._rows = rows
        self._baseline_rows = baseline_rows or []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "options_pnl_daily_snapshot" in query:
            return self._baseline_rows
        return self._rows


def _legs_json() -> str:
    return json.dumps(
        {
            "protection": [
                {
                    "tradingsymbol": "NFO:NIFTY24SEP24700PE",
                    "instrument_type": "PE",
                    "strike": 24700.0,
                    "direction": "BUY",
                    "lots": 1,
                    "lot_size": 50,
                    "premium": 20.0,
                    "delta": -0.1,
                }
            ],
            "income": [
                {
                    "tradingsymbol": "NFO:NIFTY24SEP24800PE",
                    "instrument_type": "PE",
                    "strike": 24800.0,
                    "direction": "SELL",
                    "lots": 1,
                    "lot_size": 50,
                    "premium": 45.0,
                    "delta": -0.25,
                }
            ],
        }
    )


def _row(**overrides: Any) -> dict[str, Any]:
    base = {
        "position_id": "OPT-1",
        "underlying": "NSE:NIFTY",
        "strategy_type": "bull_put_spread",
        "status": "OPEN",
        "legs": _legs_json(),
        "expiry": date(2026, 9, 25),
        "dte_at_entry": 14,
        "lot_size_at_entry": 50,
        "net_credit": 1250.0,
        "max_loss": 3750.0,
        "max_profit": 1250.0,
        "entry_margin": 3750.0,
        "entry_delta": -0.15,
        "entry_gamma": 0.001,
        "entry_theta": -2.5,
        "entry_vega": 1.2,
        "realized_pnl": None,
        "exit_reason": "",
        "opened_at": datetime(2026, 9, 10, 9, 30),
        "closed_at": None,
        "is_paper": True,
    }
    base.update(overrides)
    return base


class _FakeKite:
    def __init__(self, ticks: dict[str, dict[str, float]]) -> None:
        self._ticks = ticks

    def ltp(self, symbols: list[str]) -> dict[str, dict[str, float]]:
        return self._ticks


@pytest.mark.asyncio
async def test_open_position_computes_live_leg_and_position_pnl(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _FakePool([_row()]))
    monkeypatch.setattr(
        server,
        "_kite",
        lambda: _FakeKite(
            {
                "NFO:NIFTY24SEP24700PE": {"last_price": 15.0},
                "NFO:NIFTY24SEP24800PE": {"last_price": 30.0},
            }
        ),
    )

    out = await server.get_options_positions()

    assert len(out) == 1
    row = out[0]
    # BUY leg: (15-20)*50 = -250. SELL leg: (45-30)*50 = +750. Total = 500.
    assert row["pnl"] == 500.0
    assert row["status"] == "OPEN"
    assert row["isPaper"] is True
    assert len(row["legs"]) == 2
    buy_leg = next(leg for leg in row["legs"] if leg["direction"] == "BUY")
    assert buy_leg["pnl"] == -250.0
    assert row["maxProfit"] == 1250.0
    assert row["pctOfMaxProfit"] == pytest.approx(500.0 / 1250.0 * 100)


@pytest.mark.asyncio
async def test_breakeven_and_pop_computed_for_calibrated_strategy(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _FakePool([_row()]))
    monkeypatch.setattr(
        server,
        "_kite",
        lambda: _FakeKite(
            {
                "NFO:NIFTY24SEP24700PE": {"last_price": 15.0},
                "NFO:NIFTY24SEP24800PE": {"last_price": 30.0},
                "NSE:NIFTY 50": {"last_price": 24900.0},
            }
        ),
    )

    out = await server.get_options_positions()

    row = out[0]
    # short PE strike 24800, net_credit 1250 / qty 50 = 25/share -> 24800-25=24775
    assert len(row["breakeven"]) == 1
    assert row["breakeven"][0]["strike"] == 24775.0
    assert row["breakeven"][0]["pctFromSpot"] == pytest.approx(
        (24775.0 - 24900.0) / 24900.0 * 100, abs=0.01
    )
    assert row["spot"] == 24900.0
    assert row["pop"] is not None and 0.0 < row["pop"] < 1.0
    assert row["bookedPnl"] == 0.0
    assert row["unbookedPnl"] == row["pnl"]


@pytest.mark.asyncio
async def test_pop_uses_crude_proxy_for_uncalibrated_strategy(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _FakePool([_row(strategy_type="iron_butterfly")]))
    monkeypatch.setattr(server, "_kite", lambda: _FakeKite({}))

    out = await server.get_options_positions()

    # no backtested calibration for this type -> calibrated_pop's own crude
    # 1-|delta| fallback, still a real number (short PE delta is -0.25 in _row())
    assert out[0]["pop"] == pytest.approx(1.0 - 0.25)


@pytest.mark.asyncio
async def test_today_pnl_falls_back_to_total_pnl_with_no_baseline(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _FakePool([_row()]))
    monkeypatch.setattr(
        server,
        "_kite",
        lambda: _FakeKite(
            {
                "NFO:NIFTY24SEP24700PE": {"last_price": 15.0},
                "NFO:NIFTY24SEP24800PE": {"last_price": 30.0},
            }
        ),
    )

    out = await server.get_options_positions()

    # no snapshot row for today -> opened-today assumption -> todayPnl == pnl
    assert out[0]["todayPnl"] == out[0]["pnl"] == 500.0


@pytest.mark.asyncio
async def test_today_pnl_diffs_against_baseline_when_present(monkeypatch):
    monkeypatch.setattr(
        server,
        "_db_pool",
        _FakePool(
            [_row()],
            baseline_rows=[{"position_id": "OPT-1", "unrealized_pnl": 200.0}],
        ),
    )
    monkeypatch.setattr(
        server,
        "_kite",
        lambda: _FakeKite(
            {
                "NFO:NIFTY24SEP24700PE": {"last_price": 15.0},
                "NFO:NIFTY24SEP24800PE": {"last_price": 30.0},
            }
        ),
    )

    out = await server.get_options_positions()

    assert out[0]["pnl"] == 500.0
    assert out[0]["todayPnl"] == 300.0  # 500 total - 200 baseline


@pytest.mark.asyncio
async def test_closed_position_uses_realized_pnl_not_live_ltp(monkeypatch):
    monkeypatch.setattr(
        server,
        "_db_pool",
        _FakePool(
            [_row(status="CLOSED", realized_pnl=612.5, closed_at=datetime(2026, 9, 12, 15, 20))]
        ),
    )
    monkeypatch.setattr(server, "_kite", lambda: _FakeKite({}))

    out = await server.get_options_positions()

    assert out[0]["pnl"] == 612.5
    assert out[0]["status"] == "CLOSED"
    # legs on a closed row carry no live ltp/pnl -- never re-fetched from the broker
    assert all(leg["ltp"] is None and leg["pnl"] is None for leg in out[0]["legs"])


@pytest.mark.asyncio
async def test_ltp_fetch_failure_leaves_pnl_none_not_zero(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", _FakePool([_row()]))

    def _raise():
        raise RuntimeError("kite down")

    monkeypatch.setattr(server, "_kite", _raise)

    out = await server.get_options_positions()

    assert out[0]["pnl"] is None
    assert out[0]["pctOfMaxProfit"] is None


@pytest.mark.asyncio
async def test_db_failure_returns_empty_list(monkeypatch):
    class _FailingPool:
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

    monkeypatch.setattr(server, "_db_pool", _FailingPool())

    assert await server.get_options_positions() == []


@pytest.mark.asyncio
async def test_no_pool_returns_empty_list(monkeypatch):
    monkeypatch.setattr(server, "_db_pool", None)

    assert await server.get_options_positions() == []


def _strangle_legs_json() -> str:
    """options_intraday_short_premium mid-day: the PE leg was stopped out (legs["closed"]), the CE is live.
    Numbers are the real 2026-10-12 paper position (3 lots x 65)."""
    return json.dumps(
        {
            "protection": [],
            "income": [
                {
                    "tradingsymbol": "NFO:NIFTY26O1322600CE",
                    "instrument_type": "CE",
                    "strike": 22600.0,
                    "direction": "SELL",
                    "lots": 3,
                    "lot_size": 65,
                    "premium": 39.85,
                    "delta": 0.2,
                }
            ],
            "closed": [
                {
                    "tradingsymbol": "NFO:NIFTY26O1322300PE",
                    "instrument_type": "PE",
                    "strike": 22300.0,
                    "direction": "SELL",
                    "lots": 3,
                    "lot_size": 65,
                    "premium": 38.65,
                    "exit_premium": 47.55,
                    "exit_at": "2026-10-12T11:01:31+05:30",
                    "reason": "SHORT_PREMIUM_SL 23%",
                    "trigger": 47.54,
                }
            ],
            "meta": {"sl_pct": {"CE": 23, "PE": 23}},
        }
    )


@pytest.mark.asyncio
async def test_a_leg_stopped_out_while_the_position_is_open_is_shown_and_booked(monkeypatch):
    """Dashboard showed +2,086 (the CE only) and 'Booked 0': the stopped PE leg's -1,736 vanished from
    both the leg table and the totals. It must appear as a CLOSED leg and be counted as BOOKED."""
    monkeypatch.setattr(
        server,
        "_db_pool",
        _FakePool(
            [
                _row(
                    position_id="OPT-S",
                    strategy_type="options_intraday_short_premium",
                    legs=_strangle_legs_json(),
                    net_credit=15307.5,
                    max_profit=15307.5,
                )
            ]
        ),
    )
    monkeypatch.setattr(
        server, "_kite", lambda: _FakeKite({"NFO:NIFTY26O1322600CE": {"last_price": 29.15}})
    )

    row = (await server.get_options_positions())[0]

    live_ce = (39.85 - 29.15) * 195  # +2,086.5
    closed_pe = (38.65 - 47.55) * 195  # -1,735.5
    assert row["status"] == "OPEN" and len(row["legs"]) == 2
    ce = next(leg for leg in row["legs"] if leg["instrumentType"] == "CE")
    pe = next(leg for leg in row["legs"] if leg["instrumentType"] == "PE")
    assert ce["state"] == "OPEN" and ce["ltp"] == 29.15 and ce["pnl"] == pytest.approx(live_ce)
    assert (
        pe["state"] == "CLOSED"
        and pe["exitPremium"] == 47.55
        and pe["pnl"] == pytest.approx(closed_pe)
    )
    assert pe["exitReason"] == "SHORT_PREMIUM_SL 23%" and pe["exitAt"].startswith(
        "2026-10-12T11:01"
    )
    assert row["bookedPnl"] == pytest.approx(closed_pe)  # realized part of the still-open position
    assert row["unbookedPnl"] == pytest.approx(live_ce)  # only the live leg is unbooked
    assert row["pnl"] == pytest.approx(live_ce + closed_pe)  # Total = +351, not +2,086


@pytest.mark.asyncio
async def test_headline_numbers_follow_the_remaining_leg_once_a_leg_is_stopped(monkeypatch):
    """Max profit / max loss / target / stop were still the whole original structure after the PE leg was
    stopped out. They must now describe what is left: booked + the open leg's premium, the open leg's stop
    less what is booked, and a target measured on the open leg."""
    import src.options.short_premium as sp

    cfg = sp.parse_cfg({"enabled": True, "allow_naked": True, "target_pct": 50, "sl_pct": 23})
    monkeypatch.setattr(sp, "load_cfg", lambda: cfg)
    monkeypatch.setattr(
        server,
        "_db_pool",
        _FakePool(
            [
                _row(
                    position_id="OPT-S",
                    strategy_type=sp.SHORT_PREMIUM_TYPE,
                    legs=_strangle_legs_json(),
                    net_credit=15307.5,
                    max_profit=15307.5,
                    max_loss=3521.0,
                )
            ]
        ),
    )
    monkeypatch.setattr(
        server, "_kite", lambda: _FakeKite({"NFO:NIFTY26O1322600CE": {"last_price": 29.15}})
    )

    row = (await server.get_options_positions())[0]
    open_credit = 39.85 * 195  # the CE leg's premium, rupees
    booked = (38.65 - 47.55) * 195  # PE stopped out
    assert row["maxProfit"] == pytest.approx(
        booked + open_credit
    )  # +6,035, not the original +15,308
    assert row["stopAmount"] == pytest.approx(0.23 * open_credit)  # only the CE leg's stop remains
    assert row["maxLoss"] == pytest.approx(
        0.23 * open_credit - booked
    )  # worst total = stop + already booked
    assert row["targetAmount"] == pytest.approx(
        0.5 * open_credit
    )  # 50% of what the open leg can still earn


@pytest.mark.asyncio
async def test_closed_legs_pnl_on_sums_only_todays_legs_of_still_open_positions():
    from zoneinfo import ZoneInfo

    from src.db.repositories.options import OptionsRepository

    def _pos(exit_at: str) -> dict[str, Any]:
        legs = json.loads(_strangle_legs_json())
        legs["closed"][0]["exit_at"] = exit_at
        return {"legs": json.dumps(legs)}

    class _Conn:
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            assert "status = 'OPEN'" in query  # CLOSED positions already carry it in realized_pnl
            return [_pos("2026-10-12T11:01:31+05:30"), _pos("2026-10-09T11:01:31+05:30")]

    class _Acquire:
        async def __aenter__(self) -> _Conn:
            return _Conn()

        async def __aexit__(self, *a: Any) -> None:
            return None

    class _Pool:
        def acquire(self) -> _Acquire:
            return _Acquire()

    repo = OptionsRepository(_Pool())  # type: ignore[arg-type]
    pnl = await repo.closed_legs_pnl_on(date(2026, 10, 12), is_paper=True)
    assert pnl == pytest.approx((38.65 - 47.55) * 195)  # today's leg only, not the one from 9 Oct
    assert ZoneInfo("Asia/Kolkata")  # (tz handling is by IST date, not UTC)
