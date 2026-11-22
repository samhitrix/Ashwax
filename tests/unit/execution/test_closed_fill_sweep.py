"""Closed live rows are corrected to the broker's real fills, and only when provably right.
All values are made up."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from src.brokers.base import Order
from src.execution import closed_fill_sweep as sweep


def _o(
    symbol: str,
    side: str,
    qty: int,
    avg: float,
    status: str = "FILLED",
    *,
    product: str = "INTRADAY",
    at: datetime | None = None,
) -> Order:
    return Order(
        broker_order_id=f"{symbol}-{side}-{avg}",
        symbol=symbol,
        direction=side,
        quantity=qty,
        filled_quantity=qty,
        pending_quantity=0,
        status=status,
        order_type="MARKET",
        product=product,
        average_price=avg,
        tag="",
        placed_at=at or datetime.now(UTC),
    )


class _Pool:
    def __init__(self, trades: list[dict], options: list[dict]) -> None:
        self.trades, self.options = trades, options
        self.updates: list[tuple] = []

    async def fetch(self, sql: str, *args):
        return self.options if "options_positions" in sql else self.trades

    async def execute(self, sql: str, *args):
        self.updates.append((sql, args))
        if "options_positions" in sql:
            self.options[0]["legs"] = args[1]
            self.options[0]["realized_pnl"] = args[2]
        else:
            self.trades[0]["fill_price"], self.trades[0]["exit_price"] = args[1], args[2]


def _broker(orders: list[Order]) -> MagicMock:
    b = MagicMock()
    b.get_all_orders = AsyncMock(return_value=orders)
    return b


def _trade(**kw) -> dict:
    row = dict(
        agent_trade_id="T1",
        symbol="ABCSTOCK",
        direction="long",
        product="MIS",
        quantity=10,
        partial_qty_booked=0,
        fill_price=500.0,
        exit_price=492.0,
        pnl_realized=-130.0,  # gross -80 at the booked 492, once charges are added back
        charges_taxes=50.0,
    )
    row.update(kw)
    return row


def _sensex() -> dict:
    leg = dict(lots=1, lot_size=20, direction="SELL")
    return {
        "position_id": "OPT-1",
        "realized_pnl": -2400.0,
        "legs": {
            "income": [],
            "protection": [],
            "closed": [
                {
                    **leg,
                    "tradingsymbol": "BFO:INDEX26JAN20000PE",
                    "premium": 110.0,
                    "exit_premium": 170.0,
                },
                {
                    **leg,
                    "tradingsymbol": "BFO:INDEX26JAN20000CE",
                    "premium": 120.0,
                    "exit_premium": 180.0,
                },
            ],
        },
    }


SENSEX_ORDERS = [
    _o("INDEX26JAN20000PE", "SELL", 20, 109.0),
    _o("INDEX26JAN20000CE", "SELL", 20, 119.0),
    _o("INDEX26JAN20000PE", "BUY", 20, 172.0),
    _o("INDEX26JAN20000CE", "BUY", 20, 185.0),
]


async def test_straddle_replays_to_the_brokers_real_loss() -> None:
    pool = _Pool([], [_sensex()])
    corrected = await sweep.reconcile_closed_fills(pool, _broker(SENSEX_ORDERS))
    assert len(corrected) == 1
    sql, args = pool.updates[0]
    assert args[2] == -2580.0
    legs = json.loads(args[1])["closed"]
    assert {x["premium"] for x in legs} == {109.0, 119.0}
    assert {x["exit_premium"] for x in legs} == {172.0, 185.0}


async def test_equity_exit_corrected_to_broker_average() -> None:
    pool = _Pool(
        [_trade()],
        [],
    )
    orders = [_o("ABCSTOCK", "BUY", 10, 500.0), _o("ABCSTOCK", "SELL", 10, 490.0)]
    await sweep.reconcile_closed_fills(pool, _broker(orders))
    _sql, args = pool.updates[0]
    assert args[2] == 490.0


async def test_correct_row_is_left_untouched() -> None:
    pool = _Pool([_trade(exit_price=490.0, pnl_realized=-150.0, charges_taxes=50.0)], [])
    orders = [_o("ABCSTOCK", "BUY", 10, 500.0), _o("ABCSTOCK", "SELL", 10, 490.0)]
    assert await sweep.reconcile_closed_fills(pool, _broker(orders)) == []
    assert pool.updates == []


async def test_right_prices_but_wrong_pnl_is_corrected() -> None:
    pool = _Pool([_trade(exit_price=490.0, pnl_realized=900.0, charges_taxes=0)], [])
    orders = [_o("ABCSTOCK", "BUY", 10, 500.0), _o("ABCSTOCK", "SELL", 10, 490.0)]
    assert await sweep.reconcile_closed_fills(pool, _broker(orders))
    assert pool.updates


async def test_every_closed_trade_logs_db_vs_live_once(caplog) -> None:
    sweep._logged.clear()
    caplog.set_level("INFO", logger=sweep.__name__)
    pool = _Pool(
        [_trade(exit_price=490.0, pnl_realized=-150.0, charges_taxes=50.0)],
        [],
    )
    orders = [_o("ABCSTOCK", "BUY", 10, 500.0), _o("ABCSTOCK", "SELL", 10, 490.0)]
    await sweep.reconcile_closed_fills(pool, _broker(orders))
    await sweep.reconcile_closed_fills(pool, _broker(orders))
    lines = [r.getMessage() for r in caplog.records if "closed_trade_verify" in r.getMessage()]
    assert len(lines) == 1
    assert "result=MATCH" in lines[0]
    for part in ("db_exit=490.00", "live_exit=490.00", "live_gross=-100.00", "db_net=-150.0"):
        assert part in lines[0]


async def test_a_correction_logs_old_and_new_prices(caplog) -> None:
    sweep._logged.clear()
    caplog.set_level("INFO", logger=sweep.__name__)
    orders = [_o("ABCSTOCK", "BUY", 10, 500.0), _o("ABCSTOCK", "SELL", 10, 490.0)]
    await sweep.reconcile_closed_fills(_Pool([_trade()], []), _broker(orders))
    line = next(r.getMessage() for r in caplog.records if "result=CORRECTED" in r.getMessage())
    assert "db_exit=492.00" in line
    assert "live_exit=490.00" in line


async def test_second_run_changes_nothing_and_sends_no_message(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(sweep, "notify", lambda kind, **kw: calls.append(kind))
    pool = _Pool([], [_sensex()])
    assert await sweep.reconcile_closed_fills(pool, _broker(SENSEX_ORDERS))
    assert await sweep.reconcile_closed_fills(pool, _broker(SENSEX_ORDERS)) == []
    assert len(pool.updates) == 1
    assert len(calls) == 1


async def test_two_rows_same_symbol_is_ambiguous_and_untouched() -> None:
    pool = _Pool([_trade(), _trade(agent_trade_id="T2")], [])
    orders = [_o("ABCSTOCK", "BUY", 10, 500.0), _o("ABCSTOCK", "SELL", 10, 490.0)]
    assert await sweep.reconcile_closed_fills(pool, _broker(orders)) == []


async def test_exit_qty_mismatch_is_untouched() -> None:
    pool = _Pool([_trade()], [])
    orders = [_o("ABCSTOCK", "BUY", 10, 500.0), _o("ABCSTOCK", "SELL", 5, 490.0)]
    assert await sweep.reconcile_closed_fills(pool, _broker(orders)) == []


async def test_multi_day_swing_keeps_its_entry_and_fixes_the_exit() -> None:
    row = _trade(product="CNC", fill_price=480.0, pnl_realized=0, charges_taxes=0)
    row["entered_at"] = datetime.now(UTC) - timedelta(days=3)
    pool = _Pool([row], [])
    await sweep.reconcile_closed_fills(
        pool, _broker([_o("ABCSTOCK", "SELL", 10, 490.0, product="DELIVERY")])
    )
    _sql, args = pool.updates[0]
    assert args[1] == 480.0
    assert args[2] == 490.0


async def test_same_strike_traded_twice_is_untouched() -> None:
    pool = _Pool([], [_sensex()])
    orders = [*SENSEX_ORDERS, _o("INDEX26JAN20000PE", "SELL", 20, 200.0)]
    assert await sweep.reconcile_closed_fills(pool, _broker(orders)) == []


# ── fills matched by round trip, so a manual exit or a stray order never blinds it ──


def _t(hh: int, mm: int) -> datetime:
    now = datetime.now(UTC).astimezone(sweep._IST)
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _straddle_closed_by_hand() -> dict:
    """Shape under test: CE stopped by the agent; PE squared off by hand, yet the DB booked
    the agent's later (stray) 15:10 buy as its exit."""
    leg = dict(lots=1, lot_size=20, direction="SELL", product="INTRADAY")
    return {
        "position_id": "OPT-X",
        "realized_pnl": 900.0,
        "opened_at": _t(10, 5),
        "legs": {
            "income": [],
            "protection": [],
            "closed": [
                {
                    **leg,
                    "tradingsymbol": "BFO:IDXA00100CE",
                    "premium": 150.0,
                    "exit_premium": 230.0,
                },
                {**leg, "tradingsymbol": "BFO:IDXA00100PE", "premium": 140.0, "exit_premium": 1.0},
            ],
        },
    }


