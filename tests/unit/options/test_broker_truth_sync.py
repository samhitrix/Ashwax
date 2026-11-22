"""A live options leg closed OUTSIDE the agent (manually in Kite) must be seen.

A short leg squared off by hand must not stay "open" in the desk, or the time exit would buy
it again and open a fresh long. The desk now reads the broker's own net book
before every live close and every 30 s (sync_with_broker); a flat leg is booked, never re-sent."""

from __future__ import annotations

from typing import Any

import pytest
from src.options import seller_desk as sd

from tests.unit.options.test_desk_broker_executors import _capture_alerts, _mdesk
from tests.unit.options.test_seller_desk import (
    _IST,
    _FakeExecutor,
    _FakeRepo,
    _iron_condor_position,
)


class _Book(_FakeExecutor):
    """Live executor whose broker book the test sets (None = the read fails)."""

    def __init__(self) -> None:
        super().__init__()
        self.net: dict[str, int] | None = {}

    async def net_quantities(self) -> dict[str, int]:
        if self.net is None:
            raise RuntimeError("positions unreadable")
        return dict(self.net)

    async def last_price(self, tradingsymbol: str) -> float | None:
        return 7.0


class _LegRepo(_FakeRepo):
    def __init__(self) -> None:
        super().__init__()
        self.leg_exits: list[dict[str, Any]] = []

    async def record_leg_exit(self, position_id, rec):  # type: ignore[no-untyped-def]
        self.leg_exits.append(rec)

    async def set_legs_meta(self, position_id, meta):  # type: ignore[no-untyped-def]
        pass


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _live(monkeypatch) -> tuple[Any, _Book, _LegRepo, list[dict[str, Any]], _Clock, Any]:  # type: ignore[no-untyped-def]
    alerts = _capture_alerts(monkeypatch)
    clock = _Clock()
    monkeypatch.setattr(sd, "_monotonic", clock)
    repo = _LegRepo()
    desk, *_ = _mdesk(is_paper=False, repo=repo)
    book = _Book()
    desk._executors["live:kite"] = book
    pos = _iron_condor_position("K")
    pos.exec_broker, pos.is_paper = "kite", False
    desk.positions["K"] = pos
    # The broker holds exactly what the agent holds.
    book.net = {
        leg.tradingsymbol: sd._signed_qty(leg) for leg in pos.income_legs + pos.protection_legs
    }
    return desk, book, repo, alerts, clock, pos


def _now() -> Any:
    from datetime import datetime

    return datetime.now(_IST)


async def test_leg_closed_manually_is_booked_after_a_confirming_read_and_no_order_sent(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    desk, book, repo, _alerts, clock, pos = _live(monkeypatch)
    short_pe = pos.income_legs[0]
    book.net[short_pe.tradingsymbol] = 0  # squared off by hand in Kite

    await desk.sync_with_broker()
    assert short_pe in pos.income_legs  # one read is not enough (book can lag an entry fill)
    clock.t += sd._FLAT_CONFIRM_S
    await desk.sync_with_broker()

    assert short_pe not in pos.income_legs
    assert pos.closed_legs[-1]["reason"] == "EXTERNAL_CLOSE"
    assert pos.closed_legs[-1]["exit_estimated"] is True  # the fill sweep replaces it
    assert pos.closed_legs[-1]["exit_premium"] == 7.0
    assert repo.leg_exits[-1]["tradingsymbol"] == short_pe.tradingsymbol
    assert book.closed == []  # never an order for a leg already flat
    assert "K" in desk.positions  # the other legs are still open and managed


async def test_whole_position_closed_manually_is_closed_with_no_order(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    desk, book, repo, _alerts, clock, _pos = _live(monkeypatch)
    book.net = {k: 0 for k in book.net}
    await desk.sync_with_broker()
    clock.t += sd._FLAT_CONFIRM_S
    assert await desk.sync_with_broker() == 1
    assert "K" not in desk.positions
    assert repo.closed[0]["position_id"] == "K"
    assert repo.closed[0]["exit_reason"] == "EXTERNAL_CLOSE"
    assert book.closed == []


async def test_time_exit_of_a_leg_already_flat_sends_no_order(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The failure under test: the time exit must not BUY a leg the broker shows flat.
    desk, book, _repo, _alerts, clock, pos = _live(monkeypatch)
    short_pe = pos.income_legs[0]
    book.net[short_pe.tradingsymbol] = 0
    ok = await desk.close_single_leg(pos, short_pe, ltp=1.0, now=_now(), reason="TIME_EXIT")
    assert ok is False and book.closed == []  # first flat read: held back, retried next tick
    clock.t += sd._FLAT_CONFIRM_S
    ok = await desk.close_single_leg(pos, short_pe, ltp=1.0, now=_now(), reason="TIME_EXIT")
    assert ok is True and book.closed == []
    assert pos.closed_legs[-1]["reason"] == "EXTERNAL_CLOSE"


async def test_leg_still_open_at_broker_is_closed_normally(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    desk, book, _repo, _alerts, _clock, pos = _live(monkeypatch)
    short_pe = pos.income_legs[0]
    ok = await desk.close_single_leg(pos, short_pe, ltp=40.0, now=_now(), reason="SL")
    assert ok is True and [leg for leg, _ in book.closed] == [short_pe]
    assert pos.closed_legs[-1]["reason"] == "SL"


async def test_book_matching_neither_blocks_orders_and_alerts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    desk, book, _repo, alerts, _clock, pos = _live(monkeypatch)
    short_pe = pos.income_legs[0]
    book.net[short_pe.tradingsymbol] = -10  # partly closed by hand
    ok = await desk.close_single_leg(pos, short_pe, ltp=40.0, now=_now(), reason="SL")
    assert ok is False and book.closed == []
    assert short_pe in pos.income_legs
    assert any(a["source"].startswith("options_leg_broker_mismatch:K:") for a in alerts)
    with pytest.raises(RuntimeError, match="not confirmed at the broker"):
        await desk._close_position("K", reason="STOP_LOSS")
    assert book.closed == [] and "K" in desk.positions


async def test_unreadable_book_keeps_the_close_working(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A stop must still fire when the positions read fails; it decides nothing from no data.
    desk, book, _repo, _alerts, _clock, pos = _live(monkeypatch)
    book.net = None
    short_pe = pos.income_legs[0]
    assert await desk.close_single_leg(pos, short_pe, ltp=40.0, now=_now(), reason="SL")
    assert [leg for leg, _ in book.closed] == [short_pe]
    assert await desk.sync_with_broker() == 0


async def test_flat_read_that_recovers_is_not_booked(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    desk, book, _repo, _alerts, clock, pos = _live(monkeypatch)
    short_pe = pos.income_legs[0]
    real = book.net[short_pe.tradingsymbol]
    book.net[short_pe.tradingsymbol] = 0
    await desk.sync_with_broker()  # a lagging book right after the entry fill
    book.net[short_pe.tradingsymbol] = real
    clock.t += sd._FLAT_CONFIRM_S
    await desk.sync_with_broker()
    book.net[short_pe.tradingsymbol] = 0
    await desk.sync_with_broker()  # a fresh first sighting, not a confirmation
    assert short_pe in pos.income_legs and not pos.closed_legs


async def test_paper_positions_are_never_checked_against_a_broker(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    desk, book, _repo, _alerts, clock, pos = _live(monkeypatch)
    pos.is_paper = True
    book.net = {k: 0 for k in book.net}
    await desk.sync_with_broker()
    clock.t += sd._FLAT_CONFIRM_S
    assert await desk.sync_with_broker() == 0 and "K" in desk.positions