HAND_EXIT_ORDERS = [
    _o("IDXA00100PE", "SELL", 20, 140.0, at=_t(10, 5)),
    _o("IDXA00100CE", "SELL", 20, 150.0, at=_t(10, 5)),
    _o("IDXA00100CE", "BUY", 20, 230.0, at=_t(10, 40)),
    _o("IDXA00100PE", "BUY", 20, 35.0, at=_t(13, 30)),  # the operator's manual exit
    _o("IDXA00100PE", "BUY", 20, 1.0, at=_t(15, 10)),  # the agent's stray order
    _o("IDXA00100PE", "SELL", 20, 0.9, at=_t(15, 20)),  # broker auto square-off of it
]


async def test_manual_exit_is_the_legs_exit_and_the_stray_trip_is_reported(caplog) -> None:
    sweep._logged.clear()
    caplog.set_level("INFO", logger=sweep.__name__)
    pool = _Pool([], [_straddle_closed_by_hand()])
    assert await sweep.reconcile_closed_fills(pool, _broker(HAND_EXIT_ORDERS))
    _sql, args = pool.updates[0]
    pe = next(x for x in json.loads(args[1])["closed"] if x["tradingsymbol"].endswith("PE"))
    assert pe["exit_premium"] == 35.0  # the real exit, not the stray 1.00
    assert args[2] == round((150.0 - 230.0) * 20 + (140.0 - 35.0) * 20, 2)
    untracked = [r.getMessage() for r in caplog.records if "result=UNTRACKED" in r.getMessage()]
    assert len(untracked) == 1 and "IDXA00100PE BUY 20" in untracked[0]


async def test_db_closed_but_broker_still_holds_is_never_corrected() -> None:
    pool = _Pool([], [_straddle_closed_by_hand()])
    orders = [o for o in HAND_EXIT_ORDERS if o.average_price not in (35.0, 1.0, 0.9)]
    assert await sweep.reconcile_closed_fills(pool, _broker(orders)) == []
    assert pool.updates == []


async def test_reentry_same_symbol_each_row_takes_its_own_trip() -> None:
    first = _trade(agent_trade_id="T1", exit_price=489.0)
    second = _trade(agent_trade_id="T2", fill_price=505.0, exit_price=513.0)
    first["entered_at"], second["entered_at"] = _t(9, 30), _t(13, 0)
    pool = _Pool([first, second], [])
    orders = [
        _o("ABCSTOCK", "BUY", 10, 498.0, at=_t(9, 30)),
        _o("ABCSTOCK", "SELL", 10, 490.0, at=_t(11, 0)),
        _o("ABCSTOCK", "BUY", 10, 505.0, at=_t(13, 0)),
        _o("ABCSTOCK", "SELL", 10, 515.0, at=_t(14, 0)),
    ]
    corrected = await sweep.reconcile_closed_fills(pool, _broker(orders))
    assert len(corrected) == 2
    by_tid = {args[0]: args for _sql, args in pool.updates}
    assert by_tid["T1"][2] == 490.0 and by_tid["T2"][2] == 515.0


async def test_each_broker_is_checked_with_its_own_name() -> None:
    seen: list[tuple] = []

    class _P(_Pool):
        async def fetch(self, sql: str, *args):
            seen.append(args)
            return []

    await sweep.reconcile_closed_fills(_P([], []), _broker([]), "angel_one")
    assert seen and all(a == ("angel_one",) for a in seen)
